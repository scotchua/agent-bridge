"""Offline behavioral checks for Windows platform methods lifted from source."""
from __future__ import annotations

import ast
import contextlib
import ctypes
from ctypes import wintypes
import os
import io
import queue
import time
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/agent_bridge/platform/windows.py"


def method(name, **scope):
    tree = ast.parse(SOURCE.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "WindowsPlatform")
    function = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                    and node.name == name)
    namespace = {"__name__": __name__, "Any": object, "Iterator": object,
                 "contextlib": contextlib, "ctypes": ctypes, "wintypes": wintypes,
                 "os": os}
    namespace.update(scope)
    exec(compile(ast.Module(body=[function], type_ignores=[]),
                 str(SOURCE), "exec"), namespace)
    return namespace[name]


class PathVerificationTests(unittest.TestCase):
    def test_enforce_owner_only_file_sets_capability_after_success(self):
        subject = SimpleNamespace(supports_owner_only_permissions=False,
                                  _protect_descriptor=lambda fd: (True, {}))
        method("enforce_owner_only_file")(subject, 7)
        self.assertTrue(subject.supports_owner_only_permissions)

    def test_verify_owner_only_path_requires_both_objects(self):
        subject = SimpleNamespace(supports_owner_only_permissions=True,
                                  _protect_path=lambda path: (path == "directory", {}))
        verified, evidence = method("verify_owner_only_path",
                                    WINDOWS_OWNER_ONLY_GUARANTEE="private")(subject,
                                            "directory", "probe")
        self.assertFalse(verified)
        self.assertFalse(subject.supports_owner_only_permissions)
        self.assertFalse(evidence["file_acl_verified"])

    def test_acl_round_trip_requires_both_probe_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_tempfile = SimpleNamespace(mkdtemp=lambda **kwargs: directory)
            fake_shutil = SimpleNamespace(rmtree=lambda *args, **kwargs: None)
            subject = SimpleNamespace(_set_and_verify_owner_acl=lambda path:
                                      path == directory)
            with mock.patch.dict("sys.modules", {"tempfile": fake_tempfile,
                                                  "shutil": fake_shutil}):
                self.assertFalse(method("_acl_round_trip_supported")(subject))

    def test_set_and_verify_owner_acl_returns_protection_result(self):
        subject = SimpleNamespace(_protect_path=lambda path: (path == "saved", {}))
        self.assertTrue(method("_set_and_verify_owner_acl")(subject, "saved"))

    def test_acl_diagnostics_returns_evidence_with_verdict_and_path(self):
        subject = SimpleNamespace(observe_owner_only_acl=lambda path:
                                  (True, {"owner": "Invented Co"}))
        self.assertEqual(method("acl_diagnostics")(subject, "C:\\invented"),
                         {"owner": "Invented Co", "verified": True,
                          "path": "C:\\invented"})


