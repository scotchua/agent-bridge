"""Offline regression checks for operator commands and retention boundaries."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import datetime as dt
import io
import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

from harness import Sandbox
from agent_bridge import admin, store
from agent_bridge.capacity_router import CapacityObservation, RoutingError, StageRouter


def output(call) -> tuple[object, str]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        result = call()
    return result, stream.getvalue()


class AdminMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def conversation(self, **fields) -> tuple[str, str]:
        cid = str(uuid.uuid4())
        path = self.sandbox.cfg.conversation_path(cid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        store.atomic_write_json(path, {"conversation_id": cid, "peer": "claude",
                                       "closed": False, **fields})
        return cid, path

    def ledger(self, *rows: dict) -> None:
        os.makedirs(os.path.dirname(self.sandbox.cfg.ledger_path), exist_ok=True)
        with open(self.sandbox.cfg.ledger_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def test_inside_rejects_root_and_accepts_child(self) -> None:
        root = self.sandbox.cfg.state_root
        self.assertFalse(admin._inside(root, root))
        self.assertTrue(admin._inside(root, os.path.join(root, "invented-company")))

    def test_symlink_components_and_unrelated_paths(self) -> None:
        root = self.sandbox.cfg.state_root
        child = os.path.join(root, "invented-account")
        os.mkdir(child)
        self.assertFalse(admin._has_symlink_component(root, child))
        self.assertTrue(admin._has_symlink_component(root, self.sandbox.root))
        link = os.path.join(root, "linked-account")
        try:
            os.symlink(child, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        self.assertTrue(admin._has_symlink_component(root, link))

    def test_age_days_uses_mtime_and_missing_status_fallback(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.sandbox.root) as directory:
            path = os.path.join(directory, "status.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            stamp = dt.datetime.now().timestamp() - 2 * 86400
            os.utime(path, (stamp, stamp))
            self.assertAlmostEqual(admin._age_days(path), 2.0, delta=0.01)
            self.assertAlmostEqual(admin._age_days(os.path.join(directory, "missing"), path),
                                   2.0, delta=0.01)

    def test_hold_age_uses_record_timestamp_and_legacy_mtime(self) -> None:
        path = os.path.join(self.sandbox.root, "hold.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        now = dt.datetime(2025, 4, 6, tzinfo=dt.timezone.utc).timestamp()
        stamp = dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).isoformat()
        self.assertAlmostEqual(admin._hold_age_seconds({"indeterminate_at": stamp}, path, now),
                               3600.0, delta=0.01)
        os.utime(path, (now - 7200, now - 7200))
        self.assertAlmostEqual(admin._hold_age_seconds({}, path, now), 7200.0, delta=0.01)

    def test_status_counts_jobs_and_open_conversations(self) -> None:
        jobs = self.sandbox.cfg.state("jobs")
        os.makedirs(os.path.join(jobs, "invented-job"))
        self.conversation(closed=True)
        self.conversation()
        self.conversation()
        self.ledger({"job_id": "invented-job-a"}, {"job_id": "invented-job-b"})
        code, text = output(lambda: admin.cmd_status(self.sandbox.cfg, argparse.Namespace()))
        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload["jobs_total"], 1)
        self.assertEqual(payload["conversations_total"], 3)
        self.assertEqual(payload["conversations_open"], 2)
        self.assertEqual(payload["ledger_records"], 2)

    def test_cleanup_includes_exact_retention_boundary(self) -> None:
        cfg = self.sandbox.cfg
        cfg.raw["retention"].update(job_retention_days=5, conversation_retention_days=5)
        job = os.path.join(cfg.state("jobs"), "invented-job")
        os.makedirs(job)
        store.atomic_write_json(os.path.join(job, "status.json"), {"status": "complete"})
        _, conversation = self.conversation()
        with mock.patch.object(admin, "_age_days", return_value=5.0):
            code, text = output(lambda: admin.cmd_cleanup(cfg, argparse.Namespace(apply=False)))
        self.assertEqual(code, 0)
        self.assertIn(f"would remove job: {job}", text)
        self.assertIn(f"would remove conversation: {conversation}", text)
        self.assertTrue(os.path.isdir(job))
        self.assertTrue(os.path.isfile(conversation))

    def test_cleanup_dry_run_never_deletes_and_apply_does(self) -> None:
        cfg = self.sandbox.cfg
        cfg.raw["retention"].update(job_retention_days=0, conversation_retention_days=0)
        _, path = self.conversation()
        with mock.patch.object(admin, "_age_days", return_value=1.0):
            output(lambda: admin.cmd_cleanup(cfg, argparse.Namespace(apply=False)))
            self.assertTrue(os.path.exists(path))
            output(lambda: admin.cmd_cleanup(cfg, argparse.Namespace(apply=True)))
        self.assertFalse(os.path.exists(path))

    def test_ledger_tail_and_full_shape(self) -> None:
        self.ledger(*({"job_id": f"invented-job-{i}", "status": "complete"}
                      for i in range(3)))
        args = argparse.Namespace(tail=2, full=False)
        code, text = output(lambda: admin.cmd_ledger(self.sandbox.cfg, args))
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([row["job_id"] for row in rows],
                         ["invented-job-1", "invented-job-2"])
        args.full = True
        _, text = output(lambda: admin.cmd_ledger(self.sandbox.cfg, args))
        self.assertEqual([row["job_id"] for row in json.loads(text)],
                         ["invented-job-1", "invented-job-2"])

    def test_archive_named_item_dry_run_then_apply(self) -> None:
        router = StageRouter(os.path.join(self.sandbox.root, "capacity.sqlite3"), clock=lambda: 1000.0)
        router.register("named", "build", allowed_routes=["codex"])
        args = argparse.Namespace(item=["named"], closed_tasks=None, stage=None,
                                  reason="fixture", apply=False)
        with mock.patch.object(admin, "_archive_router", return_value=router):
            code, text = output(lambda: admin.cmd_archive(self.sandbox.cfg, args))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["items"]["named"]["archived"], ["build"])
        self.assertIsNone(router.get("named", "build")["archived_at"])
        args.apply = True
        with mock.patch.object(admin, "_archive_router", return_value=router):
            code, _ = output(lambda: admin.cmd_archive(self.sandbox.cfg, args))
        self.assertEqual(code, 0)
        self.assertIsNotNone(router.get("named", "build")["archived_at"])

    def reconciliation_router(self) -> tuple[StageRouter, list[float]]:
        clock = [1000.0]
        router = StageRouter(os.path.join(self.sandbox.root, "capacity.sqlite3"),
                             clock=lambda: clock[0])
        router.observe_capacity(CapacityObservation(
            "codex", 1000.0, 1300.0, True, "test-feed"), trusted=True)
        return router, clock

    def expired_stage(self, router: StageRouter, clock: list[float]) -> dict:
        router.register("named", "build", allowed_routes=["codex"])
        router.assign("named", "build", owner_id="worker", lease_seconds=10,
                      expected_revision=0)
        clock[0] += 15
        with self.assertRaisesRegex(RoutingError, "liveness_reconciliation_required"):
            router.assign("named", "build", owner_id="second", lease_seconds=10,
                          expected_revision=1)
        return router.get("named", "build")

    def reconcile_command(self, router: StageRouter, *args: str) -> tuple[object, str]:
        with mock.patch.object(admin, "_archive_router", return_value=router), \
                mock.patch.object(admin, "load_config", return_value=self.sandbox.cfg):
            return output(lambda: admin.main(["stage-reconcile", *args]))

    def release_args(self) -> list[str]:
        return ["--item", "named", "--stage", "build", "--expected-revision", "2",
                "--owner-inactive", "--evidence", "Supervisor confirms worker exited"]

    def ledger_snapshot(self, router: StageRouter) -> list[str]:
        with router._db() as db:
            return list(db.iterdump())

    def test_stage_reconcile_empty_list(self) -> None:
        router, _ = self.reconciliation_router()
        before = self.ledger_snapshot(router)
        code, text = self.reconcile_command(router)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text), [])
        self.assertEqual(self.ledger_snapshot(router), before)

    def test_stage_reconcile_listing_is_content_free_and_read_only(self) -> None:
        router, clock = self.reconciliation_router()
        row = self.expired_stage(router, clock)
        router.register("other", "build", allowed_routes=["codex"])
        router.archive("other", actor="operator", reason="closed", apply=True)
        before = self.ledger_snapshot(router)
        code, text = self.reconcile_command(router)
        self.assertEqual(code, 0)
        expected = {key: row[key] for key in (
            "item_id", "stage", "revision", "owner_id", "owner_route", "lease_until", "updated_at")}
        expected["seconds_since_expiry"] = 5.0
        self.assertEqual(json.loads(text), [expected])
        self.assertEqual(text, json.dumps([expected], indent=2, sort_keys=True) + "\n")
        self.assertEqual(self.ledger_snapshot(router), before)

    def test_stage_reconcile_dry_run_then_apply(self) -> None:
        router, clock = self.reconciliation_router()
        before = self.expired_stage(router, clock)
        snapshot = self.ledger_snapshot(router)
        code, text = self.reconcile_command(router, *self.release_args())
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(text)["applied"])
        self.assertEqual(json.loads(text)["revision"], 2)
        self.assertEqual(json.loads(text)["owner_id"], "worker")
        self.assertEqual(router.get("named", "build"), before)
        self.assertEqual(self.ledger_snapshot(router), snapshot)
        with mock.patch.object(router, "reconcile_release", wraps=router.reconcile_release) as release:
            code, text = self.reconcile_command(router, *self.release_args(), "--apply")
        release.assert_called_once_with(
            "named", "build", actor="operator", expected_revision=2,
            owner_checked_inactive=True, liveness_evidence="Supervisor confirms worker exited")
        self.assertEqual(code, 0)
        result = json.loads(text)
        self.assertTrue(result["applied"])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["revision"], 3)
        self.assertIsNone(result["owner_id"])
        self.assertIsNone(result["owner_route"])
        self.assertIsNone(result["lease_until"])
        with router._db() as db:
            events = db.execute("SELECT actor,detail FROM events WHERE event='reconciled_release'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "operator")
        self.assertEqual(json.loads(events[0]["detail"]), {
            "owner_id": "worker", "owner_route": "codex",
            "liveness_evidence": "Supervisor confirms worker exited"})

    def test_stage_reconcile_incomplete_selectors_never_write(self) -> None:
        router, clock = self.reconciliation_router()
        before = self.expired_stage(router, clock)
        full = self.release_args()
        parts = [full[0:2], full[2:4], full[4:6], full[6:7], full[7:9]]
        cases = [["--apply"]]
        cases.extend(part for part in parts)
        cases.extend([arg for j, part in enumerate(parts) if j != i for arg in part]
                     for i in range(len(parts)))
        snapshot = self.ledger_snapshot(router)
        for args in cases:
            for apply in ([], ["--apply"]):
                with self.subTest(args=args, apply=apply):
                    code, text = self.reconcile_command(router, *args, *apply)
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(text), {"error": "selector_incomplete"})
                    self.assertEqual(router.get("named", "build"), before)
                    self.assertEqual(self.ledger_snapshot(router), snapshot)

    def test_stage_reconcile_refusals_never_write(self) -> None:
        router, clock = self.reconciliation_router()
        self.expired_stage(router, clock)
        router.register("pending", "build", allowed_routes=["codex"])
        router.register("archived", "build", allowed_routes=["codex"])
        router.archive("archived", actor="operator", reason="closed", apply=True)
        cases = [
            ({"--evidence": ""}, "liveness_evidence_required"),
            ({"--evidence": " \t "}, "liveness_evidence_required"),
            ({"--item": "missing"}, "stage_not_found"),
            ({"--stage": "missing"}, "stage_not_found"),
            ({"--item": "pending", "--expected-revision": "0"}, "reconciliation_not_required"),
            ({"--expected-revision": "1"}, "revision_conflict"),
            ({"--item": "archived", "--expected-revision": "0"}, "stage_archived"),
        ]
        snapshot = self.ledger_snapshot(router)
        before = router.get("named", "build")
        for replacements, error in cases:
            args = self.release_args()
            for flag, value in replacements.items():
                args[args.index(flag) + 1] = value
            for apply in ([], ["--apply"]):
                with self.subTest(replacements=replacements, apply=apply):
                    code, text = self.reconcile_command(router, *args, *apply)
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(text), {"error": error})
                    self.assertEqual(router.get("named", "build"), before)
                    self.assertEqual(self.ledger_snapshot(router), snapshot)

    def test_stage_reconcile_config_refusals_never_write(self) -> None:
        router, clock = self.reconciliation_router()
        before = self.expired_stage(router, clock)
        snapshot = self.ledger_snapshot(router)
        for args in ([], self.release_args(), [*self.release_args(), "--apply"]):
            for error in (RoutingError("orchestration_config_not_found"),
                          OSError("fixture_config_unreadable"), ValueError("fixture_config_invalid")):
                path = (None if isinstance(error, RoutingError)
                        else os.path.join(self.sandbox.root, "orchestration.json"))
                with self.subTest(args=args, error=str(error)), \
                        mock.patch.object(admin, "load_config", return_value=self.sandbox.cfg), \
                        mock.patch.object(admin.drift, "discover_config", return_value=path), \
                        mock.patch.object(admin.orchestration_config, "load", side_effect=error):
                    code, text = output(lambda: admin.main(["stage-reconcile", *args]))
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(text), {"error": str(error)})
                    self.assertEqual(router.get("named", "build"), before)
                    self.assertEqual(self.ledger_snapshot(router), snapshot)

    def test_stage_reconcile_has_no_bulk_option(self) -> None:
        with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr"):
            admin.main(["stage-reconcile", "--all"])
        self.assertEqual(raised.exception.code, 2)

    def test_archive_closed_tasks_dry_run_then_apply(self) -> None:
        router = StageRouter(os.path.join(self.sandbox.root, "capacity.sqlite3"), clock=lambda: 1000.0)
        for item in ("closed", "open", "without-record"):
            router.register(item, "build", allowed_routes=["codex"])
        tasks = os.path.join(self.sandbox.root, "tasks")
        os.makedirs(os.path.join(tasks, "closed")); os.makedirs(os.path.join(tasks, "open"))
        store.atomic_write_json(os.path.join(tasks, "closed", "task.json"), {"status": "done"})
        store.atomic_write_json(os.path.join(tasks, "open", "task.json"), {"status": "working"})
        args = argparse.Namespace(item=None, closed_tasks=tasks, stage=None,
                                  reason="closed-task", apply=False)
        with mock.patch.object(admin, "_archive_router", return_value=router):
            code, text = output(lambda: admin.cmd_archive(self.sandbox.cfg, args))
        plan = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(plan["items"]["closed"]["archived"], ["build"])
        self.assertEqual(plan["skipped"], {"not_closed": 1, "without_record": 1})
        self.assertIsNone(router.get("closed", "build")["archived_at"])
        args.apply = True
        with mock.patch.object(admin, "_archive_router", return_value=router):
            code, _ = output(lambda: admin.cmd_archive(self.sandbox.cfg, args))
        self.assertEqual(code, 0)
        self.assertIsNotNone(router.get("closed", "build")["archived_at"])

    def test_archive_stage_requires_named_item(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            admin.main(["archive", "--closed-tasks", self.sandbox.root, "--stage", "build"])

    def test_reporting_rate_and_exact_minimum_sample(self) -> None:
        self.ledger(*({"peer": "claude", "peer_observed_version": "invented-v1",
                       "resume": False, "peer_model_reported": model,
                       "peer_cost_reported": True} for model in (False, True, True)))
        _, text = output(lambda: admin.cmd_reporting(
            self.sandbox.cfg, argparse.Namespace(peer="claude", min_sample=3)))
        version_line = next(line for line in text.splitlines() if "invented-v1" in line)
        self.assertIn("1/3", version_line)
        self.assertIn("0/3", version_line)
        self.assertNotIn("below --min-sample", text)

    def test_local_first_counts_malformed_declaration(self) -> None:
        self.ledger({"peer": "claude", "local_first": "invented-malformed"})
        _, text = output(lambda: admin.cmd_local_first(
            self.sandbox.cfg, argparse.Namespace(peer="claude")))
        line = next(line for line in text.splitlines() if line.strip().startswith("malformed"))
        self.assertEqual(line.split()[1], "1")

    def test_resolve_missing_conversation_returns_failure(self) -> None:
        code, _ = output(lambda: admin.cmd_resolve(
            self.sandbox.cfg, argparse.Namespace(conversation_id=str(uuid.uuid4()),
                                                 apply=False, acknowledge_live_process=False)))
        self.assertEqual(code, 1)

    def test_resolve_dry_run_preserves_hold(self) -> None:
        cid, path = self.conversation(indeterminate=True, indeterminate_attempts=[],
                                      indeterminate_reaped=[],
                                      indeterminate_job_id=str(uuid.uuid4()))
        code, text = output(lambda: admin.cmd_resolve(
            self.sandbox.cfg, argparse.Namespace(conversation_id=cid, apply=False,
                                                 acknowledge_live_process=False)))
        self.assertEqual(code, 0)
        self.assertIn("Dry run", text)
        self.assertTrue(store.read_json_or_none(path)["indeterminate"])

    def test_resolve_clean_hold_requires_no_live_process_acknowledgement(self) -> None:
        cid, path = self.conversation(indeterminate=True, indeterminate_attempts=[],
                                      indeterminate_reaped=[],
                                      indeterminate_job_id=str(uuid.uuid4()))
        code, _ = output(lambda: admin.cmd_resolve(
            self.sandbox.cfg, argparse.Namespace(conversation_id=cid, apply=True,
                                                 acknowledge_live_process=False)))
        self.assertEqual(code, 0)
        self.assertFalse(store.read_json_or_none(path)["indeterminate"])

    def test_indeterminate_lists_only_held_conversations(self) -> None:
        held, _ = self.conversation(indeterminate=True)
        self.conversation(indeterminate=False)
        code, text = output(lambda: admin.cmd_indeterminate(
            self.sandbox.cfg, argparse.Namespace()))
        self.assertEqual(code, 0)
        self.assertEqual([row["conversation_id"] for row in json.loads(text)], [held])


if __name__ == "__main__":
    unittest.main()
