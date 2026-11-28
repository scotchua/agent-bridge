"""Synthetic SQLite fixtures only; no installed queue or assistant config."""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge import drift, local_status


class LocalStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.queue = self.home / "queue # synthetic"
        self.queue.mkdir()
        self.config = self.home / "orchestration.json"
        self.config.write_text(json.dumps({"config_version": "1",
            "state_root": str(self.home / "state"), "local_queue_root": str(self.queue),
            "capacity_db": str(self.home / "capacity.db"),
            "worker_executable": str(self.home / "worker"), "worker_state": str(self.home / "worker-state")}))
        self.now = dt.datetime(2026, 11, 1, 12).timestamp()
        self.start = dt.datetime(2026, 10, 26).timestamp()
        report_clock = mock.patch.object(local_status.time, "time", return_value=self.now)
        report_clock.start()
        self.addCleanup(report_clock.stop)
        with contextlib.closing(sqlite3.connect(self.queue / "localq.sqlite3")) as db, db:
            db.execute("""CREATE TABLE jobs (submitted_at REAL, caller TEXT, purpose TEXT,
                task_type TEXT, status TEXT, feedback_json TEXT, error TEXT,
                input_blob TEXT, result_blob TEXT, params_json TEXT)""")
        with contextlib.closing(sqlite3.connect(self.queue / "routing.sqlite3")) as db, db:
            db.execute("CREATE TABLE checkpoints (created_at REAL, caller TEXT, status TEXT, task_type TEXT, reason TEXT)")
            db.executemany("INSERT INTO checkpoints (created_at,caller,status) VALUES (?,?,?)", [
                (self.now, "claude", "eligible"), (self.now, "claude", "eligible"),
                (self.now, "codex", "refused"), (self.start - 1, "codex", "eligible")])

    def job(self, status="complete", feedback=None, purpose="work", task="summarize", at=None, error=None):
        with contextlib.closing(sqlite3.connect(self.queue / "localq.sqlite3")) as db, db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)", (
                self.now if at is None else at, "claude", purpose, task, status,
                json.dumps({"outcome": feedback}) if feedback else None, error,
                "SECRET INPUT", "SECRET OUTPUT", "SECRET PROMPT"))

    def test_counts_window_feedback_and_content_exclusion(self):
        for feedback in ("used", "reworked", "discarded", None):
            self.job(feedback=feedback)
        self.job(task="extract")
        self.job(purpose="test")
        for status in ("failed", "queued", "cancelled", "running", "expired", "unknown"):
            self.job(status=status, error="SECRET ERROR")
        self.job(status="queued", error="waiting_on_dependency:ollama SECRET ERROR")
        self.job(at=self.start)
        self.job(at=self.start - 1)
        self.job(at=self.now + 1)
        result = local_status.report(str(self.config), now=self.now)
        self.assertEqual(sum(row["count"] for row in result["jobs"]), 14)
        outcomes = {row["outcome"]: row["count"] for row in result["jobs"] if row["day"] == "2026-11-01" and row["purpose"] == "work"}
        self.assertEqual(outcomes["queued"], 1)
        self.assertEqual(outcomes["held waiting_on_dependency"], 1)
        self.assertEqual(outcomes["complete"], 5)
        summary = result["feedback"][1]
        self.assertEqual([summary[key] for key in ("used", "reworked", "discarded", "unreviewed")], [1, 1, 1, 2])
        self.assertAlmostEqual(summary["rework_rate"], 1 / 3)
        self.assertAlmostEqual(summary["discard_rate"], 1 / 3)
        self.assertIsNone(result["feedback"][0]["rework_rate"])
        self.assertEqual(result["checkpoints"], [
            {"caller": "claude", "status": "eligible", "count": 2},
            {"caller": "codex", "status": "refused", "count": 1}])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_failures_have_closed_codes_grouped_by_type_and_window(self):
        prefix = "RuntimeError: child exited unsuccessfully: "
        for detail in ("extract_document_0_json_invalid", "extract_document_15_json_invalid"):
            self.job(status="failed", task="extract", error=prefix + "GemmaRefusal: " + detail)
        for task, error in (
            ("summarize", prefix + "GemmaRefusal: gemma_delegate_timeout"),
            ("summarize", prefix + "GemmaRefusal: summary_empty_output"),
            ("extract", prefix + "GemmaRefusal: extract_document_2_failed:receipt_invalid"),
            ("extract", prefix + "GemmaRefusal: extract_document_0_failed:gemma_certification_refused_matching_certificate_missing"),
            ("extract", prefix + "GemmaRefusal: gemma_certification_refused_private_source_text"),
            ("extract", "GemmaRefusal: SECRET INPUT"),
            ("summarize", "JSONDecodeError"),
            ("classify", prefix + "GemmaRefusal: classify_line_22_invalid"),
            ("SECRET TYPE", "SECRET ERROR"),
        ):
            self.job(status="failed", task=task, error=error)
        self.job(status="failed", error="SECRET", at=self.start - 1)
        self.job(status="failed", error="SECRET", at=self.now + 1)
        self.job(status="complete", error="SECRET")
        result = local_status.report(str(self.config), now=self.now)
        rows = {(row["task_type"], row["error_code"]): row["count"] for row in result["failed_jobs"]}
        self.assertEqual(rows, {
            ("extract", "extract_json_invalid"): 2,
            ("summarize", "gemma_delegate_timeout"): 1,
            ("summarize", "summary_empty_output"): 1,
            ("extract", "receipt_invalid"): 1,
            ("extract", "gemma_certification_refused"): 2,
            ("extract", "unknown_failure"): 1,
            ("summarize", "JSONDecodeError"): 1,
            ("classify", "classify_line_invalid"): 1,
            ("unknown", "unknown_failure"): 1,
        })
        self.assertNotIn("SECRET", json.dumps(result["failed_jobs"]))
        self.assertNotIn("private_source_text", json.dumps(result))
        for flags in ([], ["--work-split"], ["--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(local_status.main(["--config", str(self.config), *flags]), 0)
            self.assertNotIn("SECRET ERROR", out.getvalue())
            if flags == ["--json"]:
                self.assertEqual(json.loads(out.getvalue())["failed_jobs"], result["failed_jobs"])
            else:
                self.assertIn("extract\textract_json_invalid\t2", out.getvalue())

    def test_checkpoint_refusals_show_fixed_triage_reasons_without_contents(self):
        with contextlib.closing(sqlite3.connect(self.queue / "routing.sqlite3")) as db, db:
            db.execute("DELETE FROM checkpoints")
            db.executemany("INSERT INTO checkpoints VALUES (?,?,?,?,?)", [
                (self.now, "claude", "refused", "log_triage", "unsupported_kind"),
                (self.now, "claude", "refused", "log_triage", "unsupported_kind"),
                (self.now, "codex", "refused", "log_triage", "quality_disabled"),
                (self.now, "codex", "refused", "log_triage", "SECRET REASON"),
                (self.now, "codex", "eligible", "summarize", "eligible_mechanical_work"),
                (self.start - 1, "codex", "refused", "log_triage", "classification_refused"),
                (self.now + 1, "codex", "refused", "log_triage", "classification_refused"),
            ])
        result = local_status.report(str(self.config), now=self.now)
        self.assertEqual(result["checkpoint_refusals"], [
            {"caller": "claude", "task_type": "log_triage", "reason": "unsupported_kind", "count": 2},
            {"caller": "codex", "task_type": "log_triage", "reason": "quality_disabled", "count": 1},
            {"caller": "codex", "task_type": "log_triage", "reason": "unknown_refusal", "count": 1},
        ])
        self.assertNotIn("SECRET", json.dumps(result))
        for flags in ([], ["--work-split"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(local_status.main(["--config", str(self.config), *flags]), 0)
            self.assertIn("claude\tlog_triage\tunsupported_kind\t2", out.getvalue())
            self.assertNotIn("SECRET", out.getvalue())

    def test_cli_registration_json_text_and_readonly(self):
        self.job(status="queued", error="waiting_on_dependency:ollama SECRET")
        spec = importlib.util.spec_from_file_location("setup_bridge", ROOT / "setup_bridge.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        before = {p.name: p.read_bytes() for p in self.queue.iterdir()}
        real_connect = sqlite3.connect
        calls = []
        def connect(database, **kwargs):
            calls.append((database, kwargs))
            return real_connect(database, **kwargs)
        with mock.patch.object(local_status.sqlite3, "connect", side_effect=connect), \
             mock.patch.object(local_status.time, "time", return_value=self.now):
            for flags in ([], ["--json"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(entry.main(["local-status", "--config", str(self.config), "--days", "1", *flags]), 0)
                self.assertNotIn("SECRET", out.getvalue())
                if flags:
                    self.assertEqual(json.loads(out.getvalue())["days"], 1)
                else:
                    self.assertIn("held waiting_on_dependency", out.getvalue())
        self.assertTrue(all(uri.endswith("?mode=ro") and kw == {"uri": True} for uri, kw in calls))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.queue.iterdir()})
        with contextlib.closing(local_status._connect(self.queue / "localq.sqlite3")) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM jobs")

    def test_inline_output_counts_by_caller_reason_and_window(self):
        ledger = self.home / "state" / "routing" / "inline-output-routing.jsonl"
        ledger.parent.mkdir(parents=True)
        rows = [
            {"at": self.start, "caller": "claude", "routed": True, "waiver_reason": None},
            {"at": self.now, "caller": "codex", "routed": True, "waiver_reason": None},
            {"at": self.now, "caller": "codex", "routed": False, "waiver_reason": "below_threshold"},
            {"at": self.now, "caller": "codex", "routed": False, "waiver_reason": "below_threshold"},
            {"at": self.now, "caller": "claude", "routed": False, "waiver_reason": "timeout"},
            {"at": self.now, "caller": "codex", "routed": False,
             "waiver_reason": "admission_deferred:client_data_health_unverified"},
            {"at": self.now, "routed": False, "waiver_reason": "local_failed"},
            {"at": self.start - 1, "caller": "codex", "routed": True, "waiver_reason": None},
            {"at": self.now + 1, "caller": "codex", "routed": True, "waiver_reason": None},
            {"routed": True, "waiver_reason": None},
        ]
        ledger.write_text("".join(json.dumps({**row, "command": "SECRET COMMAND", "output": "SECRET OUTPUT",
                                              "job_id": "SECRET ID", "repo": "SECRET PATH"}) + "\n"
                                  for row in rows))
        before = ledger.read_bytes()
        result = local_status.report(str(self.config), now=self.now)
        self.assertEqual(result["inline_output_routing"], [
            {"caller": "claude", "routed": False, "waiver_reason": "timeout", "count": 1},
            {"caller": "claude", "routed": True, "waiver_reason": None, "count": 1},
            {"caller": "codex", "routed": False,
             "waiver_reason": "admission_deferred:client_data_health_unverified", "count": 1},
            {"caller": "codex", "routed": False, "waiver_reason": "below_threshold", "count": 2},
            {"caller": "codex", "routed": True, "waiver_reason": None, "count": 1},
            {"caller": "unknown", "routed": False, "waiver_reason": "local_failed", "count": 1},
        ])
        self.assertEqual(result["inline_output_undated_records"], 1)
        self.assertNotIn("SECRET", json.dumps(result))
        with mock.patch.object(local_status.time, "time", return_value=self.now):
            for flags in ([], ["--json"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(local_status.main(["--config", str(self.config), *flags]), 0)
                self.assertNotIn("SECRET", out.getvalue())
                if flags:
                    self.assertEqual(json.loads(out.getvalue()), result)
                else:
                    self.assertIn("codex\twaived\tbelow_threshold\t2", out.getvalue())
                    self.assertIn("codex\trouted\t-\t1", out.getvalue())
                    self.assertIn("Undated inline output records excluded from window: 1", out.getvalue())
        self.assertEqual(ledger.read_bytes(), before)

    def test_missing_inline_ledger_is_empty_and_never_created(self):
        result = local_status.report(str(self.config), now=self.now)
        self.assertEqual(result["inline_output_routing"], [])
        self.assertEqual(result["inline_output_undated_records"], 0)
        self.assertFalse((self.home / "state").exists())

    def test_invalid_inline_ledger_is_a_fixed_report_error(self):
        ledger = self.home / "state" / "routing" / "inline-output-routing.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("SECRET INVALID LEDGER")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local_status.main(["--config", str(self.config), "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "local_status:report_error"})

    def test_missing_databases_are_fixed_errors_and_never_created(self):
        for filename in ("routing.sqlite3", "localq.sqlite3"):
            (self.queue / filename).unlink()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = local_status.main(["--config", str(self.config), "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.getvalue()), {"error": "local_status:database_error"})
            self.assertFalse((self.queue / filename).exists())

    def test_bad_schema_and_feedback_do_not_leak_errors(self):
        self.job()
        with contextlib.closing(sqlite3.connect(self.queue / "localq.sqlite3")) as db, db:
            db.execute("UPDATE jobs SET feedback_json='SECRET MALFORMED JSON'")
        out = io.StringIO()
        # Keep the CLI's default seven-day window aligned with this fixture's
        # synthetic timestamps.  Without this, the malformed row ages out as
        # calendar time advances and the test stops exercising its error path.
        with mock.patch.object(local_status.time, "time", return_value=self.now), contextlib.redirect_stdout(out):
            self.assertEqual(local_status.main(["--config", str(self.config), "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "local_status:report_error"})
        with contextlib.closing(sqlite3.connect(self.queue / "localq.sqlite3")) as db, db:
            db.execute("DROP TABLE jobs")
        out = io.StringIO()
        with mock.patch.object(local_status.time, "time", return_value=self.now), contextlib.redirect_stdout(out):
            self.assertEqual(local_status.main(["--config", str(self.config), "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "local_status:database_error"})

    def test_invalid_config_and_days(self):
        self.config.write_text("SECRET INVALID CONFIG")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local_status.main(["--config", str(self.config), "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "local_status:config_error"})
        for value in ("0", "-1", "bad"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
                local_status.main(["--days", value])
            self.assertEqual(stop.exception.code, 2)


class DriftFixtureTests(unittest.TestCase):
    def test_summary_budget_has_a_complete_sentence_and_margin(self):
        # Equivalent split for this fixture: terminal .!? followed by whitespace.
        # There are no abbreviations, decimal points, or embedded newlines.
        sentences = re.split(r"(?<=[.!?])\s+", drift.GEMMA_CHECK_TEXT.strip())
        self.assertTrue(10 <= len(sentences) <= 14)
        self.assertTrue(all(sentence.endswith(".") for sentence in sentences))
        self.assertLessEqual(min(map(len, sentences)) + 3, len(drift.GEMMA_CHECK_TEXT) // 10)

    def test_drift_invokes_existing_certified_path_with_new_fixture(self):
        from types import SimpleNamespace
        from agent_bridge.localq import gemma_child
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        cfg = SimpleNamespace(local_backend="gemma_certified", gemma_delegate_executable="delegate",
            gemma_python_executable="python", gemma_receipt_validator_executable="validator",
            gemma_model_digest="digest", gemma_timeout_seconds=600)
        with mock.patch("agent_bridge.orchestration.config.load", return_value=cfg), \
             mock.patch.object(drift, "_sha256", side_effect=["delegate-hash", "validator-hash"] * 2), \
             mock.patch.object(gemma_child, "invoke", return_value={"invocation_id": "receipt"}) as invoke:
            success = drift.run_gemma_check("synthetic-config", home=root.name)
            self.assertTrue(success["ok"])
            self.assertEqual(success["receipt_id"], "receipt")
            directory = Path(success["receipt_directory"])
            self.assertEqual(directory.parent, Path(drift.paths(root.name)["root"], "gemma-probes"))
            if os.name == "posix":  # Windows has no POSIX mode bits
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(invoke.call_args.args[0]["input"], drift.GEMMA_CHECK_TEXT)
            self.assertEqual(invoke.call_args.kwargs["delegate_sha256"], "delegate-hash")
            self.assertEqual(invoke.call_args.kwargs["validator_sha256"], "validator-hash")
            self.assertEqual(invoke.call_args.kwargs["model_digest"], "digest")
            def failed_probe(job, *, receipt_root, **kwargs):
                Path(receipt_root, "operation.json").write_text("synthetic failure")
                raise gemma_child.GemmaRefusal("gemma_delegate_unavailable")

            invoke.side_effect = failed_probe
            failure = drift.run_gemma_check("synthetic-config", home=root.name)
            self.assertFalse(failure["ok"])
            self.assertEqual(failure["reason"], "gemma_delegate_unavailable")
            failed_directory = Path(failure["receipt_directory"])
            self.assertNotEqual(failed_directory, directory)
            self.assertEqual((failed_directory / "operation.json").read_text(), "synthetic failure")

        with mock.patch("agent_bridge.orchestration.config.load", return_value=cfg), \
             mock.patch.object(drift, "_sha256", return_value="digest"), \
             mock.patch.object(drift.store, "secure_mkdir", side_effect=PermissionError(13, "denied")), \
             mock.patch.object(gemma_child, "invoke") as invoke:
            failure = drift.run_gemma_check("synthetic-config", home=root.name)
            self.assertEqual(failure["reason"], "diagnostic_receipt_store_unavailable")
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
