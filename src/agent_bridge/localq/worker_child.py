"""Side-effect-free child adapter for private-local-worker 0.3.0."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

TASK_MAP = {name: name for name in ("summarize", "extract", "checklist", "log_triage", "test_draft")}


def _paths(worker: str, state: str) -> tuple[Path, Path]:
    script, root = Path(worker), Path(state)
    if not script.is_absolute() or not script.is_file() or script.is_symlink():
        raise ValueError("worker path must be an existing absolute regular file")
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("worker state must be an existing absolute directory")
    return script, root


def _provenance(queue_root: str, job_id: str) -> tuple[str, str, str]:
    database = Path(queue_root) / "localq.sqlite3"
    if not database.is_file():
        raise ValueError("queue database unavailable")
    connection = sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT classification,caller,purpose FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    finally:
        connection.close()
    if row is None or tuple(row) not in {(c, a, p) for c in ("synthetic", "public", "internal_nonclient") for a in ("codex", "claude") for p in ("work", "test")}:
        raise ValueError("queue provenance unavailable")
    return row[0], row[1], row[2]


def invoke(payload: dict[str, Any], *, worker: str, state: str, queue_root: str, python: str = sys.executable,
           timeout: float = 65.0) -> dict[str, Any]:
    script, root = _paths(worker, state)
    task = TASK_MAP.get(payload.get("task_type"))
    if task is None or not isinstance(payload.get("input"), str) or not isinstance(payload.get("params"), dict):
        raise ValueError("unsupported queue payload")
    instruction = payload["params"].get("instruction", "")
    if not isinstance(instruction, str):
        raise ValueError("instruction must be text")
    provider = payload["params"].get("provider", "auto")
    if provider not in {"auto", "apple", "qwen"}:
        raise ValueError("provider must be auto, apple, or qwen")
    classification, caller, purpose = _provenance(queue_root, payload.get("job_id", ""))
    arguments = {"task": task, "text": payload["input"], "instruction": instruction,
                 "classification": classification, "caller": caller, "purpose": purpose,
                 "provider": provider}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "local_worker_process", "arguments": arguments}}
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    proc = subprocess.run([python, str(script), "--state", str(root), "--allow-inline-nonclient", "serve"],
                          input=json.dumps(init) + "\n" + json.dumps(call) + "\n", text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=timeout,
                          check=False, shell=False)
    if proc.returncode != 0:
        raise RuntimeError("private worker exited unsuccessfully")
    lines = proc.stdout.splitlines()
    if len(lines) != 2:
        raise RuntimeError("private worker protocol response invalid")
    reply = json.loads(lines[1])
    result = reply.get("result") if isinstance(reply, dict) else None
    if not isinstance(result, dict) or result.get("isError") is not False:
        raise RuntimeError("private worker rejected request")
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise RuntimeError("private worker response invalid")
    value = json.loads(content[0].get("text", ""))
    if not isinstance(value, dict) or value.get("status") != "draft" or not isinstance(value.get("output"), dict):
        raise RuntimeError("private worker response invalid")
    return {"output": value["output"], "provider": value.get("provider", "unknown"),
            "worker_job_id": value.get("job_id", "unknown")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--queue-root", required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        print(json.dumps(invoke(payload, worker=args.worker, state=args.state, queue_root=args.queue_root, python=args.python), ensure_ascii=True))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
