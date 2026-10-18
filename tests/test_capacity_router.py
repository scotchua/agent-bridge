"""Focused tests for conservative capacity-aware stage binding."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import (
    CAPACITY_NONE, MAX_FRESHNESS_SECONDS, PRESENCE_SOURCE, CapacityObservation,
    RoutingError, StageRouter, capacity_fingerprint)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CapacityRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.router = StageRouter(os.path.join(self.temp.name, "router.sqlite3"), clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def capacity(self, route, *, available=True, lifetime=60, trusted=True):
        self.router.observe_capacity(CapacityObservation(
            route, self.clock(), self.clock() + lifetime, available, "test-feed"),
            trusted=trusted)

    def test_unknown_and_stale_capacity_never_grant_assignment(self):
        self.router.register("a", "build", allowed_routes=["codex"])
        blocked = self.router.assign("a", "build", owner_id="worker-a",
                                     lease_seconds=30, expected_revision=0)
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["blocked_reason"], "no_fresh_eligible_route")

        self.capacity("claude", lifetime=1)
        self.clock.now += 2
        self.router.register("b", "build", allowed_routes=["claude"])
        stale = self.router.assign("b", "build", owner_id="worker-b",
                                   lease_seconds=30, expected_revision=0)
        self.assertEqual(stale["state"], "blocked")
        self.assertEqual(self.router.report()["capacity"]["claude"]["status"], "stale")

    def test_preference_and_route_suitability_select_one_owner(self):
        self.capacity("local")
        self.capacity("codex")
        self.router.register("a", "extract", allowed_routes=["local", "codex"],
                             preferred_routes=["local", "codex"])
        assigned = self.router.assign("a", "extract", owner_id="local-worker",
                                      lease_seconds=30, expected_revision=0)
        self.assertEqual((assigned["owner_route"], assigned["owner_id"]),
                         ("local", "local-worker"))
        with self.assertRaisesRegex(RoutingError, "already_owned"):
            self.router.assign("a", "extract", owner_id="other",
                               lease_seconds=30, expected_revision=1)

    def test_review_must_be_independent_by_provider_and_owner(self):
        self.capacity("claude")
        self.capacity("codex")
        self.router.register("a", "review", allowed_routes=["claude", "codex"],
                             preferred_routes=["codex", "claude"], is_review=True,
                             author_owner="codex-author", author_route="codex")
        with self.assertRaisesRegex(RoutingError, "review_independence_required"):
            self.router.assign("a", "review", owner_id="codex-author",
                               lease_seconds=30, expected_revision=0)
        assigned = self.router.assign("a", "review", owner_id="claude-reviewer",
                                      lease_seconds=30, expected_revision=0)
        self.assertEqual(assigned["owner_route"], "claude")

    def test_expired_lease_requires_explicit_liveness_reconciliation(self):
        self.capacity("codex", lifetime=300)
        self.router.register("a", "build", allowed_routes=["codex"])
        self.router.assign("a", "build", owner_id="first", lease_seconds=10,
                           expected_revision=0)
        self.clock.now += 11
        with self.assertRaisesRegex(RoutingError, "liveness_reconciliation_required"):
            self.router.assign("a", "build", owner_id="second", lease_seconds=10,
                               expected_revision=1)
        current = self.router.get("a", "build")
        self.assertEqual(current["state"], "reconcile_required")
        self.assertEqual(current["owner_id"], "first")
        with self.assertRaisesRegex(RoutingError, "liveness_evidence_required"):
            self.router.reconcile_release("a", "build", actor="coordinator",
                                          expected_revision=2,
                                          owner_checked_inactive=False,
                                          liveness_evidence="")
        released = self.router.reconcile_release(
            "a", "build", actor="coordinator", expected_revision=2,
            owner_checked_inactive=True, liveness_evidence="host reports owner stopped")
        self.assertEqual(released["state"], "pending")
        reassigned = self.router.assign("a", "build", owner_id="second",
                                        lease_seconds=10, expected_revision=3)
        self.assertEqual(reassigned["owner_id"], "second")

    def test_paid_fallback_is_forbidden_and_blocked_is_terminal(self):
        self.capacity("local")
        self.router.register("a", "build", allowed_routes=["local"])
        with self.assertRaisesRegex(RoutingError, "paid_fallback_forbidden"):
            self.router.assign("a", "build", owner_id="worker", lease_seconds=10,
                               expected_revision=0, paid_fallback=True)
        self.router.observe_capacity(CapacityObservation(
            "local", self.clock(), self.clock() + 60, False, "pressure-check"),
            trusted=True)
        blocked = self.router.assign("a", "build", owner_id="worker",
                                     lease_seconds=10, expected_revision=0)
        self.assertEqual(blocked["state"], "blocked")
        with self.assertRaisesRegex(RoutingError, "stage_terminal"):
            self.router.assign("a", "build", owner_id="worker", lease_seconds=10,
                               expected_revision=1)
        self.assertEqual(self.router.report()["blocked"][0]["item_id"], "a")

    def test_revision_guard_renew_and_complete(self):
        self.capacity("claude")
        self.router.register("a", "build", allowed_routes=["claude"])
        assigned = self.router.assign("a", "build", owner_id="owner",
                                      lease_seconds=10, expected_revision=0)
        with self.assertRaisesRegex(RoutingError, "revision_conflict"):
            self.router.renew("a", "build", owner_id="owner", lease_seconds=10,
                              expected_revision=0)
        renewed = self.router.renew("a", "build", owner_id="owner", lease_seconds=10,
                                    expected_revision=assigned["revision"])
        done = self.router.complete("a", "build", owner_id="owner",
                                    expected_revision=renewed["revision"])
        self.assertEqual(done["state"], "complete")

    def test_assignment_advances_for_every_nonrenewal_transition(self):
        self.capacity("claude")
        registered = self.router.register("a", "build", allowed_routes=["claude"])
        self.assertEqual(registered["assignment"], 0)
        assigned = self.router.assign("a", "build", owner_id="owner",
                                      lease_seconds=10, expected_revision=0)
        self.assertEqual(assigned["assignment"], 1)
        renewed = self.router.renew("a", "build", owner_id="owner",
                                    lease_seconds=10, expected_revision=assigned["revision"])
        self.assertEqual(renewed["assignment"], assigned["assignment"])
        done = self.router.complete("a", "build", owner_id="owner",
                                    expected_revision=renewed["revision"])
        self.assertEqual(done["assignment"], 2)

        blocked = self.router.register("blocked", "build", allowed_routes=["codex"])
        blocked = self.router.assign("blocked", "build", owner_id="owner",
                                     lease_seconds=10,
                                     expected_revision=blocked["revision"])
        self.assertEqual((blocked["state"], blocked["assignment"]), ("blocked", 1))

        expiring = self.router.register("expiring", "build", allowed_routes=["claude"])
        expiring = self.router.assign("expiring", "build", owner_id="owner",
                                      lease_seconds=1,
                                      expected_revision=expiring["revision"])
        self.clock.now += 2
        with self.assertRaisesRegex(RoutingError, "liveness_reconciliation_required"):
            self.router.assign("expiring", "build", owner_id="other", lease_seconds=10,
                               expected_revision=expiring["revision"])
        reconciliation = self.router.get("expiring", "build")
        self.assertEqual(reconciliation["assignment"], 2)
        released = self.router.reconcile_release(
            "expiring", "build", actor="operator",
            expected_revision=reconciliation["revision"], owner_checked_inactive=True,
            liveness_evidence="fixture")
        self.assertEqual(released["assignment"], 3)

    def test_stage_assignment_migration_preserves_legacy_revision_binding(self):
        legacy = os.path.join(self.temp.name, "legacy-stages.sqlite3")
        db = sqlite3.connect(legacy, isolation_level=None)
        db.execute("""CREATE TABLE stages (
            item_id TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
            allowed_routes TEXT NOT NULL, preferred_routes TEXT NOT NULL,
            owner_id TEXT, owner_route TEXT, lease_until REAL,
            author_owner TEXT, author_route TEXT, is_review INTEGER NOT NULL,
            blocked_reason TEXT, revision INTEGER NOT NULL,
            updated_at REAL NOT NULL, PRIMARY KEY(item_id, stage))""")
        db.execute("""INSERT INTO stages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   ("a", "build", "owned", '["claude"]', '["claude"]',
                    "owner", "claude", 2000.0, None, None, 0, None, 7, 1000.0))
        db.close()

        router = StageRouter(legacy, clock=self.clock)
        current = router.require_live_execution_binding(
            "a", "build", owner_id="owner", route="claude", revision=7)
        self.assertEqual(current["assignment"], 0)
        renewed = router.renew("a", "build", owner_id="owner", lease_seconds=10,
                               expected_revision=7)
        with self.assertRaisesRegex(RoutingError, "execution_stage_binding_invalid"):
            router.require_live_execution_binding(
                "a", "build", owner_id="owner", route="claude", revision=7)
        router.require_live_execution_binding(
            "a", "build", owner_id="owner", route="claude",
            revision=7, assignment=renewed["assignment"])


