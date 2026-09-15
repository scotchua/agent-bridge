"""Windows implementation of the agent-bridge platform interface."""

from __future__ import annotations

from . import base
from .windows_acl import (
    WINDOWS_OWNER_ONLY_GUARANTEE, SE_DACL_PRESENT, SE_DACL_PROTECTED,
    SecurityState, acl_size, build_owner_only_acl, is_exactly_owner_only,
    judge_security, parse_acl, parse_sid,
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
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
ERROR_MORE_DATA = 234
WAIT_TIMEOUT = 258
WAIT_OBJECT_0 = 0
WAIT_FAILED = 0xFFFFFFFF
# A pending ReadFile cannot be cancelled safely by closing its Python stream
# from another thread: close() waits for that read to return.  Cleanup must
# therefore only wait a short, fixed interval for its owning daemon thread.
STREAM_THREAD_JOIN_SECONDS = 0.1
PROCESS_CLEANUP_WAIT_SECONDS = 5


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
kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryInformationJobObject.restype = wintypes.BOOL
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
kernel32.GetExitCodeProcess.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
]
kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.ReOpenFile.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
kernel32.ReOpenFile.restype = wintypes.HANDLE
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


kernel32.GetFileInformationByHandle.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
]
kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

# The owner-only ACL is read, written and read back through these, on an
# open handle, with no helper program and no text. Prototypes are declared
# for every one of them: GetCurrentProcess returns the pseudo-handle -1,
# which ctypes' default int return truncates to 0xFFFFFFFF on x64, and
# OpenProcessToken then fails with ERROR_INVALID_HANDLE (seen on the hosted
# x64 runner, run 34938146871).
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
advapi32.GetLengthSid.restype = wintypes.DWORD
advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
advapi32.IsValidSid.restype = wintypes.BOOL
advapi32.IsValidAcl.argtypes = [ctypes.c_void_p]
advapi32.IsValidAcl.restype = wintypes.BOOL
advapi32.GetSecurityInfo.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
]
advapi32.GetSecurityInfo.restype = wintypes.DWORD
advapi32.SetSecurityInfo.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
advapi32.SetSecurityInfo.restype = wintypes.DWORD
advapi32.GetSecurityDescriptorControl.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG),
]
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
ERROR_INSUFFICIENT_BUFFER = 122
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1


