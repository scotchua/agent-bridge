"""Standalone consumer for queued cross-provider execution jobs.

This process must run as the logged-in user outside either desktop app's MCP
sandbox.  MCP servers submit and inspect jobs only; they never invoke provider
CLIs.  One advisory lock covers the complete queue so two launch agents or a
foreground test cannot send the same request twice.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import socket
import sys
import time
from pathlib import Path

from .config import load
from .execution_queue import (
    ExecutionAdmissionError,
    ExecutionQueue,
    Harnesses,
    SubprocessHarnessExecutor,
)


class WorkerAlreadyRunning(RuntimeError):
    """Another process owns the queue-level worker lock."""


class WorkerLock:
    def __init__(self, queue_root: Path):
        self.path = queue_root / ".execution-worker.lock"
        self._fd: int | None = None

    def __enter__(self) -> "WorkerLock":
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise WorkerAlreadyRunning("execution_worker_already_running") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        self._fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _configured_queue(config_path: str) -> ExecutionQueue:
    cfg = load(config_path)
    required = (cfg.execution_queue_root, cfg.codex_task_executable,
                cfg.claude_task_executable, cfg.python_executable)
    if any(value is None for value in required):
        raise ExecutionAdmissionError("execution_configuration_missing")
    harnesses = Harnesses(codex=cfg.codex_task_executable,
                          claude=cfg.claude_task_executable,
                          python=cfg.python_executable)
    return ExecutionQueue(cfg.execution_queue_root,
                          SubprocessHarnessExecutor(harnesses),
                          recover_interrupted=True)


def run(config_path: str, *, once: bool, interval: float,
        worker_id: str | None = None) -> int:
    if interval <= 0:
        raise ExecutionAdmissionError("interval_invalid")
    cfg = load(config_path)
    if cfg.execution_queue_root is None:
        raise ExecutionAdmissionError("execution_configuration_missing")
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    identity = worker_id or f"{socket.gethostname()}-{os.getpid()}"
    try:
        with WorkerLock(cfg.execution_queue_root):
            # Recovery happens only after this process owns the global lock.
            # Any uncertain prior send becomes blocked and is never repeated.
            queue = _configured_queue(config_path)
            if once:
                result = queue.run_once(identity)
                if result is not None:
                    print(f"{result['job_id']} {result['state']}")
                return 0
            while not stop:
                result = queue.run_once(identity)
                if result is None:
                    time.sleep(interval)
            return 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consume agent-bridge execution jobs outside desktop app sandboxes.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true",
                        help="Process at most one job in the foreground, then exit.")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--worker-id")
    args = parser.parse_args(argv)
    try:
        return run(args.config, once=args.once, interval=args.interval,
                   worker_id=args.worker_id)
    except (ExecutionAdmissionError, WorkerAlreadyRunning, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
