"""POSIX implementation of the agent-bridge platform interface."""

from __future__ import annotations

from . import base

import contextlib
import fcntl
import os
import selectors
import signal
import subprocess
import time
from typing import Any, Iterator


class PosixPlatform:
    supports_owner_only_permissions = True

    def set_owner_only_umask(self) -> None:
        os.umask(0o077)

    def enforce_owner_only_file(self, fd: int) -> None:
        os.fchmod(fd, 0o600)

    @contextlib.contextmanager
    def lock_exclusive(self, fd: int, lock_path: str,
                       timeout: float) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not lock {lock_path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)

    def verify_owner_only_path(self, directory: str,
                               probe_file: str) -> tuple[bool, dict[str, Any]]:
        """Read the modes back. Unchanged from the original inline check.

        Catches a filesystem that accepts chmod without keeping it, which is
        what WSL's DrvFs does when state lands under /mnt/c.
        """
        observed_dir = os.stat(directory).st_mode & 0o777
        observed_file = os.stat(probe_file).st_mode & 0o777
        return (observed_dir == 0o700 and observed_file == 0o600, {
            "mechanism": "posix mode bits",
            "directory_mode": oct(observed_dir),
            "file_mode": oct(observed_file),
        })

    def spawn_isolated(self, argv: list[str], *, cwd: str,
                       env: dict[str, str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603 - fixed argv, shell explicitly off
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,   # own process group, killable as a unit
            shell=False,
            close_fds=True,
        )

    def isolated_process_group(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except OSError:
            return None

    def terminate_process_tree(self, group_id: int,
                               grace: float) -> dict[str, Any]:
        """SIGTERM the group, wait out the grace period, then SIGKILL. Report both."""
        report: dict[str, Any] = {
            "pgid": group_id, "sigterm": False, "sigkill": False,
        }
        try:
            os.killpg(group_id, signal.SIGTERM)
            report["sigterm"] = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline:
            try:
                os.killpg(group_id, 0)
            except ProcessLookupError:
                report["group_gone_after_sigterm"] = True
                return report
            except OSError:
                break
            time.sleep(0.05)
        try:
            os.killpg(group_id, signal.SIGKILL)
            report["sigkill"] = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
        time.sleep(0.05)
        try:
            os.killpg(group_id, 0)
            report["group_survived_sigkill"] = True
        except ProcessLookupError:
            report["group_gone_after_sigkill"] = True
        except OSError:
            pass
        return report

    def process_alive(self, pid: int) -> bool:
        """Signal 0 asks whether the process exists, and sends nothing."""
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True          # exists, owned by someone else
        except (OSError, ValueError):
            return False

    def process_tree_alive(self, group_id: int) -> bool:
        try:
            os.killpg(group_id, 0)
            return True
        except (ProcessLookupError, OSError):
            return False

    def process_group_members(self, group_id: int) -> list[str] | None:
        """Enumerate a group without relying on incompatible `ps -g` meanings.

        `ps -g <pgid>` selects a process group on macOS but a session or
        effective group name on Linux, so list every process and filter.
        """
        try:
            probe = subprocess.run(  # noqa: S603 - fixed argv, shell off
                ["ps", "-A", "-o", "pid=,pgid="], capture_output=True,
                timeout=10, check=False, shell=False)
        except (OSError, subprocess.SubprocessError):
            return None
        members: list[str] = []
        for line in probe.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                if int(parts[1]) == int(group_id) and int(parts[0]) != os.getpid():
                    members.append(parts[0])
            except ValueError:
                continue
        return members

    def process_liveness(self, pid: int) -> dict[str, Any]:
        evidence: dict[str, Any] = {"probe": "os.kill signal 0"}
        try:
            os.kill(pid, 0)
            evidence["alive"] = True
        except ProcessLookupError:
            evidence.update(alive=False, reason="no such process")
        except PermissionError:
            # Exists, owned by someone else. Alive for our purposes.
            evidence.update(alive=True, reason="permission denied, so it exists")
        except OSError as exc:
            evidence.update(alive=False, reason=f"errno {exc.errno}")
        return evidence

    def process_identity(self, pid: int) -> str:
        try:
            probe = subprocess.run(  # noqa: S603 - fixed argv, shell off
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True, timeout=10, check=False, shell=False)
            return probe.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def process_group_identity(self, pid: int) -> tuple[str, str] | None:
        try:
            probe = subprocess.run(  # noqa: S603 - fixed argv, shell off
                ["ps", "-o", "lstart=,pgid=", "-p", str(pid)],
                capture_output=True, timeout=10, check=False, shell=False)
        except (OSError, subprocess.SubprocessError):
            return None
        text = probe.stdout.decode("utf-8", "replace").strip()
        if not text:
            return ("", "")
        parts = text.rsplit(None, 1)
        if len(parts) != 2:
            return (text, "")
        return parts[0].strip(), parts[1].strip()

    def is_own_process_group(self, group_id: int) -> bool:
        return group_id == os.getpgrp()

    def read_streams_with_caps(
        self,
        proc: subprocess.Popen[bytes],
        stdin_data: str,
        timeout: float,
        stdout_cap: int,
        stderr_cap: int,
        post_exit_drain_seconds: float,
    ) -> tuple[bytes, bytes, bool, bool, bool]:
        timed_out = False
        cap_exceeded = False
        descendant_held_pipes = False

        # Non-blocking selector loop, no reader threads.
        buffers: dict[str, bytearray] = {
            "stdout": bytearray(), "stderr": bytearray(),
        }
        caps = {"stdout": max(0, stdout_cap), "stderr": max(0, stderr_cap)}
        pending = stdin_data.encode("utf-8")
        selector = selectors.DefaultSelector()
        registered: set[Any] = set()

        def register(stream: Any, events: int, name: str) -> None:
            if stream is None:
                return
            try:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events, name)
                registered.add(stream)
            except (OSError, ValueError, KeyError):
                pass

        def unregister(stream: Any) -> None:
            try:
                selector.unregister(stream)
            except (KeyError, ValueError, OSError):
                pass
            registered.discard(stream)
            try:
                stream.close()
            except (OSError, ValueError):
                pass

        try:
            register(proc.stdout, selectors.EVENT_READ, "stdout")
            register(proc.stderr, selectors.EVENT_READ, "stderr")
            if pending:
                register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            elif proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

            deadline = time.monotonic() + timeout
            exited_at: float | None = None
            while True:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                reading = any(
                    key.data in ("stdout", "stderr")
                    for key in selector.get_map().values()
                ) if selector.get_map() else False
                if proc.poll() is not None:
                    if exited_at is None:
                        exited_at = time.monotonic()
                    if not reading:
                        break
                    if time.monotonic() - exited_at > post_exit_drain_seconds:
                        descendant_held_pipes = True
                        break
                for key, _events in selector.select(timeout=0.05):
                    name = key.data
                    stream = key.fileobj
                    if name == "stdin":
                        try:
                            raw = getattr(stream, "raw", None)
                            written = raw.write(pending) if raw is not None \
                                else stream.write(pending)
                        except BlockingIOError as exc:
                            written = getattr(exc, "characters_written", 0) or 0
                        except InterruptedError:
                            continue
                        except (BrokenPipeError, OSError, ValueError):
                            pending = b""
                            unregister(stream)
                            continue
                        pending = pending[(written or 0):]
                        if not pending:
                            unregister(stream)
                        continue
                    try:
                        chunk = stream.read(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except (OSError, ValueError):
                        unregister(stream)
                        continue
                    if not chunk:
                        unregister(stream)
                        continue
                    buffer = buffers[name]
                    room = caps[name] - len(buffer)
                    if room > 0:
                        buffer.extend(chunk[:room])
                    if len(buffer) >= caps[name]:
                        cap_exceeded = True
                        break
                if cap_exceeded:
                    break
                if not selector.get_map() and proc.poll() is not None:
                    break
        finally:
            for stream in list(registered):
                unregister(stream)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            selector.close()

        return base.StreamReadResult(
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            timed_out=timed_out,
            cap_exceeded=cap_exceeded,
            descendant_held_pipes=descendant_held_pipes,
        )
