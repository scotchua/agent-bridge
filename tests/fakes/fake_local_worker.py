#!/usr/bin/env python3
"""Stand-in for the external `private-local-worker 0.3.0` MCP server.

`localq/worker_child.py` is an adapter for a worker that is not part of this
repository: it runs `<worker> --state <dir> --allow-inline-nonclient serve`,
sends `initialize` then one `tools/call local_worker_process`, and expects
exactly two JSON-RPC lines back with a `status: "draft"` payload.

This file implements that contract and then actually goes out over HTTP to a
model endpoint, so the local lane under test is the real one end to end:
automatic intake, the durable queue, the resource sampler, the service loop,
this child process, a real HTTP request and response, and the draft written
back into the queue's receipt. Only the model's weights are standing in.

The endpoint is read from `<state>/endpoint.txt` because `worker_child` hands
its child a fixed minimal environment (PATH, LANG, LC_ALL only), which is
itself a property worth keeping: a test cannot steer this through os.environ.

  <state>/endpoint.txt   base URL of the model service, e.g. http://127.0.0.1:1234
  <state>/mode.txt       optional: "refuse" to reject the request, "break" to
                         emit a protocol violation
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ALLOWED_TASKS = ("summarize", "extract", "checklist", "log_triage", "test_draft")
ALLOWED_CLASSIFICATIONS = ("synthetic", "public", "internal_nonclient")
MAX_OUTPUT_CHARS = 20_000


def _read(state: Path, name: str, default: str = "") -> str:
    path = state / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() else default


def _generate(endpoint: str, task: str, text: str, instruction: str) -> dict:
    """One real HTTP round trip to the model service."""
    body = json.dumps({"model": "stand-in-local-model", "stream": False,
                       "prompt": f"task={task}\ninstruction={instruction}\n\n{text}"}
                      ).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), str):
        raise RuntimeError("model service returned an unusable response")
    return {"model": payload.get("model", "unknown"),
            "text": payload["response"][:MAX_OUTPUT_CHARS]}


def _reply(request_id, result) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--allow-inline-nonclient", action="store_true")
    parser.add_argument("command", choices=["serve"])
    args = parser.parse_args(argv)
    state = Path(args.state)
    mode = _read(state, "mode.txt", "ok")

    lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
    if len(lines) < 2:
        return 1
    initialize = json.loads(lines[0])
    call = json.loads(lines[1])

    print(_reply(initialize.get("id"), {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "stand-in-private-local-worker", "version": "0.3.0"}}))

    if mode == "break":
        return 0                      # one line only: a protocol violation

    arguments = (call.get("params") or {}).get("arguments") or {}
    task = arguments.get("task")
    classification = arguments.get("classification")
    failure = None
    if task not in ALLOWED_TASKS:
        failure = "task_not_supported"
    elif classification not in ALLOWED_CLASSIFICATIONS:
        failure = "classification_refused"
    elif not args.allow_inline_nonclient and classification == "internal_nonclient":
        failure = "inline_nonclient_not_authorised"
    elif mode == "refuse":
        failure = "worker_refused_for_test"

    if failure is not None:
        print(_reply(call.get("id"), {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps({"error": failure})}]}))
        return 0

    try:
        generated = _generate(_read(state, "endpoint.txt"), task,
                              arguments.get("text", ""),
                              arguments.get("instruction", ""))
    except (OSError, ValueError, urllib.error.URLError, RuntimeError) as exc:
        print(_reply(call.get("id"), {
            "isError": True,
            "content": [{"type": "text",
                         "text": json.dumps({"error": type(exc).__name__})}]}))
        return 0

    print(_reply(call.get("id"), {
        "isError": False,
        "content": [{"type": "text", "text": json.dumps({
            "status": "draft",
            "output": {"task": task, "text": generated["text"],
                       "observed_model": generated["model"]},
            "provider": "stand-in-local",
            "job_id": uuid.uuid4().hex})}]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
