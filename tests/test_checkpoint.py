"""Checkpoint ledger tests: migration, exact route binding, content-free
checkpoints, the assistant-facing tools that mint and consume them, and the
aggregate audit surface.

Classification for every fixture in this file is synthetic/internal_nonclient
test material; nothing here is client data.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import StageRouter
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import AdmissionError, FakeBackend, LocalQueue, ResourceSnapshot
from agent_bridge.orchestration.mcp import build_tools


class Sampler:
    def sample(self):
        return ResourceSnapshot(1000.0, "normal", "normal", True, 120.0)


def nonblank_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


class MigrationTests(unittest.TestCase):
    """Requirement 1: a pre-checkpoint ``routing_receipts`` table gains a
    nullable ``checkpoint_id`` column and its partial unique index, and an
    existing receipt survives the upgrade untouched."""

    def test_upgrading_a_pre_checkpoint_database_keeps_its_existing_receipt(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = temp.name
        db_path = os.path.join(root, "routing.sqlite3")

        # The exact pre-checkpoint schema: no checkpoint_id column at all.
        connection = sqlite3.connect(db_path)
        connection.execute("""CREATE TABLE routing_receipts (
            receipt_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL, policy_version TEXT NOT NULL,
            classification TEXT NOT NULL, decision TEXT NOT NULL,
            reason TEXT NOT NULL, task_type TEXT NOT NULL, caller TEXT NOT NULL,
            purpose TEXT NOT NULL, priority TEXT NOT NULL, flags_json TEXT NOT NULL,
            input_sha256 TEXT NOT NULL, input_bytes INTEGER NOT NULL,
            nonblank_lines INTEGER NOT NULL, job_id TEXT)""")
        connection.execute("""INSERT INTO routing_receipts (
            receipt_id, request_key, created_at, policy_version, classification, decision,
            reason, task_type, caller, purpose, priority, flags_json, input_sha256,
            input_bytes, nonblank_lines, job_id) VALUES (
            'pre-checkpoint-receipt', 'pre-checkpoint-key', 1000.0, 'localq-intake/v1',
            'internal_nonclient', 'local', 'eligible_mechanical_work', 'summarize', 'codex',
            'work', 'interactive', '[]', 'deadbeef', 100, 5, 'pre-checkpoint-job')""")
        connection.commit()
        connection.close()

        queue = LocalQueue(root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 2000.0)
        intake = AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=10, min_nonblank_lines=1),
                                 clock=lambda: 2000.0)

        inspect = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in inspect.execute("PRAGMA table_info(routing_receipts)").fetchall()}
            self.assertIn("checkpoint_id", columns)
            index_names = {row[0] for row in inspect.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='routing_receipts'").fetchall()}
            self.assertIn("routing_receipts_checkpoint_id_uq", index_names)
        finally:
            inspect.close()

        # The pre-existing receipt is untouched and readable, with a NULL
        # (not a manufactured) checkpoint_id.
        receipt = intake.receipt("pre-checkpoint-receipt")
        self.assertEqual(receipt["classification"], "internal_nonclient")
        self.assertEqual(receipt["decision"], "local")
        self.assertIsNone(receipt["checkpoint_id"])

        # The partial unique index must not treat two legacy NULLs as a
        # collision: two purpose="test" (no-checkpoint) receipts both land.
        first = intake.route(task_type="summarize", input="synthetic legacy note\n" * 12,
                             params=None, priority="interactive", classification="internal_nonclient",
                             caller="codex", purpose="test", idempotency_key="legacy-1")
        second = intake.route(task_type="summarize", input="another legacy note\n" * 12,
                              params=None, priority="interactive", classification="internal_nonclient",
                              caller="codex", purpose="test", idempotency_key="legacy-2")
        self.assertIsNone(first["checkpoint_id"])
        self.assertIsNone(second["checkpoint_id"])
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])

        # And the new checkpoint + route flow works on an upgraded database.
        text = "upgraded database eligible operating line\n" * 12
        checkpoint = intake.checkpoint(task_id="upgrade-check", task_type="summarize",
                                       classification="internal_nonclient", caller="codex",
                                       input_bytes=len(text.encode("utf-8")),
                                       nonblank_lines=nonblank_lines(text))
        self.assertEqual(checkpoint["status"], "eligible")
        routed = intake.route(task_type="summarize", input=text, params=None, priority="interactive",
                              classification="internal_nonclient", caller="codex", purpose="work",
                              checkpoint_id=checkpoint["checkpoint_id"])
        self.assertEqual(routed["decision"], "local")
        self.assertEqual(routed["checkpoint_id"], checkpoint["checkpoint_id"])
        self.assertIsNotNone(routed["job_id"])


class CheckpointLedgerTests(unittest.TestCase):
    """Direct AutomaticIntake.checkpoint / route tests, requirements 2, 4-10."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = LocalQueue(self.temp.name, sampler=Sampler(), backend=FakeBackend(),
                                clock=lambda: 5000.0)
        self.intake = AutomaticIntake(
            self.queue, policy=IntakePolicy(min_input_chars=10, min_nonblank_lines=1),
            clock=lambda: 5000.0)

    def route_with_checkpoint(self, text, *, checkpoint_id, task_type="summarize",
                              classification="internal_nonclient", caller="codex"):
        return self.intake.route(task_type=task_type, input=text, params=None,
                                 priority="interactive", classification=classification,
                                 caller=caller, purpose="work", checkpoint_id=checkpoint_id)

    def mint(self, text, *, task_id, task_type="summarize", classification="internal_nonclient",
             caller="codex"):
        return self.intake.checkpoint(task_id=task_id, task_type=task_type,
                                      classification=classification, caller=caller,
                                      input_bytes=len(text.encode("utf-8")),
                                      nonblank_lines=nonblank_lines(text))

    # -- requirement 2: purpose="work" requires a checkpoint -------------

    def test_direct_work_without_a_checkpoint_refuses(self):
        with self.assertRaisesRegex(AdmissionError, "checkpoint_required"):
            self.intake.route(task_type="summarize", input="eligible operating line\n" * 12,
                              params=None, priority="interactive", classification="internal_nonclient",
                              caller="codex", purpose="work")

    def test_purpose_test_may_omit_a_checkpoint(self):
        routed = self.intake.route(task_type="summarize", input="calibration only\n" * 12,
                                   params=None, priority="interactive",
                                   classification="internal_nonclient", caller="codex", purpose="test")
        self.assertEqual(routed["decision"], "local")
        self.assertIsNone(routed["checkpoint_id"])

    # -- requirement 9: no_eligible_unit ----------------------------------

    def test_no_eligible_unit_is_append_only_and_not_routable(self):
        row = self.intake.checkpoint(task_id="scan-found-nothing", task_type="summarize",
                                     classification="internal_nonclient", caller="codex",
                                     input_bytes=0, nonblank_lines=0, no_eligible_unit=True)
        self.assertEqual(row["status"], "no_eligible_unit")
        self.assertEqual(row["reason"], "no_eligible_unit")
        with self.assertRaisesRegex(AdmissionError, "checkpoint_not_routable"):
            self.route_with_checkpoint("", checkpoint_id=row["checkpoint_id"])
        self.assertEqual(self.intake.audit()["no_eligible_unit"], 1)
        with self.assertRaises(Exception):
            with self.intake._db() as db:
                db.execute("UPDATE checkpoints SET status='eligible' WHERE checkpoint_id=?",
                          (row["checkpoint_id"],))

    # -- requirement 5/6: exact route binding -----------------------------

    def test_two_same_sized_distinct_units_remain_distinct(self):
        text_a = "aaaa bbbb cccc\n" * 12
        text_b = "dddd eeee ffff\n" * 12
        self.assertEqual(len(text_a.encode("utf-8")), len(text_b.encode("utf-8")))
        checkpoint_a = self.mint(text_a, task_id="unit-a")
        checkpoint_b = self.mint(text_b, task_id="unit-b")
        self.assertNotEqual(checkpoint_a["checkpoint_id"], checkpoint_b["checkpoint_id"])
        receipt_a = self.route_with_checkpoint(text_a, checkpoint_id=checkpoint_a["checkpoint_id"])
        receipt_b = self.route_with_checkpoint(text_b, checkpoint_id=checkpoint_b["checkpoint_id"])
        self.assertNotEqual(receipt_a["receipt_id"], receipt_b["receipt_id"])
        self.assertNotEqual(receipt_a["job_id"], receipt_b["job_id"])

    def test_same_metrics_different_content_cannot_consume_an_existing_receipt(self):
        text_a = "aaaa bbbb cccc\n" * 12
        text_b = "dddd eeee ffff\n" * 12
        self.assertEqual(len(text_a.encode("utf-8")), len(text_b.encode("utf-8")))
        self.assertEqual(nonblank_lines(text_a), nonblank_lines(text_b))
        checkpoint = self.mint(text_a, task_id="reuse-attempt")
        first = self.route_with_checkpoint(text_a, checkpoint_id=checkpoint["checkpoint_id"])
        self.assertEqual(first["decision"], "local")
        with self.assertRaisesRegex(AdmissionError, "idempotency_key_conflict"):
            self.route_with_checkpoint(text_b, checkpoint_id=checkpoint["checkpoint_id"])

    def test_checkpoint_binding_validates_every_declared_field(self):
        text = "binding validation operating line\n" * 12
        checkpoint = self.mint(text, task_id="binding-check")
        with self.assertRaisesRegex(AdmissionError, "checkpoint_binding_mismatch"):
            self.route_with_checkpoint(text, checkpoint_id=checkpoint["checkpoint_id"], caller="claude")
        with self.assertRaisesRegex(AdmissionError, "checkpoint_binding_mismatch"):
            self.route_with_checkpoint(text, checkpoint_id=checkpoint["checkpoint_id"],
                                       classification="public")
        with self.assertRaisesRegex(AdmissionError, "checkpoint_binding_mismatch"):
            self.route_with_checkpoint(text, checkpoint_id=checkpoint["checkpoint_id"],
                                       task_type="extract")

    def test_a_different_idempotency_key_on_a_consumed_checkpoint_is_refused(self):
        text = "fixed request key operating line\n" * 12
        checkpoint = self.mint(text, task_id="fixed-key-check")
        with self.assertRaisesRegex(AdmissionError, "checkpoint_idempotency_conflict"):
            self.intake.route(task_type="summarize", input=text, params=None, priority="interactive",
                              classification="internal_nonclient", caller="codex", purpose="work",
                              checkpoint_id=checkpoint["checkpoint_id"], idempotency_key="something-else")

    def test_same_checkpoint_concurrent_retry_creates_one_job_and_receipt(self):
        text = "concurrent retry operating line\n" * 12
        checkpoint = self.mint(text, task_id="concurrency-check")
        results: list[dict] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(6)

        def attempt():
            try:
                barrier.wait(timeout=5)
                results.append(self.route_with_checkpoint(text, checkpoint_id=checkpoint["checkpoint_id"]))
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [], errors)
        self.assertEqual(len(results), 6)
        self.assertEqual(len({result["receipt_id"] for result in results}), 1)
        self.assertEqual(len({result["job_id"] for result in results}), 1)
        self.assertEqual(self.queue.state_report()["counts"], {"queued": 1})

    # -- requirement: refused checkpoint-bound work never queues ---------

    def test_refused_checkpoint_bound_work_does_not_queue_or_fallback(self):
        lines = ["x" * 20 for _ in range(50)]
        text = "\n".join(lines)
        checkpoint = self.mint(text, task_id="refused-no-queue", task_type="tax_position")
        self.assertEqual(checkpoint["status"], "refused")
        self.assertEqual(checkpoint["reason"], "task_requires_cloud_or_human_judgment")
        result = self.route_with_checkpoint(text, checkpoint_id=checkpoint["checkpoint_id"],
                                            task_type="tax_position")
        self.assertEqual(result["decision"], "refused")
        self.assertIsNone(result["job_id"])
        self.assertEqual(result["fallback"], "none")
        self.assertEqual(self.queue.state_report()["counts"], {})

    # -- requirement 10: UTF-8 bytes, not characters, at both thresholds --

    def test_multibyte_input_is_measured_in_utf8_bytes_not_characters(self):
        policy = IntakePolicy(min_input_chars=10, min_nonblank_lines=100)
        intake = AutomaticIntake(
            LocalQueue(os.path.join(self.temp.name, "multibyte"), sampler=Sampler(),
                      backend=FakeBackend(), clock=lambda: 5000.0),
            policy=policy, clock=lambda: 5000.0)
        text = "é" * 9  # 9 code points, 18 UTF-8 bytes: over the byte
        # threshold (10) though under it in characters, and one line, well
        # under the 100-line threshold -- eligibility here can only be
        # explained by counting UTF-8 bytes.
        self.assertEqual(len(text), 9)
        self.assertEqual(len(text.encode("utf-8")), 18)
        routed = intake.route(task_type="summarize", input=text, params=None, priority="interactive",
                              classification="internal_nonclient", caller="codex", purpose="test")
        self.assertEqual(routed["decision"], "local")
        self.assertEqual(routed["reason"], "eligible_mechanical_work")
        self.assertEqual(routed["input_bytes"], 18)
        checkpoint = intake.checkpoint(task_id="multibyte-checkpoint", task_type="summarize",
                                       classification="internal_nonclient", caller="codex",
                                       input_bytes=18, nonblank_lines=1)
        self.assertEqual(checkpoint["status"], "eligible")

    # -- requirement 8: content-free aggregate audit ----------------------

    def test_audit_reports_total_eligible_dispatched_no_eligible_unit_and_reasons(self):
        eligible_text = "audit eligible operating line\n" * 12
        checkpoint = self.mint(eligible_text, task_id="audit-eligible-dispatch")
        self.route_with_checkpoint(eligible_text, checkpoint_id=checkpoint["checkpoint_id"])

        refused = self.intake.checkpoint(task_id="audit-refused-classification", task_type="summarize",
                                         classification="client_derived", caller="codex",
                                         input_bytes=1000, nonblank_lines=50)
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reason"], "classification_refused")

        self.intake.checkpoint(task_id="audit-no-unit", task_type="summarize",
                               classification="internal_nonclient", caller="codex",
                               input_bytes=0, nonblank_lines=0, no_eligible_unit=True)

        audit = self.intake.audit()
        self.assertEqual(audit["total"], 3)
        self.assertEqual(audit["eligible"], 1)
        self.assertEqual(audit["dispatched"], 1)
        self.assertEqual(audit["no_eligible_unit"], 1)
        self.assertEqual(audit["refused_by_reason"]["classification_refused"], 1)
        self.assertEqual(audit["refused_by_reason"]["below_local_delegation_threshold"], 0)
        self.assertNotIn("task_id", audit)
        self.assertNotIn("checkpoint_id", audit)


