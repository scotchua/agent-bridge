#!/usr/bin/env python3
"""Offline stand-in for the installed local-delegate CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_MODEL_DIGEST = "ab" * 32


def _read(here: Path, name: str, default: str = "") -> str:
    path = here / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() else default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--parent-task-id", required=True)
    parser.add_argument("--job-timeout", required=True, type=float)
    args = parser.parse_args(argv)
    here = Path(__file__).resolve().parent
    mode = _read(here, "mode.txt", "complete")
    calls = here / "calls.txt"
    calls.write_text(str(int(_read(here, "calls.txt", "0")) + 1), encoding="utf-8")
    input_bytes = sys.stdin.buffer.read()
    (here / "observed-stdin.bin").write_bytes(input_bytes)

    if mode == "slow":
        time.sleep(float(_read(here, "sleep_seconds.txt", "5")))
    if mode == "unavailable":
        return 3

    output = b"SUMMARY: " + input_bytes[:80]
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    if mode == "missing-receipt":
        return 0

    receipt_root = Path(os.environ["CODEX_BRIDGE_RECEIPTS_DIR"])
    receipt = {
        "contract": "local-delegate/v2",
        "invocation_id": args.invocation_id,
        "task": args.task,
        "result_status": "success",
        "model_digest": _read(here, "model_digest.txt", DEFAULT_MODEL_DIGEST),
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "canonical_input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "effective_options": {
            "chunk_chars": 24000, "job_timeout": float(args.job_timeout),
            "think": False, "schema": False, "second_pass": False,
            "repair_retries": 0, "map_scope": "run", "roster_sha256": None,
        },
        # Mirror the installed delegate's documented CLI contract: callers
        # pass a raw identity and the delegate stores only its SHA-256 binding.
        "parent_task_id": hashlib.sha256(args.parent_task_id.encode("utf-8")).hexdigest(),
        "attempt_id": None,
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "output_hash_kind": "stdout",
    }
    override_path = here / "receipt_override.json"
    if override_path.is_file():
        receipt.update(json.loads(override_path.read_text(encoding="utf-8")))
    text = json.dumps(receipt)
    if mode == "duplicate-key":
        text = '{"contract":"local-delegate/v2","contract":"bad"}'
    elif mode == "malformed":
        text = "{not-json"
    target = receipt_root / f"local-delegate-{args.invocation_id}.json"
    target.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
