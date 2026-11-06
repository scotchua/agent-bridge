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
        with sqlite3.connect(self.queue / "localq.sqlite3") as db:
            db.execute("""CREATE TABLE jobs (submitted_at REAL, caller TEXT, purpose TEXT,
                task_type TEXT, status TEXT, feedback_json TEXT, error TEXT,
                input_blob TEXT, result_blob TEXT, params_json TEXT)""")
        with sqlite3.connect(self.queue / "routing.sqlite3") as db:
            db.execute("CREATE TABLE checkpoints (created_at REAL, caller TEXT, status TEXT)")
            db.executemany("INSERT INTO checkpoints VALUES (?,?,?)", [
                (self.now, "claude", "eligible"), (self.now, "claude", "eligible"),
                (self.now, "codex", "refused"), (self.start - 1, "codex", "eligible")])

    def job(self, status="complete", feedback=None, purpose="work", task="summarize", at=None, error=None):
        with sqlite3.connect(self.queue / "localq.sqlite3") as db:
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
        with sqlite3.connect(self.queue / "localq.sqlite3") as db:
            db.execute("UPDATE jobs SET feedback_json='SECRET MALFORMED JSON'")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local_status.main(["--config", str(self.config), "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "local_status:report_error"})
        with sqlite3.connect(self.queue / "localq.sqlite3") as db:
            db.execute("DROP TABLE jobs")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
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
        cfg = SimpleNamespace(local_backend="gemma_certified", gemma_delegate_executable="delegate",
            gemma_python_executable="python", gemma_receipt_validator_executable="validator",
            gemma_model_digest="digest", gemma_timeout_seconds=600)
        with mock.patch("agent_bridge.orchestration.config.load", return_value=cfg), \
             mock.patch.object(drift, "_sha256", side_effect=["delegate-hash", "validator-hash"] * 2), \
             mock.patch.object(gemma_child, "invoke", return_value={"invocation_id": "receipt"}) as invoke:
            self.assertEqual(drift.run_gemma_check("synthetic-config"), {"ok": True, "receipt_id": "receipt"})
            self.assertEqual(invoke.call_args.args[0]["input"], drift.GEMMA_CHECK_TEXT)
            self.assertEqual(invoke.call_args.kwargs["delegate_sha256"], "delegate-hash")
            self.assertEqual(invoke.call_args.kwargs["validator_sha256"], "validator-hash")
            self.assertEqual(invoke.call_args.kwargs["model_digest"], "digest")
            invoke.side_effect = gemma_child.GemmaRefusal("gemma_delegate_unavailable")
            self.assertFalse(drift.run_gemma_check("synthetic-config")["ok"])


if __name__ == "__main__":
    unittest.main()
