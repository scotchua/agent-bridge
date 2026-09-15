"""Windows implementation of the agent-bridge platform interface."""

from __future__ import annotations

from . import base
from .windows_acl import (
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
    WINDOWS_OWNER_ONLY_GUARANTEE, DirectoryEntry, SecurityState,
    accepted_owners, build_owner_only_descriptor, encode_object_name,
    is_exactly_owner_only, judge_security, parse_directory_listing,
    parse_security_descriptor, parse_sid,
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

kernel32.GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
]
kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

# The caller's identity comes from its token, and the descriptor's SDDL
# text is produced for evidence only. Prototypes are declared for every
# call: GetCurrentProcess returns the pseudo-handle -1, which ctypes'
# default int return truncates to 0xFFFFFFFF on x64, and OpenProcessToken
# then fails with ERROR_INVALID_HANDLE (seen on the hosted x64 runner, run
# 34938146871).
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
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG),
]
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

# Security is read and written through ntdll, on an open handle, one
# object at a time. Two things kernel32 and advapi32 cannot do are needed:
#
# * NtCreateFile with RootDirectory set opens a single name inside an
#   already-open directory and nowhere else, so a tree is walked from one
#   verified root without ever resolving an absolute path again.
# * NtSetSecurityObject sets the descriptor of the object behind the
#   handle and touches nothing else. advapi32's handle-based writer walks
#   a directory's subtree and rewrites every inheriting descendant, which
#   changed a child's DACL before that child's own ownership check had run
#   (Codex review of ccb85ef, R2; reproduced live on the ARM64 VM).
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_void_p),
    ]


ntdll.NtCreateFile.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
    ctypes.POINTER(_OBJECT_ATTRIBUTES), ctypes.POINTER(_IO_STATUS_BLOCK),
    ctypes.POINTER(wintypes.LARGE_INTEGER), wintypes.ULONG, wintypes.ULONG,
    wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
]
ntdll.NtCreateFile.restype = wintypes.ULONG
ntdll.NtQuerySecurityObject.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG),
]
ntdll.NtQuerySecurityObject.restype = wintypes.ULONG
ntdll.NtSetSecurityObject.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
]
ntdll.NtSetSecurityObject.restype = wintypes.ULONG

TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
TOKEN_OWNER_CLASS = 4
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_FILES = 18
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
FILE_LIST_DIRECTORY = 0x00000001
FILE_READ_ATTRIBUTES = 0x00000080
#: The share mode every security open here uses: readers and writers may
#: keep working, but the object may not be deleted or renamed while the
#: handle is held. Without the delete share bit (0x4) in our mode, an open
#: that asks for DELETE (which a rename or a delete does) fails with a
#: sharing violation for as long as we hold the object. The kernel applies
#: sharing only between opens that ask for data access, and a directory
#: open here asks for FILE_LIST_DIRECTORY, so the root and every directory
#: under it keep their names and existence for the whole tree pass: a
#: directory cannot be swapped for another under the same name between
#: its open and the judgment of what its handle lists (Codex review of
#: ccb85ef..2e9ed0f, B1; shown live by NativeTreeTests). A file open asks
#: for no data access, so a file is not pinned: pinning one would need
#: FILE_READ_DATA, which the pass neither has nor needs, and which would
#: make a file we own but cannot read unrepairable. Whatever happens to a
#: name after a pass ends is outside what that pass proved; the read-only
#: pass that follows enforcement enumerates afresh, and its result is
#: what the lane relies on. Conversely, our own open fails if another
#: handle already holds a directory with DELETE access; that refusal is
#: fail-closed and reported.
FILE_SHARE_KEEP_NAME = 0x00000003
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_OPEN = 1
FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x00000040
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
STATUS_BUFFER_TOO_SMALL = 0xC0000023
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1
FILE_FULL_DIRECTORY_INFO = 14
FILE_FULL_DIRECTORY_RESTART_INFO = 15
DIRECTORY_LISTING_BUFFER = 65536
#: Objects (root included) a tree pass will open before refusing the tree
#: as something this lane did not create, unless the caller lowers it.
MAX_TREE_OBJECTS = 20000
#: Directory nesting a tree pass will follow.
MAX_TREE_DEPTH = 32


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
        self._default_owner_cache: str | None = None

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
    # Everything below works on an open handle. A single object is opened
    # once by name (with FILE_FLAG_OPEN_REPARSE_POINT, and refused if it
    # turns out to be a reparse point), its owner and DACL are read
    # through that handle, the DACL is replaced in one NtSetSecurityObject
    # call with the exact owner-only DACL, and the result is read back
    # through the same handle. No helper program runs, no text is parsed,
    # and nothing is addressed by name after the open.
    #
    # A tree is walked from its root handle: each directory is enumerated
    # through its own handle and each entry is opened relative to that
    # handle (NtCreateFile with RootDirectory), so an ancestor swapped for a
    # junction after the root was opened cannot redirect the walk, an entry
    # that is itself a reparse point is opened as the reparse point and
    # refused, and a directory replaced wholesale is enumerated as it is
    # now, extra objects included. Every write is to one object only: the
    # writer does not propagate to descendants, so a child is never
    # changed before it has been judged through its own handle, and no
    # object owned by another account is ever written to.
    #
    # Known limitation, stated rather than hidden: the root itself is
    # opened once by its resolved absolute path. A component above the
    # root swapped for a junction between that resolution and the open
    # points the whole pass at a different tree; the pass then judges and
    # protects that tree's objects, which it can only do if the caller owns
    # them, and it never reaches into the intended tree. Nothing below the
    # root is addressed by absolute path.
    # ------------------------------------------------------------------

    def _token_sid(self, information_class: int) -> str | None:
        """One SID from this process's token: TokenUser or TokenOwner."""
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, information_class, None, 0,
                                         ctypes.byref(needed))
            if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not needed.value:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, information_class, buffer,
                                                needed, ctypes.byref(needed)):
                return None
            # TOKEN_USER begins with SID_AND_ATTRIBUTES and TOKEN_OWNER is a
            # single PSID; in both the first field is the SID pointer.
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_pointer or not advapi32.IsValidSid(sid_pointer):
                return None
            raw = ctypes.string_at(sid_pointer, advapi32.GetLengthSid(sid_pointer))
            try:
                return parse_sid(raw)
            except ValueError:
                return None
        finally:
            kernel32.CloseHandle(token)

    def _caller_sid(self) -> str | None:
        """The SID of this process's token user, from the token itself."""
        if self._caller_sid_cache is None:
            self._caller_sid_cache = self._token_sid(TOKEN_USER_CLASS)
        return self._caller_sid_cache

    def _default_owner_sid(self) -> str | None:
        """The SID Windows makes the owner of objects this process creates.

        For most accounts this is the user itself. For an elevated member
        of Administrators it is the Administrators group: the hosted
        Windows runner (run 34940818792) showed every temporary file it had
        just created owned by S-1-5-32-544, and a check that accepted only
        the user SID refused the runner's own files as another account's.
        A non-elevated account's default owner is itself, so for it an
        Administrators-owned object stays foreign.
        """
        if self._default_owner_cache is None:
            self._default_owner_cache = self._token_sid(TOKEN_OWNER_CLASS)
        return self._default_owner_cache

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

    def _open_securable(self, path: str, *, write: bool, listing: bool = False
                        ) -> tuple[int | None, bool, dict[str, Any]]:
        """Open ``path`` for its security descriptor, never through a link.

        The one open by absolute path: a standalone object, or the root of
        a tree whose every descendant is then opened relative to it.
        ``listing`` asks for FILE_LIST_DIRECTORY as well, for a root that
        is about to be enumerated.
        """
        access = READ_CONTROL | (WRITE_DAC if write else 0)
        access |= FILE_LIST_DIRECTORY if listing else 0
        handle = kernel32.CreateFileW(
            path, access, FILE_SHARE_KEEP_NAME, None, OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            return None, False, {"open_error": ctypes.get_last_error()}
        return self._examine_handle(handle)

    def _open_child(self, parent: int, name: str, *, write: bool, listing: bool
                    ) -> tuple[int | None, bool, dict[str, Any]]:
        """Open one name inside an open directory, relative to its handle.

        NtCreateFile with RootDirectory set resolves ``name`` inside that
        directory and nowhere else, whatever any ancestor has become since
        the directory was opened. The name must be a single component: a
        separator, a dot entry, a NUL or a stream colon is refused before
        any call is made. FILE_OPEN_REPARSE_POINT opens a junction or link
        as itself, and :meth:`_examine_handle` then refuses it.
        """
        if not name or name in (".", "..") or any(ch in name for ch in "\\/\0:"):
            return None, False, {"refused": "not a single path component"}
        access = READ_CONTROL | FILE_READ_ATTRIBUTES | (WRITE_DAC if write else 0)
        access |= FILE_LIST_DIRECTORY if listing else 0
        # Length is the UTF-16 byte count, which len(name) * 2 understates
        # for a supplementary character; the understated length would open
        # a shorter name. MaximumLength is the buffer's real capacity.
        try:
            encoded = encode_object_name(name)
        except ValueError:
            return None, False, {"refused": "name is not a valid object name"}
        buffer = ctypes.create_string_buffer(encoded, len(encoded) + 2)
        text = _UNICODE_STRING(len(encoded), ctypes.sizeof(buffer),
                               ctypes.cast(buffer, wintypes.LPWSTR))
        attributes = _OBJECT_ATTRIBUTES(
            ctypes.sizeof(_OBJECT_ATTRIBUTES), parent, ctypes.pointer(text),
            OBJ_CASE_INSENSITIVE, None, None)
        handle = wintypes.HANDLE()
        status_block = _IO_STATUS_BLOCK()
        status = ntdll.NtCreateFile(
            ctypes.byref(handle), access, ctypes.byref(attributes),
            ctypes.byref(status_block), None, 0, FILE_SHARE_KEEP_NAME, FILE_OPEN,
            FILE_OPEN_REPARSE_POINT | FILE_OPEN_FOR_BACKUP_INTENT, None, 0)
        if status != 0 or not handle.value:
            return None, False, {"open_status": f"0x{status:08X}"}
        return self._examine_handle(handle.value)

    def _reopen_descriptor(self, fd: int, *, write: bool
                           ) -> tuple[int | None, bool, dict[str, Any]]:
        """A second handle to the very file ``fd`` holds, with security access.

        ReOpenFile works from the existing handle, not from any name, so the
        DACL that follows is applied to the file the caller is about to
        write and to nothing else. FILE_FLAG_OPEN_REPARSE_POINT is passed
        so that a reparse point is reopened as itself and refused, rather
        than followed.
        """
        try:
            original = msvcrt.get_osfhandle(fd)
        except OSError as exc:
            return None, False, {"descriptor_error": str(exc)}
        access = READ_CONTROL | (WRITE_DAC if write else 0)
        handle = kernel32.ReOpenFile(original, access, FILE_SHARE_KEEP_NAME,
                                     FILE_FLAG_OPEN_REPARSE_POINT)
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            return None, False, {"reopen_error": ctypes.get_last_error()}
        return self._examine_handle(handle)

    def _list_directory(self, handle: int
                        ) -> tuple[tuple[DirectoryEntry, ...] | None, dict[str, Any]]:
        """Every entry of the directory behind ``handle``, from the handle."""
        entries: list[DirectoryEntry] = []
        buffer = ctypes.create_string_buffer(DIRECTORY_LISTING_BUFFER)
        information_class = FILE_FULL_DIRECTORY_RESTART_INFO
        while True:
            if not kernel32.GetFileInformationByHandleEx(
                    handle, information_class, buffer, len(buffer)):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    return tuple(entries), {}
                return None, {"listing_error": error}
            information_class = FILE_FULL_DIRECTORY_INFO
            try:
                entries.extend(parse_directory_listing(buffer.raw))
            except ValueError as exc:
                return None, {"listing_decode_error": str(exc)}
            if len(entries) > MAX_TREE_OBJECTS:
                return None, {"listing_error": "more entries than the lane creates"}

    def _sddl(self, descriptor: Any) -> str:
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
        wanted = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        needed = wintypes.ULONG(0)
        status = ntdll.NtQuerySecurityObject(handle, wanted, None, 0,
                                             ctypes.byref(needed))
        if status != STATUS_BUFFER_TOO_SMALL or not needed.value:
            return None, {"read_status": f"0x{status:08X}"}
        buffer = ctypes.create_string_buffer(needed.value)
        status = ntdll.NtQuerySecurityObject(handle, wanted, buffer, needed,
                                             ctypes.byref(needed))
        if status != 0:
            return None, {"read_status": f"0x{status:08X}"}
        try:
            state = parse_security_descriptor(
                buffer.raw[:needed.value], is_directory=is_directory,
                sddl=self._sddl(buffer))
        except ValueError as exc:
            return None, {"decode_error": str(exc)}
        return state, {}

    def _write_owner_only(self, handle: int, caller: str,
                          is_directory: bool) -> str | None:
        """Replace this object's DACL in one call, on this object only.

        Returns an error string, or None. The descriptor carries the exact
        owner-only DACL and the protected bit, and nothing else; the call
        sets it on the object behind ``handle`` and does not propagate to
        any descendant.
        """
        descriptor = build_owner_only_descriptor(caller, directory=is_directory)
        buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
        status = ntdll.NtSetSecurityObject(
            handle, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            buffer)
        return None if status == 0 else f"NtSetSecurityObject failed with 0x{status:08X}"

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
        default_owner = self._default_owner_sid()
        evidence["caller_sid"] = caller
        if default_owner is not None and default_owner != caller:
            evidence["default_owner_sid"] = default_owner
        owners = accepted_owners(caller, default_owner)
        before, error = self._read_security(handle, is_directory)
        if before is None:
            evidence.update(error)
            return False, evidence
        evidence["owner_sid"] = before.owner_sid
        evidence["before"] = before.sddl
        if before.owner_sid not in owners:
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
        problems = judge_security(after, caller, default_owner_sid=default_owner)
        exact = is_exactly_owner_only(after, caller, default_owner_sid=default_owner)
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
        default_owner = self._default_owner_sid()
        evidence["caller_sid"] = caller
        state, error = self._read_security(handle, is_directory)
        if state is None:
            evidence.update(error)
            return False, evidence
        problems = judge_security(state, caller, allow_inherited=allow_inherited,
                                  default_owner_sid=default_owner)
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

    def _walk_tree(self, root_handle: int, *, write: bool, max_objects: int
                   ) -> tuple[list[tuple[str, int, bool, int]], dict[str, Any]]:
        """Open every object under an open root, each relative to its parent.

        Returns ``(objects, error)``. ``objects`` lists ``(label, handle,
        is_directory, depth)`` in walk order, the root first with label
        ``"."`` and depth 0; every handle is open and the caller closes them
        all. A non-empty ``error`` means the walk stopped early (an entry
        that could not be opened, a reparse point, an object whose kind
        changed between listing and open, or a tree larger or deeper than
        the lane creates); the objects opened so far are still returned so
        they can be closed. Nothing here writes.
        """
        objects: list[tuple[str, int, bool, int]] = [(".", root_handle, True, 0)]
        pending: list[tuple[int, str, int]] = [(root_handle, "", 0)]
        while pending:
            parent, prefix, depth = pending.pop()
            if depth >= MAX_TREE_DEPTH:
                return objects, {"error": "tree deeper than the lane creates",
                                 "path": prefix or "."}
            entries, error = self._list_directory(parent)
            if entries is None:
                return objects, {**error, "path": prefix or "."}
            for entry in sorted(entries, key=lambda item: item.name):
                label = f"{prefix}\\{entry.name}" if prefix else entry.name
                if len(objects) >= max_objects:
                    return objects, {"error": "tree holds more objects than the lane creates",
                                     "path": label}
                handle, is_directory, error = self._open_child(
                    parent, entry.name, write=write, listing=entry.is_directory)
                if handle is None:
                    return objects, {**error, "path": label}
                objects.append((label, handle, is_directory, depth + 1))
                if is_directory != entry.is_directory:
                    return objects, {"error": "object changed kind between listing and open",
                                     "path": label}
                if is_directory:
                    pending.append((handle, label, depth + 1))
        return objects, {}

    def _open_tree(self, root: str, *, write: bool, max_objects: int
                   ) -> tuple[list[tuple[str, int, bool, int]], dict[str, Any]]:
        """The root by path, then everything under it by handle."""
        handle, is_directory, error = self._open_securable(
            root, write=write, listing=True)
        if handle is None:
            return [], {**error, "path": "."}
        if not is_directory:
            kernel32.CloseHandle(handle)
            return [], {"error": "root is not a directory", "path": "."}
        return self._walk_tree(handle, write=write, max_objects=max_objects)

    def observe_owner_only_tree(self, root: str, *,
                                max_objects: int = MAX_TREE_OBJECTS
                                ) -> tuple[bool, dict[str, Any]]:
        """Judge every object under ``root``, each through its own handle.

        Changes nothing. The tree is enumerated now, from the root handle,
        so what is judged is what is there, extra objects included. The
        root must be strictly owner-only (protected, no inherited entry);
        each descendant must be owned by the caller and carry only
        permitted principals with the caller among them, inherited entries
        allowed, because with the root protected an inherited entry can
        only be a copy of one on an ancestor this same pass judges.
        """
        evidence: dict[str, Any] = {
            "mechanism": "windows acl tree read-back",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
        }
        caller = self._caller_sid()
        if caller is None:
            evidence["error"] = "caller identity unavailable"
            return False, evidence
        default_owner = self._default_owner_sid()
        evidence["caller_sid"] = caller
        objects, walk_error = self._open_tree(root, write=False,
                                              max_objects=max_objects)
        failed: list[str] = []
        try:
            for label, handle, is_directory, depth in objects:
                state, _error = self._read_security(handle, is_directory)
                if state is None or judge_security(
                        state, caller, allow_inherited=depth > 0,
                        default_owner_sid=default_owner):
                    failed.append(label)
        finally:
            for _label, handle, _is_directory, _depth in objects:
                kernel32.CloseHandle(handle)
        if walk_error:
            evidence.update(walk_error)
            failed.append(walk_error.get("path", "."))
        evidence.update({"objects_seen": len(objects),
                         "objects_failed": failed[:32]})
        return not failed, evidence

    def enforce_owner_only_tree(self, root: str, *,
                                max_objects: int = MAX_TREE_OBJECTS
                                ) -> tuple[bool, dict[str, Any]]:
        """Bring every object under ``root`` to owner-only, each through its own handle.

        The whole tree is opened and every object's ownership is read
        before anything is written. If any object is owned by another
        account, or any object's descriptor could not be read, or the walk
        could not complete, nothing is written and the pass fails: an
        object whose owner is unknown may be another account's, and this
        lane must not use a store holding one. Every directory handle,
        the root's included, is held without delete sharing until the
        pass ends, so no directory judged here can be renamed away or
        replaced meanwhile (files are not pinned; see
        FILE_SHARE_KEEP_NAME). Otherwise the root is judged strictly
        and each descendant with inherited entries allowed; an object that
        passes is left alone, and one that fails has its DACL replaced, in
        a single write to that object only, with the exact owner-only DACL
        and is read back. No write propagates, so no object's descriptor
        changes before its own judgment.
        """
        evidence: dict[str, Any] = {
            "mechanism": "windows acl tree enforcement",
            "guarantee": WINDOWS_OWNER_ONLY_GUARANTEE,
        }
        caller = self._caller_sid()
        if caller is None:
            evidence["error"] = "caller identity unavailable"
            return False, evidence
        default_owner = self._default_owner_sid()
        evidence["caller_sid"] = caller
        owners = accepted_owners(caller, default_owner)
        objects, walk_error = self._open_tree(root, write=True,
                                              max_objects=max_objects)
        repaired = 0
        failed: list[str] = []
        foreign_owned: list[str] = []
        try:
            states: list[SecurityState | None] = []
            for label, handle, is_directory, _depth in objects:
                state, _error = self._read_security(handle, is_directory)
                states.append(state)
                if state is None:
                    failed.append(label)
                elif state.owner_sid not in owners:
                    foreign_owned.append(label)
            if not foreign_owned and not walk_error and not failed:
                for (label, handle, is_directory, depth), state in zip(objects, states):
                    if judge_security(state, caller, allow_inherited=depth > 0,
                                      default_owner_sid=default_owner):
                        verified, _evidence = self._protect_handle(handle, is_directory)
                        if verified:
                            repaired += 1
                        else:
                            failed.append(label)
        finally:
            for _label, handle, _is_directory, _depth in objects:
                kernel32.CloseHandle(handle)
        if walk_error:
            evidence.update(walk_error)
            failed.append(walk_error.get("path", "."))
        evidence.update({
            "objects_seen": len(objects),
            "objects_repaired": repaired,
            "objects_failed": failed[:32],
            "objects_foreign_owned": foreign_owned[:32],
        })
        return not failed and not foreign_owned, evidence