class JobAndProcessTests(unittest.TestCase):
    def test_spawn_isolated_rejects_missing_process_identity(self):
        proc = SimpleNamespace(pid=2345, kill=mock.Mock())
        popen = mock.Mock(return_value=proc)
        fake_subprocess = SimpleNamespace(Popen=popen, CREATE_NEW_PROCESS_GROUP=512,
                                          PIPE=-1)
        subject = SimpleNamespace(_process_creation_time=lambda pid: "")
        with self.assertRaisesRegex(OSError, "identity"):
            method("spawn_isolated", subprocess=fake_subprocess)(subject,
                         ["invented.exe"], cwd="C:\\invented", env={})
        proc.kill.assert_called_once()

    def test_isolated_process_group_reports_only_registered_pid(self):
        subject = SimpleNamespace(_jobs_lock=threading.Lock(), _jobs={2345: 99})
        self.assertEqual(method("isolated_process_group")(subject, 2345), 2345)
        self.assertIsNone(method("isolated_process_group")(subject, 2346))

    def test_terminate_process_tree_does_not_terminate_missing_job(self):
        kernel = SimpleNamespace(TerminateJobObject=mock.Mock())
        subject = SimpleNamespace(is_own_process_group=lambda pid: False,
                                  process_tree_alive=mock.Mock(side_effect=[True, False]),
                                  _job_handle=lambda pid: None,
                                  _close_job=mock.Mock())
        report = method("terminate_process_tree", kernel32=kernel,
                        time=SimpleNamespace(sleep=lambda seconds: None))(subject, 2345, 0)
        kernel.TerminateJobObject.assert_not_called()
        self.assertFalse(report["job_terminated"])
        self.assertTrue(report["group_gone_after_termination"])

    def test_process_tree_alive_uses_members_when_query_succeeds(self):
        subject = SimpleNamespace(process_group_members=lambda pid: [],
                                  process_alive=mock.Mock(return_value=True))
        self.assertFalse(method("process_tree_alive")(subject, 2345))
        subject.process_alive.assert_not_called()

    def test_process_identity_returns_creation_time(self):
        subject = SimpleNamespace(_process_creation_time=lambda pid: "invented-time")
        self.assertEqual(method("process_identity")(subject, 2345), "invented-time")

    def test_process_group_identity_requires_creation_time(self):
        subject = SimpleNamespace(_process_creation_time=lambda pid: "",
                                  _job_handle=mock.Mock())
        self.assertEqual(method("process_group_identity")(subject, 2345), ("", ""))
        subject._job_handle.assert_not_called()

    def test_is_own_process_group_matches_current_pid(self):
        self.assertTrue(method("is_own_process_group")(object(), os.getpid()))
        self.assertFalse(method("is_own_process_group")(object(), os.getpid() + 1))

    def test_create_job_rejects_zero_handle(self):
        kernel = SimpleNamespace(CreateJobObjectW=lambda unused, name: 0)
        fake_ctypes = SimpleNamespace(get_last_error=lambda: 5,
                                      WinError=lambda error: OSError(error))
        with self.assertRaises(OSError):
            method("_create_job", kernel32=kernel, ctypes=fake_ctypes)(object(),
                                                                         "invented-job")

    def test_job_name_contains_pid_and_identity(self):
        self.assertEqual(method("_job_name")(object(), 2345, "invented-time"),
                         "Local\\agent-bridge-2345-invented-time")

    def test_job_handle_accepts_only_an_existing_named_job(self):
        kernel = SimpleNamespace(CreateJobObjectW=lambda unused, name: 52,
                                 CloseHandle=mock.Mock())
        subject = SimpleNamespace(_jobs_lock=threading.Lock(), _jobs={},
                                  _job_name=lambda pid, identity: "invented-job")
        fake_ctypes = SimpleNamespace(get_last_error=lambda: 183)
        self.assertEqual(method("_job_handle", kernel32=kernel,
                                ctypes=fake_ctypes)(subject, 2345, "invented-time"), 52)
        self.assertEqual(subject._jobs[2345], 52)
        kernel.CloseHandle.assert_not_called()

    def test_close_job_closes_registered_handle(self):
        kernel = SimpleNamespace(CloseHandle=mock.Mock())
        subject = SimpleNamespace(_jobs_lock=threading.Lock(), _jobs={2345: 52})
        method("_close_job", kernel32=kernel)(subject, 2345)
        kernel.CloseHandle.assert_called_once_with(52)
        self.assertNotIn(2345, subject._jobs)

    def test_process_creation_time_returns_empty_on_failed_open(self):
        kernel = SimpleNamespace(OpenProcess=lambda *args: 0,
                                 CloseHandle=mock.Mock())
        result = method("_process_creation_time", kernel32=kernel,
                        PROCESS_QUERY_LIMITED_INFORMATION=0x1000)(object(), 2345)
        self.assertEqual(result, "")
        kernel.CloseHandle.assert_not_called()


