"""Boundary and metadata checks found by autoroute mutation testing."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import autoroute  # noqa: E402


class MainWorktreeTests(unittest.TestCase):
    def test_symlinked_git_marker_cannot_inherit_main_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            main = base / "invented-main"
            linked = base / "invented-linked"
            gitdir = main / ".git" / "worktrees" / "linked"
            gitdir.mkdir(parents=True)
            linked.mkdir()
            marker_target = base / "marker-text"
            marker_target.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
            try:
                (linked / ".git").symlink_to(marker_target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            (gitdir / "gitdir").write_text(str(linked / ".git"), encoding="utf-8")
            self.assertIsNone(autoroute._main_worktree(str(linked)))


class LoadTests(unittest.TestCase):
    def test_known_load_formats_its_ratio(self):
        self.assertEqual(autoroute.Load(0.375, True).busy_at, "0.38")
        self.assertEqual(autoroute.Load(0.375, False).busy_at, "unknown")


class ProbeLoadTests(unittest.TestCase):
    def test_zero_cores_reports_unknown_load(self):
        with mock.patch.object(autoroute.os, "getloadavg", return_value=(2.0, 2.0, 2.0)):
            self.assertEqual(autoroute.probe_load(cpu_count=0), autoroute.Load(known=False))


class DecisionTests(unittest.TestCase):
    def test_dispatches_only_for_a_route(self):
        retained = autoroute.Decision(autoroute.RETAIN, "retained_is_the_policy", "", {})
        routed = autoroute.Decision("codex", "routed_peer_implementation", "", {})
        self.assertFalse(retained.dispatches)
        self.assertTrue(routed.dispatches)


class DecideTests(unittest.TestCase):
    def decide(self, load_ratio: float, idle_ratio: float | None):
        policy = autoroute.Policy(
            repos={"/invented-repo": autoroute.RepoPolicy(
                "public", ("local",), mechanical_ok=True)},
            max_local_load_ratio=0.5)
        return autoroute.decide(
            autoroute.Signal("claude", "/invented-repo", "mechanical"), policy,
            fresh_routes=frozenset({"local"}),
            load=autoroute.Load(load_ratio, True), cpu_idle_ratio=idle_ratio)

    def test_idle_at_rescue_threshold_allows_local_route(self):
        decision = self.decide(0.9, autoroute.MIN_LOCAL_IDLE_RATIO)
        self.assertEqual(decision.route, "local")

    def test_load_at_ceiling_defers_local_route(self):
        decision = self.decide(0.5, None)
        self.assertEqual(decision.code, "retained_local_load_high")


class PolicyFingerprintTests(unittest.TestCase):
    def test_policy_digest_has_full_recorded_length(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(autoroute.policy_path(temporary))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"version": 1, "repos": {}}), encoding="utf-8")
            self.assertEqual(len(autoroute.policy_fingerprint(temporary)), 32)


class ParsePolicyTests(unittest.TestCase):
    def test_maximum_documented_local_load_is_accepted(self):
        policy = autoroute.parse_policy({"version": 1, "repos": {},
                                         "max_local_load_ratio": 64})
        self.assertEqual(policy.max_local_load_ratio, 64.0)


if __name__ == "__main__":
    unittest.main()
