"""Offline regression checks for the consultation MCP tool surface and transport."""
from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from agent_bridge import mcp_server, preflight, store
from agent_bridge.errors import BrokerError, ErrorCategory


class SchemaTests(unittest.TestCase):
    def test_isolation_notes_match_the_peer(self) -> None:
        self.assertIn("hard spend ceiling", mcp_server._isolation_note("claude"))
        self.assertIn("restricts writes, not reads", mcp_server._isolation_note("codex"))

    def test_local_first_schema_requires_a_known_bypass(self) -> None:
        schema = mcp_server._local_first_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["digest_receipt_id"]["minLength"], 1)
        self.assertEqual(schema["properties"]["bypass"]["enum"], [
            "needs_judgment", "not_mechanical", "local_unavailable",
        ])

    def test_continuation_schema_includes_conversation_id(self) -> None:
        schema = mcp_server._continue_schema("claude")
        self.assertIn("conversation_id", schema["required"])
        self.assertEqual(schema["properties"]["conversation_id"]["type"], "string")

    def test_job_schema_requires_job_id(self) -> None:
        schema = mcp_server._job_schema("poll")
        self.assertEqual(schema["required"], ["job_id"])
        self.assertEqual(schema["properties"]["job_id"]["type"], "string")

    def test_build_tools_advertises_only_allowed_classifications(self) -> None:
        cfg = mock.Mock()
        cfg.peer_allowed_classifications.return_value = ("internal", "synthetic")
        cfg.prompt_budget.side_effect = {"start": 80, "continue": 40}.get
        tools = mcp_server.build_tools("codex", cfg)
        schema = tools["claude_start"]["inputSchema"]
        self.assertEqual(schema["properties"]["source_classification"]["enum"],
                         ["internal", "synthetic"])
        cfg.peer_allowed_classifications.return_value = ("internal", "client_derived")
        schema = mcp_server.build_tools("codex", cfg)["claude_start"]["inputSchema"]
        self.assertIn("client-derived", schema["properties"]["source_classification"]["enum"])

    def test_build_tools_uses_default_prompt_budgets(self) -> None:
        tools = mcp_server.build_tools("claude")
        self.assertEqual(tools["codex_start"]["inputSchema"]["properties"]["prompt"]["maxLength"], 32000)
        self.assertEqual(tools["codex_continue"]["inputSchema"]["properties"]["prompt"]["maxLength"], 16000)


class ServerInitializationTests(unittest.TestCase):
    def test_unknown_caller_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            mcp_server.Server("unknown", object())


class CallToolTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = mock.Mock()
        cfg.peer_allowed_classifications.return_value = ("internal",)
        cfg.prompt_budget.side_effect = {"start": 80, "continue": 40}.get
        self.server = mcp_server.Server("codex", cfg)

    def test_unknown_tool_returns_input_error_envelope(self) -> None:
        result = self.server.call_tool({"name": "codex_start", "arguments": {}})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error_category"],
                         ErrorCategory.INPUT_SCHEMA_INVALID.value)
        self.assertIn("No such tool", result["structuredContent"]["error_hint"])

    def test_arguments_are_forwarded_to_the_handler(self) -> None:
        received = []

        def handler(cfg, caller, args):
            received.append((cfg, caller, args))
            return {"ok": True, "job_id": "invented-job"}

        self.server.tools["claude_poll"]["handler"] = handler
        arguments = {"job_id": "invented-job"}
        result = self.server.call_tool({"name": "claude_poll", "arguments": arguments})
        self.assertEqual(received, [(self.server.cfg, "codex", arguments)])
        self.assertFalse(result["isError"])

    def test_content_keeps_structured_and_text_payloads_equal(self) -> None:
        payload = {"ok": False, "message": "Invented account A"}
        result = self.server._content(payload, is_error=True)
        self.assertEqual(result["structuredContent"], payload)
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        self.assertTrue(result["isError"])


class RequestIdTests(unittest.TestCase):
    def test_string_request_ids_are_accepted(self) -> None:
        self.assertTrue(mcp_server.Server._valid_request_id("invented-request"))

    def test_finite_numeric_request_ids_are_accepted(self) -> None:
        valid = mcp_server.Server._valid_request_id
        self.assertTrue(valid(7))
        self.assertTrue(valid(7.5))
        self.assertFalse(valid(float("inf")))


class ServeTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = mock.Mock()
        cfg.peer_allowed_classifications.return_value = ("internal",)
        cfg.prompt_budget.side_effect = {"start": 80, "continue": 40}.get
        self.server = mcp_server.Server("codex", cfg)

    def serve(self, payload: str) -> tuple[int, list[object]]:
        output = io.StringIO()
        status = self.server.serve(io.StringIO(payload), output)
        return status, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_exact_frame_limit_is_accepted(self) -> None:
        payload = json.dumps({"id": 1, "method": "ping"})
        with mock.patch.object(mcp_server, "MAX_MCP_FRAME_CHARS", len(payload)):
            status, responses = self.serve(payload)
        self.assertEqual(status, 0)
        self.assertEqual(responses[0]["result"], {})

    def test_empty_batch_returns_invalid_request(self) -> None:
        status, responses = self.serve("[]\n")
        self.assertEqual(status, 0)
        self.assertEqual(responses[0]["error"]["code"], -32600)

    def test_batch_emits_only_responses(self) -> None:
        payload = json.dumps([
            {"method": "ping"},
            {"id": 2, "method": "ping"},
        ]) + "\n"
        status, responses = self.serve(payload)
        self.assertEqual(status, 0)
        self.assertEqual(len(responses), 1)
        self.assertEqual([frame["id"] for frame in responses[0]], [2])

    def test_handler_crash_becomes_internal_error(self) -> None:
        with mock.patch.object(self.server, "handle", side_effect=RuntimeError("invented fault")):
            status, responses = self.serve('{"id":3,"method":"ping"}\n')
        self.assertEqual(status, 0)
        self.assertEqual(responses[0]["error"]["code"], -32603)


class MainTests(unittest.TestCase):
    def test_preflight_refusal_returns_exit_code_two(self) -> None:
        cfg = SimpleNamespace(state=lambda name: f"/tmp/invented-{name}")
        refusal = BrokerError(ErrorCategory.INPUT_SCHEMA_INVALID)
        with (mock.patch.object(mcp_server.store, "set_umask"),
              mock.patch.object(mcp_server, "load_config", return_value=cfg),
              mock.patch.object(store, "secure_mkdir"),
              mock.patch.object(preflight, "assert_state_root_secure", side_effect=refusal),
              mock.patch.object(mcp_server.sys, "stderr", new_callable=io.StringIO)):
            self.assertEqual(mcp_server.main(["--caller", "codex"]), 2)


if __name__ == "__main__":
    unittest.main()