class HandleAndSecurityTests(unittest.TestCase):
    def test_path_from_fd_rejects_failed_length_query(self):
        kernel = SimpleNamespace(GetFinalPathNameByHandleW=lambda *args: 0)
        fake_ctypes = SimpleNamespace(get_last_error=lambda: 5,
                                      WinError=lambda error: OSError(error))
        fake_msvcrt = SimpleNamespace(get_osfhandle=lambda fd: 44)
        with self.assertRaises(OSError):
            method("_path_from_fd", kernel32=kernel, ctypes=fake_ctypes,
                   msvcrt=fake_msvcrt)(object(), 7)

    def test_caller_sid_loads_an_empty_cache(self):
        subject = SimpleNamespace(_caller_sid_cache=None,
                                  _token_sid=mock.Mock(return_value="invented-sid"))
        self.assertEqual(method("_caller_sid", TOKEN_USER_CLASS=1)(subject),
                         "invented-sid")
        subject._token_sid.assert_called_once_with(1)

    def test_default_owner_sid_loads_an_empty_cache(self):
        subject = SimpleNamespace(_default_owner_cache=None,
                                  _token_sid=mock.Mock(return_value="invented-sid"))
        self.assertEqual(method("_default_owner_sid", TOKEN_OWNER_CLASS=4)(subject),
                         "invented-sid")
        subject._token_sid.assert_called_once_with(4)

    def test_examine_handle_closes_a_failed_query(self):
        kernel = SimpleNamespace(GetFileInformationByHandle=lambda *args: False,
                                 CloseHandle=mock.Mock())
        fake_ctypes = SimpleNamespace(byref=lambda value: value,
                                      get_last_error=lambda: 5)
        result = method("_examine_handle", kernel32=kernel, ctypes=fake_ctypes,
                        _BY_HANDLE_FILE_INFORMATION=lambda: object())(object(), 44)
        self.assertEqual(result, (None, False, {"attributes_error": 5}))
        kernel.CloseHandle.assert_called_once_with(44)

    def test_open_securable_rejects_invalid_handle(self):
        kernel = SimpleNamespace(CreateFileW=lambda *args: 0)
        subject = SimpleNamespace(_examine_handle=mock.Mock())
        result = method("_open_securable", kernel32=kernel,
                        ctypes=SimpleNamespace(get_last_error=lambda: 5),
                        READ_CONTROL=1, WRITE_DAC=2, FILE_LIST_DIRECTORY=4,
                        FILE_SHARE_KEEP_NAME=3, OPEN_EXISTING=3,
                        FILE_FLAG_BACKUP_SEMANTICS=8, FILE_FLAG_OPEN_REPARSE_POINT=16,
                        INVALID_HANDLE_VALUE=-1)(subject, "C:\\invented", write=False)
        self.assertEqual(result[:2], (None, False))
        subject._examine_handle.assert_not_called()

    def test_reopen_descriptor_rejects_invalid_handle(self):
        kernel = SimpleNamespace(ReOpenFile=lambda *args: 0)
        fake_msvcrt = SimpleNamespace(get_osfhandle=lambda fd: 44)
        subject = SimpleNamespace(_examine_handle=mock.Mock())
        result = method("_reopen_descriptor", kernel32=kernel, msvcrt=fake_msvcrt,
                        ctypes=SimpleNamespace(get_last_error=lambda: 5),
                        READ_CONTROL=1, WRITE_DAC=2, FILE_SHARE_KEEP_NAME=3,
                        FILE_FLAG_OPEN_REPARSE_POINT=16,
                        INVALID_HANDLE_VALUE=-1)(subject, 7, write=False)
        self.assertEqual(result[:2], (None, False))
        subject._examine_handle.assert_not_called()

    def test_list_directory_finishes_on_no_more_files(self):
        kernel = SimpleNamespace(GetFileInformationByHandleEx=lambda *args: False)
        fake_ctypes = SimpleNamespace(create_string_buffer=lambda size: bytearray(size),
                                      get_last_error=lambda: 18)
        result = method("_list_directory", kernel32=kernel, ctypes=fake_ctypes,
                        DIRECTORY_LISTING_BUFFER=64,
                        FILE_FULL_DIRECTORY_RESTART_INFO=15,
                        ERROR_NO_MORE_FILES=18)(object(), 44)
        self.assertEqual(result, ((), {}))

    def test_read_security_retries_after_size_query(self):
        class Native:
            def __init__(self):
                self.calls = 0
            def NtQuerySecurityObject(self, handle, wanted, buffer, size, needed):
                self.calls += 1
                needed.value = 4
                return 0xC0000023 if self.calls == 1 else 0
        native = Native()
        fake_ctypes = SimpleNamespace(byref=lambda value: value,
                                      create_string_buffer=lambda size: SimpleNamespace(raw=b"abcd"))
        subject = SimpleNamespace(_sddl=lambda buffer: "")
        result = method("_read_security", ntdll=native, ctypes=fake_ctypes,
                        OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4,
                        STATUS_BUFFER_TOO_SMALL=0xC0000023,
                        parse_security_descriptor=lambda data, **kwargs: "parsed")(subject,
                                                                                      44, False)
        self.assertEqual(result, ("parsed", {}))
        self.assertEqual(native.calls, 2)

    def test_write_owner_only_accepts_success_status(self):
        fake_ctypes = SimpleNamespace(create_string_buffer=lambda data, size: data)
        native = SimpleNamespace(NtSetSecurityObject=lambda *args: 0)
        result = method("_write_owner_only", ctypes=fake_ctypes, ntdll=native,
                        build_owner_only_descriptor=lambda caller, **kwargs: b"acl",
                        DACL_SECURITY_INFORMATION=4,
                        PROTECTED_DACL_SECURITY_INFORMATION=8)(object(), 44,
                                                               "invented-sid", False)
        self.assertIsNone(result)

    def test_protect_path_reports_open_error_without_protecting(self):
        subject = SimpleNamespace(_open_securable=lambda *args, **kwargs:
                                  (None, False, {"open_error": 5}),
                                  _protect_handle=mock.Mock())
        verified, evidence = method("_protect_path",
                                    WINDOWS_OWNER_ONLY_GUARANTEE="private")(subject,
                                                              "C:\\invented")
        self.assertFalse(verified)
        self.assertEqual(evidence["open_error"], 5)
        subject._protect_handle.assert_not_called()

    def test_protect_descriptor_reports_reopen_error_without_protecting(self):
        subject = SimpleNamespace(_reopen_descriptor=lambda *args, **kwargs:
                                  (None, False, {"reopen_error": 5}),
                                  _protect_handle=mock.Mock())
        verified, evidence = method("_protect_descriptor",
                                    WINDOWS_OWNER_ONLY_GUARANTEE="private")(subject, 7)
        self.assertFalse(verified)
        self.assertEqual(evidence["reopen_error"], 5)
        subject._protect_handle.assert_not_called()

    def test_observe_handle_rejects_unknown_caller(self):
        subject = SimpleNamespace(_caller_sid=lambda: None,
                                  _read_security=mock.Mock())
        verified, evidence = method("_observe_handle",
                                    WINDOWS_OWNER_ONLY_GUARANTEE="private")(subject,
                                                  44, False, allow_inherited=False)
        self.assertFalse(verified)
        self.assertEqual(evidence["error"], "caller identity unavailable")
        subject._read_security.assert_not_called()

    def test_observe_acl_reports_open_error_without_reading(self):
        subject = SimpleNamespace(_open_securable=lambda *args, **kwargs:
                                  (None, False, {"open_error": 5}),
                                  _observe_handle=mock.Mock())
        verified, evidence = method("observe_owner_only_acl",
                                    WINDOWS_OWNER_ONLY_GUARANTEE="private")(subject,
                                                               "C:\\invented")
        self.assertFalse(verified)
        self.assertEqual(evidence["open_error"], 5)
        subject._observe_handle.assert_not_called()

    def test_walk_tree_refuses_depth_limit(self):
        subject = SimpleNamespace(_list_directory=mock.Mock())
        objects, error = method("_walk_tree", MAX_TREE_DEPTH=0)(subject, 44,
                                                       write=False, max_objects=2)
        self.assertEqual(objects, [(".", 44, True, 0)])
        self.assertEqual(error["error"], "tree deeper than the lane creates")
        subject._list_directory.assert_not_called()


