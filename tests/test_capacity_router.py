"""Focused tests for conservative capacity-aware stage binding."""

import os
import sys
import tempfile
import unittest

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

    def test_the_asking_client_own_presence_row_is_left_out(self):
        rows = self.rows(("claude", PRESENCE_SOURCE), ("codex", "feed"))
        self.assertEqual(capacity_fingerprint(rows, 1500.0, exclude_client="claude"),
                         "codex")
        self.assertEqual(capacity_fingerprint(rows, 1500.0, exclude_client="codex"),
                         "claude,codex")

    def test_only_that_row_is_left_out_not_the_route(self):
        """A route the operator declared stays in even for the asking client."""
        rows = self.rows(("claude", "policy:operator-declared"))
        self.assertEqual(capacity_fingerprint(rows, 1500.0, exclude_client="claude"),
                         "claude")

    def test_stale_untrusted_and_unavailable_rows_are_all_out(self):
        rows = self.rows(("codex", "feed"), ("local", "feed"), ("claude", "feed"))
        rows[0]["fresh_until"] = 1200.0        # stale at 1500
        rows[1]["trusted"] = 0
        rows[2]["available"] = 0
        self.assertEqual(capacity_fingerprint(rows, 1500.0), CAPACITY_NONE)


if __name__ == "__main__":
    unittest.main()
