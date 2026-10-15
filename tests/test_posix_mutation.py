"""Offline behavioral checks for the POSIX platform boundary."""
from __future__ import annotations

import os
import fcntl
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge.platform.posix import PosixPlatform  # noqa: E402


class PosixMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = PosixPlatform()

    def test_set_owner_only_umask(self):
        with mock.patch("agent_bridge.platform.posix.os.umask") as umask:
            self.platform.set_owner_only_umask()
        umask.assert_called_once_with(0o077)

    def test_enforce_owner_only_file(self):
        with mock.patch("agent_bridge.platform.posix.os.fchmod") as fchmod:
            self.platform.enforce_owner_only_file(17)
        fchmod.assert_called_once_with(17, 0o600)

    def test_lock_exclusive_retries_and_unlocks(self):
        calls = []

        def flock(_fd, flags):
            calls.append(flags)
            if len(calls) == 1:
                raise OSError("busy")

        with mock.patch("agent_bridge.platform.posix.fcntl.flock", side_effect=flock), \
             mock.patch("agent_bridge.platform.posix.time.sleep") as sleep, \
             mock.patch("agent_bridge.platform.posix.time.monotonic", side_effect=[0, 0]):
            with self.platform.lock_exclusive(17, "synthetic.lock", 1):
                self.assertEqual(calls, [fcntl.LOCK_EX | fcntl.LOCK_NB] * 2)
        self.assertEqual(calls[-1], fcntl.LOCK_UN)
        sleep.assert_called_once_with(0.02)

    def test_lock_exclusive_times_out_at_deadline(self):
        with mock.patch("agent_bridge.platform.posix.fcntl.flock", side_effect=OSError("busy")), \
             mock.patch("agent_bridge.platform.posix.time.monotonic", side_effect=[0, 1, 1]), \
             mock.patch("agent_bridge.platform.posix.time.sleep", side_effect=AssertionError("late retry")):
            with self.assertRaisesRegex(TimeoutError, "synthetic.lock"):
                with self.platform.lock_exclusive(17, "synthetic.lock", 1):
                    pass

    def test_verify_owner_only_path_requires_both_modes(self):
        for directory_mode, file_mode, expected in (
            (0o700, 0o600, True), (0o700, 0o644, False),
            (0o755, 0o600, False),
        ):
            with self.subTest(directory_mode=directory_mode, file_mode=file_mode):
                with mock.patch("agent_bridge.platform.posix.os.stat", side_effect=[
                    SimpleNamespace(st_mode=directory_mode),
                    SimpleNamespace(st_mode=file_mode),
                ]):
                    verified, report = self.platform.verify_owner_only_path(
                        "synthetic-directory", "synthetic-file")
                self.assertIs(verified, expected)
                self.assertEqual(report["directory_mode"], oct(directory_mode))
                self.assertEqual(report["file_mode"], oct(file_mode))

    def test_spawn_isolated_passes_isolation_options(self):
        sentinel = object()
        with mock.patch("agent_bridge.platform.posix.subprocess.Popen", return_value=sentinel) as popen:
            actual = self.platform.spawn_isolated(["synthetic-peer"], cwd="synthetic-cwd",
                                                  env={"SYNTHETIC": "1"})
        self.assertIs(actual, sentinel)
        self.assertEqual(popen.call_args.args, (["synthetic-peer"],))
        self.assertEqual(popen.call_args.kwargs, {
            "cwd": "synthetic-cwd", "env": {"SYNTHETIC": "1"},
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE, "start_new_session": True,
            "shell": False, "close_fds": True,
        })

    def test_isolated_process_group_uses_pid_and_handles_missing_process(self):
        with mock.patch("agent_bridge.platform.posix.os.getpgid", return_value=42) as getpgid:
            self.assertEqual(self.platform.isolated_process_group(17), 42)
        getpgid.assert_called_once_with(17)
        with mock.patch("agent_bridge.platform.posix.os.getpgid", side_effect=ProcessLookupError):
            self.assertIsNone(self.platform.isolated_process_group(17))

    def test_terminate_process_tree_signals_and_reports(self):
        calls = []

        def killpg(group_id, sig):
            calls.append((group_id, sig))
            if sig == 0:
                raise ProcessLookupError

        ticks = iter((0, 0, 2, 2))
        with mock.patch("agent_bridge.platform.posix.os.killpg", side_effect=killpg), \
             mock.patch("agent_bridge.platform.posix.time.monotonic", side_effect=lambda: next(ticks, 2)), \
             mock.patch("agent_bridge.platform.posix.time.sleep"):
            report = self.platform.terminate_process_tree(42, 0)
        self.assertEqual(calls, [(42, signal.SIGTERM), (42, signal.SIGKILL), (42, 0)])
        self.assertEqual(report, {"pgid": 42, "sigterm": True, "sigkill": True,
                                  "group_gone_after_sigkill": True})

    def test_process_alive_distinguishes_probe_results(self):
        for outcome, expected in ((None, True), (ProcessLookupError(), False),
                                  (PermissionError(), True), (OSError(), False)):
            with self.subTest(outcome=type(outcome).__name__):
                with mock.patch("agent_bridge.platform.posix.os.kill", side_effect=outcome) as kill:
                    self.assertIs(self.platform.process_alive("17"), expected)
                kill.assert_called_once_with(17, 0)

    def test_process_tree_alive_distinguishes_probe_results(self):
        for outcome, expected in ((None, True), (ProcessLookupError(), False)):
            with self.subTest(outcome=type(outcome).__name__):
                with mock.patch("agent_bridge.platform.posix.os.killpg", side_effect=outcome) as killpg:
                    self.assertIs(self.platform.process_tree_alive(42), expected)
                killpg.assert_called_once_with(42, 0)

    def test_process_group_members_filters_group_and_self(self):
        output = f"17 42\n18 43\n{os.getpid()} 42\nbad 42\n19\n".encode()
        with mock.patch("agent_bridge.platform.posix.subprocess.run",
                        return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(self.platform.process_group_members(42), ["17"])
        with mock.patch("agent_bridge.platform.posix.subprocess.run", side_effect=OSError):
            self.assertIsNone(self.platform.process_group_members(42))

    def test_process_liveness_reports_probe_evidence(self):
        cases = ((None, True, None), (ProcessLookupError(), False, "no such process"),
                 (PermissionError(), True, "permission denied, so it exists"))
        for outcome, alive, reason in cases:
            with self.subTest(outcome=type(outcome).__name__):
                with mock.patch("agent_bridge.platform.posix.os.kill", side_effect=outcome):
                    evidence = self.platform.process_liveness(17)
                self.assertIs(evidence["alive"], alive)
                if reason is not None:
                    self.assertEqual(evidence["reason"], reason)

    def test_process_identity_decodes_ps_or_returns_empty(self):
        with mock.patch("agent_bridge.platform.posix.subprocess.run",
                        return_value=SimpleNamespace(stdout=b" Tue Oct 13 10:11:12 2026 \n")):
            self.assertEqual(self.platform.process_identity(17), "Tue Oct 13 10:11:12 2026")
        with mock.patch("agent_bridge.platform.posix.subprocess.run", side_effect=OSError):
            self.assertEqual(self.platform.process_identity(17), "")

    def test_process_group_identity_parses_ps_or_returns_empty(self):
        for output, expected in ((b"", ("", "")),
                                 (b"Tue Oct 13 10:11:12 2026 42\n",
                                  ("Tue Oct 13 10:11:12 2026", "42")),
                                 (b"unparsed", ("unparsed", ""))):
            with self.subTest(output=output):
                with mock.patch("agent_bridge.platform.posix.subprocess.run",
                                return_value=SimpleNamespace(stdout=output)):
                    self.assertEqual(self.platform.process_group_identity(17), expected)

    def test_is_own_process_group_compares_current_group(self):
        with mock.patch("agent_bridge.platform.posix.os.getpgrp", return_value=42):
            self.assertTrue(self.platform.is_own_process_group(42))
            self.assertFalse(self.platform.is_own_process_group(43))

    def test_read_streams_with_caps_writes_stdin_and_reads_both_streams(self):
        with tempfile.TemporaryDirectory(prefix="posix-stream-") as cwd:
            proc = subprocess.Popen([sys.executable, "-c",
                "import sys; data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(data); sys.stderr.buffer.write(b'err')"],
                cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
            result = self.platform.read_streams_with_caps(proc, "hello", 2, 8, 8, 0.2)
            proc.wait(timeout=2)
        self.assertEqual(result.stdout, b"hello")
        self.assertEqual(result.stderr, b"err")
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cap_exceeded)

    def test_read_streams_with_caps_enforces_exact_and_zero_caps(self):
        for cap, expected in ((0, b""), (5, b"abcde")):
            with self.subTest(cap=cap):
                proc = subprocess.Popen([sys.executable, "-c",
                    "import sys,time; sys.stdout.buffer.write(b'abc'); "
                    "sys.stdout.buffer.flush(); time.sleep(.08); "
                    "sys.stdout.buffer.write(b'defgh'); sys.stdout.buffer.flush()"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True)
                try:
                    result = self.platform.read_streams_with_caps(proc, "", 2, cap, 8, 0.2)
                    self.assertEqual(result.stdout, expected)
                    self.assertTrue(result.cap_exceeded)
                finally:
                    proc.wait(timeout=2)

    def test_read_streams_with_caps_reports_timeout(self):
        proc = SimpleNamespace(stdin=None, stdout=None, stderr=None, poll=lambda: None)
        result = self.platform.read_streams_with_caps(proc, "", 0.01, 8, 8, 0.01)
        self.assertTrue(result.timed_out)

    def test_read_streams_with_caps_stops_at_exact_deadline(self):
        proc = SimpleNamespace(stdin=None, stdout=None, stderr=None, poll=lambda: None)
        selector = mock.Mock()
        selector.get_map.return_value = {}
        selector.select.return_value = []
        with mock.patch("agent_bridge.platform.posix.selectors.DefaultSelector",
                        return_value=selector), \
             mock.patch("agent_bridge.platform.posix.time.monotonic",
                        side_effect=[0, 0, 1, 1]):
            result = self.platform.read_streams_with_caps(proc, "", 0, 8, 8, 0.01)
        self.assertTrue(result.timed_out)
        selector.select.assert_not_called()

    def test_read_streams_with_caps_reports_held_pipe(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb")
        proc = SimpleNamespace(stdin=None, stdout=reader, stderr=None, poll=lambda: 0)
        try:
            result = self.platform.read_streams_with_caps(proc, "", 0.3, 8, 8, 0.01)
            self.assertTrue(result.descendant_held_pipes)
            self.assertFalse(result.timed_out)
        finally:
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
