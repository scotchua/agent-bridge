#!/usr/bin/env python3
"""Stand-in `codex` CLI for the *execution* lane (not consultation).

The Codex counterpart of `fake_exec_claude.py`. It answers `--version` and
`login status`, and on `codex exec` it writes files into the workspace named
by `-C`, emits the JSONL event stream the harness parses (`thread.started`
carrying `thread_id`), and writes the final message to the path given by
`--output-last-message`.

As with the Claude stand-in, only the model's judgement is standing in: the
worktrees, the patch, the re-application, the confinement and the integrity
snapshot are all real. `--version` reports `fake-exec-codex` so no receipt
built on it can be read as a provider run.

  FAKE_EXEC_WRITE       JSON object of {relative path: file content}
  FAKE_EXEC_MODE        ok | auth_failure | nonzero | no_edit | no_thread |
                        no_last_message | turn_failed
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


def _option(argv: list[str], name: str) -> str | None:
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def _event(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print(os.environ.get("FAKE_EXEC_CODEX_VERSION", "fake-exec-codex 0.0.1"))
        return 0

    control = _control()
    mode = control.get("mode", "ok")

    if "login" in argv and "status" in argv:
        if mode == "auth_failure":
            print("Not logged in")
            return 1
        print("Logged in using ChatGPT")
        return 0

    brief = "" if sys.stdin.isatty() else sys.stdin.read()
    log = control.get("brief_log")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"brief": brief, "argv": argv, "cwd": os.getcwd()}) + "\n")

    workspace = _option(argv, "-C") or os.getcwd()
    last_message = _option(argv, "--output-last-message")

    thread_id = str(uuid.uuid4())
    if mode != "no_thread":
        _event({"type": "thread.started", "thread_id": thread_id})

    if mode == "nonzero":
        sys.stderr.write("fake exec codex refused\n")
        return 5
    if mode == "turn_failed":
        _event({"type": "turn.failed", "error": {"message": "synthetic turn failure"}})
        return 0

    if mode != "no_edit":
        for relative, content in (control.get("write") or {}).items():
            target = pathlib.Path(workspace) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            _event({"type": "item.completed",
                    "item": {"type": "file_change", "path": relative}})

    _event({"type": "turn.completed", "thread_id": thread_id})
    if last_message and mode != "no_last_message":
        pathlib.Path(last_message).write_text(
            "applied the synthetic change\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
