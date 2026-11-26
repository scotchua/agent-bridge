"""Work-split reporting from isolated queues and synthetic execution receipts."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_bridge import local_status
from agent_bridge.capacity_router import StageRouter
from agent_bridge.localq.intake import AutomaticIntake
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot
from agent_bridge.orchestration.mcp import build_tools


class Sampler:
    def __init__(self, now):
        self.now = now

    def sample(self):
        return ResourceSnapshot(self.now, "normal", "normal", True, 10000.0)


class WorkSplitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = dt.datetime(2026, 11, 26, 12).timestamp()
        self.old = dt.datetime(2026, 11, 19).timestamp() - 1
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.queue = LocalQueue(self.root / "localq", sampler=Sampler(self.now), backend=FakeBackend(), clock=lambda: self.now)
        self.intake = AutomaticIntake(self.queue, clock=lambda: self.now)
        self.router = StageRouter(self.root / "stages.sqlite3")
        self.tools = build_tools("claude", self.router, self.queue, self.intake)
        self.executions = self.root / "executions"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"config_version": "1", "state_root": str(self.root / "state"),
            "local_queue_root": str(self.queue.root), "capacity_db": str(self.root / "capacity.db"),
            "worker_executable": str(self.root / "unused-worker"), "worker_state": str(self.root / "unused-state"),
            "execution_queue_root": str(self.executions), "codex_task_executable": str(self.root / "unused-codex"),
            "claude_task_executable": str(self.root / "unused-claude"), "python_executable": sys.executable}), encoding="utf-8")

    def checkpoint(self, label, task="summarize"):
        return self.tools["work_checkpoint"]["handler"]({"task_id": label, "task_type": task,
            "classification": "internal_nonclient", "input_bytes": 4, "nonblank_lines": 1})

    def execution(self, number, *, at=None, provider="codex", kind="execution", state="complete"):
        directory = self.executions / f"{number:032x}"
        directory.mkdir(parents=True)
        (directory / "request.json").write_text(json.dumps({"submitted_at": self.now if at is None else at,
            "provider": provider, "kind": kind, "brief": "SECRET BRIEF"}), encoding="utf-8")
        (directory / "receipt.json").write_text(json.dumps({"state": state, "output": "SECRET OUTPUT"}), encoding="utf-8")

    def report(self):
        return local_status.report(str(self.config), days=7, now=self.now)["work_split"]

    def test_counts_before_and_after_real_checkpoint_route_feedback_and_waiver(self):
        before = self.report()
        self.assertEqual(before["local_jobs"], [])
        skipped = self.checkpoint("pending-or-skipped", "log_triage")
        self.assertTrue(skipped["ok"])
        self.assertEqual(sum(row["count"] for row in self.report()["unaccounted_chores"]), 1)
        waived = self.tools["work_chore_waiver"]["handler"]({"checkpoint_id": skipped["checkpoint_id"], "reason": "local_unavailable"})
        self.assertTrue(waived["ok"])
        for index, (task, observation) in enumerate((("log_triage", "used"), ("summarize", "discarded"), ("summarize", None))):
            checkpoint = self.checkpoint(f"job-{index}", task)
            routed = self.tools["work_route_local"]["handler"]({"task_type": task, "input": f"{index:04}",
                "priority": "interactive", "classification": "internal_nonclient", "checkpoint_id": checkpoint["checkpoint_id"]})
            self.assertTrue(routed["ok"], routed)
            self.queue.run_once("offline-worker")
            if observation:
                self.queue.feedback(routed["job_id"], observation)
        self.execution(1)
        self.execution(2, state="failed")
        self.execution(3, at=self.old)
        self.execution(4, at=self.now + 1)
        self.execution(5, kind="review")
        self.execution(6, provider="claude")
        after = self.report()
        self.assertEqual(sum(row["count"] for row in after["local_jobs"]), 3)
        self.assertEqual({row["feedback"] for row in after["local_jobs"]}, {"used", "discarded", "unreviewed"})
        self.assertEqual(after["codex_execution_jobs"], [{"state": "complete", "count": 1}, {"state": "failed", "count": 1}])
        self.assertEqual(after["chore_waivers"], [{"caller": "claude", "task_type": "log_triage", "reason": "local_unavailable", "count": 1}])
        self.assertEqual(after["unaccounted_chores"], [])
        self.assertNotIn("SECRET", json.dumps(after))

    def test_waivers_and_pending_are_caller_bound_and_separately_counted(self):
        checkpoint = self.checkpoint("cloud-summary")
        codex = build_tools("codex", self.router, self.queue, self.intake)
        wrong = codex["work_chore_waiver"]["handler"]({"checkpoint_id": checkpoint["checkpoint_id"], "reason": "local_failed", "caller": "claude"})
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["error"], "checkpoint_binding_mismatch")
        invalid = self.tools["work_chore_waiver"]["handler"]({"checkpoint_id": checkpoint["checkpoint_id"], "reason": "SECRET REASON"})
        self.assertEqual(invalid["error"], "chore_waiver_reason_invalid")
        self.assertEqual(self.report()["unaccounted_chores"], [{"caller": "claude", "task_type": "summarize", "status": "eligible", "count": 1}])

    def test_file_digest_keeps_its_floor_while_the_same_short_chore_routes(self):
        repo = (self.root / "repo").resolve()
        (repo / ".git").mkdir(parents=True)
        state = self.root / "state"
        (state / "routing").mkdir(parents=True)
        (state / "routing" / "routing-policy.json").write_text(json.dumps({
            "version": 1, "local_first": {"enabled": True},
            "repos": {str(repo): {"classification": "internal_nonclient",
                "allowed_routes": ["local"], "mechanical_ok": True,
                "mechanical_globs": ["**/*.log"]}},
        }), encoding="utf-8")
        tools = build_tools("claude", self.router, self.queue, self.intake, state_root=str(state))
        text = "short\n"
        target = repo / "tiny.log"
        target.write_text(text, encoding="utf-8")
        for task_type in ("summarize", "log_triage"):
            with self.subTest(task_type=task_type):
                args = {"path": str(target), "task_type": task_type}
                refused = tools["work_digest_file"]["handler"](args)
                self.assertTrue(refused["ok"], refused)
                self.assertEqual(refused["decision"], "refused")
                self.assertEqual(refused["reason"], "below_local_delegation_threshold")
                self.assertIsNone(refused["job_id"])
                repeated = tools["work_digest_file"]["handler"](args)
                self.assertEqual(repeated["receipt_id"], refused["receipt_id"])
                self.assertTrue(repeated["deduplicated"])
                routed = tools["work_route_local"]["handler"]({
                    "task_type": task_type, "input": text,
                    "classification": "internal_nonclient", "priority": "interactive"})
                self.assertTrue(routed["ok"], routed)
                self.assertEqual(routed["decision"], "local")
                self.assertIsNotNone(routed["job_id"])
                for label, content in (("bytes", "x" * self.intake.policy.byte_threshold),
                                       ("lines", "x\n" * self.intake.policy.min_nonblank_lines)):
                    eligible = repo / f"{task_type}-{label}.log"
                    eligible.write_text(content, encoding="utf-8")
                    digested = tools["work_digest_file"]["handler"]({
                        "path": str(eligible), "task_type": task_type})
                    self.assertTrue(digested["ok"], digested)
                    self.assertIsNotNone(digested["job_id"])

    def test_deterministic_text_readonly_and_window(self):
        self.execution(1, at=self.now)
        (self.executions / ("f" * 32)).mkdir()  # source preparation, not an admitted job
        self.checkpoint("unaccounted")
        # SQLite may maintain its shared-memory sidecar for a read-only WAL
        # connection. Compare durable data, not SQLite's coordination files.
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(("-shm", "-wal"))}
        outputs = []
        for _ in range(2):
            out = io.StringIO()
            with mock.patch.object(local_status.time, "time", return_value=self.now), contextlib.redirect_stdout(out):
                code = local_status.main(["--config", str(self.config), "--days", "7", "--work-split"])
            self.assertEqual(code, 0, out.getvalue())
            outputs.append(out.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("codex\tcomplete\t1", outputs[0])
        self.assertIn("claude\tsummarize\teligible\t1", outputs[0])
        self.assertNotIn("SECRET", outputs[0])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(("-shm", "-wal"))})

    def test_bad_execution_receipt_fails_visibly_without_leaking_content(self):
        self.execution(1, state="SECRET")
        out = io.StringIO()
        with mock.patch.object(local_status.time, "time", return_value=self.now), contextlib.redirect_stdout(out):
            self.assertEqual(local_status.main(["--config", str(self.config), "--json", "--work-split"]), 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "local_status:report_error"})


if __name__ == "__main__":
    unittest.main()
