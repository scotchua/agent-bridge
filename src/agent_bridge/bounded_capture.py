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

    path: Path | None
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

    def __init__(self, stream: BinaryIO, *, path: Path | None,
                 output_file: BinaryIO | None, limit: int) -> None:
        if (path is None) == (output_file is None):
            raise ValueError("provide exactly one capture destination")
        self.stream = stream
        self.path = path
        self.output_file = output_file
        self.limit = limit
        self.bytes = 0
        self.digest = hashlib.sha256()
        self.overflowed = False
        self.unavailable = False
        # EOF is the pipe-lifecycle fact.  A durable named file may still be
        # in fsync after EOF, which must not turn a clean child exit into a
        # false stream_unclosed failure.
        self.eof_reached = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def result(self) -> CapturedStream:
        return CapturedStream(self.path, self.bytes, self.digest.hexdigest(), self.overflowed)

    def _run(self) -> None:
        fd = -1
        try:
            if self.output_file is None:
                assert self.path is not None
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                host_platform.enforce_owner_only_file(fd)
                output = os.fdopen(fd, "wb", closefd=True)
                fd = -1
                close_output = True
            else:
                output = self.output_file
                close_output = False
            try:
                while True:
                    chunk = self.stream.read(READ_CHUNK_BYTES)
                    if not chunk:
                        self.eof_reached = True
                        output.flush()
                        # Anonymous TemporaryFiles have no evidence path to
                        # make durable.  More importantly, avoid a costly
                        # fsync after EOF keeping their pump thread alive.
                        if close_output:
                            os.fsync(output.fileno())
                        return
                    room = self.limit - self.bytes
                    if room <= 0:
                        self.overflowed = True
                        return
                    captured = chunk[:room]
                    output.write(captured)
                    self.digest.update(captured)
                    self.bytes += len(captured)
                    if len(captured) != len(chunk):
                        self.overflowed = True
                        return
            finally:
                if close_output:
                    output.close()
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


def capture_process(process: subprocess.Popen[bytes], *,
                    stdout_path: Path | None = None,
                    stderr_path: Path | None = None,
                    stdout_file: BinaryIO | None = None,
                    stderr_file: BinaryIO | None = None,
                    limit: int, timeout: float,
                    stdin_data: bytes | None = None) -> CaptureResult:
    """Capture an already-isolated process with one cap per stream.

    ``failure`` is deliberately a fixed vocabulary: ``timeout``,
    ``output_overflow``, ``capture_unavailable``, or ``stream_unclosed``.
    In particular, an overflow is never a truncated successful result.
    """
    if limit <= 0:
        raise ValueError("capture limit must be positive")
    if process.stdout is None:
        raise ValueError("process must provide a stdout pipe")
    if process.stderr is None and (stderr_path is not None or stderr_file is not None):
        raise ValueError("stderr destination requires a stderr pipe")
    stdout = _FilePump(process.stdout, path=stdout_path, output_file=stdout_file, limit=limit)
    stderr = (_FilePump(process.stderr, path=stderr_path, output_file=stderr_file, limit=limit)
              if process.stderr is not None else None)
    stdout.start()
    if stderr is not None:
        stderr.start()
    stdin_writer: threading.Thread | None = None
    if stdin_data is not None:
        stdin_writer = threading.Thread(target=_write_stdin, args=(process, stdin_data), daemon=True)
        stdin_writer.start()

    deadline = time.monotonic() + timeout
    failure: str | None = None
    while process.poll() is None:
        if stdout.unavailable or (stderr is not None and stderr.unavailable):
            failure = "capture_unavailable"
            break
        if stdout.overflowed or (stderr is not None and stderr.overflowed):
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
        if stderr is not None:
            stderr.thread.join(POST_EXIT_DRAIN_SECONDS)
        if stdout.unavailable or (stderr is not None and stderr.unavailable):
            failure = "capture_unavailable"
        elif stdout.overflowed or (stderr is not None and stderr.overflowed):
            failure = "output_overflow"
        elif not stdout.eof_reached or (stderr is not None and not stderr.eof_reached):
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
    if stderr is not None:
        stderr.thread.join(POST_EXIT_DRAIN_SECONDS)
    if process.poll() is None and failure is None:
        failure = "stream_unclosed"
    for stream, reader in ((process.stdin, None), (process.stdout, stdout),
                           (process.stderr, stderr)):
        if stream is not None:
            # Buffered ``close`` can wait on the same lock a reader holds
            # while blocked in ``read``. A daemon reader is safer than
            # violating the parent deadline after a child escaped EOF.
            if ((reader is not None and reader.thread.is_alive())
                    or (reader is None and stdin_writer is not None
                        and stdin_writer.is_alive())):
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    empty_stderr = CapturedStream(None, 0, hashlib.sha256().hexdigest(), False)
    return CaptureResult(process.poll(), stdout.result(),
                         stderr.result() if stderr is not None else empty_stderr, failure)
