"""Focused offline broker boundaries found by the mutation sweep."""
from __future__ import annotations

import os
import sqlite3
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import Sandbox
import redaction_handoff
from agent_bridge import broker, store
from agent_bridge.errors import BrokerError, ErrorCategory


class BrokerMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def test_error_response_keeps_closed_category_and_identifier(self) -> None:
        response = broker.error_response(ErrorCategory.INPUT_SCHEMA_INVALID, job_id="invented-job")
        self.assertFalse(response["ok"])
        self.assertEqual(response["job_id"], "invented-job")
        self.assertEqual(response["error_category"], ErrorCategory.INPUT_SCHEMA_INVALID.value)

    def test_validate_common_accepts_exact_budget_and_refuses_blank_prompt(self) -> None:
        args = {"prompt": "abcde", "source_classification": "internal"}
        with mock.patch.object(self.sandbox.cfg, "prompt_budget", return_value=5):
            broker._validate_common(self.sandbox.cfg, args, broker.START_FIELDS, "claude")
            with self.assertRaises(BrokerError) as caught:
                broker._validate_common(self.sandbox.cfg, {**args, "prompt": "   "},
                                        broker.START_FIELDS, "claude")
        self.assertEqual(caught.exception.category, ErrorCategory.INPUT_SCHEMA_INVALID)

    def test_oversized_start_and_continue_report_safe_size_details(self) -> None:
        sandbox = Sandbox(limits={"prompt_max_chars": 10_000,
                                  "prompt_start_max_chars": 10,
                                  "prompt_continue_max_chars": 5})
        self.addCleanup(sandbox.cleanup)

        def mcp(name: str, arguments: dict) -> dict:
            result = sandbox.mcp("codex", [{
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }])
            return result[0]["result"]["structuredContent"]

        prompt = "secret-start-prompt"
        start = mcp("claude_start", {"prompt": prompt, "source_classification": "internal"})
        self.assertEqual(start["error_category"], ErrorCategory.INPUT_TOO_LARGE.value)
        self.assertEqual({key: start[key] for key in ("limit_chars", "actual_chars", "operation")}, {
            "limit_chars": 10, "actual_chars": len(prompt), "operation": "start",
        })
        self.assertNotIn(prompt, str(start))
        self.assertEqual(start["error_hint"],
                         "start prompt is 19 characters; the limit is 10.")

        started, status, result = sandbox.run_to_completion("codex", prompt="hello")
        self.assertEqual(status["status"], "complete")
        self.assertTrue(result["ok"])
        prompt = "hidden"
        continued = mcp("claude_continue", {
            "conversation_id": started["conversation_id"], "prompt": prompt,
            "source_classification": "internal",
        })
        self.assertEqual(continued["error_category"], ErrorCategory.INPUT_TOO_LARGE.value)
        self.assertEqual({key: continued[key] for key in ("limit_chars", "actual_chars", "operation")}, {
            "limit_chars": 5, "actual_chars": len(prompt), "operation": "continue",
        })
        self.assertNotIn(prompt, str(continued))

    def test_envelope_limit_reports_full_envelope_size(self) -> None:
        sandbox = Sandbox(limits={"prompt_max_chars": 100, "prompt_start_max_chars": 99})
        self.addCleanup(sandbox.cleanup)
        prompt = "unique-envelope-prompt"
        started = sandbox.consult("codex", prompt=prompt)
        status = sandbox.wait(started["job_id"])
        result = broker.read(sandbox.cfg, "codex", {"job_id": started["job_id"]})
        self.assertEqual(status["error_category"], ErrorCategory.INPUT_TOO_LARGE.value)
        self.assertEqual({key: result[key] for key in ("limit_chars", "operation")}, {
            "limit_chars": 100, "operation": "start",
        })
        self.assertGreater(result["actual_chars"], result["limit_chars"])
        self.assertNotIn(prompt, str(result))

    def test_receipt_count_accepts_zero_but_not_negative(self) -> None:
        self.assertTrue(broker._receipt_count(0))
        self.assertFalse(broker._receipt_count(-1))

    def test_receipt_duration_accepts_zero_but_not_negative(self) -> None:
        self.assertTrue(broker._receipt_duration(0.0))
        self.assertFalse(broker._receipt_duration(-1.0))

    def test_receipt_hash_accepts_exact_lowercase_digest(self) -> None:
        self.assertTrue(broker._receipt_hash("a" * 64))
        self.assertFalse(broker._receipt_hash("a" * 63))

    def test_receipt_chunk_counts_counts_each_state(self) -> None:
        chunks = [{"status": "complete"}, {"status": "failed"}]
        self.assertEqual(broker._receipt_chunk_counts(chunks), {
            "pending": 0, "running": 0, "complete": 1, "failed": 1, "total": 2,
        })

    def test_receipt_pass_states_keeps_pass_order(self) -> None:
        chunks = [{"pass": 2, "status": "failed"}, {"pass": 1, "status": "complete"}]
        self.assertEqual([item["status"] for item in broker._receipt_pass_states(chunks)],
                         ["complete", "failed"])

    def test_validate_delegation_receipt_refuses_unknown_task(self) -> None:
        record = {key: None for key in broker.RECEIPT_FIELDS}
        record.update(version=2, contract=broker.RECEIPT_CONTRACT, invocation_id="a" * 32,
                      task="invented-task", chunk_counts=broker._receipt_chunk_counts([]),
                      chunks=[], passes=[], parser_repairs=0, triage_repairs=0,
                      duration_seconds=0.0, result_status="unavailable",
                      output_hash_kind="response_bodies", parser_status="not_run",
                      request_options=[], input_count=0, output_count=0,
                      human_review_required=True, semantic_coverage="unproven")
        with self.assertRaisesRegex(ValueError, "invalid receipt task"):
            broker._validate_delegation_receipt(record)

    def test_handoff_json_refuses_nonfinite_constant(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid handoff constant"):
            broker._handoff_json(b'{"amount":NaN}')

    def test_read_clearance_accepts_matching_digest(self) -> None:
        prep = redaction_handoff.Preparation(self.sandbox, "claude")
        self.assertEqual(broker._read_clearance(prep.args)["assembly"]["conversation_id"],
                         prep.cid)

    def test_initial_handoff_identity_returns_reviewed_identity(self) -> None:
        identity = str(uuid.uuid4())
        with mock.patch.object(broker, "_read_clearance", return_value={
            "assembly": {"conversation_id": identity},
        }):
            self.assertEqual(broker._initial_handoff_identity({}), identity)

    def test_validate_handoff_accepts_matching_preparation(self) -> None:
        prep = redaction_handoff.Preparation(self.sandbox, "claude")
        pinned = broker.validate_handoff(self.sandbox.cfg, prep.args, "claude", prep.cid,
                                         prep.session_id, resume=False)
        self.assertEqual(pinned["contract"], broker.PEER_HANDOFF_CONTRACT)

    def test_sqlite_uri_path_escapes_path_metacharacters(self) -> None:
        path = os.path.join(self.sandbox.root, "receipt%?#.sqlite3")
        self.assertTrue(broker._sqlite_uri_path(path).endswith("receipt%25%3F%23.sqlite3"))

    def test_consume_receipt_once_rejects_replay(self) -> None:
        self.assertTrue(broker._consume_receipt_once(self.sandbox.cfg, "invented-receipt", 100.0))
        self.assertFalse(broker._consume_receipt_once(self.sandbox.cfg, "invented-receipt", 101.0))

    def test_digest_receipt_routed_locally_accepts_fresh_local_receipt(self) -> None:
        queue = Path(self.sandbox.root) / "local-queue"
        queue.mkdir()
        with sqlite3.connect(queue / "routing.sqlite3") as db:
            db.execute("CREATE TABLE routing_receipts "
                       "(receipt_id TEXT, decision TEXT, created_at REAL)")
            db.execute("INSERT INTO routing_receipts VALUES (?, ?, ?)",
                       ("invented-receipt", "local", time.time()))
        self.sandbox.cfg.raw["local_first"]["local_queue_root"] = str(queue)
        self.assertTrue(broker._digest_receipt_routed_locally(
            self.sandbox.cfg, "invented-receipt"))

    def test_validate_local_first_checks_voluntary_shape(self) -> None:
        with self.assertRaises(BrokerError) as caught:
            broker._validate_local_first(self.sandbox.cfg, "short invented prompt",
                                         {"bypass": "invented-reason"})
        self.assertEqual(caught.exception.category, ErrorCategory.LOCAL_FIRST_REQUIRED)

    def test_validate_identifier_request_returns_canonical_id(self) -> None:
        identifier = str(uuid.uuid4())
        self.assertEqual(broker._validate_identifier_request({"job_id": identifier}, "job_id"),
                         identifier)

    def test_spawn_worker_returns_detached_process_pid(self) -> None:
        job_dir = Path(self.sandbox.root) / "invented-job"
        job_dir.mkdir()
        with mock.patch.object(broker.subprocess, "Popen", return_value=SimpleNamespace(pid=4321)):
            self.assertEqual(broker._spawn_worker(self.sandbox.cfg, str(job_dir)), 4321)

    def test_timeout_response_has_error_and_receipt(self) -> None:
        with mock.patch.object(broker.receipts, "build", return_value={"id": "invented-receipt"}):
            response = broker._timeout_response(self.sandbox.cfg, "claude", "invented prompt",
                                                time.monotonic())
        self.assertEqual(response["error_category"], ErrorCategory.PEER_TIMEOUT.value)
        self.assertEqual(response["receipt"], {"id": "invented-receipt"})

    def test_request_preflight_returns_timeout_response(self) -> None:
        marker = {"ok": False, "error_category": ErrorCategory.PEER_TIMEOUT.value}
        with (mock.patch.object(broker.preflight, "check_peer",
                                side_effect=BrokerError(ErrorCategory.PEER_TIMEOUT)),
              mock.patch.object(broker, "_timeout_response", return_value=marker)):
            self.assertIs(broker._request_preflight(
                self.sandbox.cfg, "claude", "invented prompt", time.monotonic()), marker)

    def test_admission_uses_remaining_deadline_as_gate_timeout(self) -> None:
        with (mock.patch.object(broker.time, "monotonic", return_value=100.0),
              mock.patch.object(broker.registry, "admission_gate") as gate):
            with broker._admission(self.sandbox.cfg, 105.0):
                pass
        self.assertEqual(gate.call_args.kwargs["timeout"], 5.0)

    def test_release_quietly_releases_owned_claim(self) -> None:
        identifier = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        with mock.patch.object(broker.registry, "release_conversation_slot") as release:
            broker._release_quietly(self.sandbox.cfg, identifier, job_id)
        release.assert_called_once_with(self.sandbox.cfg, identifier, job_id)

    def test_prepare_job_writes_request_and_deadline(self) -> None:
        conversation_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        args = {"prompt": "invented prompt", "source_classification": "internal"}
        broker._prepare_job(self.sandbox.cfg, "codex", "claude", conversation_id,
                            job_id, args, resume=False, budget_started=1000.0)
        request = store.read_json_atomic(os.path.join(self.sandbox.cfg.job_dir(job_id),
                                                      "request.json"))
        self.assertEqual(request["job_id"], job_id)
        self.assertEqual(request["deadline_monotonic"],
                         1000.0 + self.sandbox.cfg.request_timeout("claude"))


if __name__ == "__main__":
    unittest.main()
