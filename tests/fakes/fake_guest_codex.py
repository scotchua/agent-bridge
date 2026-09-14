#!/usr/bin/env python3
"""Fake ``codex`` CLI for the guest provider lane.

Codex differs from Claude in the two ways the lane actually depends on, so
both are reproduced here rather than approximated:

  * it is logged in by a separate ``login --with-access-token`` invocation
    that reads the token from stdin, and
  * its final answer is written to the file named by ``--output-last-message``
    rather than printed, while stdout carries a JSONL event stream that also
    contains the prompt.

That second point is the whole reason the probe reads the answer file. A
fixture that printed the answer to stdout would let a scrape-stdout
implementation pass, which is the defect the file read exists to prevent, so
this fixture puts the sentinel in the *stream* in every mode and in the answer
file only when the turn really succeeded.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

SENTINEL = "agent-bridge-auth-probe-ok-7f3a1c"
OVERSIZED = 3 * 1024 * 1024

#: The token the login path refuses. Any other value is accepted, so a test
#: chooses which branch it is exercising by the token it supplies.
REJECTED_TOKEN = "rejected-session-token"


def last_message_path(argv: list[str]) -> str | None:
    if "--output-last-message" in argv:
        index = argv.index("--output-last-message")
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def event(kind: str, **fields) -> str:
    return json.dumps({"type": kind, **fields}) + "\n"


def do_login() -> int:
    token = sys.stdin.read().strip()
    if not token or token == REJECTED_TOKEN:
        sys.stderr.write("error: unauthorized: invalid bearer token\n")
        return 1
    home = os.environ.get("CODEX_HOME")
    if home:
        # A real login leaves state in CODEX_HOME. Writing it proves the
        # capsule's relocation actually took effect, because a login that
        # landed in the caller's real home would write somewhere else.
        directory = pathlib.Path(home)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "auth.json").write_text(
            json.dumps({"logged_in": True}), encoding="utf-8")
    return 0


def take_mode(argv: list[str]) -> tuple[str, list[str]]:
    """Strip a leading ``--fake-mode <name>``; the rest is the real argv.

    An argv pair rather than an environment variable, because the guest runner
    rebuilds the child environment from a fixed allowlist and would strip it.
    """

    if len(argv) >= 2 and argv[0] == "--fake-mode":
        return argv[1], argv[2:]
    return "ok", list(argv)


def main(argv: list[str]) -> int:
    mode, argv = take_mode(argv)
    if argv and argv[0] == "login":
        return do_login()

    if mode == "report_env":
        sys.stdout.write(json.dumps(sorted(os.environ)))
        return 0

    prompt = sys.stdin.read()
    answer_path = last_message_path(argv)

    if mode == "hang":
        time.sleep(600)
        return 0
    if mode == "runaway":
        sys.stdout.write("x" * OVERSIZED)
        sys.stdout.flush()
        return 0
    if mode == "rejected":
        sys.stderr.write("stream error: unauthorized; session expired\n")
        return 1
    if mode == "nonzero":
        sys.stderr.write("codex: unrelated failure\n")
        return 4

    # The stream always quotes the prompt back. Finding the sentinel here
    # proves only that it was sent.
    sys.stdout.write(event("task_started"))
    sys.stdout.write(event("agent_message", message=prompt.strip()))

    if mode == "no_answer_file":
        sys.stdout.write(event("task_complete"))
        return 0
    if mode == "empty_answer":
        if answer_path:
            pathlib.Path(answer_path).write_text("", encoding="utf-8")
        sys.stdout.write(event("task_complete"))
        return 0
    if mode == "no_sentinel":
        if answer_path:
            pathlib.Path(answer_path).write_text(
                "I cannot do that.", encoding="utf-8")
        sys.stdout.write(event("task_complete"))
        return 0
    if mode == "probe_ok":
        if answer_path:
            pathlib.Path(answer_path).write_text(SENTINEL, encoding="utf-8")
        sys.stdout.write(event("task_complete"))
        return 0

    workdir = pathlib.Path.cwd()
    if mode == "modify_tracked":
        with (workdir / "README.md").open("a", encoding="utf-8") as handle:
            handle.write("edited by the provider\n")
    elif mode == "ok":
        (workdir / "IMPLEMENTED.md").write_text(prompt, encoding="utf-8")
    if answer_path:
        pathlib.Path(answer_path).write_text("done", encoding="utf-8")
    sys.stdout.write(event("task_complete"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
