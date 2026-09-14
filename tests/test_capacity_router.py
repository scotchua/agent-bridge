"""Focused tests for conservative capacity-aware stage binding."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import CapacityObservation, RoutingError, StageRouter


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

    def capacity(self, route, *, available=True, lifetime=60):
        self.router.observe_capacity(CapacityObservation(
            route, self.clock(), self.clock() + lifetime, available, "test-feed"))

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
            "local", self.clock(), self.clock() + 60, False, "pressure-check"))
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


if __name__ == "__main__":
    unittest.main()
