"""Protocol tests for the standalone local queue MCP transport."""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq.server import INSTRUCTIONS, Server
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot


class Sampler:
    def sample(self):
        return ResourceSnapshot(100.0, "normal", "normal", True, 120.0)


class Service:
    def __init__(self, root):
        self.queue = LocalQueue(root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 100.0)
        self.calls = 0

    def once(self):
        self.calls += 1
        return self.queue.run_once("test-server")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = Service(self.temporary.name)
        self.server = Server(self.service, interval=0.01)

    def tearDown(self):
        self.server.stop()
        self.temporary.cleanup()

    def request(self, method, params=None, request_id=1):
        value = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            value["params"] = params
        return self.server.handle(value)

    def test_initialize_and_tools_are_local_only(self):
        init = self.request("initialize")["result"]
        self.assertIn("Local mechanical", init["instructions"])
        self.assertEqual(init["instructions"], INSTRUCTIONS)
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"local_submit", "local_status", "local_result", "local_cancel", "local_feedback", "local_route", "local_route_receipt"})

    def test_submit_status_and_stdio_lifecycle(self):
        args = {"task_type": "summarize", "input": "fixture", "params": {}, "priority": "interactive",
                "classification": "synthetic", "caller": "codex", "purpose": "test"}
        submitted = self.request("tools/call", {"name": "local_submit", "arguments": args})["result"]["structuredContent"]
        self.assertTrue(submitted["ok"])
        status = self.request("tools/call", {"name": "local_status", "arguments": {"job_id": submitted["job_id"]}})
        self.assertEqual(status["result"]["structuredContent"]["status"], "queued")
        stdin = io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        stdout = io.StringIO()
        self.assertEqual(self.server.serve(stdin, stdout), 0)
        self.assertEqual(json.loads(stdout.getvalue())["id"], 7)
        self.assertIsNone(self.server._thread)
        self.assertGreaterEqual(self.service.calls, 1)

    def test_bad_requests_do_not_crash_server(self):
        self.assertEqual(self.server.handle([])["error"]["code"], -32600)
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"], -32601)
        wire = io.StringIO('not json\n')
        out = io.StringIO()
        self.server.serve(wire, out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
