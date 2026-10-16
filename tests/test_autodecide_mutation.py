"""Focused behavior checks for automatic routing decisions."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import autodecide, autoroute, gate  # noqa: E402


class OutcomeRetained(unittest.TestCase):
    def test_retained_matches_only_the_retain_route(self):
        retained = autodecide.Outcome(SimpleNamespace(route=autoroute.RETAIN), None)
        dispatched = autodecide.Outcome(SimpleNamespace(route="codex"), None)
        self.assertTrue(retained.retained)
        self.assertFalse(dispatched.retained)


class ItemIdFor(unittest.TestCase):
    def test_repository_id_has_a_sixteen_character_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            item_id = autodecide.item_id_for(os.path.join(directory, "invented-co"))
        self.assertTrue(item_id.startswith("auto-"))
        self.assertEqual(len(item_id), len("auto-") + 16)


class FreshRoutes(unittest.TestCase):
    def test_only_available_routes_are_eligible(self):
        router = SimpleNamespace(report=lambda: {"capacity": {
            "claude": {"status": "available"},
            "codex": {"status": "stale"},
            "local": {"status": "unavailable"},
        }})
        self.assertEqual(autodecide.fresh_routes(router), frozenset({"claude"}))


class ObserveDeclaredRoutes(unittest.TestCase):
    def test_current_declaration_is_not_withdrawn_before_replay(self):
        class Router:
            def __init__(self):
                self.retracted = []
                self.observed = []

            def clock(self):
                return 1800000000.0

            def retract_capacity(self, route, *, source):
                self.retracted.append((route, source))

            def observe_capacity(self, observation, *, trusted):
                self.observed.append((observation.route, trusted))

        router = Router()
        policy = SimpleNamespace(declared_routes=frozenset({"codex"}))
        autodecide._observe_declared_routes(router, policy, client="claude")
        self.assertEqual(router.retracted, [("local", autodecide.DECLARED_SOURCE)])
        self.assertEqual(router.observed, [("codex", True)])


class IntentPath(unittest.TestCase):
    def test_intent_lives_in_the_intents_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(autodecide.intent_path(directory, os.path.join(directory, "invented-co")))
            expected = Path(gate.receipt_dir(directory)) / autodecide.INTENT_DIR
            self.assertEqual(path.parent, expected)


if __name__ == "__main__":
    unittest.main()
