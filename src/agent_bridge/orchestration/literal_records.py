"""Recognize a deliberately tiny, non-executing literal receipt writer.

This is syntax recognition, not a Python sandbox. Only a clean POSIX env
envelope with isolated Python startup and exclusive text creation with builtin
open are recognized. No imports, assignments, reads, substitutions, arbitrary
calls or overwrite are accepted. Unknown syntax returns None; the caller
denies attempted new-form commands and preserves legacy handling otherwise.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import sys


MAX_COMMAND = 131_072
MAX_FILES = 16
_ASSIGNMENT = re.compile(r"(HOME|PATH|LANG|PYTHONPATH)=(.*)", re.S)
_GATE_INTERPRETER = os.path.realpath(sys.executable)


def _interpreter_path_is_clean(path: str) -> bool:
    # Exact spelling matters: CPython detects pyvenv.cfg next to argv[0]
    # before following executable symlinks, even under -I -S.
    if path != _GATE_INTERPRETER or os.path.realpath(path) != path:
        return False
    directory = os.path.dirname(path)
    try:
        for root in (directory, os.path.dirname(directory)):
            for name in os.listdir(root):
                if name.lower() == "pyvenv.cfg" or name.lower().endswith("._pth"):
                    return False
    except OSError:
        return False
    return True


def is_attempt(command: str) -> bool:
    """Identify the new isolated receipt envelope, even if its body is bad.

    This probe grants no permission. It prevents rejected new-form syntax
    from falling into the legacy heuristic's read-only fast path. Ordinary
    -B -c scripts remain outside this new workflow.
    """
    if os.name != "posix" or not isinstance(command, str):
        return False
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return False
    if words[:1] == ["exec"]:
        words = words[1:]
    if words[:2] != ["/usr/bin/env", "-i"]:
        return False
    words = words[2:]
    while words and "=" in words[0]:
        words = words[1:]
    if not words or not (os.path.realpath(words[0]) == os.path.realpath(sys.executable)
                         or re.fullmatch(r"python(?:3(?:\.\d+)?t?)?", os.path.basename(words[0]))):
        return False
    flags = words[1:words.index("-c")] if "-c" in words else words[1:]
    return "-I" in flags or "-S" in flags


def canonical_argv(command: str) -> list[str] | None:
    """Decode canonical words; never authorize their body or write targets.

    This proves quoting only. A caller handling zsh must also account for
    leading-equals expansion, and nested interpreters remain separate layers.
    """
    if os.name != "posix" or not isinstance(command, str) or len(command) > MAX_COMMAND:
        return None
    try:
        command.encode("utf-8", "strict")
    except UnicodeError:
        return None
    # Retain quoting to reject executable shell expansion before decoding.
    token = re.compile(r'''(?:'[^']*'|"(?:\\.|[^"\\])*"|\\[^\n]|[^\s;&|<>()'"\\])+''')
    raw: list[str] = []
    end = 0
    for match in token.finditer(command):
        if re.fullmatch(r"[ \t]*", command[end:match.start()]) is None:
            return None
        part = match.group()
        raw.append(part)
        end = match.end()
    if re.fullmatch(r"[ \t]*", command[end:]) is None or not raw:
        return None
    try:
        words = [shlex.split(part, posix=True) for part in raw]
    except ValueError:
        return None
    if any(len(parts) != 1 for parts in words):
        return None
    argv = [parts[0] for parts in words]
    # Accept only canonical POSIX shlex.quote spelling, not every spelling
    # shlex.split understands. In particular shlex and the shell disagree
    # on double-quoted backslash-dollar/backtick decoding. Canonical words
    # contain only safe bare characters or single-quoted literal segments
    # joined by the exact double-quoted apostrophe sequence. No expansions.
    if any(part != shlex.quote(word) for part, word in zip(raw, argv)):
        return None
    return argv


def targets(command: str) -> list[str] | None:
    """Return every destination only when the entire command is understood.

    Accepted body, repeated at most MAX_FILES times::

        with open('/absolute/new-receipt.json', 'x', encoding='utf-8') as receipt:
            receipt.write('literal text, including quoted peer prose')

    The interpreter must use the exact canonical path pinned by this gate, with
    isolated startup and no site processing. This is not a binary hash
    attestation. As with existing shell recognition, files can change between
    judgment and execution. Protected paths and task ownership are checked
    by the caller after this function returns.
    """
    argv = canonical_argv(command)
    if argv is None:
        return None
    if argv[:1] == ["exec"]:
        argv = argv[1:]
    if argv[:2] != ["/usr/bin/env", "-i"]:
        return None
    argv = argv[2:]
    environment: dict[str, str] = {}
    while argv and "=" in argv[0]:
        match = _ASSIGNMENT.fullmatch(argv[0])
        if match is None or match[1] in environment or "\x00" in match[2]:
            return None
        environment[match[1]] = match[2]
        argv = argv[1:]
    if set(environment) != {"HOME", "PATH", "LANG", "PYTHONPATH"}:
        return None
    if environment["LANG"] != "en_US.UTF-8":
        return None
    if (len(argv) != 6 or not os.path.isabs(argv[0])
            or not _interpreter_path_is_clean(argv[0])
            or argv[1:5] != ["-I", "-S", "-B", "-c"]):
        return None
    try:
        tree = ast.parse(argv[5])
    except (SyntaxError, ValueError, RecursionError):
        return None
    if not 1 <= len(tree.body) <= MAX_FILES:
        return None
    found: list[str] = []
    for statement in tree.body:
        if not isinstance(statement, ast.With) or len(statement.items) != 1:
            return None
        item = statement.items[0]
        call = item.context_expr
        alias = item.optional_vars
        if (not isinstance(alias, ast.Name) or alias.id != "receipt"
                or not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                or call.func.id != "open" or len(call.args) != 2
                or len(call.keywords) != 1):
            return None
        path, mode = call.args
        encoding = call.keywords[0]
        if (not isinstance(path, ast.Constant) or not isinstance(path.value, str)
                or not os.path.isabs(path.value) or "\x00" in path.value
                or not isinstance(mode, ast.Constant) or mode.value != "x"
                or encoding.arg != "encoding" or not isinstance(encoding.value, ast.Constant)
                or encoding.value.value != "utf-8" or len(statement.body) != 1):
            return None
        write = statement.body[0]
        if not isinstance(write, ast.Expr) or not isinstance(write.value, ast.Call):
            return None
        write = write.value
        if (not isinstance(write.func, ast.Attribute) or write.func.attr != "write"
                or not isinstance(write.func.value, ast.Name) or write.func.value.id != "receipt"
                or len(write.args) != 1 or write.keywords
                or not isinstance(write.args[0], ast.Constant)
                or not isinstance(write.args[0].value, str)):
            return None
        try:
            path.value.encode("utf-8", "strict")
            write.args[0].value.encode("utf-8", "strict")
        except UnicodeError:
            return None
        # Python 3.14 dedents -c source. Keep literal spelling on one physical
        # line so that normalization cannot change an embedded blank line.
        # Escaped newlines in repr-style content remain supported.
        for literal in (path, write.args[0]):
            segment = ast.get_source_segment(argv[5], literal)
            if segment is None or "\n" in segment or "\r" in segment:
                return None
        found.append(path.value)
    return found
