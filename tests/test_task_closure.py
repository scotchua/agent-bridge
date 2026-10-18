"""Regression tests for conservative task closure recognition."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_bridge.orchestration.task_closure import is_task_closed, task_record_state


class TaskClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "tasks"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def record(self, item, value):
        directory = self.root / item
        directory.mkdir()
        (directory / "task.json").write_text(json.dumps(value), encoding="utf-8")

    def test_closed_spellings_and_only_whole_values_count(self):
        for number, value in enumerate(("closed", "complete", "completed", "done", "CLOSED")):
            item = f"closed-{number}"
            self.record(item, {"status": value})
            self.assertTrue(is_task_closed(self.root, item))
        self.record("state", {"state": "Done"})
        self.assertTrue(is_task_closed(self.root, "state"))
        self.record("prose", {"status": "complete 2026-10-03: verified"})
        self.assertFalse(is_task_closed(self.root, "prose"))

    def test_missing_bad_and_unsafe_records_are_not_closed(self):
        self.assertEqual(task_record_state(self.root, "missing"), "no_record")
        for item in ("../outside", "nested/item", "nested\\item", ".hidden", "has..dots"):
            self.assertFalse(is_task_closed(self.root, item))
        bad = self.root / "bad"; bad.mkdir()
        (bad / "task.json").write_text("{", encoding="utf-8")
        self.assertEqual(task_record_state(self.root, "bad"), "not_closed")
        self.assertFalse(is_task_closed(self.root, "bad"))

    def test_symlink_escaping_task_root_is_not_closed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "task.json").write_text('{"status":"closed"}', encoding="utf-8")
        try:
            os.symlink(outside, self.root / "escaped", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        self.assertFalse(is_task_closed(self.root, "escaped"))


if __name__ == "__main__":
    unittest.main()
