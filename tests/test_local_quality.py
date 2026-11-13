"""Synthetic quality evidence; all files and assistant homes are disposable."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_bridge import drift, local_quality, local_status
from agent_bridge.capacity_router import StageRouter
from agent_bridge.localq import quality
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy, CLOSED_REFUSAL_REASONS, accepted_values
from agent_bridge.localq.spool import AdmissionError, FakeBackend, LocalQueue, MECHANICAL_TASKS, ResourceSnapshot
from agent_bridge.orchestration import autoroute, gate, localfirst, mcp, server as server_module
from agent_bridge.orchestration.server import Server


class QualityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        self.state = self.home / "state"
        sampler = SimpleNamespace(sample=lambda: ResourceSnapshot(self.now, "normal", "normal", True, 120))
        self.queue = LocalQueue(self.home / "queue", sampler=sampler, backend=FakeBackend(),
                                clock=lambda: self.now, state_root=self.state)
        self.intake = AutomaticIntake(self.queue, policy=IntakePolicy(min_input_chars=1), clock=lambda: self.now)
        self.config = self.home / "orchestration.json"
        self.config.write_text(json.dumps({"config_version": "1", "state_root": str(self.state),
            "local_queue_root": str(self.queue.root), "capacity_db": str(self.home / "capacity.db"),
            "worker_executable": str(self.home / "worker"), "worker_state": str(self.home / "worker-state")}))

    def rows(self, n=20, not_used=15, task="summarize", backend="private_worker", at=None):
        return [{"backend_id": backend, "task_type": task, "status": "complete", "purpose": "work",
                 "submitted_at": 0, "feedback_json": json.dumps({"recorded_at": self.now if at is None else at,
                    "outcome": "used" if i >= not_used else "reworked" if i % 2 else "discarded"})}
                for i in range(n)]

    def measure(self, rows, **kwargs):
        return quality.evaluate(rows, backend_id="private_worker", now=self.now, **kwargs)["summarize"]

    def seed(self, rows=None):
        rows = self.rows() if rows is None else rows
        with self.queue._db() as db:
            for i, row in enumerate(rows):
                db.execute("""INSERT INTO jobs(job_id,idem_key,content_key,task_type,priority,classification,
                    caller,purpose,params_json,input_blob,status,submitted_at,updated_at,feedback_json,backend_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (f"seed{i}", f"seed{i}", f"seed{i}", row["task_type"],
                    "interactive", "synthetic", "codex", row["purpose"], "{}", "SECRET", row["status"],
                    row["submitted_at"], self.now, row["feedback_json"], row["backend_id"]))

    def update(self):
        return quality.update(self.state, self.queue.db_path, self.queue.backend_id,
                              self.queue.allowed_task_types, now=self.now)

    def checkpoint(self, task="summarize", key="test"):
        return self.intake.checkpoint(task_id=key, task_type=task, classification="synthetic",
                    caller="codex", input_bytes=5, nonblank_lines=1)

    def route(self, cp, task="summarize"):
        return self.intake.route(task_type=task, input="hello", params={}, priority="interactive",
            classification="synthetic", caller="codex", purpose="work", checkpoint_id=cp["checkpoint_id"])

    def disable(self):
        self.seed()
        self.update()

    def test_sample_floor_and_wilson_boundary(self):
        for n in (0, 1, 19):
            row = quality.evaluate(self.rows(n, n), backend_id="private_worker", now=self.now,
                                   task_types=["summarize"])["summarize"]
            self.assertEqual(row["verdict"], "insufficient data")
            self.assertIsNone(row["disabled_at"])
        row = self.measure(self.rows())
        self.assertEqual(row["n"], 20)
        self.assertEqual(row["used_rate"], .25)
        self.assertGreater(row["lower_bound"], .5)
        self.assertEqual(row["verdict"], "below the quality bar")
        row = self.measure(self.rows(not_used=11))
        self.assertLess(row["lower_bound"], .5)
        self.assertIsNone(row["disabled_at"])

    def test_window_uses_recorded_at_and_only_completed_work(self):
        # Old submissions with fresh reviews count; fresh submissions with
        # stale reviews, test jobs, incomplete jobs and future reviews do not.
        rows = self.rows()
        for at in (self.now - quality.WINDOW_SECONDS - 1, self.now + 1):
            rows += [{**r, "submitted_at": self.now} for r in self.rows(at=at)]
        rows += [{**r, "purpose": "test"} for r in self.rows()]
        rows += [{**r, "status": "queued"} for r in self.rows()]
        self.assertEqual(self.measure(rows)["n"], 20)
        self.assertEqual(self.measure(self.rows(at=self.now-quality.WINDOW_SECONDS))["n"], 20)

    def test_malformed_feedback_is_not_reviewed(self):
        rows = self.rows(19, 19)
        rows += [{**self.rows(1)[0], "feedback_json": value}
                 for value in ('{broken', '[]', 'null', '{"outcome": [], "recorded_at": 1}',
                               '[' * 2000)]
        measured = self.measure(rows)
        self.assertEqual(measured["n"], 19)
        self.assertEqual(measured["not_used"], 19)
        self.assertEqual(measured["verdict"], "insufficient data")
        self.assertIsNone(measured["disabled_at"])

    def test_backend_and_task_scoping(self):
        self.seed(self.rows(backend="gemma_certified"))
        self.update()
        self.assertFalse(quality.disabled_types(self.state, "private_worker"))
        quality.update(self.state, self.queue.db_path, "gemma_certified", ["summarize"], now=self.now)
        self.assertEqual(quality.disabled_types(self.state, "gemma_certified"), {"summarize"})
        self.assertFalse(self.queue.quality_disabled("summarize"))
        self.assertFalse(self.queue.quality_disabled("extract"))

    def test_backend_evidence_names_the_executor_that_produced_it(self):
        created = self.route(self.checkpoint())
        self.queue.backend_id = "gemma_certified"
        self.queue.run_once("runner")
        self.queue.feedback(created["job_id"], "used")
        with self.queue._db() as db:
            rows = quality.job_rows(db)
        self.assertEqual(rows[0]["backend_id"], "gemma_certified")
        result = quality.evaluate(rows, backend_id="gemma_certified", now=self.now)
        self.assertEqual(result["summarize"]["n"], 1)
        self.assertNotIn("summarize", quality.evaluate(rows, backend_id="private_worker", now=self.now))

    def test_lost_initialized_state_never_restores_routing(self):
        self.disable()
        quality.state_path(self.state).unlink()
        self.assertEqual(quality.disabled_types(self.state, "private_worker"), MECHANICAL_TASKS)
        self.assertEqual(self.checkpoint()["reason"], "quality_disabled")
        with self.assertRaisesRegex(ValueError, "quality_state_invalid"):
            self.update()
        self.assertFalse(quality.state_path(self.state).exists())

    def test_interrupted_first_update_recovers_despite_abandoned_lock(self):
        self.seed()
        faults = [("database", mock.patch.object(quality, "job_rows", side_effect=sqlite3.OperationalError)),
                  ("write", mock.patch.object(quality.store, "atomic_write_json", side_effect=OSError("disk full")))]
        for name, fault in faults:
            with self.subTest(fault=name):
                root = self.state / name
                path = quality.state_path(root)
                with fault, self.assertRaises((sqlite3.OperationalError, OSError)):
                    quality.update(root, self.queue.db_path, "private_worker", MECHANICAL_TASKS, now=self.now)
                self.assertTrue(path.with_suffix(".lock").exists())
                self.assertFalse(path.exists())
                self.assertFalse(path.with_suffix(".initialized").exists())
                self.assertEqual(quality.read_state(root), {})
                self.assertFalse(quality.disabled_types(root, "private_worker"))
                quality.update(root, self.queue.db_path, "private_worker", MECHANICAL_TASKS, now=self.now)
                self.assertTrue(path.with_suffix(".initialized").exists())
                self.assertEqual(quality.disabled_types(root, "private_worker"), {"summarize"})

    def test_windows_missing_state_does_not_enter_atomic_read_retry(self):
        with mock.patch.object(quality.store, "WINDOWS", True), \
             mock.patch.object(quality.store, "read_json_atomic", wraps=quality.store.read_json) as read:
            self.assertEqual(quality.read_state(self.state), {})
            self.assertFalse(self.state.exists())
            self.disable()
            self.assertEqual(quality.disabled_types(self.state, "private_worker"), {"summarize"})
            self.assertTrue(read.called)
            read.reset_mock()
            quality.state_path(self.state).unlink()
            self.assertEqual(quality.disabled_types(self.state, "private_worker"), MECHANICAL_TASKS)
            read.assert_not_called()

    def test_corrupt_initialized_state_needs_operator_repair_then_enable(self):
        self.disable()
        path = quality.state_path(self.state)
        path.write_text("{broken")
        with self.assertRaises(ValueError):
            self.update()
        self.assertEqual(path.read_text(), "{broken")
        self.assertEqual(quality.disabled_types(self.state, "private_worker"), MECHANICAL_TASKS)
        self.assertIn("setup_bridge.py local-quality --repair", quality.attention(self.state, "private_worker")[0])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_quality.main(["--config", str(self.config), "--json", "--enable", "summarize"]), 1)
            self.assertEqual(local_quality.main(["--config", str(self.config), "--json", "--repair"]), 0)
        self.assertEqual(quality.disabled_types(self.state, "private_worker"), {"summarize"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_quality.main(["--config", str(self.config), "--enable", "summarize"]), 0)
        self.update()
        self.assertFalse(quality.disabled_types(self.state, "private_worker"))

    def test_repair_preserves_readable_sticky_stops_across_backends(self):
        self.disable()
        state = quality.read_state(self.state)
        original = state["private_worker"]["summarize"].copy()
        state["gemma_certified"] = {"extract": {**original, "disabled_at": self.now - 1}}
        # A bad sibling row invalidates the whole file; even a damaged row's
        # readable disabled_at must survive repair after its reviews age out.
        state["private_worker"]["classify"] = {"disabled_at": self.now - 2, "n": "broken"}
        state["private_worker"]["extract"] = {"broken": True}
        quality.state_path(self.state).write_text(json.dumps(state))
        self.now += quality.WINDOW_SECONDS + 1
        self.assertEqual(quality.disabled_types(self.state, "private_worker"), MECHANICAL_TASKS)
        quality.repair(self.state, self.queue.db_path, "private_worker", MECHANICAL_TASKS, now=self.now)
        repaired = quality.read_state(self.state)
        self.assertEqual(repaired["private_worker"]["summarize"], original)
        self.assertEqual(repaired["private_worker"]["classify"]["disabled_at"], original["disabled_at"] - 2)
        self.assertEqual(repaired["gemma_certified"]["extract"], state["gemma_certified"]["extract"])
        self.assertEqual(quality.disabled_types(self.state, "private_worker"), {"summarize", "classify"})
        self.assertEqual(quality.disabled_types(self.state, "gemma_certified"), {"extract"})

    def test_repair_missing_initialized_state_and_database_failure(self):
        self.disable()
        path = quality.state_path(self.state)
        path.unlink()
        with mock.patch.object(quality, "job_rows", side_effect=sqlite3.OperationalError), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(local_quality.main(["--config", str(self.config), "--json", "--repair"]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "local_quality:state_unavailable"})
        self.assertFalse(path.exists())
        self.assertEqual(quality.disabled_types(self.state, "private_worker"), MECHANICAL_TASKS)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_quality.main(["--config", str(self.config), "--repair"]), 0)
        self.assertEqual(quality.disabled_types(self.state, "private_worker"), {"summarize"})

    def test_sticky_disable_enable_and_fresh_evidence(self):
        self.disable()
        original = quality.read_state(self.state)["private_worker"]["summarize"]
        self.now += quality.WINDOW_SECONDS + 1
        self.update()
        self.assertEqual(quality.read_state(self.state)["private_worker"]["summarize"], original)
        quality.enable(self.state, "private_worker", "summarize", now=self.now)
        self.update()
        self.assertFalse(self.queue.quality_disabled("summarize"))
        before = self.now
        self.now += 1
        # At exactly reenabled_at is excluded; only strictly later reviews count.
        rows = self.rows(at=before) + self.rows(at=self.now)
        result = quality.evaluate(rows, backend_id="private_worker", now=self.now,
            previous=quality.read_state(self.state)["private_worker"])["summarize"]
        self.assertEqual(result["n"], 20)
        self.assertEqual(result["disabled_at"], self.now)
        with self.queue._db() as db:
            db.execute("UPDATE jobs SET feedback_json=?", (json.dumps({"outcome": "discarded", "recorded_at": self.now}),))
        self.update()
        self.assertTrue(self.queue.quality_disabled("summarize"))

    def test_intake_rechecks_bound_checkpoint_and_audit_counts(self):
        cp = self.checkpoint()
        self.disable()
        routed = self.route(cp)
        self.assertEqual(routed["reason"], "quality_disabled")
        self.assertIsNone(routed["job_id"])
        self.assertEqual(self.checkpoint(key="later")["reason"], "quality_disabled")
        self.assertIn("quality_disabled", CLOSED_REFUSAL_REASONS)
        self.assertIn("accepted_task_types", accepted_values("quality_disabled", MECHANICAL_TASKS))
        # Test/calibration receipts must not inflate either work count.
        test_route = self.intake.route(task_type="summarize", input="test route", params={},
            priority="interactive", classification="synthetic", caller="codex", purpose="test")
        self.assertEqual(test_route["reason"], "quality_disabled")
        audit = self.intake.audit()
        self.assertEqual(audit["refused_by_reason"]["quality_disabled"], 1)
        self.assertEqual(audit["route_refused_quality_disabled"], 1)
        self.assertEqual(audit["eligible"], 1)
        self.assertEqual(audit["dispatched"], 0)
        self.assertEqual(audit["total"], audit["eligible"] + audit["no_eligible_unit"]
                         + sum(audit["refused_by_reason"].values()))

    def test_submit_race_is_recorded_and_replay_returns_created_job(self):
        cp = self.checkpoint()
        with mock.patch.object(self.queue, "submit", side_effect=AdmissionError("quality_disabled")):
            routed = self.route(cp)
        self.assertEqual(routed["reason"], "quality_disabled")
        self.assertEqual(self.intake.audit()["refused_by_reason"]["quality_disabled"], 0)
        self.assertEqual(self.intake.audit()["route_refused_quality_disabled"], 1)
        cp2 = self.checkpoint(key="another")
        created = self.route(cp2)
        self.disable()
        replay = self.route(cp2)
        self.assertEqual(replay["job_id"], created["job_id"])
        self.assertTrue(replay["deduplicated"])
        with self.assertRaisesRegex(AdmissionError, "^quality_disabled$"):
            self.queue.submit(task_type="summarize", input="new input", params={}, priority="interactive",
                classification="synthetic", caller="codex", purpose="work")

    def test_queued_jobs_held_and_other_types_run_then_resume(self):
        job = self.route(self.checkpoint())
        other = self.queue.submit(task_type="extract", input="other", params={}, priority="interactive",
            classification="synthetic", caller="codex", purpose="test")
        self.disable()
        self.queue.run_once("runner")
        self.assertEqual(self.queue.status(job["job_id"])["error"], "quality_disabled:summarize")
        self.assertEqual(self.queue.status(job["job_id"])["status"], "queued")
        self.assertEqual(self.queue.status(other["job_id"])["status"], "complete")
        quality.enable(self.state, "private_worker", "summarize", now=self.now)
        self.queue.run_once("runner")
        self.assertEqual(self.queue.status(job["job_id"])["status"], "complete")

    def gate_fixture(self):
        repo = self.home / "repo"
        (repo / ".git").mkdir(parents=True)
        artifact = repo / "data.log"
        artifact.write_text("synthetic\n" * 1000)
        policy = autoroute.parse_policy({"version": 1, "local_first": {"enabled": True},
            "repos": {str(repo): {"classification": "synthetic", "mechanical_ok": True,
                "allowed_routes": ["local"], "mechanical_globs": ["*.log"], "extract_globs": ["*.log"]}}})
        return repo, artifact, policy

    def test_corrupt_and_unreadable_state_fail_closed_and_gate_waives(self):
        repo, artifact, policy = self.gate_fixture()
        path = quality.state_path(self.state)
        path.parent.mkdir(parents=True)
        for content in ("{broken", "[]", '{"private_worker":{"summarize":{}}}'):
            with self.subTest(content=content):
                path.write_text(content)
                self.assertEqual(self.checkpoint(key=content)["reason"], "quality_disabled")
                with self.assertRaisesRegex(AdmissionError, "quality_disabled"):
                    self.queue.submit(task_type="summarize", input=content, params={}, priority="interactive",
                        classification="synthetic", caller="codex", purpose="test")
                decision = gate._judge_read_path(str(artifact), "codex", policy=policy,
                    state_root=str(self.state), local_queue_root=str(self.queue.root),
                    worker_executable="", protected=(), clock=lambda: self.now, allowed_pilot_tasks=frozenset())
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.code, "local_first_waived")
                self.assertEqual(decision.extra["waiver_reason"], "quality_disabled:summarize")
        with mock.patch.object(quality, "read_state", side_effect=PermissionError):
            self.assertEqual(quality.disabled_types(self.state, "private_worker"), MECHANICAL_TASKS)

    def test_disabled_pilot_falls_back_and_inline_output_does_not_rewrite(self):
        repo, artifact, policy = self.gate_fixture()
        selected = localfirst.file_task_for_path(str(artifact), str(repo), policy.for_repo(str(repo)),
            policy.local_first, allowed_pilot_tasks=frozenset({"extract"}), disabled_tasks=frozenset({"extract"}))
        self.assertEqual(selected[0], "summarize")
        self.disable()
        self.assertEqual(gate.quality_disabled_tasks_from_config(str(self.config)), {"summarize"})
        policy_path = self.state / "routing" / "routing-policy.json"
        policy_path.write_text(json.dumps({"version": 1, "local_first": {"inline_output_router": True},
            "repos": {str(repo): {"classification": "synthetic", "allowed_routes": ["local"], "mechanical_ok": True}}}))
        self.assertIsNone(gate.inline_output_update("codex", {"tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "git status"}}, gate.Decision("allow", "ok", "ok"),
            state_root=str(self.state), config_path=str(self.config)))

    def test_file_tools_status_attention_and_operator_cli(self):
        self.disable()
        router = StageRouter(self.home / "stages.db")
        tools = mcp.build_tools("codex", router, self.queue, self.intake, state_root=str(self.state))
        for name, args in (("work_digest_file", {"path": "/synthetic.log", "task_type": "summarize"}),):
            result = tools[name]["handler"](args)
            self.assertEqual(result["reason"], "quality_disabled")
        self.assertEqual(tools["orchestration_status"]["handler"]({})["disabled_for_quality"], ["summarize"])
        message = "Local summarize jobs are below the quality bar (5/20 used); stopped routing them. Re-enable with setup_bridge.py local-quality --enable summarize"
        self.assertEqual(quality.attention(self.state, "private_worker"), [message])
        self.assertEqual(drift._quality_attention(str(self.config)), [message])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(local_quality.main(["--config", str(self.config), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["disabled_for_quality"], ["summarize"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_quality.main(["--config", str(self.config), "--enable", "summarize"]), 0)
        self.assertFalse(self.queue.quality_disabled("summarize"))

    def test_certified_file_refusals_and_satisfiable_summarize_fallback(self):
        self.queue.backend_id = "gemma_certified"
        self.seed(self.rows(task="extract", backend="gemma_certified")
                  + self.rows(task="classify", backend="gemma_certified"))
        self.update()
        repo, artifact, policy = self.gate_fixture()
        # The pilot path does not match the legacy summarize glob.
        artifact = artifact.rename(repo / "records.txt")
        document = {"version": 1, "local_first": {"enabled": True},
            "repos": {str(repo): {"classification": "synthetic", "allowed_routes": ["local"],
                "mechanical_ok": True, "extract_globs": ["*.txt"]}}}
        (self.state / "routing" / "routing-policy.json").write_text(json.dumps(document))
        policy = autoroute.parse_policy(document)
        with mock.patch.object(localfirst, "readiness", return_value=localfirst.Readiness(True, "ready", "ready")):
            decision = gate._judge_read_path(str(artifact), "codex", policy=policy,
                state_root=str(self.state), local_queue_root=str(self.queue.root), worker_executable="",
                protected=(), clock=lambda: self.now, allowed_pilot_tasks=frozenset({"extract"}),
                disabled_tasks=quality.disabled_types(self.state, "gemma_certified"))
        self.assertEqual(decision.code, "local_digest_required")
        self.assertEqual(decision.extra["task_type"], "summarize")
        tools = mcp.build_tools("codex", StageRouter(self.home / "stages.db"), self.queue,
                                self.intake, state_root=str(self.state))
        for name in ("work_extract_file", "work_classify_file"):
            refused = tools[name]["handler"]({"path": str(artifact)})
            self.assertEqual(refused["reason"], "quality_disabled")
        digest = tools["work_digest_file"]["handler"]({"path": str(artifact), "task_type": "summarize"})
        self.assertTrue(digest["ok"], digest)
        self.assertIsNotNone(digest["job_id"])

    def test_local_status_quality_is_readonly_and_content_free(self):
        self.disable()
        before = quality.state_path(self.state).read_bytes()
        result = local_status.report(str(self.config), now=self.now)
        row = next(row for row in result["quality"] if row["task_type"] == "summarize")
        self.assertEqual((row["n"], row["used_rate"], row["verdict"]), (20, .25, "below the quality bar"))
        self.assertEqual(result["disabled_for_quality"], ["summarize"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(before, quality.state_path(self.state).read_bytes())
        with contextlib.closing(local_status._connect(self.queue.db_path)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM jobs")

    def test_local_status_pending_crossing_waits_for_persisted_disable(self):
        self.seed()
        path = quality.state_path(self.state)
        for initialized in (False, True):
            with self.subTest(initialized=initialized):
                if initialized:
                    quality.enable(self.state, "private_worker", "summarize", now=self.now - 1)
                before = path.read_bytes() if path.exists() else None
                result = local_status.report(str(self.config), now=self.now)
                row = next(row for row in result["quality"] if row["task_type"] == "summarize")
                self.assertEqual((row["n"], row["used_rate"], row["verdict"]), (20, .25, "pending evaluation"))
                self.assertIsNone(row["disabled_at"])
                self.assertEqual(result["disabled_for_quality"], [])
                self.assertEqual(path.read_bytes() if path.exists() else None, before)
        self.update()
        result = local_status.report(str(self.config), now=self.now)
        row = next(row for row in result["quality"] if row["task_type"] == "summarize")
        self.assertEqual(row["verdict"], "below the quality bar")
        self.assertEqual(row["disabled_at"], self.now)
        self.assertEqual(result["disabled_for_quality"], ["summarize"])

    def test_backend_column_migration_tolerates_concurrent_alter_only(self):
        for competing_writer in (True, False):
            with self.subTest(competing_writer=competing_writer), self.queue._db() as db:
                pragmas = []
                alters = []
                def execute(sql):
                    if sql == "PRAGMA table_info(jobs)":
                        pragmas.append(sql)
                        rows = list(db.execute(sql))
                        if len(pragmas) == 1 or not competing_writer:
                            return [row for row in rows if row[1] != "backend_id"]
                        return rows
                    if sql.startswith("ALTER TABLE"):
                        alters.append(sql)
                        if not competing_writer:
                            raise sqlite3.OperationalError("database is locked")
                        # The other starter already added the real column.
                        # Exercise SQLite's actual duplicate-column error.
                    return db.execute(sql)
                connection = SimpleNamespace(execute=execute)
                with mock.patch.object(self.queue, "_db", return_value=contextlib.nullcontext(connection)):
                    if competing_writer:
                        self.queue._init_db()
                    else:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                            self.queue._init_db()
                self.assertEqual(len(pragmas), 2)
                self.assertEqual(len(alters), 1)

    def run_executor_fault(self, fault):
        self.seed([{**row, "feedback_json": "{broken" if fault == "feedback" else row["feedback_json"]}
                   for row in self.rows()])
        if fault == "state":
            self.update()
            quality.state_path(self.state).write_text("{broken")
        service = SimpleNamespace(queue=self.queue, once=mock.Mock(), record_loop_error=mock.Mock())
        real_lock = quality.store.file_lock
        real_connect = sqlite3.connect
        def lock(path, **kwargs):
            if fault == "lock" and path == str(quality.state_path(self.state).with_suffix(".lock")):
                raise TimeoutError("quality lock timeout")
            return real_lock(path, **kwargs)
        def connect(path, **kwargs):
            if fault == "database" and str(path).endswith("?mode=ro"):
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(path, **kwargs)
        def serve(server):
            self.addCleanup(server.stop)
            passes = []
            def wait(interval):
                passes.append(interval)
                if len(passes) == 2:
                    server._stop.set()
            server._stop.wait = wait
            server._executor()
            return 0
        with mock.patch.object(server_module.Service, "for_config", return_value=service), \
             mock.patch.object(Server, "serve", serve), \
             mock.patch.object(quality.store, "file_lock", side_effect=lock), \
             mock.patch.object(quality.sqlite3, "connect", side_effect=connect), \
             mock.patch.object(drift, "quick_check", return_value={"changed": [], "attention": ["dependency attention"]}) as check, \
             mock.patch.object(drift, "report_attention") as attention, \
             mock.patch.object(self.queue, "hold_quality", wraps=self.queue.hold_quality) as hold_quality, \
             mock.patch.object(self.queue, "hold_dependency", wraps=self.queue.hold_dependency) as hold_dependency:
            self.assertEqual(server_module.main(["--caller", "codex", "--config", str(self.config)]), 0)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(service.once.call_count, 2)
        self.assertEqual(hold_quality.call_count, 2)
        self.assertEqual(hold_dependency.call_args_list, [mock.call(None), mock.call(None)])
        self.assertEqual(attention.call_count, 2)
        self.assertIn("dependency attention", attention.call_args.args[1]["attention"])
        if fault == "feedback":
            service.record_loop_error.assert_not_called()
            self.assertEqual(quality.read_state(self.state)["private_worker"]["summarize"]["n"], 0)
        else:
            self.assertEqual(service.record_loop_error.call_count, 2)
        return attention

    def test_executor_corrupt_state_keeps_dependency_checks_and_once_running(self):
        attention = self.run_executor_fault("state")
        lines = attention.call_args.args[1]["attention"]
        self.assertTrue(any("quality state is unavailable" in line for line in lines), lines)
        self.assertTrue(any("setup_bridge.py local-quality --repair" in line for line in lines), lines)

    def test_executor_lock_timeout_keeps_dependency_checks_and_once_running(self):
        self.run_executor_fault("lock")

    def test_executor_malformed_feedback_keeps_dependency_checks_and_once_running(self):
        self.run_executor_fault("feedback")

    def test_executor_database_open_failure_keeps_dependency_checks_and_once_running(self):
        self.run_executor_fault("database")

    def test_executor_evaluates_before_running_and_reports_attention(self):
        self.seed()
        queued = self.route(self.checkpoint())
        service = SimpleNamespace(queue=self.queue)
        server = Server("codex", service, StageRouter(self.home / "stages.db"), state_root=str(self.state))
        self.addCleanup(server.stop)
        observed = []
        def once():
            observed.append(self.queue.status(queued["job_id"]))
            server._stop.set()
        service.once = once
        server._executor()
        self.assertEqual(observed[0]["error"], "quality_disabled:summarize")
        self.assertEqual(observed[0]["status"], "queued")
        self.assertTrue(self.queue.quality_disabled("summarize"))


if __name__ == "__main__":
    unittest.main()
