#!/usr/bin/env python3
"""Stand-in `claude` CLI for the *execution* lane (not consultation).

`tests/fakes/fake_claude.py` stands in for the consultation backend and speaks
the peer-response contract. This one stands in for the subscription
implementation lane: it answers `--version` and `auth status --json`, and on a
generation turn it writes files into its working directory and prints the
`--output-format json` result envelope the harness parses.

The point of this file is that everything *around* the model is real. The
harness creates a real disposable worktree, captures a real patch with real
git, re-applies it to a second real worktree, runs real verification commands
under the host's real confinement backend, and takes a real content snapshot
of the source repository. Only the model's judgement is standing in, and a
receipt produced this way says `fake-exec-claude` in its executable version
so no evidence built on it can be mistaken for a provider run.

Behaviour is driven by the environment so one file covers every case:

  FAKE_EXEC_WRITE       JSON object of {relative path: file content} to write
  FAKE_EXEC_MODE        ok | auth_failure | nonzero | no_edit | git_tamper |
                        escape_write | is_error
  FAKE_EXEC_SUBSCRIPTION  subscriptionType reported by `auth status`
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid


#: The harness deliberately hands a provider CLI a minimal fixed environment
#: (PATH, HOME, LANG, the git pins, and the provider's own store), so a test
#: cannot steer this stand-in through the environment alone. HOME does cross
#: that boundary, so the control file lives there, which is also how a real
#: CLI finds its own configuration.
CONTROL_FILE = "fake-exec.json"


def _control() -> dict:
    """Environment first, then $HOME/fake-exec.json. Missing means defaults."""
    control = {}
    path = os.path.join(os.environ.get("HOME", ""), CONTROL_FILE)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            control = json.load(handle)
    if os.environ.get("FAKE_EXEC_MODE"):
        control["mode"] = os.environ["FAKE_EXEC_MODE"]
    if os.environ.get("FAKE_EXEC_WRITE"):
        control["write"] = json.loads(os.environ["FAKE_EXEC_WRITE"])
    return control


def _write_requested(control: dict) -> list[str]:
    written = []
    for relative, content in (control.get("write") or {}).items():
        target = pathlib.Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(relative)
    return written


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(os.environ.get("FAKE_EXEC_CLAUDE_VERSION", "fake-exec-claude 0.0.1"))
        return 0

    control = _control()
    mode = control.get("mode", "ok")

    if "auth" in argv and "status" in argv:
        if mode == "auth_failure":
            print(json.dumps({"loggedIn": False}))
            return 0
        print(json.dumps({
            "loggedIn": True, "authMethod": "claude.ai",
            "subscriptionType": control.get("subscription", "max")}))
        return 0

    # A generation turn. The brief arrives on stdin.
    brief = "" if sys.stdin.isatty() else sys.stdin.read()
    log = control.get("brief_log")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"brief": brief, "argv": argv, "cwd": os.getcwd()}) + "\n")

    if mode == "nonzero":
        sys.stderr.write("fake exec claude refused\n")
        return 4
    if mode == "git_tamper":
        # Rewrite the worktree's .git pointer file. The harness compares it
        # byte for byte before trusting the patch.
        pathlib.Path(".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    elif mode == "escape_write":
        # Try to write above the disposable worktree. On a host whose
        # confinement is only network-deep this succeeds, and the harness's
        # source-integrity snapshot is what must catch it.
        outside = control.get("escape_target")
        if outside:
            pathlib.Path(outside).write_text("escaped\n", encoding="utf-8")
    elif mode != "no_edit":
        _write_requested(control)

    envelope = {
        "type": "result", "subtype": "success",
        "is_error": mode == "is_error",
        "result": "applied the synthetic change",
        "session_id": str(uuid.uuid4()), "num_turns": 1,
        "stop_reason": "end_turn", "terminal_reason": "success",
        "modelUsage": {"fake-exec-model": {"inputTokens": 1}},
    }
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
