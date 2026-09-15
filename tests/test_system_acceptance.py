"""Synthetic acceptance checks across the additive routing components.

These tests deliberately use no live model, account, or assistant configuration.
They prove durable handoff semantics and tool-surface compatibility with fake
local execution.  Provider execution harnesses have their own containment tests.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import CapacityObservation, RoutingError, StageRouter
from agent_bridge.config import Config
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.server import Server as LocalServer
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot
from agent_bridge.mcp_server import Server as ConsultationServer


class Sampler:
    def sample(self):
        return ResourceSnapshot(1000.0, "normal", "normal", True, 300.0, 0.0)


class LocalService:
    def __init__(self, root):
        self.queue = LocalQueue(
            root,
            sampler=Sampler(),
            backend=FakeBackend(lambda payload: {"draft": payload["input"].upper()}),
            clock=lambda: 1000.0,
        )

    def once(self):
        return self.queue.run_once("synthetic-acceptance")


def consultation_config(root):
    path = os.path.join(os.path.dirname(__file__), "..", "config", "broker.json")
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["state_root"] = os.path.join(root, "consultation-state")
    return Config(raw, os.path.join(root, "synthetic-config.json"))


class SystemAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.local_root = os.path.join(self.temp.name, "localq")
        self.service = LocalService(self.local_root)
        self.local_server = LocalServer(self.service, interval=0.01)
        self.intake = AutomaticIntake(
            self.service.queue,
            policy=IntakePolicy(min_input_chars=80, min_nonblank_lines=4),
            clock=lambda: 1000.0,
        )

    def tearDown(self):
        self.local_server.stop()
        self.temp.cleanup()

    def route(self, caller, **changes):
        args = {
            "task_type": "summarize",
            "input": "synthetic operations line\n" * 12,
            "params": {"instruction": "select facts"},
            "priority": "interactive",
            "classification": "internal_nonclient",
            "caller": caller,
            "purpose": "test",
            "risk_flags": [],
        }
        args.update(changes)
        return self.intake.route(**args)

    def test_both_callers_can_route_and_finish_eligible_local_work(self):
        for caller in ("codex", "claude"):
            with self.subTest(caller=caller):
                routed = self.route(caller, input=(f"{caller} synthetic line\n" * 12))
                self.assertEqual((routed["decision"], routed["fallback"]), ("local", "none"))
                completed = self.service.once()
                self.assertEqual(completed["status"], "complete")
                result = self.service.queue.result(routed["job_id"])
                self.assertEqual(result["caller"], caller)
                self.assertTrue(result["ready"])
                self.assertIn(caller.upper(), result["result"]["draft"])

    def test_refusals_are_terminal_routing_decisions_with_no_fallback(self):
        cases = (
            {"classification": "client"},
            {"risk_flags": ["confidential"]},
            {"risk_flags": ["professional_judgment"]},
            {"task_type": "tax_position"},
            {"input": "small"},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                changes.setdefault("input", (f"refusal {index} line\n" * 12))
                routed = self.route("codex", **changes)
                self.assertEqual(routed["decision"], "refused")
                self.assertEqual(routed["fallback"], "none")
                self.assertIsNone(routed["job_id"])
        self.assertEqual(self.service.queue.state_report()["counts"], {})

    def test_queue_and_receipts_survive_process_reconstruction(self):
        routed = self.route("claude")
        receipt_id, job_id = routed["receipt_id"], routed["job_id"]

        rebuilt_queue = LocalQueue(
            self.local_root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 1000.0
        )
        rebuilt_intake = AutomaticIntake(rebuilt_queue, clock=lambda: 1000.0)
        self.assertEqual(rebuilt_queue.status(job_id)["status"], "queued")
        self.assertEqual(rebuilt_intake.receipt(receipt_id)["job_id"], job_id)

    def test_stage_ownership_and_independent_review_survive_restart(self):
        path = os.path.join(self.temp.name, "stage-router.sqlite3")
        router = StageRouter(path, clock=lambda: 1000.0)
        for route in ("codex", "claude"):
            router.observe_capacity(CapacityObservation(route, 1000.0, 1100.0, True, "fixture"),
                                    trusted=True)
        router.register("artifact", "build", allowed_routes=["codex", "claude"],
                        preferred_routes=["codex", "claude"])
        owned = router.assign("artifact", "build", owner_id="codex-author",
                              lease_seconds=50, expected_revision=0)

        rebuilt = StageRouter(path, clock=lambda: 1000.0)
        self.assertEqual(rebuilt.get("artifact", "build")["owner_id"], "codex-author")
        with self.assertRaisesRegex(RoutingError, "already_owned"):
            rebuilt.assign("artifact", "build", owner_id="claude-author",
                           lease_seconds=50, expected_revision=owned["revision"])
        rebuilt.register("artifact", "review", allowed_routes=["codex", "claude"],
                         preferred_routes=["codex", "claude"], is_review=True,
                         author_owner="codex-author", author_route="codex")
        review = rebuilt.assign("artifact", "review", owner_id="claude-reviewer",
                                lease_seconds=50, expected_revision=0)
        self.assertEqual(review["owner_route"], "claude")

    def test_consultation_surfaces_remain_directional_and_unmodified(self):
        cfg = consultation_config(self.temp.name)
        expected = {
            "codex": {"claude_start", "claude_continue", "claude_poll", "claude_read", "claude_close"},
            "claude": {"codex_start", "codex_continue", "codex_poll", "codex_read", "codex_close"},
        }
        for caller, names in expected.items():
            with self.subTest(caller=caller):
                server = ConsultationServer(caller, cfg)
                listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                actual = {tool["name"] for tool in listed["result"]["tools"]}
                self.assertEqual(actual, names)
                self.assertFalse(any(name.startswith("local_") for name in actual))

        local_list = self.local_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        local_names = {tool["name"] for tool in local_list}
        self.assertIn("local_route", local_names)
        self.assertFalse(any(name.startswith(("claude_", "codex_")) for name in local_names))


if __name__ == "__main__":
    unittest.main()