class CapacityIsEvidenceNotAnAssertion(unittest.TestCase):
    """An adversarial review found this table was evidence in name only.

    An assistant could name the route, the availability, the source and a
    freshness window of any length. These tests pin the three things that
    changed: an untrusted row cannot route work, a window longer than the
    lease is refused rather than clamped, and an installed ledger's existing
    rows are untrusted after the migration.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = os.path.join(self.temp.name, "router.sqlite3")
        self.router = StageRouter(self.path, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, route, *, trusted, lifetime=60, available=True,
                source="test-feed"):
        self.router.observe_capacity(CapacityObservation(
            route, self.clock(), self.clock() + lifetime, available, source),
            trusted=trusted)

    def test_an_untrusted_observation_never_assigns_a_stage(self):
        self.observe("codex", trusted=False)
        self.router.register("a", "build", allowed_routes=["codex"])
        blocked = self.router.assign("a", "build", owner_id="owner",
                                     lease_seconds=10, expected_revision=0)
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["blocked_reason"], "no_fresh_eligible_route")

    def test_the_same_observation_trusted_does_assign(self):
        """The difference is the writer, so nothing else about it may matter."""
        self.observe("codex", trusted=True)
        self.router.register("a", "build", allowed_routes=["codex"])
        owned = self.router.assign("a", "build", owner_id="owner",
                                   lease_seconds=10, expected_revision=0)
        self.assertEqual(owned["state"], "owned")
        self.assertEqual(owned["owner_route"], "codex")

    def test_an_untrusted_row_is_reported_rather_than_hidden(self):
        self.observe("local", trusted=False)
        entry = self.router.report()["capacity"]["local"]
        self.assertEqual(entry["status"], "untrusted")
        self.assertFalse(entry["trusted"])

    def test_a_window_longer_than_the_lease_is_refused(self):
        with self.assertRaisesRegex(RoutingError, "capacity_freshness_excessive"):
            self.observe("codex", trusted=True,
                         lifetime=MAX_FRESHNESS_SECONDS + 1)
        self.observe("codex", trusted=True, lifetime=MAX_FRESHNESS_SECONDS)
        self.assertEqual(self.router.report()["capacity"]["codex"]["status"],
                         "available")

    def test_a_refused_window_leaves_no_row_behind(self):
        with self.assertRaises(RoutingError):
            self.observe("codex", trusted=True, lifetime=365 * 24 * 3600)
        self.assertEqual(self.router.report()["capacity"], {})

    def test_a_retraction_removes_only_its_own_source_row(self):
        self.observe("codex", trusted=True, source="policy:operator-declared")
        self.assertFalse(self.router.retract_capacity("codex", source="somebody-else"))
        self.assertIn("codex", self.router.report()["capacity"])
        self.assertTrue(self.router.retract_capacity(
            "codex", source="policy:operator-declared"))
        self.assertNotIn("codex", self.router.report()["capacity"])

    def test_retracting_an_unknown_route_is_refused(self):
        with self.assertRaisesRegex(RoutingError, "route_invalid"):
            self.router.retract_capacity("openai", source="anything")

    def test_rows_an_older_version_accepted_stop_counting(self):
        """The migration is the fix, not just a schema change.

        An installed ledger already holds whatever the removed
        ``capacity_observe`` tool was told. Opening it with this version must
        not inherit those claims.
        """
        import sqlite3

        legacy = os.path.join(self.temp.name, "legacy.sqlite3")
        db = sqlite3.connect(legacy, isolation_level=None)
        db.execute("""CREATE TABLE capacity (
            route TEXT PRIMARY KEY, observed_at REAL NOT NULL,
            fresh_until REAL NOT NULL, available INTEGER NOT NULL,
            source TEXT NOT NULL)""")
        db.execute("INSERT INTO capacity VALUES('codex',1000.0,3000.0,1,'model-said-so')")
        db.close()

        router = StageRouter(legacy, clock=self.clock)
        entry = router.report()["capacity"]["codex"]
        self.assertEqual(entry["status"], "untrusted")
        router.register("a", "build", allowed_routes=["codex"])
        blocked = router.assign("a", "build", owner_id="owner", lease_seconds=10,
                                expected_revision=0)
        self.assertEqual(blocked["state"], "blocked")


class TheCapacityFingerprint(unittest.TestCase):
    """What a receipt records so a capacity change re-decides at once."""

    def rows(self, *entries):
        return [{"route": route, "observed_at": 1000.0, "fresh_until": 2000.0,
                 "available": 1, "trusted": 1, "source": source}
                for route, source in entries]

    def test_it_names_the_eligible_routes_and_nothing_else(self):
        digest = capacity_fingerprint(self.rows(("codex", "feed"), ("local", "feed")),
                                      1500.0)
        self.assertEqual(digest, "codex,local")

    def test_no_eligible_route_is_a_word_rather_than_an_empty_string(self):
        self.assertEqual(capacity_fingerprint([], 1500.0), CAPACITY_NONE)

    def test_a_timestamp_moving_does_not_change_it(self):
        """The reason it is route names only: the gate rewrites its own
        presence row on every call, so a digest of the table would re-decide
        every call."""
        first = capacity_fingerprint(self.rows(("codex", "feed")), 1500.0)
        later = [dict(row, observed_at=1400.0, fresh_until=2400.0)
                 for row in self.rows(("codex", "feed"))]
        self.assertEqual(capacity_fingerprint(later, 1500.0), first)

    def test_it_does_not_depend_on_who_is_asking(self):
        """The property that stops the two clients livelocking each other.

        An earlier version subtracted the asking client's own presence row, so
        the two clients computed different digests from the identical ledger,
        each found the other's receipt overtaken, and each re-decided it to
        route the work to the other. Both were then permanently denied. A
        receipt is one shared artifact, so its digest takes no client.
        """
        rows = self.rows(("claude", PRESENCE_SOURCE), ("codex", "feed"))
        self.assertEqual(capacity_fingerprint(rows, 1500.0), "claude,codex")
        import inspect
        self.assertNotIn(
            "client", inspect.signature(capacity_fingerprint).parameters,
            "the digest must not be computable differently per client")

    def test_a_presence_row_counts_like_any_other(self):
        """Presence is what makes an undeclared peer eligible, so it counts."""
        self.assertEqual(
            capacity_fingerprint(self.rows(("codex", PRESENCE_SOURCE)), 1500.0),
            "codex")

    def test_stale_untrusted_and_unavailable_rows_are_all_out(self):
        rows = self.rows(("codex", "feed"), ("local", "feed"), ("claude", "feed"))
        rows[0]["fresh_until"] = 1200.0        # stale at 1500
        rows[1]["trusted"] = 0
        rows[2]["available"] = 0
        self.assertEqual(capacity_fingerprint(rows, 1500.0), CAPACITY_NONE)


class CapacityRouterMutationGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.path = os.path.join(self.temp.name, "routing.sqlite3")
        self.router = StageRouter(self.path, clock=self.clock)

    def observe(self, route="codex", *, available=True):
        self.router.observe_capacity(CapacityObservation(
            route, 1000.0, 1060.0, available, "invented-feed"), trusted=True)

    def test_init_creates_private_parent_and_wal_database(self):
        path = os.path.join(self.temp.name, "nested", "routing.sqlite3")
        mkdir = type(self.router.path).mkdir
        modes = []

        def record_mkdir(directory, *args, **kwargs):
            modes.append(kwargs["mode"])
            return mkdir(directory, *args, **kwargs)

        with mock.patch.object(type(self.router.path), "mkdir", record_mkdir):
            router = StageRouter(path, clock=self.clock)
        self.assertEqual(modes, [0o700])
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        with router._db() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_init_explicitly_sets_full_synchronization(self):
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        path = os.path.join(self.temp.name, "traced.sqlite3")
        with mock.patch("agent_bridge.capacity_router.sqlite3.connect",
                        side_effect=traced_connect):
            StageRouter(path, clock=self.clock)
        self.assertIn("PRAGMA synchronous=FULL", statements)

    def test_connect_uses_bounded_sqlite_wait(self):
        connect = sqlite3.connect
        with mock.patch("agent_bridge.capacity_router.sqlite3.connect", wraps=connect) as patched:
            db = self.router._connect()
        db.close()
        patched.assert_called_once_with(self.router.path, timeout=5.0, isolation_level=None)

    def test_db_context_closes_connection(self):
        with self.router._db() as db:
            db.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_routes_reject_empty_list(self):
        with self.assertRaisesRegex(RoutingError, "routes_invalid"):
            self.router.register("invented-account", "build", allowed_routes=[])

    def test_register_rejects_blank_identity_and_returns_row(self):
        with self.assertRaisesRegex(RoutingError, "identity_invalid"):
            self.router.register("", "build", allowed_routes=["codex"])
        row = self.router.register("invented-account", "build", allowed_routes=["codex"])
        self.assertEqual(row["item_id"], "invented-account")

    def test_missing_stage_has_routing_error(self):
        with self.assertRaisesRegex(RoutingError, "stage_not_found"):
            self.router.get("invented-account", "absent")

    def test_register_records_event(self):
        self.router.register("invented-account", "build", allowed_routes=["codex"])
        with self.router._db() as db:
            events = db.execute("SELECT event FROM events WHERE item_id=?",
                                ("invented-account",)).fetchall()
        self.assertEqual([row[0] for row in events], ["registered"])

    def test_capacity_rows_returns_stored_observation(self):
        self.observe()
        self.assertEqual(self.router.capacity_rows()[0]["source"], "invented-feed")

    def test_observation_rejects_unknown_route(self):
        with self.assertRaisesRegex(RoutingError, "route_invalid"):
            self.router.observe_capacity(CapacityObservation(
                "invented-route", 1000.0, 1060.0, True, "invented-feed"), trusted=True)

    def test_future_capacity_cannot_assign(self):
        with self.router._db() as db:
            db.execute("""INSERT INTO capacity
                (route,observed_at,fresh_until,available,source,trusted)
                VALUES(?,?,?,?,?,?)""",
                       ("codex", 1002.0, 1060.0, 1, "invented-feed", 1))
        self.router.register("invented-account", "build", allowed_routes=["codex"])
        row = self.router.assign("invented-account", "build", owner_id="invented-owner",
                                 lease_seconds=10, expected_revision=0)
        self.assertEqual(row["state"], "blocked")

    def test_assign_rejects_blank_owner(self):
        self.observe()
        self.router.register("invented-account", "build", allowed_routes=["codex"])
        with self.assertRaisesRegex(RoutingError, "assignment_invalid"):
            self.router.assign("invented-account", "build", owner_id="",
                               lease_seconds=10, expected_revision=0)

    def test_renew_rejects_zero_lease(self):
        self.observe()
        self.router.register("invented-account", "build", allowed_routes=["codex"])
        owned = self.router.assign("invented-account", "build", owner_id="invented-owner",
                                   lease_seconds=10, expected_revision=0)
        with self.assertRaisesRegex(RoutingError, "lease_invalid"):
            self.router.renew("invented-account", "build", owner_id="invented-owner",
                              lease_seconds=0, expected_revision=owned["revision"])

    def test_report_distinguishes_stale_boundary_and_unavailable(self):
        self.observe()
        self.clock.now = 1060.0
        self.assertEqual(self.router.report()["capacity"]["codex"]["status"], "available")
        self.router.observe_capacity(CapacityObservation(
            "local", 1000.0, 1060.0, False, "invented-feed"), trusted=True)
        self.assertEqual(self.router.report()["capacity"]["local"]["status"], "unavailable")

    def test_fingerprint_includes_inclusive_time_boundaries(self):
        rows = [{"route": "codex", "available": 1, "trusted": 1,
                 "observed_at": 1001.0, "fresh_until": 1000.0}]
        self.assertEqual(capacity_fingerprint(rows, 1000.0), "codex")


class StageArchivalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = os.path.join(self.temp.name, "routing.sqlite3")
        self.router = StageRouter(self.path, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def _owned(self, item, stage, lease=10):
        self.router.observe_capacity(CapacityObservation(
            "codex", self.clock(), self.clock() + 60, True, "test"), trusted=True)
        row = self.router.register(item, stage, allowed_routes=["codex"])
        return self.router.assign(item, stage, owner_id="owner", lease_seconds=lease,
                                  expected_revision=row["revision"])

    def test_migrates_archived_column_idempotently(self):
        legacy = os.path.join(self.temp.name, "legacy.sqlite3")
        db = sqlite3.connect(legacy)
        db.execute("""CREATE TABLE stages (
            item_id TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
            allowed_routes TEXT NOT NULL, preferred_routes TEXT NOT NULL,
            owner_id TEXT, owner_route TEXT, lease_until REAL,
            author_owner TEXT, author_route TEXT, is_review INTEGER NOT NULL,
            blocked_reason TEXT, revision INTEGER NOT NULL,
            updated_at REAL NOT NULL, PRIMARY KEY(item_id, stage))""")
        db.commit(); db.close()
        StageRouter(legacy, clock=self.clock)
        StageRouter(legacy, clock=self.clock)
        db = sqlite3.connect(legacy)
        self.assertIn("archived_at", {row[1] for row in db.execute("PRAGMA table_info(stages)")})
        db.close()

    def test_dry_run_apply_and_report_preserve_history(self):
        self.router.register("done", "pending", allowed_routes=["codex"])
        dry = self.router.archive("done", actor="operator", reason="closed", apply=False)
        self.assertEqual(dry, {"archived": ["pending"], "refused": [], "already": []})
        self.assertIsNone(self.router.get("done", "pending")["archived_at"])
        applied = self.router.archive("done", actor="operator", reason="closed", apply=True)
        self.assertEqual(applied["archived"], ["pending"])
        row = self.router.get("done", "pending")
        self.assertEqual(row["archived_at"], self.clock())
        self.assertEqual(self.router.archive("done", actor="operator", reason="closed", apply=False),
                         {"archived": [], "refused": [], "already": ["pending"]})
        self.assertEqual(self.router.report()["states"], {"archived": 1})
        with self.router._db() as db:
            event = db.execute("SELECT event,detail FROM events WHERE event='archived'").fetchone()
        self.assertEqual(event[0], "archived")
        self.assertEqual(json.loads(event[1]),
                         {"prior_state": "pending", "reason": "closed"})

    def test_refuses_only_reconciliation_and_live_owner(self):
        live = self._owned("live", "build")
        expired = self._owned("expired", "build", lease=1)
        expired_archive = self._owned("expired-archive", "build", lease=1)
        self.clock.now += 2
        with self.assertRaisesRegex(RoutingError, "liveness_reconciliation_required"):
            self.router.assign("expired", "build", owner_id="other", lease_seconds=1,
                               expected_revision=expired["revision"])
        self.router.register("pending", "build", allowed_routes=["codex"])
        self.router.register("blocked", "build", allowed_routes=["claude"])
        blocked = self.router.assign("blocked", "build", owner_id="owner", lease_seconds=1,
                                     expected_revision=0)
        complete = self._owned("complete", "build")
        self.router.complete("complete", "build", owner_id="owner",
                             expected_revision=complete["revision"])
        plan = self.router.archive("live", actor="operator", reason="closed", apply=False)
        self.assertEqual(plan["refused"][0]["why"], "lease_unexpired")
        plan = self.router.archive("expired", actor="operator", reason="closed", apply=True)
        self.assertEqual(plan["refused"][0]["why"], "liveness_reconciliation_required")
        for item in ("expired-archive", "pending", "blocked", "complete"):
            self.assertEqual(self.router.archive(item, actor="operator", reason="closed", apply=True)["archived"],
                             ["build"])
        self.assertEqual(live["state"], "owned")

    def test_archived_stage_refuses_mutations_but_get_and_generation_keep_it(self):
        owned = self._owned("item", "implementation")
        self.clock.now += 11
        self.router.archive("item", actor="operator", reason="closed", apply=True)
        archived = self.router.get("item", "implementation")
        self.assertIsNotNone(archived["archived_at"])
        for operation in (
                lambda: self.router.register("item", "implementation", allowed_routes=["codex"]),
                lambda: self.router.assign("item", "implementation", owner_id="owner", lease_seconds=1,
                                           expected_revision=owned["revision"]),
                lambda: self.router.renew("item", "implementation", owner_id="owner", lease_seconds=1,
                                          expected_revision=owned["revision"]),
                lambda: self.router.complete("item", "implementation", owner_id="owner",
                                             expected_revision=owned["revision"])):
            with self.subTest(operation=operation), self.assertRaisesRegex(RoutingError, "stage_archived"):
                operation()
        from agent_bridge.orchestration.autodecide import stage_name
        self.assertEqual(stage_name(self.router, "item", "implementation"), "implementation#2")


if __name__ == "__main__":
    unittest.main()
