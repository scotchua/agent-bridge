"""The verification-command policy both bounded harnesses enforce.

One definition, imported by ``claude_task``, ``codex_task`` and the execution
queue's admission check, so that a request the harness would refuse is
refused at submission with the same words, instead of being admitted, run,
and reported as a bare ``TaskError`` twenty seconds later (jobs d8e5763d,
7a79ae7f and 19869b0e in the live queue all failed this way).

The policy is deliberately small: a fixed allowlist of programs named without
a path, Python limited to its two test runners as modules, git limited to
read-only subcommands, and no control characters anywhere in an argument.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable


class VerifyPolicyError(ValueError):
    """A verification command the harnesses will not run. Fixed text."""


ALLOWED_VERIFY_PROGRAMS = frozenset(
    {"git", "pytest", "python", "python3", "npm", "pnpm", "yarn", "cargo", "go"})
PYTHON_PROGRAMS = frozenset({"python", "python3"})
PYTHON_TEST_MODULES = ("pytest", "unittest")
GIT_READ_ONLY = frozenset({"diff", "status"})

MESSAGE_SHAPE = "verification must be JSON argv arrays"
MESSAGE_PROGRAM = "verification executable is not allowlisted"
MESSAGE_PYTHON = "Python verification is limited to python -m pytest or python -m unittest"
MESSAGE_GIT = "git verification is read-only"
MESSAGE_CONTROL = "control character in verification argv"
MESSAGE_NOT_ON_PATH = "verification program is not on the worker's PATH: {}"
DEFAULT_FILE = ".agent-bridge-verification.json"
DEFAULT_SOURCES = ("repo", "explicit", "none")


def resolve_verification(repo: Path, commands: object, *, timeout: int | None = None
                         ) -> tuple[list[list[str]], str, int]:
    """Resolve omitted/empty checks once, before generation, and validate them.

    Repository defaults have exactly the explicit-command policy; they cannot
    introduce shell execution, Python scripts, arbitrary environment variables,
    or a less confined verifier. A malformed/unreadable file is a refusal.
    """
    selected_timeout = 300 if timeout is None else timeout
    if commands is not None:
        checked = check_verify_argv(commands)
        if checked:
            return checked, "explicit", selected_timeout
    path = repo / DEFAULT_FILE
    try:
        if path.is_symlink():
            raise VerifyPolicyError("verification_default_invalid")
        with path.open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
    except FileNotFoundError:
        return [], "none", selected_timeout
    except OSError:
        raise VerifyPolicyError("verification_default_unreadable") from None
    try:
        value = json.loads(raw)
        if (len(raw) > 64 * 1024 or not isinstance(value, dict)
                or "verify_argv" not in value
                or set(value) - {"verify_argv", "verify_timeout_seconds"}):
            raise ValueError
        checked = check_verify_argv(value["verify_argv"])
        if not checked:
            raise ValueError
        repo_timeout = value.get("verify_timeout_seconds", 300)
        if isinstance(repo_timeout, bool) or not isinstance(repo_timeout, int) or not 1 <= repo_timeout <= 3600:
            raise ValueError
        if timeout is None:
            selected_timeout = repo_timeout
    except (ValueError, UnicodeError):
        raise VerifyPolicyError("verification_default_invalid") from None
    return checked, "repo", selected_timeout


def _bare_name(program: str) -> bool:
    """Whether ``program`` names an executable with no directory on either OS."""
    return (PurePosixPath(program).name == program
            and PureWindowsPath(program).name == program
            and "\\" not in program and "/" not in program)


def check_verify_argv(commands: object) -> list[list[str]]:
    """Return a copy of ``commands`` if every command is permitted, or raise.

    Emptiness is the caller's business: the Claude lane requires at least one
    command and the Codex lane permits none, and both decide that before
    calling here.
    """
    if not isinstance(commands, list):
        raise VerifyPolicyError(MESSAGE_SHAPE)
    for command in commands:
        if (not isinstance(command, list) or not command
                or not all(isinstance(part, str) and part for part in command)):
            raise VerifyPolicyError(MESSAGE_SHAPE)
        program = command[0]
        if not _bare_name(program) or program not in ALLOWED_VERIFY_PROGRAMS:
            raise VerifyPolicyError(MESSAGE_PROGRAM)
        if program in PYTHON_PROGRAMS and (
                len(command) < 3 or command[1] != "-m"
                or command[2] not in PYTHON_TEST_MODULES):
            raise VerifyPolicyError(MESSAGE_PYTHON)
        if program == "git" and (len(command) < 2 or command[1] not in GIT_READ_ONLY):
            raise VerifyPolicyError(MESSAGE_GIT)
        if any(any(char in part for char in ("\0", "\n", "\r")) for part in command):
            raise VerifyPolicyError(MESSAGE_CONTROL)
    return [list(command) for command in commands]


def check_runnable(commands: list[list[str]], path: str, *,
                   which: Callable[..., str | None] = shutil.which) -> None:
    """Refuse a job whose verification program is not on this worker's PATH.

    Called by the execution worker before it spends a provider run: live jobs
    c0849b26, beffeccc and 7dcda0b7 generated for 12 to 14 minutes and then
    failed because sandbox-exec could not find `pytest` or `python` on the
    worker's PATH. ``commands`` has already passed ``check_verify_argv``, so
    every program is a bare allowlisted name and safe to name in a reason.

    A lookup only; nothing is executed. Whether an interpreter can import
    pytest is deliberately not probed here: running a PATH-selected program
    outside the verification sandbox, with import rules that differ from the
    sandboxed `python -m pytest` in the worktree, is less safe and less exact
    than letting verification report it. Only absolute PATH entries count, so
    the answer never depends on the worker's current directory. git is
    resolved by the harness from a fixed location and is not checked here.
    """
    absolute = os.pathsep.join(entry for entry in path.split(os.pathsep)
                               if entry and os.path.isabs(entry))
    for command in commands:
        program = command[0]
        if program == "git":
            continue
        # An empty search path is not "nowhere" to which(): it searches the
        # current directory. No absolute entry means nothing can be found.
        if not absolute or not which(program, path=absolute):
            raise VerifyPolicyError(MESSAGE_NOT_ON_PATH.format(program))