class AssistantFacingCheckpointToolTests(unittest.TestCase):
    """The MCP tool surface (orchestration/mcp.py): requirements 3, 4, 7, 8."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = LocalQueue(os.path.join(self.temp.name, "localq"), sampler=Sampler(),
                                backend=FakeBackend(), clock=lambda: 9000.0)
        self.intake = AutomaticIntake(
            self.queue, policy=IntakePolicy(min_input_chars=10, min_nonblank_lines=1),
            clock=lambda: 9000.0)
        self.router = StageRouter(os.path.join(self.temp.name, "stage.sqlite3"), clock=lambda: 9000.0)

    def build(self, caller):
        return build_tools(caller, self.router, self.queue, self.intake)

    def test_both_bound_callers_auto_checkpoint_and_route(self):
        for caller in ("codex", "claude"):
            with self.subTest(caller=caller):
                tools = self.build(caller)
                reply = tools["work_route_local"]["handler"]({
                    "task_type": "summarize", "input": f"{caller} eligible operating line\n" * 12,
                    "priority": "interactive", "classification": "internal_nonclient",
                })
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["decision"], "local")
                self.assertEqual(reply["caller"], caller)
                self.assertEqual(reply["purpose"], "work")
                self.assertIsNotNone(reply["job_id"])
                self.assertIsNotNone(reply["checkpoint_id"])

    def test_purpose_and_caller_cannot_be_spoofed_by_the_caller(self):
        tools = self.build("codex")
        reply = tools["work_route_local"]["handler"]({
            "task_type": "summarize",
            "input": "spoof attempt operating line\n" * 12,
            "priority": "interactive", "classification": "internal_nonclient",
            "purpose": "test", "caller": "claude",
        })
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["caller"], "codex")
        self.assertEqual(reply["purpose"], "work")
        receipt = self.intake.receipt(reply["receipt_id"])
        self.assertEqual(receipt["purpose"], "work")
        self.assertIsNotNone(receipt["checkpoint_id"])
        self.assertNotIn("purpose", self.build("codex")["work_route_local"]["inputSchema"]["properties"])

    def test_prior_public_checkpoint_is_consumed_without_creating_a_second_one(self):
        tools = self.build("codex")
        text = "one bounded operating unit\n" * 12
        checkpoint = tools["work_checkpoint"]["handler"]({
            "task_id": "bounded-unit-1", "task_type": "summarize",
            "classification": "internal_nonclient",
            "input_bytes": len(text.encode("utf-8")),
            "nonblank_lines": nonblank_lines(text),
        })
        routed = tools["work_route_local"]["handler"]({
            "task_type": "summarize", "input": text, "priority": "interactive",
            "classification": "internal_nonclient",
            "checkpoint_id": checkpoint["checkpoint_id"],
        })
        self.assertTrue(routed["ok"], routed)
        self.assertEqual(routed["checkpoint_id"], checkpoint["checkpoint_id"])
        self.assertEqual(self.intake.audit()["total"], 1)
        self.assertEqual(self.intake.audit()["dispatched"], 1)

    def test_work_checkpoint_tool_is_content_free_and_caller_bound(self):
        tools = self.build("codex")
        reply = tools["work_checkpoint"]["handler"]({
            "task_id": "content-free-check", "task_type": "summarize",
            "classification": "internal_nonclient", "input_bytes": 500, "nonblank_lines": 20,
        })
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["status"], "eligible")
        self.assertEqual(reply["caller"], "codex")
        self.assertNotIn("input", tools["work_checkpoint"]["inputSchema"]["properties"])
        self.assertNotIn("purpose", tools["work_checkpoint"]["inputSchema"]["properties"])
        self.assertNotIn("caller", tools["work_checkpoint"]["inputSchema"]["properties"])

    def test_no_eligible_unit_tool_records_a_closure_counted_by_audit(self):
        tools = self.build("codex")
        reply = tools["work_checkpoint_no_eligible_unit"]["handler"]({
            "task_id": "scan-nothing-found", "task_type": "summarize",
            "classification": "internal_nonclient",
        })
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["status"], "no_eligible_unit")
        audit = tools["work_checkpoint_audit"]["handler"]({})
        self.assertEqual(audit["no_eligible_unit"], 1)

    def test_checkpoint_audit_tool_is_content_free_and_aggregate(self):
        tools = self.build("codex")
        tools["work_route_local"]["handler"]({
            "task_type": "summarize", "input": "audit tool operating line\n" * 12,
            "priority": "interactive", "classification": "internal_nonclient",
        })
        audit = tools["work_checkpoint_audit"]["handler"]({})
        self.assertTrue(audit["ok"], audit)
        self.assertNotIn("task_id", audit)
        self.assertNotIn("checkpoint_id", audit)
        self.assertGreaterEqual(audit["total"], 1)
        self.assertGreaterEqual(audit["dispatched"], 1)
        self.assertEqual(tools["work_checkpoint_audit"]["inputSchema"]["properties"], {})


if __name__ == "__main__":
    unittest.main()