class InitializationAndLockTests(unittest.TestCase):
    def test_initial_capability_is_unknown_until_verified(self):
        subject = SimpleNamespace()
        method("__init__", threading=threading)(subject)
        self.assertFalse(subject.supports_owner_only_permissions)

    def test_owner_only_umask_uses_077(self):
        with mock.patch.object(os, "umask") as umask:
            method("set_owner_only_umask")(object())
        umask.assert_called_once_with(0o077)

    def test_empty_lock_file_gets_a_byte_before_locking(self):
        fake_msvcrt = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2,
                                      locking=mock.Mock())
        with tempfile.TemporaryFile() as file:
            lock = method("lock_exclusive", msvcrt=fake_msvcrt, time=time)
            with lock(object(), file.fileno(), "invented.lock", 1):
                self.assertEqual(os.fstat(file.fileno()).st_size, 1)
        self.assertEqual(fake_msvcrt.locking.call_count, 2)


class RemainingJobAndSecurityTests(unittest.TestCase):
    def test_process_group_members_stops_on_non_resize_error(self):
        class Kernel:
            def __init__(self):
                self.calls = 0
            def QueryInformationJobObject(self, handle, kind, buffer, size, returned):
                self.calls += 1
                buffer[:4] = (2).to_bytes(4, "little")
                return False
        kernel = Kernel()
        subject = SimpleNamespace(_job_handle=lambda pid: 44)
        with mock.patch.object(ctypes, "get_last_error", lambda: 5, create=True):
            result = method("process_group_members", kernel32=kernel,
                            JOB_OBJECT_BASIC_PROCESS_ID_LIST=3,
                            ERROR_MORE_DATA=234)(subject, 2345)
        self.assertIsNone(result)
        self.assertEqual(kernel.calls, 1)

    def test_token_sid_continues_after_expected_size_error(self):
        class Advapi:
            def __init__(self):
                self.queries = 0
            def OpenProcessToken(self, process, access, token):
                token._obj.value = 55
                return True
            def GetTokenInformation(self, token, kind, buffer, size, needed):
                self.queries += 1
                needed._obj.value = 4
                return False
        advapi = Advapi()
        kernel = SimpleNamespace(GetCurrentProcess=lambda: 1,
                                 CloseHandle=mock.Mock())
        fake_ctypes = SimpleNamespace(byref=ctypes.byref,
                                      get_last_error=lambda: 122,
                                      create_string_buffer=ctypes.create_string_buffer)
        result = method("_token_sid", advapi32=advapi, kernel32=kernel,
                        ctypes=fake_ctypes, TOKEN_QUERY=8,
                        ERROR_INSUFFICIENT_BUFFER=122)(object(), 1)
        self.assertIsNone(result)
        self.assertEqual(advapi.queries, 2)
        kernel.CloseHandle.assert_called_once()

    def test_open_child_accepts_successful_native_status(self):
        class Native:
            def NtCreateFile(self, handle, *args):
                handle.value = 44
                return 0
        fake_ctypes = SimpleNamespace(create_string_buffer=lambda *args: object(),
                                      sizeof=lambda value: 8,
                                      cast=lambda *args: object(),
                                      pointer=lambda value: value,
                                      byref=lambda value: value)
        subject = SimpleNamespace(_examine_handle=mock.Mock(
            return_value=(44, False, {})))
        result = method("_open_child", ctypes=fake_ctypes, ntdll=Native(),
                        encode_object_name=lambda name: b"a\x00",
                        _UNICODE_STRING=lambda *args: object(),
                        _OBJECT_ATTRIBUTES=lambda *args: object(),
                        _IO_STATUS_BLOCK=lambda: object(),
                        READ_CONTROL=1, FILE_READ_ATTRIBUTES=2,
                        WRITE_DAC=4, FILE_LIST_DIRECTORY=8, FILE_READ_DATA=16,
                        FILE_SHARE_KEEP_NAME=3, FILE_OPEN=1,
                        FILE_OPEN_REPARSE_POINT=32,
                        FILE_OPEN_FOR_BACKUP_INTENT=64,
                        OBJ_CASE_INSENSITIVE=128)(subject, 10, "a",
                                                   write=False, listing=False)
        self.assertEqual(result, (44, False, {}))
        subject._examine_handle.assert_called_once_with(44)

    def test_sddl_returns_text_after_successful_conversion(self):
        class Advapi:
            def ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    self, descriptor, revision, flags, text, length):
                text.value = 77
                return True
        kernel = SimpleNamespace(LocalFree=mock.Mock())
        fake_ctypes = SimpleNamespace(c_void_p=lambda: SimpleNamespace(value=None),
                                      byref=lambda value: value,
                                      wstring_at=lambda ptr: "invented-sddl")
        result = method("_sddl", advapi32=Advapi(), kernel32=kernel,
                        ctypes=fake_ctypes, SDDL_REVISION_1=1,
                        OWNER_SECURITY_INFORMATION=1,
                        DACL_SECURITY_INFORMATION=4)(object(), object())
        self.assertEqual(result, "invented-sddl")
        kernel.LocalFree.assert_called_once()

    def test_observe_tree_judges_root_strictly(self):
        from test_windows_acl_security import OWNER_SID
        kernel = SimpleNamespace(CloseHandle=mock.Mock())
        subject = SimpleNamespace(_caller_sid=lambda: OWNER_SID,
                                  _default_owner_sid=lambda: OWNER_SID,
                                  _open_tree=lambda *args, **kwargs:
                                  ([(".", 44, True, 0)], {}),
                                  _read_security=lambda *args: (object(), {}))
        judge = lambda state, caller, *, allow_inherited, **kwargs: (
            [] if allow_inherited else ["inherited entry on root"])
        verified, evidence = method("observe_owner_only_tree", kernel32=kernel,
                                    judge_security=judge,
                                    MAX_TREE_OBJECTS=20000,
                                    WINDOWS_OWNER_ONLY_GUARANTEE="private")(
                                        subject, "C:\\invented")
        self.assertFalse(verified)
        self.assertEqual(evidence["objects_failed"], ["."])


