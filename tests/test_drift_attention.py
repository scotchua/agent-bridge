"""Drift attention: one desktop alert per change, a state file that marks
real changes, and no alert storm when many processes check at once."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge import drift, store

DRIFTED = {"ok": False, "changed": ["ollama"], "attention": []}
OTHER = {"ok": False, "changed": ["gemma_delegate", "ollama"], "attention": []}
CLEAR = {"ok": True, "changed": [], "attention": []}


class AttentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(drift.subprocess, "run")
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def alerts(self) -> int:
        return sum("osascript" in call.args[0] for call in self.run_mock.call_args_list)

    def state(self) -> dict:
        return store.read_json(drift.paths(self.home)["attention"])

    def test_the_same_drift_alerts_once(self) -> None:
        for _ in range(5):
            drift.report_attention(self.home, dict(DRIFTED))
        self.assertEqual(self.alerts(), 1)

    def test_state_is_rewritten_only_when_the_signature_changes(self) -> None:
        drift.report_attention(self.home, {**DRIFTED, "checked_at": 1.0})
        first = self.state()
        drift.report_attention(self.home, {**DRIFTED, "checked_at": 2.0})
        self.assertEqual(self.state(), first)
        drift.report_attention(self.home, dict(OTHER))
        self.assertEqual(self.state()["changed"], OTHER["changed"])

    def test_a_flickering_dependency_does_not_alert_on_every_flip(self) -> None:
        for report in (DRIFTED, OTHER, DRIFTED, OTHER, DRIFTED):
            drift.report_attention(self.home, dict(report))
        self.assertEqual(self.alerts(), 2)

    def test_a_clear_report_records_state_without_alerting(self) -> None:
        drift.report_attention(self.home, dict(CLEAR))
        self.assertEqual(self.alerts(), 0)
        self.assertTrue(self.state()["ok"])

    def test_alert_memory_survives_a_brief_recovery(self) -> None:
        for report in (DRIFTED, CLEAR, DRIFTED, CLEAR, DRIFTED):
            drift.report_attention(self.home, dict(report))
        self.assertEqual(self.alerts(), 1)
        self.assertFalse(self.state()["ok"])

    def test_the_same_drift_alerts_again_after_the_quiet_window(self) -> None:
        drift.report_attention(self.home, dict(DRIFTED))
        later = drift.time.time() + drift.ALERT_REPEAT_SECONDS + 1
        with mock.patch.object(drift.time, "time", return_value=later):
            drift.report_attention(self.home, dict(DRIFTED))
        self.assertEqual(self.alerts(), 2)

    def test_needs_you_lines_are_part_of_the_signature(self) -> None:
        drift.report_attention(self.home, {"ok": False, "changed": [], "needs_you": ["Sign in."]})
        drift.report_attention(self.home, {"ok": False, "changed": [], "needs_you": ["Sign in."]})
        drift.report_attention(self.home, {"ok": False, "changed": [], "needs_you": ["Update."]})
        self.assertEqual(self.alerts(), 2)

    def test_a_corrupt_alert_memory_is_treated_as_empty(self) -> None:
        location = drift.paths(self.home)["alerts"]
        os.makedirs(os.path.dirname(location), exist_ok=True)
        Path(location).write_text("{not json", encoding="utf-8")
        drift.report_attention(self.home, dict(DRIFTED))
        drift.report_attention(self.home, dict(DRIFTED))
        self.assertEqual(self.alerts(), 1)
        self.assertEqual(list(json.loads(Path(location).read_text(encoding="utf-8"))), [self.state()["signature"]])

    def test_a_held_lock_skips_quietly(self) -> None:
        with mock.patch.object(drift.store, "file_lock", side_effect=TimeoutError("busy")):
            drift.report_attention(self.home, dict(DRIFTED))
        self.assertEqual(self.alerts(), 0)

    def test_many_concurrent_callers_alert_once(self) -> None:
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def call() -> None:
            try:
                barrier.wait(timeout=10)
                drift.report_attention(self.home, dict(DRIFTED))
            except BaseException as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(self.alerts(), 1)


if __name__ == "__main__":
    unittest.main()
