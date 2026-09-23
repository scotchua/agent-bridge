"""Expose the reviewed branch checks to unittest without changing their assertions."""
from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
import peer_budget_checks
import redaction_handoff
from agent_bridge import broker, registry, worker
from agent_bridge.errors import BrokerError, ErrorCategory


class ReplayOverlapTests(unittest.TestCase):
    def test_handoff_without_admitted_status_cannot_send(self):
        for peer in ("claude", "codex"):
            with self.subTest(peer=peer), redaction_handoff.local_job(peer) as (sb, prep, sends, _):
                admitted = redaction_handoff.admit(sb, prep, peer)
                job_dir = sb.cfg.job_dir(admitted["job_id"])
                (Path(job_dir) / "status.json").unlink()
                with self.assertRaises(BrokerError) as refused:
                    worker.execute(job_dir)
                self.assertEqual(refused.exception.category, ErrorCategory.JOB_NOT_FOUND)
                self.assertEqual(sends, [])

    def test_handoff_still_requires_local_first(self):
        for peer in ("claude", "codex"):
            with self.subTest(peer=peer), redaction_handoff.local_job(peer) as (sb, prep, sends, _):
                sb.cfg.raw["local_first"].update(enabled=True, read_gate_min_bytes=1)
                with self.assertRaises(BrokerError) as refused:
                    redaction_handoff.admit(sb, prep, peer)
                self.assertEqual(refused.exception.category, ErrorCategory.LOCAL_FIRST_REQUIRED)
                self.assertEqual(sends, [])

    def test_local_first_receipt_is_consumed_once_per_handoff(self):
        for peer in ("claude", "codex"):
            with self.subTest(peer=peer), redaction_handoff.local_job(peer) as (sb, prep, sends, _):
                queue = Path(sb.root) / "local-queue"
                queue.mkdir()
                with sqlite3.connect(queue / "routing.sqlite3") as db:
                    db.execute("CREATE TABLE routing_receipts "
                               "(receipt_id TEXT, decision TEXT, created_at REAL)")
                    db.execute("INSERT INTO routing_receipts VALUES (?, ?, ?)",
                               ("synthetic-receipt", "local", time.time()))
                sb.cfg.raw["local_first"].update(
                    enabled=True, read_gate_min_bytes=1, local_queue_root=str(queue))
                prep.args["local_first"] = {"digest_receipt_id": "synthetic-receipt"}
                admitted = redaction_handoff.admit(sb, prep, peer)
                worker.execute(sb.cfg.job_dir(admitted["job_id"]))
                self.assertEqual(registry.read_status(sb.cfg, admitted["job_id"])["status"],
                                 "complete")
                self.assertEqual(len(sends), 1)
                session = registry.load_conversation(sb.cfg, prep.cid)["peer_session_id"]
                followup = redaction_handoff.Preparation(
                    sb, peer, conversation_id=prep.cid, session_id=session)
                followup.args["local_first"] = prep.args["local_first"]
                with self.assertRaises(BrokerError) as refused:
                    redaction_handoff.admit(sb, followup, peer)
                self.assertEqual(refused.exception.category, ErrorCategory.LOCAL_FIRST_REQUIRED)
                self.assertEqual(len(sends), 1)

    def test_native_structured_exhaustion_keeps_diagnostic_and_adds_receipt(self):
        sb = harness.Sandbox()
        try:
            sb.env(FAKE_CLAUDE_MODE="structured_output_exhausted")
            started, status, response = sb.run_to_completion("codex")
            self.assertEqual(status["error_category"], "peer_output_schema_invalid")
            self.assertEqual(response["error_category"], "peer_structured_output_exhausted")
            self.assertFalse(response["retryable_by_caller"])
            receipt = response["receipt"]
            self.assertEqual(receipt["outcome"], "structured_output_exhausted")
            self.assertEqual(receipt["next_action"], "decompose")
            self.assertEqual(receipt["attempt_count"], 1)
            self.assertEqual(broker.poll(sb.cfg, "codex", {"job_id": started["job_id"]})[
                "receipt"], receipt)
        finally:
            sb.cleanup()


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite([tests])
    for module in (peer_budget_checks, redaction_handoff):
        for name, function in vars(module).items():
            if not name.startswith("test_") or name == "test_redaction_handoff":
                continue

            def run(function=function):
                before = len(harness.FAILED)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    function()
                if len(harness.FAILED) != before:
                    raise AssertionError(output.getvalue())

            suite.addTest(unittest.FunctionTestCase(run, description=name))
    return suite
