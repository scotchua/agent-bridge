"""Offline fixtures for the content-free reliability report."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_bridge import reliability_report


FIXTURES = Path(__file__).parent / "fixtures" / "reliability_report"
NOW = 1_700_000_100.0


class ReliabilityReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.broker = self.root / "broker"; (self.broker / "jobs").mkdir(parents=True)
        self.execution = self.root / "execution"; self.execution.mkdir()
        self.local = self.root / "local"; self.local.mkdir()
        self.capacity = self.root / "capacity.sqlite3"
        self.cfg = SimpleNamespace(execution_queue_root=self.execution,
                                   local_queue_root=self.local, capacity_db=self.capacity)

    def tearDown(self):
        self.temp.cleanup()

    def _report(self, *, ci=False):
        with mock.patch.object(reliability_report.broker_config, "load",
                               return_value=SimpleNamespace(state_root=str(self.broker))), \
             mock.patch.object(reliability_report.drift, "discover_config", return_value="/private/config"), \
             mock.patch.object(reliability_report.orchestration_config, "load", return_value=self.cfg):
            return reliability_report.build_report(7, ci=ci, now=NOW)

    def test_sections_use_fixture_records_and_never_expose_content(self):
        job = self.execution / "one"; job.mkdir()
        (job / "receipt.json").write_bytes((FIXTURES / "execution_receipt.json").read_bytes())
        (self.execution / "unreadable").mkdir(); (self.execution / "unreadable" / "receipt.json").write_text("{")
        consultation = self.broker / "jobs" / "one"; consultation.mkdir()
        (consultation / "status.json").write_bytes((FIXTURES / "consultation_status.json").read_bytes())
        (self.broker / "jobs" / "bad").mkdir(); (self.broker / "jobs" / "bad" / "status.json").write_text("{")
        db = sqlite3.connect(self.local / "localq.sqlite3")
        db.execute("CREATE TABLE jobs (status TEXT, error TEXT, submitted_at REAL)")
        db.execute("INSERT INTO jobs VALUES ('failed', 'executor_lease_lost', ?)", (NOW - 5,)); db.commit(); db.close()
        db = sqlite3.connect(self.capacity)
        db.execute("CREATE TABLE stages (state TEXT, blocked_reason TEXT, updated_at REAL)")
        db.execute("INSERT INTO stages VALUES ('blocked', 'lease_expired', ?)", (NOW - 90000,)); db.commit(); db.close()
        report = self._report()
        self.assertEqual(report["execution"]["by_provider"], {"claude": 1})
        self.assertEqual(report["execution"]["by_state"]["unreadable"], 1)
        self.assertEqual(report["consultations"]["by_direction"], {"codex->claude": 1})
        self.assertEqual(report["consultations"]["by_error_category"], {"peer_timeout": 1})
        self.assertEqual(report["local_queue"]["by_failure_code"], {"executor_lease_lost": 1})
        self.assertEqual(report["stage_health"]["blocked_over_24_hours"], 1)
        rendered = reliability_report._markdown(report) + json.dumps(report)
        self.assertNotIn("RELIABILITY_REPORT_SECRET_SENTINEL", rendered)

    def test_empty_state_is_reported_without_crashing(self):
        report = self._report()
        self.assertEqual(report["execution"]["by_state"], {})
        self.assertEqual(report["consultations"]["by_outcome"], {})
        self.assertEqual(report["local_queue"]["by_state"], {})

    def test_ci_uses_fake_gh_with_bounded_argv_calls(self):
        fake = self.root / "gh"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\na=sys.argv\nif 'list' in a: print(json.dumps([{'databaseId': 7}]))\nelif '--json' in a: print(json.dumps({'jobs':[{'name':'offline','conclusion':'failure'}]}))\nelse: print('FAILED tests/test_report.py::test_failure')\n")
        fake.chmod(0o700)
        with mock.patch.object(reliability_report.shutil, "which", return_value=str(fake)):
            report = self._report(ci=True)
        self.assertEqual(report["ci"]["failure_rate_by_job_name"]["offline"]["rate"], 1.0)
        self.assertEqual(report["ci"]["frequent_failing_test_ids"], {"tests/test_report.py::test_failure": 1})

    def test_ci_notes_missing_gh(self):
        with mock.patch.object(reliability_report.shutil, "which", return_value=None):
            self.assertEqual(self._report(ci=True)["ci"]["note"], "gh_unavailable")


if __name__ == "__main__":
    unittest.main()
