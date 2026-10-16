"""Automatic local-routing admission and receipt tests."""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import (AdmissionError, FakeBackend, LocalQueue,
                                       QueueCaps, ResourceSnapshot)
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

    def test_client_derived_private_worker_is_refused(self):
        with self.assertRaisesRegex(AdmissionError, "client_data_backend_unsupported"):
            self.route(classification="client_derived", risk_flags=["client_derived"])

    def test_client_derived_classification_needs_no_flag(self):
        with self.assertRaisesRegex(AdmissionError, "client_data_backend_unsupported"):
            self.route(classification="client_derived")

    def test_a_client_derived_flag_cannot_contradict_the_classification(self):
        for classification in ("synthetic", "public", "internal_nonclient"):
            with self.subTest(classification=classification):
                routed = self.route(classification=classification, risk_flags=["client_derived"],
                                    input=("contradictory label text " * 8) + classification)
                self.assertEqual(routed["decision"], "refused")
                self.assertEqual(routed["reason"], "risk_flags_invalid")
                self.assertIsNone(routed["job_id"])

    def test_client_derived_checkpoints_are_recorded_like_any_other(self):
        closed = self.intake.checkpoint(task_id="cd-none", task_type="summarize",
                                        classification="client_derived", caller="claude",
                                        input_bytes=0, nonblank_lines=0, no_eligible_unit=True)
        self.assertEqual(closed["status"], "no_eligible_unit")
        eligible = self.intake.checkpoint(task_id="cd-unit", task_type="summarize",
                                          classification="client_derived", caller="claude",
                                          input_bytes=5000, nonblank_lines=80)
        self.assertEqual(eligible["status"], "eligible")

    def test_an_idempotency_key_cannot_relabel_client_derived_work(self):
        with self.assertRaisesRegex(AdmissionError, "client_data_backend_unsupported"):
            self.route(idempotency_key="cd", classification="client_derived")
        # A refused submission made no receipt, so it cannot consume the key.
        self.assertEqual(self.route(idempotency_key="cd", classification="internal_nonclient")["decision"], "local")

    def test_client_derived_routes_with_certified_backend_and_passing_gate(self):
        class PassingGate:
            def check(self):
                return None
        queue = LocalQueue(self.temp.name + "-gemma", sampler=Sampler(), backend=FakeBackend(),
                           caps=QueueCaps(), clock=lambda: 1000.0, backend_id="gemma_certified",
                           allowed_task_types=frozenset({"summarize"}),
                           client_data_health_gate=PassingGate())
        intake = AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=80, min_nonblank_lines=4),
                                clock=lambda: 1000.0)
        routed = intake.route(task_type="summarize", input="client note " * 20, params={},
                              priority="interactive", classification="client_derived", caller="codex",
                              purpose="test", risk_flags=[])
        self.assertEqual(routed["decision"], "local")
        self.assertEqual(queue.status(routed["job_id"])["classification"], "client_derived")

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

    def test_database_initialization_is_idempotent_and_durable(self):
        AutomaticIntake(self.queue, policy=self.intake.policy, clock=lambda: 1000.0)
        with self.intake._db() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_database_initialization_restores_full_synchronous_mode(self):
        class PrimedIntake(AutomaticIntake):
            @contextmanager
            def _db(self):
                with super()._db() as db:
                    db.execute("PRAGMA synchronous=OFF")
                    yield db
                    self.synchronous_during_init = db.execute("PRAGMA synchronous").fetchone()[0]

        intake = PrimedIntake(self.queue, policy=self.intake.policy, clock=lambda: 1000.0)
        self.assertEqual(intake.synchronous_during_init, 2)

    def test_database_initialization_keeps_receipts_append_only(self):
        receipt = self.route()
        with self.intake._db() as db, self.assertRaises(Exception):
            db.execute("DELETE FROM routing_receipts WHERE receipt_id=?", (receipt["receipt_id"],))
        self.assertEqual(self.intake.receipt(receipt["receipt_id"])["decision"], "local")

    def test_validated_flags_rejects_a_string_instead_of_a_list(self):
        with self.assertRaisesRegex(AdmissionError, "risk_flags_invalid"):
            self.route(risk_flags="confidential")

    def test_checkpoint_rejects_invalid_task_ids_and_accepts_the_limit(self):
        args = dict(task_type="summarize", classification="synthetic", caller="codex",
                    input_bytes=100, nonblank_lines=1)
        with self.assertRaisesRegex(AdmissionError, "task_id_invalid"):
            self.intake.checkpoint(task_id="", **args)
        with self.assertRaisesRegex(AdmissionError, "task_id_invalid"):
            self.intake.checkpoint(task_id="x" * 257, **args)
        self.assertEqual(self.intake.checkpoint(task_id="x" * 256, **args)["task_id"], "x" * 256)

    def test_checkpoint_rejects_an_oversized_request_key(self):
        with self.assertRaisesRegex(AdmissionError, "idempotency_key_invalid"):
            self.intake.checkpoint(task_id="key-limit", task_type="summarize",
                                   classification="synthetic", caller="codex", input_bytes=100,
                                   nonblank_lines=1, idempotency_key="k" * 257)

    def test_checkpoint_replay_returns_the_saved_checkpoint(self):
        args = dict(task_id="replay", task_type="summarize", classification="synthetic",
                    caller="codex", input_bytes=100, nonblank_lines=1,
                    idempotency_key="checkpoint-replay")
        first = self.intake.checkpoint(**args)
        second = self.intake.checkpoint(**args)
        self.assertEqual(second["checkpoint_id"], first["checkpoint_id"])
        self.assertTrue(second["deduplicated"])

    def test_checkpoint_replay_rejects_each_changed_field(self):
        args = dict(task_id="original-unit", task_type="summarize", classification="synthetic",
                    caller="codex", input_bytes=100, nonblank_lines=1, risk_flags=[])
        changes = (dict(task_id="revised-unit"), dict(task_type="extract"),
                   dict(classification="public"), dict(caller="claude"),
                   dict(input_bytes=101), dict(nonblank_lines=2),
                   dict(risk_flags=["confidential"]))
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                key = "checkpoint-conflict-" + str(index)
                self.intake.checkpoint(**args, idempotency_key=key)
                with self.assertRaisesRegex(AdmissionError, "idempotency_key_conflict"):
                    self.intake.checkpoint(**{**args, **change}, idempotency_key=key)

    def test_get_checkpoint_returns_the_row_and_rejects_unknown_ids(self):
        checkpoint = self.intake.checkpoint(task_id="lookup", task_type="summarize",
                                            classification="synthetic", caller="codex",
                                            input_bytes=100, nonblank_lines=1)
        self.assertEqual(self.intake.get_checkpoint(checkpoint["checkpoint_id"])["task_id"], "lookup")
        with self.assertRaisesRegex(AdmissionError, "checkpoint_not_found"):
            self.intake.get_checkpoint("missing-checkpoint")

    def test_health_refusal_is_committed_to_the_audit(self):
        with mock.patch.object(self.queue, "submit", side_effect=AdmissionError("client_data_health_failed")):
            with self.assertRaisesRegex(AdmissionError, "client_data_health_failed"):
                self.route()
        self.assertEqual(self.intake.audit()["health_refusals_by_reason"]["client_data_health_failed"], 1)

    def test_receipt_replay_rejects_a_different_caller(self):
        self.route(idempotency_key="caller-bound")
        with self.assertRaisesRegex(AdmissionError, "idempotency_key_conflict"):
            self.route(idempotency_key="caller-bound", caller="claude")

    def test_checkpoint_binding_rejects_empty_and_missing_ids(self):
        for checkpoint_id, reason in (("", "checkpoint_id_invalid"),
                                      ("missing-checkpoint", "checkpoint_not_found")):
            with self.subTest(checkpoint_id=checkpoint_id):
                with self.assertRaisesRegex(AdmissionError, reason):
                    self.route(purpose="work", checkpoint_id=checkpoint_id)

    def test_route_rejects_non_text_input_and_invalid_purpose(self):
        with self.assertRaisesRegex(AdmissionError, "input_invalid"):
            self.route(input=42)
        with self.assertRaisesRegex(AdmissionError, "provenance_invalid"):
            self.route(input="tiny", purpose="invalid")

    def test_receipt_rejects_an_unknown_id(self):
        with self.assertRaisesRegex(AdmissionError, "routing_receipt_not_found"):
            self.intake.receipt("missing-receipt")


if __name__ == "__main__":
    unittest.main()
