"""File-backed, bounded capture for one isolated subprocess.

This is deliberately smaller in scope than :mod:`agent_bridge.runner`: it
does not create lifecycle markers or make a routing decision.  Its callers
already own those duties.  It only prevents a parent process from buffering a
child's unbounded stdout/stderr in memory while preserving exact hashes of
the bounded bytes retained in private files.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .platform import platform as host_platform


READ_CHUNK_BYTES = 64 * 1024
POST_EXIT_DRAIN_SECONDS = 0.5


@dataclass(frozen=True)
class CapturedStream:
    """Metadata for the exact prefix durably captured for one stream."""

    path: Path
    bytes: int
    sha256: str
    overflowed: bool


@dataclass(frozen=True)
class CaptureResult:
    """The fixed result vocabulary for a file-backed capture attempt."""

    returncode: int | None
    stdout: CapturedStream
    stderr: CapturedStream
    failure: str | None


class _FilePump:
    """Copy one pipe into an owner-only file without accumulating it in RAM."""

    def __init__(self, stream: BinaryIO, path: Path, limit: int) -> None:
        self.stream = stream
        self.path = path
        self.limit = limit
        self.bytes = 0
        self.digest = hashlib.sha256()
        self.overflowed = False
        self.unavailable = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def result(self) -> CapturedStream:
        return CapturedStream(self.path, self.bytes, self.digest.hexdigest(), self.overflowed)

    def _run(self) -> None:
        fd = -1
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            host_platform.enforce_owner_only_file(fd)
            with os.fdopen(fd, "wb", closefd=True) as output:
                fd = -1
                while True:
                    chunk = self.stream.read(READ_CHUNK_BYTES)
                    if not chunk:
                        output.flush()
                        os.fsync(output.fileno())
                        return
                    room = self.limit - self.bytes
                    if room <= 0:
                        self.overflowed = True
                        output.flush()
                        os.fsync(output.fileno())
                        return
                    captured = chunk[:room]
                    output.write(captured)
                    self.digest.update(captured)
                    self.bytes += len(captured)
                    if len(captured) != len(chunk):
                        self.overflowed = True
                        output.flush()
                        os.fsync(output.fileno())
                        return
        except BaseException:
            self.unavailable = True
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the isolated child group, including inherited-pipe children."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            # Nested delegate processes deliberately remain in their outer
            # worker's group. Their pid is not a group id, so terminate the
            # direct child and let that outer owner sweep its own group.
            pass
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _write_stdin(process: subprocess.Popen[bytes], payload: bytes) -> None:
    try:
        if process.stdin is not None:
            process.stdin.write(payload)
            process.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        # The child owns whether it reads its declared request.  A refusal to
        # read it is represented by its return code, not raw pipe text.
        pass


def capture_process(process: subprocess.Popen[bytes], *, stdout_path: Path,
                    stderr_path: Path, limit: int, timeout: float,
                    stdin_data: bytes | None = None) -> CaptureResult:
    """Capture an already-isolated process with one cap per stream.

    ``failure`` is deliberately a fixed vocabulary: ``timeout``,
    ``output_overflow``, ``capture_unavailable``, or ``stream_unclosed``.
    In particular, an overflow is never a truncated successful result.
    """
    if limit <= 0:
        raise ValueError("capture limit must be positive")
    if process.stdout is None or process.stderr is None:
        raise ValueError("process must provide stdout and stderr pipes")
    stdout = _FilePump(process.stdout, stdout_path, limit)
    stderr = _FilePump(process.stderr, stderr_path, limit)
    stdout.start()
    stderr.start()
    if stdin_data is not None:
        threading.Thread(target=_write_stdin, args=(process, stdin_data), daemon=True).start()

    deadline = time.monotonic() + timeout
    failure: str | None = None
    while process.poll() is None:
        if stdout.unavailable or stderr.unavailable:
            failure = "capture_unavailable"
            break
        if stdout.overflowed or stderr.overflowed:
            failure = "output_overflow"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = "timeout"
            break
        try:
            process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            pass

    if failure is None:
        # A direct child can exit while a descendant holds a copied stdout or
        # stderr descriptor.  Bound that drain as well; otherwise EOF is an
        # unbounded wait under a different name.
        stdout.thread.join(POST_EXIT_DRAIN_SECONDS)
        stderr.thread.join(POST_EXIT_DRAIN_SECONDS)
        if stdout.unavailable or stderr.unavailable:
            failure = "capture_unavailable"
        elif stdout.overflowed or stderr.overflowed:
            failure = "output_overflow"
        elif stdout.thread.is_alive() or stderr.thread.is_alive():
            failure = "stream_unclosed"

    if failure is not None:
        _terminate_group(process)
    try:
        process.wait(timeout=POST_EXIT_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_group(process)
        try:
            process.wait(timeout=POST_EXIT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    stdout.thread.join(POST_EXIT_DRAIN_SECONDS)
    stderr.thread.join(POST_EXIT_DRAIN_SECONDS)
    if process.poll() is None and failure is None:
        failure = "stream_unclosed"
    for stream, reader in ((process.stdin, None), (process.stdout, stdout),
                           (process.stderr, stderr)):
        if stream is not None:
            # Buffered ``close`` can wait on the same lock a reader holds
            # while blocked in ``read``. A daemon reader is safer than
            # violating the parent deadline after a child escaped EOF.
            if reader is not None and reader.thread.is_alive():
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    return CaptureResult(process.poll(), stdout.result(), stderr.result(), failure)
