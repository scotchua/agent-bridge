"""Offline fixtures for the content-free reliability report."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
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
SRC = Path(__file__).resolve().parents[1] / "src" / "agent_bridge"
NOW = 1_700_000_100.0


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


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
        self.assertEqual(report["config"], {"broker": "ok", "orchestration": "ok"})
        self.assertEqual(report["execution"]["by_error_detail"], {"verify_not_runnable": 1})
        self.assertEqual(report["execution"]["by_provider"], {"claude": 1})
        self.assertEqual(report["execution"]["by_state"]["unreadable"], 1)
        self.assertEqual(report["consultations"]["by_direction"], {"codex->claude": 1})
        self.assertEqual(report["consultations"]["by_error_category"], {"peer_timeout": 1})
        self.assertEqual(report["local_queue"]["by_failure_code"], {"executor_lease_lost": 1})
        self.assertEqual(report["stage_health"]["blocked_over_24_hours"], 1)
        rendered = reliability_report._markdown(report) + json.dumps(report)
        self.assertNotIn("RELIABILITY_REPORT_SECRET_SENTINEL", rendered)
        self.assertNotIn("pytest is not on PATH", rendered)

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
            {"error": "ExecutionAdmissionError",
             "error_detail": "model_reserved_to_operator:RELIABILITY_REPORT_SECRET_SENTINEL"},
            {"error": "ExecutionAdmissionError", "error_detail": "verify_not_runnable: no such file"},
            {"error": "OSError", "error_detail": "[Errno 13] Permission denied: '/private/x'"},
            {"error": "SomethingNew", "error_detail": "queue_directory_not_owner_only"},
        )
        for number, receipt in enumerate(receipts):
            directory = self.execution / str(number); directory.mkdir()
            receipt.update(submitted_at=NOW - 1, provider="claude", state="failed")
            (directory / "receipt.json").write_text(json.dumps(receipt))
        report = self._report()
        # One entry per job, class names counted apart.
        self.assertEqual(report["execution"]["by_error_detail"], {
            "command_timeout": 1,
            "interrupted_requires_reconciliation": 1,
            "model_reserved_to_operator": 1,
            "queue_directory_not_owner_only": 1,
            "source_repository_changed": 1,
            "unrecognized": 1,
            "verify_not_runnable": 1,
        })
        self.assertEqual(report["execution"]["by_error_class"], {
            "ExecutionAdmissionError": 2, "OSError": 1, "unrecognized": 1,
        })
        # Only the first two carry a harness mapping (with no status); jobs
        # with no harness outcome add nothing to the harness buckets.
        self.assertEqual(report["execution"]["by_harness_status"], {"unrecognized": 2})
        self.assertEqual(report["execution"]["by_harness_verdict"], {"unrecognized": 2})
        self.assertEqual(report["execution"]["by_source_integrity_reasons"], {
            "head_changed": 1, "status_changed": 1,
        })
        rendered = reliability_report._markdown(report) + json.dumps(report)
        self.assertNotIn("RELIABILITY_REPORT_SECRET_SENTINEL", rendered)
        self.assertNotIn("command timed out after 30s", rendered)
        self.assertNotIn("Permission denied", rendered)

    def test_local_failure_codes_cover_spool_vocabulary_and_failed_states_only(self):
        db = sqlite3.connect(self.local / "localq.sqlite3")
        db.execute("CREATE TABLE jobs (status TEXT, error TEXT, submitted_at REAL)")
        for status, error in (
                ("failed", "RuntimeError: child did not return a JSON object"),
                ("failed", "backend_failure"), ("failed", "client_data_health_unverified"),
                ("unknown", "executor_lease_lost"), ("unknown", "execution_timeout"),
                ("cancelled", "cancel_requested"), ("failed", "RELIABILITY_REPORT_SECRET_SENTINEL"),
                ("queued", "deferred:busy"), ("queued", "waiting_on_dependency:codex"),
                ("complete", None)):
            db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (status, error, NOW - 5))
        db.commit(); db.close()
        report = self._report()
        self.assertEqual(report["local_queue"]["by_failure_code"], {
            "RuntimeError": 1, "backend_failure": 1, "cancel_requested": 1,
            "client_data_health_unverified": 1, "execution_timeout": 1,
            "executor_lease_lost": 1, "unrecognized": 1,
        })
        self.assertNotIn("RELIABILITY_REPORT_SECRET_SENTINEL", json.dumps(report))

    def test_stuck_stages_are_counted_however_long_ago_they_stuck(self):
        db = sqlite3.connect(self.capacity)
        db.execute("CREATE TABLE stages (state TEXT, blocked_reason TEXT, updated_at REAL)")
        db.executemany("INSERT INTO stages VALUES (?, ?, ?)", (
            ("blocked", "no_fresh_eligible_route", NOW - 8 * 86400),
            ("reconcile_required", None, NOW - 2 * 86400),
            ("blocked", "no_fresh_eligible_route", NOW - 60),
            ("owned", None, NOW - 9 * 86400),
        ))
        db.commit(); db.close()
        stages = self._report()["stage_health"]
        self.assertEqual(stages["by_blocked_reason"], {
            "liveness_reconciliation_required": 1, "no_fresh_eligible_route": 2})
        self.assertEqual(stages["blocked_over_24_hours"], 2)

    def test_unreadable_config_is_never_reported_as_absent(self):
        with mock.patch.object(reliability_report.broker_config, "load", side_effect=ValueError), \
             mock.patch.object(reliability_report.drift, "discover_config", return_value="/private/config"), \
             mock.patch.object(reliability_report.orchestration_config, "load", side_effect=OSError):
            report = reliability_report.build_report(7, now=NOW)
        self.assertEqual(report["config"], {"broker": "unreadable", "orchestration": "unreadable"})
        for section, key in (("consultations", "by_outcome"), ("execution", "by_state"),
                             ("local_queue", "by_state"), ("stage_health", "by_blocked_reason")):
            with self.subTest(section):
                self.assertTrue(report[section]["present"])
                self.assertEqual(report[section]["config"], "unreadable")
                self.assertEqual(report[section][key], {"unreadable": 1})

    def test_absent_orchestration_config_is_reported_absent(self):
        with mock.patch.object(reliability_report.broker_config, "load",
                               return_value=SimpleNamespace(state_root=str(self.broker))), \
             mock.patch.object(reliability_report.drift, "discover_config", return_value=None):
            report = reliability_report.build_report(7, now=NOW)
        self.assertEqual(report["config"]["orchestration"], "absent")
        self.assertFalse(report["execution"]["present"])
        self.assertFalse(report["stage_health"]["present"])

    def test_ok_is_not_an_error_category(self):
        for number, category in enumerate(("ok", "peer_timeout")):
            directory = self.broker / "jobs" / str(number); directory.mkdir()
            (directory / "status.json").write_text(json.dumps({
                "updated_at": NOW - 1, "caller": "claude", "peer": "codex",
                "status": "complete", "error_category": category,
            }))
        self.assertEqual(self._report()["consultations"]["by_error_category"], {"peer_timeout": 1})

    def _fake_gh(self) -> str:
        """A gh stand-in this OS can execute (Windows needs a .cmd shim)."""
        script = self.root / "gh.py"
        runs = [{"databaseId": 7, "createdAt": iso(NOW - 60)},
                {"databaseId": 8, "createdAt": iso(NOW - 30 * 86400)}]
        script.write_text(
            "#!/usr/bin/env python3\nimport json,sys\na=sys.argv\n"
            "if 'repo' in a: print(json.dumps({'nameWithOwner':'example/bridge'}))\n"
            f"elif 'list' in a: print(json.dumps({runs!r}))\n"
            "elif '--json' in a: print(json.dumps({'jobs':[{'name':'offline','conclusion':'failure'}]}))\n"
            "else: print('2026-10-16T00:00:00.0Z FAILED tests/test_report.py::test_failure\\n'"
            "'2026-10-16T00:00:00.0Z FAIL: test_x (test_mod.Case.test_x)\\n'"
            "'2026-10-16T00:00:00.0Z ERROR: test_y (test_mod.Case.test_y)\\n'"
            "'2026-10-16T00:00:00.0Z FAILED (failures=1, errors=1)')\n")
        script.chmod(0o700)
        if os.name != "nt":
            return str(script)
        shim = self.root / "gh.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(shim)

    def test_ci_uses_fake_gh_with_bounded_argv_calls(self):
        with mock.patch.object(reliability_report.shutil, "which", return_value=self._fake_gh()):
            report = self._report(ci=True)
        # Run 8 is older than the 7-day window and is not counted.
        self.assertEqual(report["ci"]["failure_rate_by_job_name"]["offline"],
                         {"failed": 1, "total": 1, "rate": 1.0})
        self.assertEqual(report["ci"]["frequent_failing_test_ids"], {
            "tests/test_report.py::test_failure": 1,
            "test_mod.Case.test_x": 1, "test_mod.Case.test_y": 1})

    def test_ci_does_not_fetch_logs_for_a_green_run(self):
        calls = []

        def fake_gh(argv, *, cwd):
            calls.append(argv)
            if "repo" in argv:
                return 0, json.dumps({"nameWithOwner": "example/bridge"}), True
            if "list" in argv:
                return 0, json.dumps([{"databaseId": 7, "createdAt": iso(NOW - 60)}]), True
            return 0, json.dumps({"jobs": [{"name": "offline", "conclusion": "success"}]}), True

        with mock.patch.object(reliability_report.shutil, "which", return_value="gh"), \
             mock.patch.object(reliability_report, "_gh", side_effect=fake_gh):
            report = self._report(ci=True)
        self.assertEqual(report["ci"]["failure_rate_by_job_name"]["offline"]["rate"], 0.0)
        self.assertFalse(any("--log-failed" in call for call in calls))

    def test_ci_notes_missing_gh(self):
        with mock.patch.object(reliability_report.shutil, "which", return_value=None):
            self.assertEqual(self._report(ci=True)["ci"]["note"], "gh_unavailable")



class VocabularySyncTests(unittest.TestCase):
    """The report's allowlists track the codes the bridge actually writes."""

    def test_every_execution_admission_code_is_reportable(self):
        privacy = set(re.findall(
            r"PrivacyError\(\s*f?[\"']([a-z_][a-z0-9_]*)",
            (SRC / "orchestration" / "windows_privacy.py").read_text(encoding="utf-8")))
        self.assertEqual(privacy, set(reliability_report._QUEUE_PRIVACY_REASONS))
        raised = set()
        for path in SRC.rglob("*.py"):
            raised.update(re.findall(r"ExecutionAdmissionError\(\s*f?[\"']([a-z_][a-z0-9_]*)",
                                     path.read_text(encoding="utf-8")))
        prefixes = {code for code in raised if code.endswith("_")}
        self.assertEqual(prefixes, {"queue_"})
        self.assertEqual(raised - prefixes - reliability_report._SAFE_EXECUTION_CODES, set())

    def test_every_local_failure_code_is_reportable(self):
        from agent_bridge.localq import spool
        self.assertLessEqual(spool._BACKEND_EXCEPTION_NAMES, reliability_report._SAFE_LOCAL_CODES)
        source = (SRC / "localq" / "spool.py").read_text(encoding="utf-8")
        written = set(re.findall(r'return "(client_data_[a-z_]+)"', source))
        self.assertTrue(written)
        self.assertLessEqual(written, reliability_report._SAFE_LOCAL_CODES)


if __name__ == "__main__":
    unittest.main()
