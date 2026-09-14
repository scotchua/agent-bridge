"""Explicit foreground service loop for the local queue. It installs nothing."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
import sys

from .runtime import MacSampler
from .spool import LocalQueue, SubprocessBackend
from . import worker_child


def atomic_state(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Service:
    def __init__(self, root: str, worker: str, worker_state: str, *, sampler: MacSampler | None = None):
        self.root = Path(root).absolute()
        self.sampler = sampler or MacSampler()
        command = [sys.executable, str(Path(worker_child.__file__).resolve()), "--worker", worker, "--state", worker_state,
                   "--queue-root", str(self.root)]
        self.queue = LocalQueue(self.root, sampler=self.sampler, backend=SubprocessBackend(command))
        self.state_path = self.root / "runtime-state.json"

    def once(self) -> dict:
        result = self.queue.run_once("localq-service")
        state = {"version": 1, "updated_at": time.time(), "last_result": result,
                 "queue": self.queue.state_report(), "sampler": getattr(self.sampler, "last_details", {})}
        atomic_state(self.state_path, state)
        return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foreground local queue service; does not install or activate anything.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--worker-state", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("interval must be positive")
    service = Service(args.root, args.worker, args.worker_state)
    if args.once:
        service.once()
        return 0
    while True:
        service.once()
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
