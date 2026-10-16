"""Offline restart-worker tests. launchctl is always faked."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
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

    def test_waits_for_idle_then_restarts_with_fake_launchctl(self):
        with tempfile.TemporaryDirectory() as root:
            patches = self._patches(root)
            now = [0.0]
            def sleep(seconds): now[0] += seconds
            with patches[0], patches[1], patches[2], \
                 mock.patch.object(restart_worker, "_running_jobs", side_effect=[1, 0]), \
                 mock.patch.object(restart_worker.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as launchctl:
                result = restart_worker.restart(home=root, wait=2, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(result, "restart_worker_restarted")
        launchctl.assert_called_once_with(
            ["launchctl", "kickstart", "gui/501/com.agent-bridge.execution-worker"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20, check=False, shell=False)

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