class StreamBoundaryTests(unittest.TestCase):
    def test_zero_stdout_cap_is_exceeded_before_read(self):
        from test_windows_stream_security import (
            ExitedLeader, RecordingPlatform, source_windows_method)
        proc = ExitedLeader(io.BytesIO(b""))
        result = source_windows_method("read_streams_with_caps", {})(
            RecordingPlatform(), proc, "", 1, 0, 1024, 0.01)
        self.assertTrue(result.cap_exceeded)

    def test_writer_sends_stdin_when_present(self):
        from test_windows_stream_security import (
            ExitedLeader, RecordingPlatform, source_windows_method)
        class Input:
            def __init__(self):
                self.data = b""
            def write(self, data):
                self.data += data
            def close(self):
                pass
        proc = ExitedLeader(io.BytesIO(b""))
        proc.stdin = Input()
        source_windows_method("read_streams_with_caps", {})(
            RecordingPlatform(), proc, "invented input", 1, 1024, 1024, 0.01)
        self.assertEqual(proc.stdin.data, b"invented input")

    def test_exact_deadline_is_timeout(self):
        from test_windows_stream_security import source_windows_method
        class Event:
            def __init__(self):
                self.value = False
            def set(self):
                self.value = True
            def is_set(self):
                return self.value
        class Thread:
            def __init__(self, **kwargs):
                pass
            def start(self):
                pass
            def join(self, **kwargs):
                pass
            def is_alive(self):
                return False
        class Events:
            def __init__(self):
                self.calls = 0
            def get(self, **kwargs):
                self.calls += 1
                return ("stdout", None) if self.calls == 1 else ("stderr", None)
        ticks = iter([0, 1, 1, 1, 1])
        fake_time = SimpleNamespace(monotonic=lambda: next(ticks))
        fake_queue = SimpleNamespace(Queue=Events, Empty=queue.Empty)
        fake_threading = SimpleNamespace(Event=Event, Thread=Thread)
        proc = SimpleNamespace(pid=2345, stdin=None, stdout=object(),
                               stderr=object(), poll=lambda: 0)
        subject = SimpleNamespace(terminate_process_tree=mock.Mock())
        result = source_windows_method("read_streams_with_caps", {
            "time": fake_time, "queue": fake_queue,
            "threading": fake_threading})(subject, proc, "", 1, 1024, 1024, 1)
        self.assertTrue(result.timed_out)
        subject.terminate_process_tree.assert_called_once_with(2345, 0.0)


if __name__ == "__main__":
    unittest.main()
