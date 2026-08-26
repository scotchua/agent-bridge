"""Windows implementation of the agent-bridge platform interface."""

from __future__ import annotations

from . import base
from .windows_acl import (
    WINDOWS_OWNER_ONLY_GUARANTEE, icacls_listing_is_owner_only,
)

import contextlib
import ctypes
from ctypes import wintypes
import msvcrt
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Iterator


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
WAIT_TIMEOUT = 258


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
]
kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD


class WindowsPlatform:
    def __init__(self) -> None:
        # Unknown until something actually verifies it. Deliberately not
        # probed here: an import-time probe costs two subprocesses on every
        # invocation, and a cached answer can disagree with the live read-back
        # that preflight performs, which is the only answer that matters.
        self.supports_owner_only_permissions = False
        self._jobs: dict[int, int] = {}
        self._jobs_lock = threading.Lock()

    def set_owner_only_umask(self) -> None:
        os.umask(0o077)

    def enforce_owner_only_file(self, fd: int) -> None:
        """Apply an owner-only ACL, and record whether it could be verified.

        Deliberately does not raise. The capability flag is how a platform says
        "I cannot guarantee this", and preflight then refuses to start with an
        explanation. Raising here instead made every atomic write fail, so the
        bridge could not write any state at all, turning a documented refusal
        into a crash during ordinary file writes.

        The guarantee is not weakened by this: an unverified ACL sets the flag
        false, and assert_state_root_secure refuses before any consultation
        runs.
        """
        path = self._path_from_fd(fd)
        if not self._set_and_verify_owner_acl(path):
            self.supports_owner_only_permissions = False
            return
        self.supports_owner_only_permissions = True

    def verify_owner_only_path(self, directory: str,
                               probe_file: str) -> tuple[bool, dict[str, Any]]:
        """Verify the ACL, not st_mode.

        st_mode on Windows carries only a read-only bit, so the POSIX-shaped
        assertion would reject a correctly protected directory. The Windows
        guarantee is: no principal other than the owner, SYSTEM, and
        Administrators has any access.
        """
        results = {
            "mechanism": "windows acl round-trip",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
            "directory_acl_verified": self._set_and_verify_owner_acl(directory),
            "file_acl_verified": self._set_and_verify_owner_acl(probe_file),
        }
        verified = bool(results["directory_acl_verified"]
                        and results["file_acl_verified"])
        self.supports_owner_only_permissions = verified
        return verified, results

    @contextlib.contextmanager
    def lock_exclusive(self, fd: int, lock_path: str,
                       timeout: float) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not lock {lock_path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def spawn_isolated(self, argv: list[str], *, cwd: str,
                       env: dict[str, str]) -> subprocess.Popen[bytes]:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_SUSPENDED
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, shell explicitly off
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                shell=False,
                close_fds=True,
            )
            identity = self._process_creation_time(proc.pid)
            if not identity:
                raise OSError("could not read spawned process identity")
            job = self._create_job(self._job_name(proc.pid, identity))
            process_handle = wintypes.HANDLE(int(proc._handle))
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            with self._jobs_lock:
                self._jobs[proc.pid] = int(job)
            if kernel32.ResumeThread(wintypes.HANDLE(int(proc._thread))) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return proc
        except BaseException:
            if "job" in locals():
                kernel32.TerminateJobObject(job, 1)
                kernel32.CloseHandle(job)
            if "proc" in locals():
                with contextlib.suppress(OSError):
                    proc.kill()
            raise

    def isolated_process_group(self, pid: int) -> int | None:
        with self._jobs_lock:
            return pid if pid in self._jobs else None

    def terminate_process_tree(self, group_id: int,
                               grace: float) -> dict[str, Any]:
        report: dict[str, Any] = {
            "pgid": group_id, "ctrl_break": False, "job_terminated": False,
        }
        try:
            os.kill(group_id, signal.CTRL_BREAK_EVENT)
            report["ctrl_break"] = True
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline:
            if not self.process_tree_alive(group_id):
                report["group_gone_after_ctrl_break"] = True
                self._close_job(group_id)
                return report
            time.sleep(0.05)
        handle = self._job_handle(group_id)
        if handle is not None and kernel32.TerminateJobObject(handle, 1):
            report["job_terminated"] = True
        time.sleep(0.05)
        if self.process_tree_alive(group_id):
            report["group_survived_termination"] = True
        else:
            report["group_gone_after_termination"] = True
            self._close_job(group_id)
        return report

    def process_tree_alive(self, group_id: int) -> bool:
        process = kernel32.OpenProcess(SYNCHRONIZE, False, group_id)
        if not process:
            return False
        try:
            return kernel32.WaitForSingleObject(process, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(process)

    def process_identity(self, pid: int) -> str:
        return self._process_creation_time(pid)

    def process_group_identity(self, pid: int) -> tuple[str, str] | None:
        identity = self._process_creation_time(pid)
        if not identity:
            return ("", "")
        handle = self._job_handle(pid, identity)
        return identity, str(pid) if handle is not None else ""

    def is_own_process_group(self, group_id: int) -> bool:
        return group_id == os.getpid()

    def read_streams_with_caps(
        self,
        proc: subprocess.Popen[bytes],
        stdin_data: str,
        timeout: float,
        stdout_cap: int,
        stderr_cap: int,
        post_exit_drain_seconds: float,
    ) -> base.StreamReadResult:
        events: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        stop = threading.Event()
        cap_exceeded = threading.Event()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        caps = {"stdout": max(0, stdout_cap), "stderr": max(0, stderr_cap)}

        def reader(name: str, stream: Any) -> None:
            try:
                while not stop.is_set():
                    room = caps[name] - len(buffers[name])
                    if room <= 0:
                        cap_exceeded.set()
                        events.put((name, b""))
                        break
                    chunk = stream.read(min(65536, room + 1))
                    if chunk:
                        buffers[name].extend(chunk[:room])
                        if len(chunk) > room or len(buffers[name]) >= caps[name]:
                            cap_exceeded.set()
                    events.put((name, chunk or None))
                    if not chunk or cap_exceeded.is_set():
                        break
            except (OSError, ValueError):
                events.put((name, None))

        def writer() -> None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(stdin_data.encode("utf-8"))
                    proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        open_streams = {"stdout", "stderr"}
        threads = []
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is not None:
                thread = threading.Thread(target=reader, args=(name, stream), daemon=True)
                thread.start()
                threads.append(thread)
            else:
                open_streams.discard(name)
        input_thread = threading.Thread(target=writer, daemon=True)
        input_thread.start()

        timed_out = False
        descendant_held_pipes = False
        deadline = time.monotonic() + timeout
        exited_at: float | None = None
        try:
            while open_streams:
                if cap_exceeded.is_set():
                    break
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break
                if proc.poll() is not None:
                    if exited_at is None:
                        exited_at = now
                    if now - exited_at > post_exit_drain_seconds:
                        descendant_held_pipes = True
                        break
                try:
                    name, chunk = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if chunk is None:
                    open_streams.discard(name)
                    continue
        finally:
            stop.set()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()

        return base.StreamReadResult(
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            timed_out=timed_out,
            cap_exceeded=cap_exceeded.is_set(),
            descendant_held_pipes=descendant_held_pipes,
        )

    def _create_job(self, name: str) -> int:
        handle = kernel32.CreateJobObjectW(None, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        return int(handle)

    def _job_name(self, pid: int, identity: str) -> str:
        return f"Local\\agent-bridge-{pid}-{identity}"

    def _job_handle(self, pid: int, identity: str | None = None) -> int | None:
        with self._jobs_lock:
            handle = self._jobs.get(pid)
        if handle is not None:
            return handle
        identity = identity or self._process_creation_time(pid)
        if not identity:
            return None
        handle = kernel32.CreateJobObjectW(None, self._job_name(pid, identity))
        if not handle or ctypes.get_last_error() != 183:
            if handle:
                kernel32.CloseHandle(handle)
            return None
        with self._jobs_lock:
            self._jobs[pid] = int(handle)
        return int(handle)

    def _close_job(self, pid: int) -> None:
        with self._jobs_lock:
            handle = self._jobs.pop(pid, None)
        if handle is not None:
            kernel32.CloseHandle(handle)

    def _process_creation_time(self, pid: int) -> str:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_time),
                    ctypes.byref(kernel), ctypes.byref(user)):
                return ""
            return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)

    def _path_from_fd(self, fd: int) -> str:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        size = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size + 1)
        if not kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        path = buffer.value
        return path[4:] if path.startswith("\\\\?\\") else path

    def _acl_round_trip_supported(self) -> bool:
        """Actually probe, rather than assuming an answer.

        This was a stub returning False, which pinned
        supports_owner_only_permissions to False no matter how well the ACL
        machinery worked, so the bridge refused to start on every Windows
        machine for a reason unrelated to any ACL.

        Probes in a temporary directory that is discarded, so a failure here
        costs nothing and a success is evidence rather than an assumption.
        """
        import shutil
        import tempfile
        probe_dir = tempfile.mkdtemp(prefix="agent-bridge-acl-probe-")
        probe_file = os.path.join(probe_dir, "probe")
        try:
            with open(probe_file, "wb") as handle:
                handle.write(b"probe\n")
            return bool(self._set_and_verify_owner_acl(probe_dir)
                        and self._set_and_verify_owner_acl(probe_file))
        except OSError:
            return False
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def acl_diagnostics(self, path: str) -> dict[str, Any]:
        """Why an ACL attempt succeeded or failed. For operators and CI only."""
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
            timeout=10, check=False, shell=False)
        raw = identity.stdout.decode("utf-8", "replace").strip()
        fields = raw.split(",")
        user = fields[-1].strip('"') if fields else ""
        applied = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"*{user}:(F)"],
            capture_output=True, timeout=15, check=False, shell=False)
        observed = subprocess.run(
            ["icacls", path], capture_output=True, timeout=15,
            check=False, shell=False)
        return {
            "whoami_rc": identity.returncode,
            "whoami_raw": raw,
            "parsed_sid": user,
            "sid_looks_valid": user.startswith("S-1-"),
            "grant_rc": applied.returncode,
            "grant_stdout": applied.stdout.decode("utf-8", "replace").strip(),
            "grant_stderr": applied.stderr.decode("utf-8", "replace").strip(),
            "observed_rc": observed.returncode,
            "observed": observed.stdout.decode("utf-8", "replace").strip(),
        }

    def _set_and_verify_owner_acl(self, path: str) -> bool:
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
            timeout=10, check=False, shell=False,
        ).stdout.decode("utf-8", "replace").strip().split(",")
        user = identity[-1].strip('"')
        if not user.startswith("S-1-"):
            return False
        owner_name = identity[0].strip('"') if len(identity) > 1 else ""
        try:
            # The SID must be written *SID. icacls treats a bare SID as an
            # account name and fails with 1332, "No mapping between account
            # names and security IDs was done", so the grant silently never
            # applied and the capability could never be verified.
            applied = subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"*{user}:(F)"],
                capture_output=True, timeout=15, check=False, shell=False,
            )
            if applied.returncode != 0:
                return False
            observed = subprocess.run(
                ["icacls", path], capture_output=True, timeout=15,
                check=False, shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        text = observed.stdout.decode("utf-8", "replace")
        return (observed.returncode == 0
                and icacls_listing_is_owner_only(text, user, owner_name))
