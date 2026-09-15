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

from pathlib import PurePosixPath, PureWindowsPath


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
