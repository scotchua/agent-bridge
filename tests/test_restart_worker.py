"""Offline restart-worker tests. launchctl is always faked."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
import contextlib
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge import restart_worker


class RestartWorkerTests(unittest.TestCase):
    def _patches(self, root: str):
        return (
            mock.patch.object(restart_worker.drift, "discover_config", return_value="/private/config.json"),
            mock.patch.object(restart_worker, "load", return_value=SimpleNamespace(execution_queue_root=Path(root))),
            mock.patch.object(restart_worker.delegation, "launch_agent_status",
                              return_value={"status": "active", "target": "gui/501/com.agent-bridge.execution-worker"}),
        )

    def test_refuses_without_signalling_when_a_job_is_running(self):
        with tempfile.TemporaryDirectory() as root:
            patches = self._patches(root)
            with patches[0], patches[1], patches[2], \
                 mock.patch.object(restart_worker, "_running_jobs", return_value=1), \
                 mock.patch.object(restart_worker.subprocess, "run") as launchctl:
                result = restart_worker.restart(home=root)
        self.assertEqual(result, "restart_worker_refused:execution_running")
        launchctl.assert_not_called()

    def test_lock_contention_refuses_promptly_without_queue_error(self):
        with tempfile.TemporaryDirectory() as root:
            patches = self._patches(root)
            now = [0.0]
            with patches[0], patches[1], patches[2], \
                 mock.patch.object(restart_worker.store, "file_lock", side_effect=TimeoutError):
                result = restart_worker.restart(home=root, clock=lambda: now[0])
        self.assertEqual(result, "restart_worker_refused:execution_running")

    def test_wait_retries_lock_then_confirms_new_worker(self):
        with tempfile.TemporaryDirectory() as root:
            patches = self._patches(root)
            now = [0.0]

            def sleep(seconds):
                now[0] += seconds

            old = {"pid": 10, "started_at": "2026-01-01T00:00:00+00:00"}
            new = {"pid": 11, "started_at": "2026-01-01T00:00:01+00:00"}
            with patches[0], patches[1], patches[2], \
                 mock.patch.object(restart_worker.store, "file_lock",
                                   side_effect=[TimeoutError(), contextlib.nullcontext()]), \
                 mock.patch.object(restart_worker, "_running_jobs", return_value=0), \
                 mock.patch.object(restart_worker, "_worker_status", side_effect=[old, new]), \
                 mock.patch.object(restart_worker.subprocess, "run") as launchctl:
                result = restart_worker.restart(home=root, wait=2, sleep=sleep,
                                                clock=lambda: now[0])
        self.assertEqual(result, "restart_worker_restarted")
        launchctl.assert_not_called()

    def test_request_is_not_reported_as_a_restart_without_new_marker(self):
        with tempfile.TemporaryDirectory() as root:
            patches = self._patches(root)
            now = [0.0]

            def sleep(seconds):
                now[0] += seconds

            old = {"pid": 10, "started_at": "2026-01-01T00:00:00+00:00"}
            with patches[0], patches[1], patches[2], \
                 mock.patch.object(restart_worker, "_running_jobs", return_value=0), \
                 mock.patch.object(restart_worker, "_worker_status", return_value=old), \
                 mock.patch.object(restart_worker.subprocess, "run") as launchctl:
                result = restart_worker.restart(home=root, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(result, "restart_worker_requested")
        launchctl.assert_not_called()

    def test_not_installed_is_a_clear_noop(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(restart_worker.drift, "discover_config", return_value="/private/config.json"), \
                 mock.patch.object(restart_worker, "load", return_value=SimpleNamespace(execution_queue_root=Path(root))), \
                 mock.patch.object(restart_worker.delegation, "launch_agent_status", return_value={"status": "inactive"}), \
                 mock.patch.object(restart_worker.subprocess, "run") as launchctl:
                result = restart_worker.restart(home=root)
        self.assertEqual(result, "restart_worker_not_installed")
        launchctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
