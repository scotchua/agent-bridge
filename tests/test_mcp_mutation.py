"""Offline regressions for orchestration MCP mutation survivors."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge.capacity_router import StageRouter
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import AdmissionError, FakeBackend, LocalQueue, QueueCaps, ResourceSnapshot
from agent_bridge.orchestration import autoroute, mcp


class Sampler:
    def sample(self):
        return ResourceSnapshot(100.0, "normal", "normal", True, 120.0)


class MCPMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = LocalQueue(self.root / "queue", sampler=Sampler(), backend=FakeBackend(),
                                clock=lambda: 100.0)
        self.router = StageRouter(self.root / "router.sqlite3", clock=lambda: 100.0)
        self.intake = AutomaticIntake(self.queue)

    def tools(self, *, state_root=None, execution=None):
        return mcp.build_tools("codex", self.router, self.queue, self.intake,
                               execution=execution, state_root=state_root)

    def test_schema_disallows_unknown_properties(self):
        self.assertIs(mcp._schema({"account": {"type": "string"}}, ["account"])
                      ["additionalProperties"], False)

    def test_commit_rejects_invalid_revision(self):
        self.assertIsNone(mcp._commit(str(self.root), "missing-revision"))

    def test_route_local_rejects_checkpoint_and_idempotency_key_together(self):
        result = self.tools()["work_route_local"]["handler"]({
            "task_type": "summarize", "input": "Invented account entry\n" * 50,
            "priority": "interactive", "classification": "synthetic",
            "checkpoint_id": "checkpoint-1", "idempotency_key": "request-1"})
        self.assertEqual(result, {"ok": False, "error": "checkpoint_idempotency_conflict"})

    def test_digest_file_rejects_symlink_before_policy_lookup(self):
        target = self.root / "invented.log"
        target.write_text("Invented ledger line\n", encoding="utf-8")
        link = self.root / "linked.log"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        result = self.tools(state_root=str(self.root / "state"))["work_digest_file"]["handler"](
            {"path": str(link), "task_type": "summarize"})
        self.assertEqual(result, {"ok": False, "error": "digest_path_refused:symlink"})

    def test_certified_file_accepts_exact_input_cap(self):
        repo = self.root / "invented-repo"
        (repo / ".git").mkdir(parents=True)
        target = repo / "records.txt"
        target.write_bytes(b"A" * 100)
        state = self.root / "state"
        policy_path = Path(autoroute.policy_path(str(state)))
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text(json.dumps({"version": 1, "local_first": {"enabled": True},
            "repos": {str(repo): {"classification": "synthetic", "allowed_routes": ["local"],
                                   "mechanical_ok": True, "extract_globs": ["**/*.txt"]}}}),
            encoding="utf-8")
        queue = LocalQueue(self.root / "certified", sampler=Sampler(), backend=FakeBackend(),
                           caps=QueueCaps(max_input_bytes=100), backend_id="gemma_certified",
                           allowed_task_types=frozenset({"summarize", "extract"}))
        intake = AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1))
        tools = mcp.build_tools("codex", self.router, queue, intake, state_root=str(state))
        with mock.patch.object(intake, "checkpoint", side_effect=AdmissionError("boundary_reached")) as checkpoint:
            result = tools["work_extract_file"]["handler"]({"path": str(target)})
        checkpoint.assert_called_once()
        self.assertEqual(result, {"ok": False, "error": "boundary_reached"})

    def test_work_result_at_character_limit_is_not_truncated(self):
        state = str(self.root / "state")
        payload = {"ready": True, "result": {"output": {"text": "ABCDE"}}}
        with mock.patch.object(self.queue, "result", return_value=payload), \
             mock.patch.object(mcp.localfirst, "digest_job_max_output_chars", return_value=5):
            result = self.tools(state_root=state)["work_result"]["handler"]({"job_id": "job-1"})
        self.assertEqual(result["result"]["output"]["text"], "ABCDE")
        self.assertIs(result["result"]["output"]["output_truncated"], False)

    def test_review_dispatch_refuses_same_author_and_reviewer(self):
        current = {"state": "owned", "owner_id": "owner-1", "owner_route": "codex",
                   "revision": 1, "author_route": "codex"}
        router = SimpleNamespace(get=lambda *args: current)
        execution = SimpleNamespace(client_derived_routes=frozenset())
        tools = mcp.build_tools("claude", router, self.queue, self.intake, execution=execution,
                                state_root=str(self.root / "state"))
        args = {"item_id": "item-1", "stage": "review", "owner_id": "owner-1",
                "stage_revision": 1, "provider": "codex", "repo": str(self.root),
                "base": "base", "head": "head", "model": "invented-model"}
        with mock.patch.object(mcp, "_commit", return_value="a" * 40), \
             mock.patch.object(mcp.codex_review, "compute_range", return_value={}):
            result = tools["review_dispatch"]["handler"](args)
        self.assertEqual(result, {"ok": False,
                                  "error": "review_dispatch_refused:author_and_reviewer_same_provider"})

    def test_review_verify_requires_exactly_one_receipt_source(self):
        execution = SimpleNamespace(client_derived_routes=frozenset())
        tools = self.tools(execution=execution)
        result = tools["review_verify"]["handler"]({
            "repo": str(self.root), "base": "base", "head": "head",
            "dispositions": str(self.root / "dispositions.json")})
        self.assertEqual(result, {"ok": False, "error": "provide exactly one receipt or job_id"})


if __name__ == "__main__":
    unittest.main()
