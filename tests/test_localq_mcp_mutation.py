"""Offline contract tests for the local queue MCP adapter."""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import mcp
from agent_bridge.localq.spool import AdmissionError


def fail_with(error):
    def fail(**_args):
        raise error
    return fail


class McpQueueHandlerTests(unittest.TestCase):
    def test_submit_input_schema_has_exact_limit(self):
        tools = mcp.build_tools(SimpleNamespace())
        self.assertEqual(tools["local_submit"]["inputSchema"]["properties"]["input"]["maxLength"],
                         24000)

    def test_queue_handler_preserves_named_and_unnamed_errors(self):
        queue = SimpleNamespace(
            submit=fail_with(AdmissionError("invented admission refusal")),
            status=fail_with(TypeError()),
        )
        tools = mcp.build_tools(queue)
        self.assertEqual(tools["local_submit"]["handler"]({}),
                         {"ok": False, "error": "invented admission refusal"})
        self.assertEqual(tools["local_status"]["handler"]({}),
                         {"ok": False, "error": "TypeError"})


class McpIntakeHandlerTests(unittest.TestCase):
    def test_route_input_schema_has_exact_limit(self):
        tools = mcp.build_intake_tools(SimpleNamespace())
        self.assertEqual(tools["local_route"]["inputSchema"]["properties"]["input"]["maxLength"],
                         24000)

    def test_intake_handler_preserves_named_and_unnamed_errors(self):
        intake = SimpleNamespace(
            route=fail_with(AdmissionError("invented routing refusal")),
            receipt=fail_with(TypeError()),
        )
        tools = mcp.build_intake_tools(intake)
        self.assertEqual(tools["local_route"]["handler"]({}),
                         {"ok": False, "error": "invented routing refusal"})
        self.assertEqual(tools["local_route_receipt"]["handler"]({}),
                         {"ok": False, "error": "TypeError"})


if __name__ == "__main__":
    unittest.main()
