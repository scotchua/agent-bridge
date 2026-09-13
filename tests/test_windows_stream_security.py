"""Regression coverage for bounded Windows pipe-drain cleanup.

The synthetic cases execute the exact source method through AST extraction so
they can run on non-Windows hosts.  The native case runs only where the Windows
Job Object implementation is available.
"""
from __future__ import annotations

import ast
import contextlib
import ctypes
from ctypes import wintypes
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge.platform import base


def source_windows_method(name: str, scope: dict[str, Any]):
    """Load one source method without importing Windows-only bindings."""
    source = ast.parse((ROOT / "src/agent_bridge/platform/windows.py").read_text())
    window_class = next(
        node for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "WindowsPlatform"
    )
    method = next(
        node for node in window_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    method_scope = {
        "Any": Any,
        "base": base,
        "contextlib": contextlib,
        "queue": queue,
        "subprocess": subprocess,
        "threading": threading,
        "time": time,
        "STREAM_THREAD_JOIN_SECONDS": 0.05,
        "PROCESS_CLEANUP_WAIT_SECONDS": 0.05,
    }
    method_scope.update(scope)
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 "windows_source_method", "exec"), method_scope)
    return method_scope[name]


def source_method():
    return source_windows_method("read_streams_with_caps", {})


class ObservedPipe:
    """A real buffered pipe whose close calls can be observed safely."""

    def __init__(self) -> None:
        read_fd, self.write_fd = os.pipe()
        self.stream = os.fdopen(read_fd, "rb")
        self.close_calls = 0
        self.read_returned = threading.Event()

    def read(self, size: int) -> bytes:
        try:
            return self.stream.read(size)
        finally:
            self.read_returned.set()

    def close(self) -> None:
        self.close_calls += 1
        self.stream.close()

    def release(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.write_fd)


class ExitedLeader:
    pid = 43210
    stdin = None

    def __init__(self, stdout: Any) -> None:
        self.stdout = stdout
        self.stderr = io.BytesIO()

    def poll(self) -> int:
        return 0


class RunningLeader(ExitedLeader):
    def __init__(self, stdout: Any) -> None:
        super().__init__(stdout)
        self.running = True
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else 1

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def wait(self, timeout: float) -> int:
        del timeout
        return 1


class RecordingPlatform:
    def __init__(self, release: ObservedPipe | None = None,
                 fail_termination: bool = False) -> None:
        self.release = release
        self.fail_termination = fail_termination
        self.terminations: list[int] = []

    def terminate_process_tree(self, pid: int, grace: float) -> None:
        del grace
        self.terminations.append(pid)
        if self.fail_termination:
            raise OSError("synthetic Job Object failure")
        if self.release is not None:
            self.release.release()


class FakeKernel:
    def __init__(self, wait_result: int, exit_code: int) -> None:
        self.wait_result = wait_result
        self.exit_code = exit_code
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        del access, inherit, pid
        return 123

    def WaitForSingleObject(self, handle: int, milliseconds: int) -> int:
        self.wait_handle = handle
        self.wait_milliseconds = milliseconds
        return self.wait_result

    def GetExitCodeProcess(self, handle: int, code: Any) -> bool:
        del handle
        code._obj.value = self.exit_code
        return True

    def CloseHandle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True


class CtypesShim:
    byref = staticmethod(ctypes.byref)

    @staticmethod
    def set_last_error(value: int) -> None:
        del value

    @staticmethod
    def get_last_error() -> int:
        return 0


class WindowsDrainSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.method = source_method()

    def test_exited_leader_pipe_holder_is_terminated_without_blocking_close(self) -> None:
        pipe = ObservedPipe()
        platform = RecordingPlatform()
        started = time.monotonic()
        try:
            result = self.method(platform, ExitedLeader(pipe), "", 0.08,
                                 1024, 1024, 0.01)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.25)
            self.assertEqual(platform.terminations, [ExitedLeader.pid])
            self.assertTrue(result.descendant_held_pipes)
            self.assertEqual(pipe.close_calls, 0)
        finally:
            pipe.release()
        self.assertTrue(pipe.read_returned.wait(1))

    def test_failed_tree_termination_leaves_blocked_reader_daemon_and_returns(self) -> None:
        pipe = ObservedPipe()
        platform = RecordingPlatform(fail_termination=True)
        started = time.monotonic()
        try:
            result = self.method(platform, ExitedLeader(pipe), "", 0.08,
                                 1024, 1024, 0.01)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.25)
            self.assertEqual(platform.terminations, [ExitedLeader.pid])
            self.assertTrue(result.descendant_held_pipes)
            self.assertEqual(pipe.close_calls, 0)
        finally:
            pipe.release()
        self.assertTrue(pipe.read_returned.wait(1))

    def test_healthy_eof_does_not_terminate_a_tree_or_change_flags(self) -> None:
        platform = RecordingPlatform()
        proc = ExitedLeader(io.BytesIO(b"complete"))
        proc.stderr = io.BytesIO(b"diagnostic")
        result = self.method(platform, proc, "", 1, 1024, 1024, 0.01)
        self.assertEqual(result.stdout, b"complete")
        self.assertEqual(result.stderr, b"diagnostic")
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cap_exceeded)
        self.assertFalse(result.descendant_held_pipes)
        self.assertEqual(platform.terminations, [])

    def test_cap_breach_terminates_exited_leader_tree_and_preserves_cap_flag(self) -> None:
        platform = RecordingPlatform()
        proc = ExitedLeader(io.BytesIO(b"more-than-eight-bytes"))
        result = self.method(platform, proc, "", 1, 8, 1024, 0.01)
        self.assertEqual(result.stdout, b"more-tha")
        self.assertTrue(result.cap_exceeded)
        self.assertFalse(result.timed_out)
        self.assertEqual(platform.terminations, [ExitedLeader.pid])

    def test_timeout_with_running_leader_still_terminates_and_reaps(self) -> None:
        pipe = ObservedPipe()
        platform = RecordingPlatform(release=pipe)
        proc = RunningLeader(pipe)
        try:
            result = self.method(platform, proc, "", 0.05, 1024, 1024, 1)
        finally:
            pipe.release()
        self.assertTrue(result.timed_out)
        self.assertEqual(platform.terminations, [RunningLeader.pid])
        self.assertTrue(proc.killed)

    def test_exit_code_259_is_not_mistaken_for_a_live_process(self) -> None:
        kernel = FakeKernel(wait_result=0, exit_code=259)
        alive = source_windows_method("process_alive", {
            "kernel32": kernel,
            "SYNCHRONIZE": 0x00100000,
            "PROCESS_QUERY_LIMITED_INFORMATION": 0x1000,
            "WAIT_TIMEOUT": 258,
        })
        liveness = source_windows_method("process_liveness", {
            "ctypes": CtypesShim,
            "wintypes": wintypes,
            "kernel32": kernel,
            "SYNCHRONIZE": 0x00100000,
            "PROCESS_QUERY_LIMITED_INFORMATION": 0x1000,
            "WAIT_TIMEOUT": 258,
            "WAIT_OBJECT_0": 0,
        })
        self.assertFalse(alive(object(), 9012))
        evidence = liveness(object(), 9012)
        self.assertFalse(evidence["alive"])
        self.assertEqual(evidence["exit_code"], 259)
        self.assertEqual(evidence["wait_result"], 0)

    def test_unsignalled_handle_is_alive_regardless_of_exit_code_diagnostic(self) -> None:
        kernel = FakeKernel(wait_result=258, exit_code=1)
        alive = source_windows_method("process_alive", {
            "kernel32": kernel,
            "SYNCHRONIZE": 0x00100000,
            "PROCESS_QUERY_LIMITED_INFORMATION": 0x1000,
            "WAIT_TIMEOUT": 258,
        })
        self.assertTrue(alive(object(), 9012))

    def test_failed_wait_is_reported_as_indeterminate_not_as_exit_code_liveness(self) -> None:
        kernel = FakeKernel(wait_result=0xFFFFFFFF, exit_code=259)
        liveness = source_windows_method("process_liveness", {
            "ctypes": CtypesShim,
            "wintypes": wintypes,
            "kernel32": kernel,
            "SYNCHRONIZE": 0x00100000,
            "PROCESS_QUERY_LIMITED_INFORMATION": 0x1000,
            "WAIT_TIMEOUT": 258,
            "WAIT_OBJECT_0": 0,
            "WAIT_FAILED": 0xFFFFFFFF,
        })
        evidence = liveness(object(), 9012)
        self.assertTrue(evidence["liveness_indeterminate"])
        self.assertEqual(evidence["exit_code"], 259)


@unittest.skipUnless(os.name == "nt", "requires native Windows Job Object APIs")
class NativeWindowsDrainRegression(unittest.TestCase):
    def test_leader_exit_with_descendant_pipe_holder_returns_in_drain_window(self) -> None:
        from agent_bridge.platform.windows import WindowsPlatform

        platform = WindowsPlatform()
        script = (
            "import subprocess,sys;"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'], close_fds=False);"
            "print('leader-exited', flush=True)"
        )
        proc = platform.spawn_isolated(
            [sys.executable, "-c", script], cwd=tempfile.gettempdir(),
            env=dict(os.environ),
        )
        started = time.monotonic()
        try:
            result = platform.read_streams_with_caps(
                proc, "", 2, 1024, 1024, 0.1)
        finally:
            with contextlib.suppress(Exception):
                platform.terminate_process_tree(proc.pid, 0)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(result.descendant_held_pipes)
        self.assertIn(b"leader-exited", result.stdout)

    def test_exited_process_with_259_exit_status_is_not_alive(self) -> None:
        from agent_bridge.platform.windows import WindowsPlatform

        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"])
        proc.wait(timeout=5)
        # Retain Popen and its process handle so the PID cannot be reused while
        # the platform probes the exited process.
        platform = WindowsPlatform()
        self.assertFalse(platform.process_alive(proc.pid))
        evidence = platform.process_liveness(proc.pid)
        self.assertFalse(evidence["alive"])
        self.assertEqual(evidence.get("exit_code"), 259)


if __name__ == "__main__":
    unittest.main()
