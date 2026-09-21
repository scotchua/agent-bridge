"""Standalone consumer for queued cross-provider execution jobs.

This process must run as the logged-in user outside either desktop app's MCP
sandbox.  MCP servers submit and inspect jobs only; they never invoke provider
CLIs.  One advisory lock covers the complete queue so two launch agents or a
foreground test cannot send the same request twice.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from pathlib import Path

from ..platform import platform as host_platform
from . import autoroute
from .config import load
from .execution_queue import (
    ExecutionAdmissionError,
    ExecutionQueue,
    Harnesses,
    SubprocessHarnessExecutor,
)


class WorkerAlreadyRunning(RuntimeError):
    """Another process owns the queue-level worker lock."""


class QueueNotPrivate(RuntimeError):
    """The queue root could not be confirmed readable only by its owner."""


class WorkerLock:
    """One advisory lock over the whole queue, held for the worker's lifetime.

    Taken through the platform abstraction rather than ``fcntl`` directly.
    ``fcntl`` does not exist on Windows, so importing it at module scope made
    this module unimportable there, which meant a Windows host could not even
    read the worker's own error messages. The platform layer already provides
    an exclusive lock with the same guarantee on both systems.
    """

    def __init__(self, queue_root: Path, platform: object | None = None):
        self.path = queue_root / ".execution-worker.lock"
        self._fd: int | None = None
        self._release: object = None
        self._platform = platform if platform is not None else host_platform

    def __enter__(self) -> "WorkerLock":
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # O_NOFOLLOW is POSIX-only; on Windows the same protection comes from
        # the owner-only ACL the platform layer applies to the directory.
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            self._platform.enforce_owner_only_file(fd)
        except OSError:
            os.close(fd)
            raise
        except (AttributeError, NotImplementedError) as exc:
            # Not "the platform cannot do this, carry on". The queue holds job
            # requests and their outcomes; if this process cannot establish
            # that only its owner can read them, it must not start. mkdir's
            # mode argument is a no-op on Windows, so swallowing this was
            # exactly the case where the directory stayed world-readable and
            # nothing said so.
            os.close(fd)
            raise QueueNotPrivate("queue_root_acl_unenforceable") from exc
        self._verify_queue_root(fd)
        # timeout=0: this is a liveness question, not a queue to wait in. A
        # second worker must report that one is already running, not block.
        release = self._platform.lock_exclusive(fd, str(self.path), 0.0)
        try:
            release.__enter__()
        except TimeoutError as exc:
            os.close(fd)
            raise WorkerAlreadyRunning("execution_worker_already_running") from exc
        except OSError as exc:
            os.close(fd)
            raise WorkerAlreadyRunning("execution_worker_already_running") from exc
        # Write first, then truncate to what was written. Truncating to zero
        # first would momentarily drop byte 0, which is exactly the byte the
        # Windows lock is taken on.
        os.lseek(fd, 0, os.SEEK_SET)
        written = os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.ftruncate(fd, written)
        os.fsync(fd)
        self._fd = fd
        self._release = release
        return self

    def _verify_queue_root(self, fd: int) -> None:
        """Set and read back the directory's own permissions, then trust them.

        ``enforce_owner_only_file`` covers the lock file. The directory is a
        separate object with separate permissions, and on Windows it inherits
        whatever its parent grants unless something says otherwise. The
        platform call both applies and re-reads, so a failure here means the
        protection is genuinely absent rather than merely unrequested.
        """

        verify = getattr(self._platform, "verify_owner_only_path", None)
        if verify is None:
            os.close(fd)
            raise QueueNotPrivate("queue_root_acl_unenforceable")
        try:
            verified, _evidence = verify(str(self.path.parent), str(self.path))
        except OSError as exc:
            os.close(fd)
            raise QueueNotPrivate("queue_root_acl_unverified") from exc
        if not verified:
            os.close(fd)
            # The evidence mapping can name a pathname; the exception carries
            # only the reason code, matching every other durable refusal here.
            raise QueueNotPrivate("queue_root_not_owner_only")

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            if self._release is not None:
                self._release.__exit__(None, None, None)
                self._release = None
            os.close(self._fd)
            self._fd = None


def select_executor(cfg, *, os_name: str | None = None):
    """Choose the executor this platform actually has.

    On Windows the POSIX harness executor cannot run at all, so the queue gets
    the WSL2 executor instead. That executor refuses rather than runs today,
    but it refuses with the specific reason (provisioning stage, or the
    provider authentication blocker), which is what an operator needs, instead
    of a generic "platform unsupported" from a harness that was never going to
    work here.
    """

    name = os.name if os_name is None else os_name
    if name == "nt":
        if cfg.windows_wsl_runtime_root is None:
            raise ExecutionAdmissionError("windows_wsl_configuration_missing")
        # Imported here so POSIX workers never pay for, or depend on, the
        # Windows delegation stack.
        from .windows_delegation import DelegationConfig, verified_executor
        # verified_executor, not the bare class: the worker must not decide for
        # itself that the host is provisioned. It reads the machine-bound
        # verification record, and gets an executor that refuses by name when
        # there is not one.
        return verified_executor(DelegationConfig(
            runtime_root=cfg.windows_wsl_runtime_root,
            rootfs_path=cfg.windows_wsl_rootfs_path,
            manifest_path=cfg.windows_wsl_manifest_path,
            sidecar_path=cfg.windows_wsl_sidecar_path,
        ))
    required = (cfg.codex_task_executable, cfg.claude_task_executable,
                cfg.python_executable)
    if any(value is None for value in required):
        raise ExecutionAdmissionError("execution_configuration_missing")
    return SubprocessHarnessExecutor(Harnesses(
        codex=cfg.codex_task_executable, claude=cfg.claude_task_executable,
        python=cfg.python_executable,
        claude_config_dir=cfg.claude_config_dir))


def _configured_queue(config_path: str) -> ExecutionQueue:
    cfg = load(config_path)
    if cfg.execution_queue_root is None:
        raise ExecutionAdmissionError("execution_configuration_missing")
    return ExecutionQueue(
        cfg.execution_queue_root, select_executor(cfg), recover_interrupted=True,
        model_reserved=autoroute.model_reserved_for(str(cfg.state_root)))


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
    except (ExecutionAdmissionError, WorkerAlreadyRunning, QueueNotPrivate,
            ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