class WindowsPlatform:
    name = "nt"

    def __init__(self) -> None:
        # Unknown until something actually verifies it. Deliberately not
        # probed here: a cached answer can disagree with the live read-back
        # that preflight performs, which is the only answer that matters.
        self.supports_owner_only_permissions = False
        self._jobs: dict[int, int] = {}
        self._jobs_lock = threading.Lock()
        self._caller_sid_cache: str | None = None

    def set_owner_only_umask(self) -> None:
        os.umask(0o077)

    def enforce_owner_only_file(self, fd: int) -> None:
        """Protect the open file itself, through its handle, before the
        caller writes sensitive bytes.

        The descriptor is reopened for READ_CONTROL and WRITE_DAC on the same
        file object, so the DACL is read, replaced and read back on exactly
        the file the caller holds, whatever its name points at by now.
        Failure clears the capability flag and raises PermissionError with
        the evidence attached. The caller owns the descriptor and must close
        it if enforcement fails.
        """
        verified, evidence = self._protect_descriptor(fd)
        if not verified:
            self.supports_owner_only_permissions = False
            try:
                path = self._path_from_fd(fd)
            except OSError:
                path = f"descriptor {fd}"
            raise PermissionError(
                f"could not verify owner-only ACL for {path}: {evidence!r}")
        self.supports_owner_only_permissions = True

    def verify_owner_only_path(self, directory: str,
                               probe_file: str) -> tuple[bool, dict[str, Any]]:
        """Apply the owner-only ACL to both objects and read each back.

        st_mode on Windows carries only a read-only bit, so the POSIX-shaped
        assertion would reject a correctly protected directory. The Windows
        guarantee is: the object is owned by this account and no principal
        other than the owner, SYSTEM, and Administrators has any access.
        """
        results: dict[str, Any] = {
            "mechanism": "windows acl round-trip",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
        }
        for label, target in (("directory", directory), ("file", probe_file)):
            verified, evidence = self._protect_path(target)
            results[f"{label}_acl_verified"] = verified
            # Whichever target failed carries its own evidence. A bare boolean
            # has already cost several diagnostic round trips.
            if not verified:
                results[f"{label}_evidence"] = evidence
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
        # Not CREATE_SUSPENDED. The intent was right: start suspended, assign to
        # the Job Object, then resume, so a child cannot spawn descendants
        # before it joins the job. It is not reachable through subprocess.
        # subprocess.CREATE_SUSPENDED does not exist, and Popen closes the
        # thread handle immediately after CreateProcess, so there is nothing to
        # resume: proc._thread is not a real attribute.
        #
        # Reaching it would mean calling CreateProcess through ctypes and
        # reimplementing pipe and handle-inheritance setup, which is a large
        # amount of security-relevant code to write blind.
        #
        # So the child is assigned to the job immediately after it starts. The
        # residual race is a child that spawns a descendant in the interval
        # between CreateProcess returning and AssignProcessToJobObject, which is
        # microseconds against a CLI that has not finished loading. Descendants
        # created after assignment are covered.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
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
        # No console control event. os.kill with CTRL_BREAK_EVENT calls
        # GenerateConsoleCtrlEvent, which delivers to EVERY process attached to
        # the console, not only the target. In CI it reached the test runner
        # itself and raised KeyboardInterrupt eleven seconds into the run,
        # killing the suite from inside the code meant to clean up a peer.
        #
        # This means Windows has no safe equivalent of the POSIX polite stage.
        # Termination here is immediate via the Job Object, which is targeted
        # and cannot escape to the parent. That is a real behavioural
        # difference from POSIX, so it is reported rather than papered over:
        # a peer gets no chance to exit cleanly.
        if self.is_own_process_group(group_id):
            # The POSIX implementation refuses to signal its own group. This is
            # the same refusal: terminating a job that contains this process
            # would kill the broker from inside its own cleanup path.
            return {"pgid": group_id, "refused": "would terminate this process",
                    "job_terminated": False}
        alive_before = self.process_tree_alive(group_id)
        report: dict[str, Any] = {
            "pgid": group_id,
            "graceful_stage": "unavailable on windows",
            "graceful_stage_reason": (
                "a console control event cannot be targeted at one process "
                "tree without risking delivery to this process"
            ),
            "job_terminated": False,
        }
        if not alive_before:
            report["tree_already_gone"] = True
            self._close_job(group_id)
            return report
        handle = self._job_handle(group_id)
        del grace  # no graceful stage exists on this platform to wait out
        if handle is not None and kernel32.TerminateJobObject(handle, 1):
            report["job_terminated"] = True
        time.sleep(0.05)
        if self.process_tree_alive(group_id):
            report["termination_failed"] = True
            report["group_survived_termination"] = True
        else:
            report["group_gone_after_termination"] = True
            self._close_job(group_id)
        return report

    def process_alive(self, pid: int) -> bool:
        """Ask the OS, without sending anything.

        os.kill(pid, 0) must never be used here: on Windows signal 0 is
        CTRL_C_EVENT, so it delivers a console interrupt instead of probing.
        """
        handle = kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            # Exit code 259 (STILL_ACTIVE) is an ordinary user-selected exit
            # status.  The process handle's signalled state is the liveness
            # authority; GetExitCodeProcess is diagnostic only.
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    def process_liveness(self, pid: int) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "probe": "OpenProcess+WaitForSingleObject+GetExitCodeProcess",
        }
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # 87 ERROR_INVALID_PARAMETER is what a genuinely gone pid gives.
            # 5 ERROR_ACCESS_DENIED means the process EXISTS and we could not
            # ask, which must never be read as death.
            evidence.update(alive=False, open_process_error=ctypes.get_last_error())
            return evidence
        try:
            wait_result = int(kernel32.WaitForSingleObject(handle, 0))
            evidence["wait_result"] = wait_result
            if wait_result == WAIT_TIMEOUT:
                evidence["alive"] = True
            elif wait_result == WAIT_OBJECT_0:
                evidence["alive"] = False
            elif wait_result == WAIT_FAILED:
                evidence.update(alive=False, liveness_indeterminate=True,
                                wait_error=ctypes.get_last_error())
            else:
                evidence.update(alive=False, liveness_indeterminate=True,
                                unexpected_wait_result=True)
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                evidence["exit_code"] = int(code.value)
            else:
                evidence["get_exit_code_error"] = ctypes.get_last_error()
            return evidence
        finally:
            kernel32.CloseHandle(handle)

    def process_tree_alive(self, group_id: int) -> bool:
        members = self.process_group_members(group_id)
        if members is not None:
            return bool(members)
        # The job could not be opened. That happens in TWO situations which
        # must not be conflated: the job was destroyed because every member
        # exited and its last handle went with them, or the query genuinely
        # failed. Answering "alive" to both reports every clean shutdown as a
        # containment failure, which is what happened when this returned a
        # blanket True: terminate_process_tree saw a live tree before it
        # started, so a job the reader had already terminated came back as
        # group_survived_termination on every timed-out call.
        #
        # The leader distinguishes them. It is strictly less information than
        # the job list, and the residual gap is honest: a descendant that
        # outlives its leader while the job is unqueryable reads as gone. That
        # is the pre-existing behaviour, not a new one, and it is narrower
        # than reporting every finished job as an escape.
        return self.process_alive(group_id)

    def process_group_members(self, group_id: int) -> list[str] | None:
        handle = self._job_handle(group_id)
        if handle is None:
            return None
        capacity = 1
        header_size = ctypes.sizeof(wintypes.DWORD) * 2
        while True:
            buffer_size = header_size + capacity * ctypes.sizeof(ctypes.c_size_t)
            buffer = ctypes.create_string_buffer(buffer_size)
            returned = wintypes.DWORD()
            if kernel32.QueryInformationJobObject(
                    handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST, buffer,
                    buffer_size, ctypes.byref(returned)):
                assigned = wintypes.DWORD.from_buffer(buffer, 0).value
                listed = wintypes.DWORD.from_buffer(
                    buffer, ctypes.sizeof(wintypes.DWORD)).value
                ids_type = ctypes.c_size_t * listed
                ids = ids_type.from_buffer(buffer, header_size)
                return [str(ids[index]) for index in range(listed)]
            error = ctypes.get_last_error()
            if error != ERROR_MORE_DATA:
                return None
            assigned = wintypes.DWORD.from_buffer(buffer, 0).value
            if assigned <= capacity:
                return None
            capacity = assigned

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
            finally:
                # This thread owns the stream.  Closing it here is safe only
                # after its read returned; closing it from the coordinating
                # thread while ReadFile is pending can block forever.
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

        def writer() -> None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(stdin_data.encode("utf-8"))
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                # As with the readers, this thread owns stdin while write()
                # may be pending.  It closes stdin itself once it is done.
                if proc.stdin is not None:
                    with contextlib.suppress(OSError, ValueError):
                        proc.stdin.close()

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
            io_threads = [*threads, input_thread]
            must_terminate_tree = (
                timed_out or cap_exceeded.is_set() or descendant_held_pipes
            )
            if not must_terminate_tree:
                # An EOF event is sent just before a reader returns.  Give that
                # normal path a bounded chance to finish before treating a
                # still-live IO owner as a reason to terminate the tree.
                for thread in io_threads:
                    thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)
                must_terminate_tree = any(thread.is_alive() for thread in io_threads)
            if must_terminate_tree:
                # The whole TREE, not only the leader.  A leader can exit while
                # a descendant still owns stdout or stderr, so poll() cannot
                # decide whether the Job Object must be terminated.  Failure
                # to terminate is intentionally not treated as containment:
                # the daemon reader remains detached and the incomplete-output
                # flag keeps any captured prefix from becoming a response.
                with contextlib.suppress(Exception):
                    self.terminate_process_tree(proc.pid, 0.0)
                if proc.poll() is None:
                    with contextlib.suppress(OSError, ValueError):
                        proc.kill()
                    with contextlib.suppress(
                            OSError, ValueError, subprocess.TimeoutExpired):
                        proc.wait(timeout=PROCESS_CLEANUP_WAIT_SECONDS)

            # Do not synchronously close proc.stdin/stdout/stderr here.  Each
            # stream is owned and closed by its writer or reader thread.  A
            # bounded join lets ordinary Job Object cleanup release those
            # threads while a descendant outside the job cannot make this
            # method, and therefore the worker's deadline, hang forever.
            for thread in io_threads:
                thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)

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

    # ------------------------------------------------------------------
    # Owner-only ACLs.
    #
    # Everything below works on an open handle: the object is opened once
    # (with FILE_FLAG_OPEN_REPARSE_POINT, and refused if it turns out to be
    # a reparse point), its owner and DACL are read through that handle, the
    # DACL is replaced in one SetSecurityInfo call with the exact owner-only
    # DACL, and the result is read back through the same handle. No helper
    # program runs, no text is parsed, and nothing is addressed by name
    # after the open, so a name that is swapped underneath the check points
    # the check at a different object rather than at a different DACL.
    #
    # Known limitation, stated rather than hidden: a directory tree is
    # enumerated by name (os.scandir) and each entry is then opened by name.
    # An object swapped between those two steps is caught by the
    # reparse-point refusal and by the ownership check on the handle that
    # was actually opened; it is not caught by comparing file identities.
    # ------------------------------------------------------------------

    def _caller_sid(self) -> str | None:
        """The SID of this process's token user, from the token itself."""
        if self._caller_sid_cache is not None:
            return self._caller_sid_cache
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0,
                                         ctypes.byref(needed))
            if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not needed.value:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buffer,
                                                needed, ctypes.byref(needed)):
                return None
            # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first field is
            # the PSID.
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_pointer or not advapi32.IsValidSid(sid_pointer):
                return None
            raw = ctypes.string_at(sid_pointer, advapi32.GetLengthSid(sid_pointer))
            try:
                self._caller_sid_cache = parse_sid(raw)
            except ValueError:
                return None
            return self._caller_sid_cache
        finally:
            kernel32.CloseHandle(token)

    def _examine_handle(self, handle: int) -> tuple[int | None, bool, dict[str, Any]]:
        """Refuse a reparse point; report whether the object is a directory.

        Closes the handle on refusal so callers never hold one they must
        not use.
        """
        info = _BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            return None, False, {"attributes_error": error}
        if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            kernel32.CloseHandle(handle)
            return None, False, {"refused": "reparse point"}
        return handle, bool(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY), {}

    def _open_securable(self, path: str, *, write: bool
                        ) -> tuple[int | None, bool, dict[str, Any]]:
        """Open ``path`` for its security descriptor, never through a link."""
        access = READ_CONTROL | (WRITE_DAC if write else 0)
        handle = kernel32.CreateFileW(
            path, access, FILE_SHARE_ALL, None, OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            return None, False, {"open_error": ctypes.get_last_error()}
        return self._examine_handle(handle)

    def _reopen_descriptor(self, fd: int, *, write: bool
                           ) -> tuple[int | None, bool, dict[str, Any]]:
        """A second handle to the very file ``fd`` holds, with security access.

        ReOpenFile works from the existing handle, not from any name, so the
        DACL that follows is applied to the file the caller is about to
        write and to nothing else.
        """
        try:
            original = msvcrt.get_osfhandle(fd)
        except OSError as exc:
            return None, False, {"descriptor_error": str(exc)}
        access = READ_CONTROL | (WRITE_DAC if write else 0)
        handle = kernel32.ReOpenFile(original, access, FILE_SHARE_ALL, 0)
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            return None, False, {"reopen_error": ctypes.get_last_error()}
        return self._examine_handle(handle)

    def _sddl(self, descriptor: ctypes.c_void_p) -> str:
        """The descriptor as SDDL text, for evidence only. Never judged."""
        text = ctypes.c_void_p()
        length = wintypes.ULONG(0)
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, SDDL_REVISION_1,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                ctypes.byref(text), ctypes.byref(length)):
            return ""
        try:
            return ctypes.wstring_at(text.value) if text.value else ""
        finally:
            kernel32.LocalFree(text)

    def _read_security(self, handle: int, is_directory: bool
                       ) -> tuple[SecurityState | None, dict[str, Any]]:
        """Owner and DACL of the object behind ``handle``, decoded strictly."""
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = advapi32.GetSecurityInfo(
            handle, SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, ctypes.byref(dacl), None,
            ctypes.byref(descriptor))
        if status != 0:
            return None, {"read_error": status}
        try:
            control = wintypes.WORD(0)
            revision = wintypes.DWORD(0)
            if not advapi32.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)):
                return None, {"control_error": ctypes.get_last_error()}
            owner_sid = ""
            if owner.value:
                if not advapi32.IsValidSid(owner):
                    return None, {"owner_error": "invalid owner SID"}
                owner_sid = parse_sid(
                    ctypes.string_at(owner.value, advapi32.GetLengthSid(owner)))
            aces = None
            if control.value & SE_DACL_PRESENT and dacl.value:
                header = ctypes.string_at(dacl.value, 8)
                aces = parse_acl(ctypes.string_at(dacl.value, acl_size(header)))
            state = SecurityState(
                owner_sid=owner_sid,
                protected=bool(control.value & SE_DACL_PROTECTED),
                dacl=aces,
                is_directory=is_directory,
                sddl=self._sddl(descriptor),
            )
            return state, {}
        except ValueError as exc:
            return None, {"decode_error": str(exc)}
        finally:
            kernel32.LocalFree(descriptor)

    def _write_owner_only(self, handle: int, caller: str,
                          is_directory: bool) -> str | None:
        """Replace the DACL in one call. Returns an error string, or None."""
        acl = build_owner_only_acl(caller, directory=is_directory)
        buffer = ctypes.create_string_buffer(acl, len(acl))
        if not advapi32.IsValidAcl(buffer):
            return "built DACL is not valid"
        status = advapi32.SetSecurityInfo(
            handle, SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, buffer, None)
        return None if status == 0 else f"SetSecurityInfo failed with {status}"

    def _protect_handle(self, handle: int, is_directory: bool
                        ) -> tuple[bool, dict[str, Any]]:
        """Read, replace, read back: all on one handle, one write in between.

        An object owned by another account is refused untouched: its DACL
        is that account's business, and an OWNER RIGHTS entry on it would
        grant that account, not us, however owner-only it looked.
        """
        evidence: dict[str, Any] = {
            "mechanism": "windows acl round-trip",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
            "directory": is_directory,
        }
        caller = self._caller_sid()
        if caller is None:
            evidence["error"] = "caller identity unavailable"
            return False, evidence
        evidence["caller_sid"] = caller
        before, error = self._read_security(handle, is_directory)
        if before is None:
            evidence.update(error)
            return False, evidence
        evidence["owner_sid"] = before.owner_sid
        evidence["before"] = before.sddl
        if before.owner_sid != caller:
            evidence["refused"] = "owned by another account"
            return False, evidence
        failure = self._write_owner_only(handle, caller, is_directory)
        if failure is not None:
            evidence["error"] = failure
            return False, evidence
        after, error = self._read_security(handle, is_directory)
        if after is None:
            evidence.update(error)
            return False, evidence
        evidence["after"] = after.sddl
        problems = judge_security(after, caller)
        exact = is_exactly_owner_only(after, caller)
        if not exact and not problems:
            problems = ["read-back is not the DACL that was written"]
        evidence["problems"] = problems
        return exact, evidence

    def _protect_path(self, path: str) -> tuple[bool, dict[str, Any]]:
        handle, is_directory, error = self._open_securable(path, write=True)
        if handle is None:
            return False, {"mechanism": "windows acl round-trip",
                           "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
                           "path": path, **error}
        try:
            return self._protect_handle(handle, is_directory)
        finally:
            kernel32.CloseHandle(handle)

    def _protect_descriptor(self, fd: int) -> tuple[bool, dict[str, Any]]:
        handle, is_directory, error = self._reopen_descriptor(fd, write=True)
        if handle is None:
            return False, {"mechanism": "windows acl round-trip",
                           "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE, **error}
        try:
            return self._protect_handle(handle, is_directory)
        finally:
            kernel32.CloseHandle(handle)

    def _set_and_verify_owner_acl(self, path: str) -> bool:
        """Apply the owner-only ACL to one object by path and read it back."""
        return self._protect_path(path)[0]

    def _observe_handle(self, handle: int, is_directory: bool, *,
                        allow_inherited: bool) -> tuple[bool, dict[str, Any]]:
        evidence: dict[str, Any] = {
            "mechanism": "windows acl read-back",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
            "directory": is_directory,
        }
        caller = self._caller_sid()
        if caller is None:
            evidence["error"] = "caller identity unavailable"
            return False, evidence
        evidence["caller_sid"] = caller
        state, error = self._read_security(handle, is_directory)
        if state is None:
            evidence.update(error)
            return False, evidence
        problems = judge_security(state, caller, allow_inherited=allow_inherited)
        evidence.update({
            "owner_sid": state.owner_sid,
            "protected": state.protected,
            "sddl": state.sddl,
            "problems": problems,
        })
        return not problems, evidence

    def observe_owner_only_acl(self, path: str, *, allow_inherited: bool = False
                               ) -> tuple[bool, dict[str, Any]]:
        """Read the ACL back and judge it. Changes nothing.

        The counterpart to :meth:`verify_owner_only_path`, which applies the
        ACL before it reads it. A readiness report and a test assertion both
        need the reading without the applying: describing a machine must not
        modify it, and an assertion that first repairs what it asserts proves
        nothing. Same judgment, no write.

        ``allow_inherited`` is for an object inside a tree whose protected
        root has been proved separately (the Claude lane's store): its
        entries are copies of the root's. An object proved on its own keeps
        the default and refuses inherited entries.
        """
        handle, is_directory, error = self._open_securable(path, write=False)
        if handle is None:
            return False, {"mechanism": "windows acl read-back",
                           "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
                           "path": path, **error}
        try:
            return self._observe_handle(handle, is_directory,
                                        allow_inherited=allow_inherited)
        finally:
            kernel32.CloseHandle(handle)

    def acl_diagnostics(self, path: str) -> dict[str, Any]:
        """What the ACL looks like now. Describes; never re-applies anything."""
        verified, evidence = self.observe_owner_only_acl(path)
        evidence["verified"] = verified
        evidence["path"] = path
        return evidence

    def observe_owner_only_tree(self, root: str, expected_paths: list[str]
                                ) -> tuple[bool, dict[str, Any]]:
        """Judge every object the caller walked, each through its own handle.

        Changes nothing. The root must be strictly owner-only (protected,
        no inherited entry); each descendant must be owned by the caller
        and carry only permitted principals with the caller among them,
        inherited entries allowed, because with the root protected an
        inherited entry can only be a copy of one on an ancestor this same
        pass judges. An object the caller did not walk is not judged, and
        not vouched for.
        """
        failed: list[str] = []
        verified, evidence = self.observe_owner_only_acl(root)
        if not verified:
            failed.append(root)
        for path in expected_paths:
            verified, _ = self.observe_owner_only_acl(path, allow_inherited=True)
            if not verified:
                failed.append(path)
        return not failed, {
            "mechanism": "windows acl tree read-back",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
            "caller_sid": evidence.get("caller_sid"),
            "objects_expected": len(expected_paths) + 1,
            "objects_failed": failed[:32],
        }

    def enforce_owner_only_tree(self, root: str, expected_paths: list[str]
                                ) -> tuple[bool, dict[str, Any]]:
        """Bring every walked object to owner-only, each through its own handle.

        The root is judged strictly and each descendant with inherited
        entries allowed. An object that passes is left alone. One that
        fails has its DACL replaced, in a single write, with the exact
        owner-only DACL and is read back. One owned by another account is
        never touched and fails the pass: it is that account's file, and
        this lane must not use a store holding one. Nothing here removes
        entries one at a time, so there is no intermediate DACL in which a
        deny has gone while a grant it masked remains.
        """
        evidence: dict[str, Any] = {
            "mechanism": "windows acl tree enforcement",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
            "objects_expected": len(expected_paths) + 1,
        }
        caller = self._caller_sid()
        if caller is None:
            evidence["error"] = "caller identity unavailable"
            return False, evidence
        evidence["caller_sid"] = caller
        repaired = 0
        failed: list[str] = []
        foreign_owned: list[str] = []
        for index, path in enumerate([root, *expected_paths]):
            handle, is_directory, _error = self._open_securable(path, write=True)
            if handle is None:
                failed.append(path)
            else:
                try:
                    state, _error = self._read_security(handle, is_directory)
                    if state is None:
                        failed.append(path)
                    elif state.owner_sid != caller:
                        foreign_owned.append(path)
                    elif judge_security(state, caller, allow_inherited=index > 0):
                        verified, _ = self._protect_handle(handle, is_directory)
                        if verified:
                            repaired += 1
                        else:
                            failed.append(path)
                finally:
                    kernel32.CloseHandle(handle)
            if len(failed) + len(foreign_owned) >= 32:
                break
        evidence.update({
            "objects_repaired": repaired,
            "objects_failed": failed[:32],
            "objects_foreign_owned": foreign_owned[:32],
        })
        return not failed and not foreign_owned, evidence
