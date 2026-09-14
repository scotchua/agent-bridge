"""Tests for the out-of-sandbox execution queue consumer."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.orchestration.execution_worker import WorkerAlreadyRunning, WorkerLock, run


class ExecutionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_lock_is_exclusive_and_secure(self):
        queue = self.root / "queue"
        with WorkerLock(queue):
            self.assertEqual(queue.stat().st_mode & 0o777, 0o700)
            self.assertEqual((queue / ".execution-worker.lock").stat().st_mode & 0o777,
                             0o600)
            with self.assertRaisesRegex(WorkerAlreadyRunning, "already_running"):
                with WorkerLock(queue):
                    pass
        with WorkerLock(queue):
            pass

    def test_once_mode_with_empty_queue_exits_cleanly(self):
        executable = self.root / "python"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        codex = self.root / "codex_task.py"
        claude = self.root / "claude_task.py"
        codex.write_text("# fake\n", encoding="utf-8")
        claude.write_text("# fake\n", encoding="utf-8")
        state = self.root / "state"
        config = self.root / "orchestration.json"
        config.write_text(json.dumps({
            "config_version": "1", "state_root": str(state),
            "local_queue_root": str(state / "local"),
            "capacity_db": str(state / "capacity.sqlite3"),
            "worker_executable": str(executable),
            "worker_state": str(state / "worker-state"),
            "execution_queue_root": str(state / "execution"),
            "codex_task_executable": str(codex),
            "claude_task_executable": str(claude),
            "python_executable": str(executable),
        }), encoding="utf-8")
        self.assertEqual(run(str(config), once=True, interval=0.01,
                             worker_id="test-worker"), 0)
        self.assertEqual((state / "execution").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
