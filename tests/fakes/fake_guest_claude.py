#!/usr/bin/env python3
"""Fake ``claude`` CLI for the guest provider lane.

Distinct from :mod:`tests.fakes.fake_claude`, which stands in for the
*consultation* lane and speaks the bridge's own JSON contract. This one stands
in for the binary the guest runner executes inside WSL2, so it speaks Claude
Code's real headless surface: ``-p --output-format json`` reading the prompt
from stdin and printing one result object.

Every mode here is a shape the lane has to classify correctly. They exist so
the classification is exercised against bytes a process actually wrote, rather
than against a dict a test handed straight to the parser. The two are not the
same test: a parser can be right about a dict and wrong about the stream the
dict was supposed to have come from.

Behaviour is selected by a leading ``--fake-mode <name>`` argv pair, which the
test harness bakes into a shim. Deliberately not an environment variable: the
guest runner rebuilds the child environment from a fixed allowlist, so a mode
passed that way would be stripped and every test would silently run the
default. Reading it from argv also leaves the child environment pristine, so
the tests that assert on *that* still mean something.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import uuid

SENTINEL = "agent-bridge-auth-probe-ok-7f3a1c"

#: Roughly the guest runner's own ceiling. Overshooting it is the point.
OVERSIZED = 3 * 1024 * 1024


def result_object(text: str, *, is_error: bool = False,
                  subtype: str = "success", usage: object = None) -> dict:
    """The envelope Claude Code prints under ``--output-format json``."""

    envelope = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "session_id": str(uuid.uuid4()),
        "num_turns": 1,
        "duration_ms": 11,
        "total_cost_usd": 0.0,
        "stop_reason": "end_turn",
    }
    if usage is not False:
        envelope["usage"] = usage if usage is not None else {
            "input_tokens": 12, "output_tokens": 7}
    return envelope


def prompt_text() -> str:
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def take_mode(argv: list[str]) -> tuple[str, list[str]]:
    """Strip a leading ``--fake-mode <name>``; the rest is the real argv."""

    if len(argv) >= 2 and argv[0] == "--fake-mode":
        return argv[1], argv[2:]
    return "ok", list(argv)


def main(argv: list[str]) -> int:
    mode, argv = take_mode(argv)

    # The lane must never hand a subscription job a metered key. The fixture
    # reports what it was actually given so a test can assert on it rather
    # than on the absence of an assertion.
    if mode == "report_env":
        sys.stdout.write(json.dumps(sorted(os.environ)))
        return 0

    prompt = prompt_text()

    if mode == "hang":
        time.sleep(600)
        return 0
    if mode == "runaway":
        sys.stdout.write("x" * OVERSIZED)
        sys.stdout.flush()
        return 0
    if mode == "rejected":
        # Wording taken from the shape the marker list is written against: a
        # refusal names the session, not a generic failure.
        sys.stderr.write("Invalid API key · Please run /login\n")
        return 1
    if mode == "nonzero":
        sys.stderr.write("something unrelated broke\n")
        return 3
    if mode == "not_json":
        sys.stdout.write("Claude Code 2.1.229\n")
        return 0
    if mode == "is_error":
        sys.stdout.write(json.dumps(result_object(SENTINEL, is_error=True)))
        return 0
    if mode == "wrong_subtype":
        sys.stdout.write(json.dumps(
            result_object(SENTINEL, subtype="error_during_execution")))
        return 0
    if mode == "no_usage":
        # A result object with no usage block did not come from a model turn.
        sys.stdout.write(json.dumps(result_object(SENTINEL, usage=False)))
        return 0
    if mode == "echo_prompt":
        # The classic false pass: the CLI echoes the request, so the sentinel
        # is present without a model ever having answered. Distinguished only
        # by the envelope, which is why the envelope is checked.
        sys.stdout.write(prompt)
        return 0
    if mode == "no_sentinel":
        sys.stdout.write(json.dumps(result_object("I cannot do that.")))
        return 0
    if mode == "probe_ok":
        sys.stdout.write(json.dumps(result_object(SENTINEL)))
        return 0

    # The default: a job. Write the change the brief asked for, then report.
    workdir = pathlib.Path.cwd()
    if mode == "modify_tracked":
        # Changes a file the workspace commit already contains, so
        # `git diff --exit-code` has something to fail on.
        with (workdir / "README.md").open("a", encoding="utf-8") as handle:
            handle.write("edited by the provider\n")
    elif mode == "ok":
        (workdir / "IMPLEMENTED.md").write_text(prompt, encoding="utf-8")
    sys.stdout.write(json.dumps(result_object("done")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
