"""The claim reconciler reports disagreements and never moves ownership."""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import CapacityObservation, StageRouter
from agent_bridge.orchestration import claim_reconcile

NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def claim(owner="claude-session-1", stage="build", *, lease_in=600, released=None):
    return {"owner": owner, "stage": stage, "claimed_at": NOW - 60,
            "lease_expires_at": NOW + lease_in,
            "released_at": None if released is None else NOW - 30,
            "release_reason": released}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ClaimReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.ledger = self.root / "router.sqlite3"
        self.clock = Clock()
        self.router = StageRouter(self.ledger, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def task(self, item_id, workload=None, *, raw=None):
        folder = self.tasks / item_id
        folder.mkdir(exist_ok=True)
        path = folder / "task.json"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            body = {"task_id": item_id}
            if workload is not None:
                body["workload"] = workload
            path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def owned(self, item_id, stage="build", owner="claude-session-1", lease=600):
        self.router.observe_capacity(CapacityObservation(
            "claude", self.clock(), self.clock() + 60, True, "test-feed"), trusted=True)
        self.router.register(item_id, stage, allowed_routes=["claude"])
        row = self.router.assign(item_id, stage, owner_id=owner, lease_seconds=lease,
                                 expected_revision=0)
        self.assertEqual(row["state"], "owned")
        return row

    def kinds(self, findings, item_id=None):
        return sorted(f["kind"] for f in findings if item_id in (None, f["item_id"]))

    def run_reconcile(self, **kwargs):
        return claim_reconcile.reconcile(self.ledger, self.tasks, now=self.clock(), **kwargs)

    def test_agreement_reports_nothing(self):
        self.owned("agree")
        self.task("agree", {"revision": 1, "claim": claim()})
        self.assertEqual(self.run_reconcile(), [])

    def test_owner_mismatch_is_a_conflict_the_task_record_governs(self):
        row = self.owned("split", owner="codex-worker-9")
        self.task("split", {"revision": 4, "claim": claim()})
        [finding] = self.run_reconcile()
        self.assertEqual(finding["kind"], "owner_mismatch")
        self.assertEqual(finding["severity"], claim_reconcile.CONFLICT)
        self.assertEqual(finding["governing_owner"], "claude-session-1")
        # Both sides' fencing values travel with the finding.
        self.assertEqual(finding["task_record"]["revision"], 4)
        self.assertEqual(finding["ledger"]["revision"], row["revision"])
        self.assertEqual(finding["ledger"]["assignment"], row["assignment"])
        self.assertEqual(finding["ledger"]["owner_id"], "codex-worker-9")

    def test_expired_leases_are_notices_and_never_transfer(self):
        self.owned("late", lease=30)
        self.task("late", {"revision": 1, "claim": claim(lease_in=10)})
        self.clock.now += 120
        findings = self.run_reconcile()
        self.assertEqual(self.kinds(findings), ["ledger_lease_expired", "task_lease_expired"])
        self.assertTrue(all(f["severity"] == claim_reconcile.NOTICE for f in findings))
        self.assertTrue(all(f["governing_owner"] == "claude-session-1" for f in findings))
        # Reporting an expired lease did not flip the ledger row.
        self.assertEqual(self.router.get("late", "build")["state"], "owned")

    def test_released_in_task_record_while_ledger_still_owned(self):
        self.owned("gone")
        self.task("gone", {"revision": 2, "claim": claim(released="completed")})
        [finding] = self.run_reconcile()
        self.assertEqual(finding["kind"], "released_in_task_record_only")
        self.assertEqual(finding["severity"], claim_reconcile.CONFLICT)
        self.assertNotIn("governing_owner", finding)

    def test_ledger_owner_without_any_task_claim(self):
        self.owned("orphan")
        self.task("orphan", {"revision": 0, "claim": None})
        self.assertEqual(self.kinds(self.run_reconcile()), ["ledger_owner_without_task_claim"])

    def test_task_claim_for_a_stage_the_ledger_does_not_know(self):
        self.owned("elsewhere", stage="build")
        self.task("elsewhere", {"revision": 1, "claim": claim(stage="review")})
        self.assertEqual(self.kinds(self.run_reconcile()),
                         ["ledger_owner_for_other_stage", "task_claim_stage_not_in_ledger"])

    def test_complete_and_pending_ledger_rows_against_an_active_claim(self):
        self.router.register("done", "build", allowed_routes=["claude"])
        self.router.register("waiting", "build", allowed_routes=["claude"])
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE stages SET state='complete' WHERE item_id='done'")
        self.task("done", {"revision": 1, "claim": claim()})
        self.task("waiting", {"revision": 1, "claim": claim()})
        findings = self.run_reconcile()
        self.assertEqual(self.kinds(findings, "done"), ["complete_in_ledger_only"])
        self.assertEqual(self.kinds(findings, "waiting"), ["unowned_in_ledger_only"])

    def test_reconcile_required_is_reported(self):
        self.owned("stuck", lease=30)
        self.clock.now += 120
        with self.assertRaises(Exception):
            # The router's own assign flips the expired owned row.
            self.router.assign("stuck", "build", owner_id="other", lease_seconds=30,
                               expected_revision=self.router.get("stuck", "build")["revision"])
        self.task("stuck", {"revision": 1, "claim": claim(lease_in=1000)})
        self.assertIn("ledger_reconcile_required", self.kinds(self.run_reconcile()))

    def test_missing_unreadable_and_malformed_records_fail_closed(self):
        for item_id in ("absent", "garbled", "badshape", "badclaim", "badtoken"):
            self.owned(item_id)
        self.task("garbled", raw="{not json")
        self.task("badshape", {"revision": "1", "claim": None})
        self.task("badclaim", {"revision": 1, "claim": dict(claim(), lease_expires_at="soon")})
        self.task("badtoken", {"revision": 1, "claim": claim(owner="has space")})
        findings = self.run_reconcile()
        self.assertIn("task_record_missing", self.kinds(findings, "absent"))
        for item_id in ("badshape", "badclaim", "badtoken"):
            self.assertIn("task_record_malformed", self.kinds(findings, item_id))
        self.assertIn("task_record_unreadable", self.kinds(findings, "garbled"))
        # No owner text from a rejected record reaches the report.
        self.assertNotIn("has space", json.dumps(findings))

    def test_release_reason_must_match_released_at(self):
        self.owned("reason")
        bad = dict(claim(), release_reason="completed")
        self.task("reason", {"revision": 1, "claim": bad})
        self.assertIn("task_record_malformed", self.kinds(self.run_reconcile()))

    def test_task_record_outside_the_root_is_not_read(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "task.json").write_text(
            json.dumps({"workload": {"revision": 1, "claim": claim()}}), encoding="utf-8")
        (self.tasks / "linked").symlink_to(outside, target_is_directory=True)
        self.owned("linked", owner="codex-worker-9")
        # An escape is a conflict for the operator, never a claim read from outside.
        self.assertEqual(claim_reconcile.read_task_claim(self.tasks, "linked"),
                         {"status": "unreadable"})
        self.assertEqual(self.kinds(self.run_reconcile()),
                         ["ledger_owner_without_task_claim", "task_record_unreadable"])
        for unsafe in ("../outside", ".hidden", "a/b", "", None):
            with self.subTest(item_id=unsafe):
                self.assertEqual(claim_reconcile.read_task_claim(self.tasks, unsafe),
                                 {"status": "no_record"})

    def test_reconcile_writes_nothing_on_either_side(self):
        self.owned("split", owner="codex-worker-9")
        record = self.task("split", {"revision": 4, "claim": claim()})
        self.owned("late", lease=30)
        self.task("late", {"revision": 1, "claim": claim(lease_in=10)})
        self.clock.now += 120

        def snapshot():
            # A read-only WAL reader may create the standard -wal/-shm sidecars;
            # those are SQLite's own and are checked separately below.
            names = sorted(n for n in os.listdir(self.root)
                           if not n.endswith(("-wal", "-shm")))
            return (digest(self.ledger), self.ledger.stat().st_mtime_ns, digest(record),
                    names)

        before = snapshot()
        self.run_reconcile()
        self.run_reconcile()
        self.assertEqual(before, snapshot())
        wal = self.root / "router.sqlite3-wal"
        if wal.exists():
            self.assertEqual(wal.stat().st_size, 0, "reader wrote WAL frames")
        self.assertEqual(self.router.get("late", "build")["state"], "owned")

    def test_read_only_connection_refuses_writes(self):
        self.owned("x")
        real_connect = sqlite3.connect
        seen = []

        def spy(target, *args, **kwargs):
            seen.append((target, kwargs.get("uri")))
            return real_connect(target, *args, **kwargs)

        with mock.patch.object(claim_reconcile.sqlite3, "connect", side_effect=spy):
            claim_reconcile.reconcile(self.ledger, self.tasks, now=self.clock())
        # The module's only connection is the read-only URI form.
        [(uri, as_uri)] = seen
        self.assertTrue(as_uri)
        self.assertTrue(uri.endswith("?mode=ro"))
        probe = real_connect(uri, uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                probe.execute("UPDATE stages SET state='complete'")
        finally:
            probe.close()

    def test_missing_ledger_is_an_error_not_an_empty_report(self):
        with self.assertRaises(OSError):
            claim_reconcile.reconcile(self.root / "nope.sqlite3", self.tasks, now=NOW)
        self.assertFalse((self.root / "nope.sqlite3").exists())

    def test_old_ledger_without_assignment_or_archived_columns(self):
        old = self.root / "old.sqlite3"
        with sqlite3.connect(old) as db:
            db.execute("""CREATE TABLE stages(item_id TEXT, stage TEXT, state TEXT,
                allowed_routes TEXT, preferred_routes TEXT, owner_id TEXT, owner_route TEXT,
                lease_until REAL, author_owner TEXT, author_route TEXT, is_review INTEGER,
                blocked_reason TEXT, revision INTEGER, updated_at REAL)""")
            db.execute("""INSERT INTO stages VALUES('legacy','build','owned','["claude"]',
                '["claude"]','codex-worker-9','claude',?,NULL,NULL,0,NULL,3,?)""",
                       (NOW + 600, NOW))
        db.close()
        self.task("legacy", {"revision": 2, "claim": claim()})
        [finding] = claim_reconcile.reconcile(old, self.tasks, now=NOW)
        self.assertEqual(finding["kind"], "owner_mismatch")
        self.assertEqual(finding["ledger"]["assignment"], 0)

    def test_archived_stages_are_skipped_and_items_filter_applies(self):
        self.owned("archived", owner="codex-worker-9")
        self.owned("kept", owner="codex-worker-9")
        self.task("archived", {"revision": 1, "claim": claim()})
        self.task("kept", {"revision": 1, "claim": claim()})
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE stages SET archived_at=? WHERE item_id='archived'", (NOW,))
        self.assertEqual({f["item_id"] for f in self.run_reconcile()}, {"kept"})
        only = self.run_reconcile(items=["archived"])
        # An archived item asked for by name has no live rows to compare.
        self.assertEqual(self.kinds(only), ["task_claim_stage_not_in_ledger"])

    def test_main_prints_json_and_exits_three_on_conflict(self):
        self.owned("split", owner="codex-worker-9")
        self.task("split", {"revision": 4, "claim": claim()})
        config = mock.Mock(capacity_db=self.ledger)
        out = io.StringIO()
        with mock.patch.object(claim_reconcile, "load", return_value=config), \
                mock.patch.object(claim_reconcile.time, "time", return_value=NOW), \
                contextlib.redirect_stdout(out):
            code = claim_reconcile.main(["--config", "x", "--tasks-root", str(self.tasks)])
        self.assertEqual(code, 3)
        report = json.loads(out.getvalue())
        self.assertEqual(report["conflicts"], 1)
        self.assertEqual(report["findings"][0]["kind"], "owner_mismatch")

    def test_main_config_and_ledger_failures(self):
        err = io.StringIO()
        with mock.patch.object(claim_reconcile, "load",
                               side_effect=claim_reconcile.OrchestrationConfigError("x")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(claim_reconcile.main(["--config", "x", "--tasks-root", "t"]), 2)
        config = mock.Mock(capacity_db=self.root / "nope.sqlite3")
        with mock.patch.object(claim_reconcile, "load", return_value=config), \
                contextlib.redirect_stderr(err):
            self.assertEqual(claim_reconcile.main(["--config", "x", "--tasks-root",
                                                   str(self.tasks)]), 1)
        self.assertIn("claim reconcile failed: FileNotFoundError", err.getvalue())


if __name__ == "__main__":
    unittest.main()
