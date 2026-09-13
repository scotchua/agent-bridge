"""Focused offline regressions for hostile MCP input and worker diagnostics."""
from __future__ import annotations

import io
import json
import os
import unittest
import uuid
from unittest import mock

from harness import Sandbox

from agent_bridge import envelope, mcp_server, registry, schema_validate, store, worker
from agent_bridge.errors import BrokerError, ErrorCategory


class InputSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def stage_worker_job(self) -> tuple[str, str, str]:
        job_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        job_dir = store.secure_mkdir(self.sandbox.cfg.job_dir(job_id))
        registry.create_conversation(self.sandbox.cfg, {
            "conversation_id": conversation_id,
            "caller": "codex",
            "peer": "claude",
            "active_job_id": job_id,
            "turns": 0,
            "closed": False,
        })
        store.atomic_write_json(os.path.join(job_dir, "request.json"), {
            "job_id": job_id,
            "conversation_id": conversation_id,
            "caller": "codex",
            "peer": "claude",
            "prompt": "Return a valid response.",
            "source_classification": "internal",
            "resume": False,
            "config_path": self.sandbox.config_path,
        })
        return job_id, conversation_id, job_dir

    def test_unknown_key_never_reaches_corrective_prompt(self) -> None:
        canary = "UNTRUSTED_KEY_MUST_NOT_REENTER_PROMPT"
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"known": {"type": "string"}},
        }
        violations = schema_validate.validate({"known": "ok", canary: "value"}, schema)
        corrective = envelope.build_corrective("peer_output_schema_invalid", violations, schema, "1")

        self.assertTrue(violations)
        self.assertNotIn(canary, "\n".join(violations))
        self.assertNotIn(canary, corrective)
        self.assertIn("$: unexpected property", violations)

    def test_huge_unexpected_object_stops_at_violation_limit(self) -> None:
        schema = {"type": "object", "additionalProperties": False}
        instance = {f"untrusted-{number}": None for number in range(50_000)}

        violations = schema_validate.validate(instance, schema)

        self.assertLessEqual(len(violations), schema_validate.MAX_VIOLATIONS + 1)
        self.assertTrue(all("untrusted-" not in violation for violation in violations))
        self.assertEqual(violations[-1], "further violations truncated")

    def test_overlong_array_does_not_walk_each_item_after_maxitems_failure(self) -> None:
        schema = {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "minLength": 1},
        }
        violations = schema_validate.validate([""] * 50_000, schema)
        self.assertEqual(violations, ["$: more than maxItems 2"])

    def test_deep_json_is_parse_error_and_following_ping_succeeds(self) -> None:
        server = mcp_server.Server("codex", self.sandbox.cfg)
        nested = "[" * 100_000 + "0" + "]" * 100_000
        stdin = io.StringIO(
            nested + "\n" + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
        )
        stdout = io.StringIO()

        self.assertEqual(server.serve(stdin, stdout), 0)
        frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(frames[0]["error"]["code"], -32700)
        self.assertEqual(frames[1]["id"], 7)
        self.assertEqual(frames[1]["result"], {})

    def test_oversized_frame_is_rejected_without_a_drain(self) -> None:
        class OneReadStream:
            def __init__(self) -> None:
                self.calls: list[int] = []

            def readline(self, size: int) -> str:
                self.calls.append(size)
                if len(self.calls) > 1:
                    raise AssertionError("server attempted to drain oversized input")
                return "x" * size

        server = mcp_server.Server("codex", self.sandbox.cfg)
        stdin = OneReadStream()
        stdout = io.StringIO()

        self.assertEqual(server.serve(stdin, stdout), 1)
        self.assertEqual(stdin.calls, [mcp_server.MAX_MCP_FRAME_CHARS + 1])
        frame = json.loads(stdout.getvalue())
        self.assertEqual(frame["error"]["code"], -32600)
        self.assertEqual(frame["error"]["message"], "request exceeds frame limit")

    def test_container_jsonrpc_id_is_rejected_without_echoing_it(self) -> None:
        server = mcp_server.Server("codex", self.sandbox.cfg)
        response = server.handle({"jsonrpc": "2.0", "id": {"untrusted": "id"}, "method": "ping"})
        self.assertEqual(response, {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32600, "message": "invalid request"},
        })

    def test_reconcile_reads_only_a_small_worker_log_tail(self) -> None:
        job_id = str(uuid.uuid4())
        job_dir = self.sandbox.cfg.job_dir(job_id)
        registry.write_status(self.sandbox.cfg, job_id, "running", worker_pid=12345)
        log_path = os.path.join(job_dir, "worker.log")
        with open(log_path, "wb") as handle:
            handle.write(
                b"SENSITIVE_PREFIX_MUST_NOT_LEAK" * 100_000
                + b"x" * registry.WORKER_LOG_TAIL_BYTES
                + b"\nVISIBLE_TAIL"
            )

        real_open = open
        reads: list[int] = []

        class ObservedLog:
            def __init__(self, handle) -> None:
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args) -> None:
                return self.handle.__exit__(*args)

            def read(self, size: int = -1) -> bytes:
                reads.append(size)
                return self.handle.read(size)

            def __getattr__(self, name):
                return getattr(self.handle, name)

        def observed_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if os.fspath(path) == log_path:
                return ObservedLog(handle)
            return handle

        with (mock.patch("builtins.open", side_effect=observed_open),
              mock.patch.object(registry, "pid_alive", return_value=False)):
            status = registry.reconcile(self.sandbox.cfg, job_id)

        self.assertEqual(reads, [registry.WORKER_LOG_TAIL_BYTES])
        tail = status["worker_liveness"]["worker_log_tail"]
        self.assertTrue(tail.endswith("VISIBLE_TAIL"))
        self.assertNotIn("SENSITIVE_PREFIX_MUST_NOT_LEAK", tail)

    def test_failed_conversation_release_is_recorded_in_the_ledger(self) -> None:
        _, _, job_dir = self.stage_worker_job()
        recorded: list[dict] = []
        append_ledger = store.append_ledger

        def observe_append(path: str, record: dict) -> None:
            recorded.append(dict(record))
            append_ledger(path, record)

        with (mock.patch.object(
                worker.registry,
                "release_conversation_slot",
                side_effect=BrokerError(ErrorCategory.CONVERSATION_NOT_OWNED),
              ),
              mock.patch.object(worker.store, "append_ledger", side_effect=observe_append)):
            self.assertEqual(worker.execute(job_dir), 0)

        self.assertEqual(recorded[-1]["markers_retained"],
                         "conversation release did not succeed")

    def test_ledger_append_precedes_successful_marker_retirement(self) -> None:
        _, _, job_dir = self.stage_worker_job()
        events: list[str] = []
        append_ledger = store.append_ledger

        def observe_append(path: str, record: dict) -> None:
            events.append("append")
            append_ledger(path, record)

        def observe_retirement(cfg, job_id: str) -> list[str]:
            events.append("retire")
            return []

        with (mock.patch.object(worker.store, "append_ledger", side_effect=observe_append),
              mock.patch.object(worker.registry, "retire_attempt_markers",
                                side_effect=observe_retirement)):
            self.assertEqual(worker.execute(job_dir), 0)

        self.assertEqual(events, ["append", "retire"])


if __name__ == "__main__":
    unittest.main()
