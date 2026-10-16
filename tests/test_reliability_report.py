"""Offline fixtures for the content-free reliability report."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge import reliability_report  # noqa: E402


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

    def test_execution_receipt_diagnostics_are_fixed_codes_only(self):
        receipts = (
            {"harness": {"error_detail": "source repository integrity changed: head_changed,status_changed,RELIABILITY_REPORT_SECRET_SENTINEL"}},
            {"harness": {"error_detail": "command timed out after 30s"}},
            {"error": "interrupted_requires_reconciliation"},
        )
        for number, receipt in enumerate(receipts):
            directory = self.execution / str(number); directory.mkdir()
            receipt.update(submitted_at=NOW - 1, provider="claude", state="failed")
            (directory / "receipt.json").write_text(json.dumps(receipt))
        report = self._report()
        self.assertEqual(report["execution"]["by_error_detail"], {
            "command_timeout": 1,
            "interrupted_requires_reconciliation": 1,
            "source_repository_changed": 1,
        })
        self.assertEqual(report["execution"]["by_source_integrity_reasons"], {
            "head_changed": 1, "status_changed": 1,
        })
        rendered = reliability_report._markdown(report) + json.dumps(report)
        self.assertNotIn("RELIABILITY_REPORT_SECRET_SENTINEL", rendered)
        self.assertNotIn("command timed out after 30s", rendered)

    def test_ok_is_not_an_error_category(self):
        for number, category in enumerate(("ok", "peer_timeout")):
            directory = self.broker / "jobs" / str(number); directory.mkdir()
            (directory / "status.json").write_text(json.dumps({
                "updated_at": NOW - 1, "caller": "claude", "peer": "codex",
                "status": "complete", "error_category": category,
            }))
        self.assertEqual(self._report()["consultations"]["by_error_category"], {"peer_timeout": 1})

    def test_ci_uses_fake_gh_with_bounded_argv_calls(self):
        fake = self.root / "gh"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\na=sys.argv\nif 'repo' in a: print(json.dumps({'nameWithOwner':'example/bridge'}))\nelif 'list' in a: print(json.dumps([{'databaseId': 7}]))\nelif '--json' in a: print(json.dumps({'jobs':[{'name':'offline','conclusion':'failure'}]}))\nelse: print('FAILED tests/test_report.py::test_failure')\n")
        fake.chmod(0o700)
        with mock.patch.object(reliability_report.shutil, "which", return_value=str(fake)):
            report = self._report(ci=True)
        self.assertEqual(report["ci"]["failure_rate_by_job_name"]["offline"]["rate"], 1.0)
        self.assertEqual(report["ci"]["frequent_failing_test_ids"], {"tests/test_report.py::test_failure": 1})

    def test_ci_does_not_fetch_logs_for_a_green_run(self):
        calls = []

        def fake_gh(argv, *, cwd):
            calls.append(argv)
            if "repo" in argv:
                return 0, json.dumps({"nameWithOwner": "example/bridge"}), True
            if "list" in argv:
                return 0, json.dumps([{ "databaseId": 7 }]), True
            return 0, json.dumps({"jobs": [{"name": "offline", "conclusion": "success"}]}), True

        with mock.patch.object(reliability_report.shutil, "which", return_value="gh"), \
             mock.patch.object(reliability_report, "_gh", side_effect=fake_gh):
            report = self._report(ci=True)
        self.assertEqual(report["ci"]["failure_rate_by_job_name"]["offline"]["rate"], 0.0)
        self.assertFalse(any("--log-failed" in call for call in calls))

    def test_ci_notes_missing_gh(self):
        with mock.patch.object(reliability_report.shutil, "which", return_value=None):
            self.assertEqual(self._report(ci=True)["ci"]["note"], "gh_unavailable")


if __name__ == "__main__":
    unittest.main()
