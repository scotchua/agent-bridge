"""Explicit foreground service loop for the local queue. It installs nothing."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
import sys
from typing import Any

from .runtime import MacSampler
from .spool import Backend, LocalQueue, QueueCaps, SubprocessBackend
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
    def __init__(self, root: str, worker: str, worker_state: str, *, sampler: MacSampler | None = None,
                 backend: Backend | None = None, caps: QueueCaps | None = None,
                 allowed_task_types: "frozenset[str] | None" = None,
                 backend_id: str = "private_worker"):
        """Build the queue for one local backend.

        ``worker``/``worker_state`` build the default private-worker
        ``SubprocessBackend`` command exactly as before, and remain the only
        thing most callers ever pass. ``backend`` (with ``caps`` and
        ``allowed_task_types``) lets a caller substitute a different,
        already-built backend entirely -- what :meth:`for_config` does for
        ``local_backend: "gemma_certified"`` -- without this constructor
        knowing anything about that backend's own contract.
        """
        if not isinstance(backend_id, str) or not backend_id:
            raise ValueError("backend_id_invalid")
        self.root = Path(root).absolute()
        self.backend_id = backend_id
        self.sampler = sampler or MacSampler()
        if backend is None:
            command = [sys.executable, str(Path(worker_child.__file__).resolve()), "--worker", worker, "--state", worker_state,
                       "--queue-root", str(self.root)]
            backend = SubprocessBackend(command)
        self.queue = LocalQueue(self.root, sampler=self.sampler, backend=backend,
                                caps=caps or QueueCaps(), allowed_task_types=allowed_task_types,
                                backend_id=backend_id)
        self.state_path = self.root / "runtime-state.json"

    @classmethod
    def for_config(cls, cfg: Any, *, root: str | None = None,
                   sampler: MacSampler | None = None) -> "Service":
        """Build the Service the way production does: the backend is
        chosen exactly once, from the operator's own configuration file,
        never from anything an assistant-facing request supplies.

        ``root`` overrides ``cfg.local_queue_root`` for a caller that must
        use a disposable queue (calibration, delegation verification) and
        must never touch the operator's real one; production omits it.
        """
        from .backend_select import build_backend_and_caps  # deferred: avoids a light import cycle

        queue_root = str(root) if root is not None else str(cfg.local_queue_root)
        backend, caps, allowed = build_backend_and_caps(cfg, queue_root)
        return cls(queue_root, str(cfg.worker_executable), str(cfg.worker_state),
                  sampler=sampler, backend=backend, caps=caps, allowed_task_types=allowed,
                  backend_id=str(getattr(cfg, "local_backend", "private_worker")))

    def once(self) -> dict:
        result = self.queue.run_once("localq-service")
        state = {"version": 1, "updated_at": time.time(), "backend_id": self.backend_id,
                 "last_result": result,
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
