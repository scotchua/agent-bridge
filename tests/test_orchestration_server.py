"""Additive orchestration MCP integration tests."""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import StageRouter
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot
from agent_bridge.mcp_server import build_tools as build_consultation_tools
from agent_bridge.orchestration.config import OrchestrationConfigError, load
from agent_bridge.orchestration.server import Server


class Sampler:
    def sample(self):
        return ResourceSnapshot(100.0, "normal", "normal", True, 120.0)


class Service:
    def __init__(self, root):
        self.queue = LocalQueue(root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 100.0)
        self.calls = 0

    def once(self):
        self.calls += 1
        return self.queue.run_once("integration-test")


class OrchestrationServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(os.path.join(self.temp.name, "queue"))
        self.router = StageRouter(os.path.join(self.temp.name, "capacity.sqlite3"), clock=lambda: 100.0)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def request(server, name, arguments):
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
        return reply["result"]["structuredContent"]

    def test_both_caller_modes_get_same_tools_and_bound_provenance(self):
        names = None
        for caller in ("claude", "codex"):
            server = Server(caller, self.service, self.router, interval=0.01)
            tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
            current = {tool["name"] for tool in tools}
            names = current if names is None else names
            self.assertEqual(current, names)
            route_schema = next(tool for tool in tools if tool["name"] == "work_route_local")["inputSchema"]
            self.assertNotIn("caller", route_schema["properties"])
            routed = self.request(server, "work_route_local", {
                "task_type": "summarize", "input": "synthetic line\n" * 80,
                "priority": "interactive", "classification": "synthetic",
                "purpose": "test", "idempotency_key": "request-" + caller,
            })
            self.assertTrue(routed["ok"])
            self.assertEqual(routed["caller"], caller)
        self.assertIn("stage_claim", names)
        self.assertIn("orchestration_status", names)

    def test_consultation_server_remains_unchanged_and_separate(self):
        self.assertEqual(set(build_consultation_tools("codex")), {
            "claude_start", "claude_continue", "claude_poll", "claude_read", "claude_close",
        })
        self.assertEqual(set(build_consultation_tools("claude")), {
            "codex_start", "codex_continue", "codex_poll", "codex_read", "codex_close",
        })
        self.assertFalse(set(build_consultation_tools("codex")) & {
            "work_route_local", "stage_claim", "orchestration_status",
        })

    def test_caller_provenance_cannot_be_spoofed(self):
        server = Server("codex", self.service, self.router, interval=0.01)
        routed = server.tools["work_route_local"]["handler"]({
            "task_type": "summarize", "input": "synthetic line\n" * 80,
            "priority": "interactive", "classification": "synthetic",
            "purpose": "test", "caller": "claude",
        })
        self.assertTrue(routed["ok"])
        self.assertEqual(routed["caller"], "codex")

    def test_capacity_register_claim_status_and_complete(self):
        server = Server("codex", self.service, self.router, interval=0.01)
        observed = self.request(server, "capacity_observe", {
            "route": "local", "observed_at": 100.0, "fresh_until": 200.0,
            "available": True, "source": "synthetic-test-feed",
        })
        self.assertTrue(observed["ok"])
        registered = self.request(server, "stage_register", {
            "item_id": "item-1", "stage": "extract",
            "allowed_routes": ["local", "codex"], "preferred_routes": ["local", "codex"],
        })
        claimed = self.request(server, "stage_claim", {
            "item_id": "item-1", "stage": "extract", "owner_id": "worker-1",
            "lease_seconds": 30, "expected_revision": registered["revision"],
        })
        self.assertEqual(claimed["owner_route"], "local")
        status = self.request(server, "stage_status", {"item_id": "item-1", "stage": "extract"})
        self.assertEqual(status["owner_id"], "worker-1")
        completed = self.request(server, "stage_complete", {
            "item_id": "item-1", "stage": "extract", "owner_id": "worker-1",
            "expected_revision": status["revision"],
        })
        self.assertEqual(completed["state"], "complete")

    def test_local_refusal_has_no_cloud_fallback_and_feedback_is_exposed(self):
        server = Server("claude", self.service, self.router, interval=0.01)
        routed = self.request(server, "work_route_local", {
            "task_type": "tax_position", "input": "synthetic line\n" * 80,
            "priority": "interactive", "classification": "synthetic", "purpose": "work",
        })
        self.assertEqual(routed["decision"], "refused")
        self.assertEqual(routed["fallback"], "none")
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertIn("work_feedback", {tool["name"] for tool in listed})

    def test_transport_starts_worker_and_stops_cleanly(self):
        server = Server("codex", self.service, self.router, interval=0.01)
        output = io.StringIO()
        self.assertEqual(server.serve(io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n'), output), 0)
        self.assertEqual(json.loads(output.getvalue())["id"], 7)
        self.assertIsNone(server._thread)
        self.assertGreaterEqual(self.service.calls, 1)

    def test_config_is_strict_and_requires_absolute_paths(self):
        config_path = os.path.join(self.temp.name, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"config_version": "1", "state_root": "relative",
                       "local_queue_root": "/tmp/q", "capacity_db": "/tmp/c.sqlite3",
                       "worker_executable": "/tmp/worker", "worker_state": "/tmp/state"}, handle)
        with self.assertRaisesRegex(OrchestrationConfigError, "state_root_must_be_absolute"):
            load(config_path)

        valid = {"config_version": "1", "state_root": self.temp.name,
                 "local_queue_root": os.path.join(self.temp.name, "q"),
                 "capacity_db": os.path.join(self.temp.name, "c.sqlite3"),
                 "worker_executable": os.path.join(self.temp.name, "worker"),
                 "worker_state": os.path.join(self.temp.name, "worker-state")}
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(valid, handle)
        self.assertEqual(load(config_path).interval_seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
