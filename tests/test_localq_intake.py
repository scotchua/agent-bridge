"""Automatic local-routing admission and receipt tests."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import FakeBackend, LocalQueue, QueueCaps, ResourceSnapshot
from agent_bridge.localq import mcp


class Sampler:
    def sample(self):
        return ResourceSnapshot(1000.0, "normal", "normal", True, 120.0)


class AutomaticIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = LocalQueue(
            self.temp.name, sampler=Sampler(), backend=FakeBackend(),
            caps=QueueCaps(), clock=lambda: 1000.0)
        self.intake = AutomaticIntake(
            self.queue, policy=IntakePolicy(min_input_chars=80, min_nonblank_lines=4),
            clock=lambda: 1000.0)

    def tearDown(self):
        self.temp.cleanup()

    def route(self, **changes):
        # purpose="test": this class drives AutomaticIntake.route directly to
        # calibrate its classification and idempotency behaviour, which is
        # exactly the internal test/calibration exemption from the
        # checkpoint requirement (tests/test_checkpoint.py exercises the
        # purpose="work" + checkpoint path this file does not).
        args = {
            "task_type": "summarize", "input": "synthetic operating note " * 8,
            "params": {"instruction": "Select operating facts."},
            "priority": "interactive", "classification": "internal_nonclient",
            "caller": "codex", "purpose": "test", "risk_flags": [],
        }
        args.update(changes)
        return self.intake.route(**args)

    def test_eligible_substantial_mechanical_work_is_queued_locally(self):
        routed = self.route()
        self.assertEqual(routed["decision"], "local")
        self.assertEqual(routed["fallback"], "none")
        self.assertIsNotNone(routed["job_id"])
        self.assertEqual(self.queue.status(routed["job_id"])["classification"], "internal_nonclient")
        receipt = self.intake.receipt(routed["receipt_id"])
        self.assertEqual(receipt["classification"], "internal_nonclient")
        self.assertEqual(receipt["input_sha256"], routed["input_sha256"])

    def test_client_derived_mechanical_work_is_queued_locally(self):
        # Scott, 2026-09-24: the on-device model is the safest processor of
        # client data. The flag is recognized, not refused.
        routed = self.route(classification="client_derived", risk_flags=["client_derived"])
        self.assertEqual(routed["decision"], "local")
        self.assertEqual(routed["fallback"], "none")
        self.assertEqual(self.queue.status(routed["job_id"])["classification"], "client_derived")

    def test_small_work_is_refused_without_queue_or_fallback(self):
        routed = self.route(input="tiny note")
        self.assertEqual(routed["decision"], "refused")
        self.assertEqual(routed["reason"], "below_local_delegation_threshold")
        self.assertIsNone(routed["job_id"])
        self.assertEqual(routed["fallback"], "none")
        self.assertEqual(self.queue.state_report()["counts"], {})

    def test_judgment_client_and_prohibited_work_are_refused(self):
        cases = [
            {"task_type": "tax_position"},
            {"classification": "client"},
            {"risk_flags": ["professional_judgment"]},
            {"risk_flags": ["exact_sensitive_identifiers"]},
            {"risk_flags": ["client_derived", "licensed_review"]},
        ]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                routed = self.route(input=("different substantial text " * 8) + str(index), **changes)
                self.assertEqual(routed["decision"], "refused")
                self.assertIsNone(routed["job_id"])
                self.assertEqual(routed["fallback"], "none")

    def test_decision_and_classification_receipt_are_immutable_and_idempotent(self):
        first = self.route(idempotency_key="same-request")
        second = self.route(idempotency_key="same-request")
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertTrue(second["deduplicated"])
        with self.intake._db() as db, self.assertRaises(Exception):
            db.execute("UPDATE routing_receipts SET classification='public' WHERE receipt_id=?", (first["receipt_id"],))
        self.assertEqual(self.intake.receipt(first["receipt_id"])["classification"], "internal_nonclient")

    def test_idempotency_key_cannot_reclassify_or_replace_input(self):
        self.route(idempotency_key="fixed")
        with self.assertRaisesRegex(Exception, "idempotency_key_conflict"):
            self.route(idempotency_key="fixed", classification="public")

    def test_mcp_exposes_additive_router_and_existing_feedback(self):
        queue_tools = mcp.build_tools(self.queue)
        route_tools = mcp.build_intake_tools(self.intake)
        self.assertIn("local_feedback", queue_tools)
        self.assertEqual(set(route_tools), {"local_route", "local_route_receipt"})
        reply = route_tools["local_route"]["handler"]({
            "task_type": "extract", "input": "public synthetic field line\n" * 12,
            "priority": "bulk", "classification": "public", "caller": "claude",
            "purpose": "test", "risk_flags": [],
        })
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["decision"], "local")


if __name__ == "__main__":
    unittest.main()
