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
