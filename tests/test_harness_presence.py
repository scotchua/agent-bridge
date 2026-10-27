"""Synthetic broker readiness tests. No provider binary is ever spawned."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import operator_isolation  # noqa: E402,F401

from agent_bridge.capacity_router import (
    BROKER_PROBE_SOURCE, HARNESS_PROBE_FRESHNESS_SECONDS, PRESENCE_SOURCE,
    CapacityObservation, RoutingError, StageRouter, upsert_observation,
)
from agent_bridge.orchestration.harness_probe import (
    HarnessReadinessProbe, PROBE_OUTPUT_BYTES, PROBE_TIMEOUT_SECONDS,
)
from agent_bridge.orchestration import autoroute, gate
from agent_bridge.orchestration.server import Server
from test_orchestration_server import Service


PROVIDERS = ("claude", "codex")


class HarnessPresenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.probe = mock.Mock(return_value=True)
        self.router = StageRouter(self.root / "capacity.sqlite3", clock=lambda: self.now,
                                  harness_probe=self.probe)

    def observe(self, provider, *, available=True, source=PRESENCE_SOURCE, stale=False):
        self.router.observe_capacity(CapacityObservation(
            provider, self.now - 100, self.now - 1 if stale else self.now + 100,
            available, source), trusted=True)

    def claim(self, provider, item):
        self.router.register(item, "build", allowed_routes=[provider])
        return self.router.assign(item, "build", owner_id="worker", lease_seconds=300,
                                  expected_revision=0)

    def test_stale_interactive_presence_claims_with_broker_source_for_both_providers(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.observe(provider, stale=True)
                result = self.claim(provider, provider)
                self.assertEqual((result["state"], result["owner_route"]), ("owned", provider))
                row = self.router.report()["capacity"][provider]
                self.assertEqual(row["source"], BROKER_PROBE_SOURCE)
                self.assertTrue(row["trusted"])
                self.assertEqual(row["fresh_until"] - row["observed_at"],
                                 HARNESS_PROBE_FRESHNESS_SECONDS)
        self.assertEqual(self.probe.call_args_list, [mock.call(p) for p in PROVIDERS])

    def test_failure_and_timeout_refuse_without_fresh_evidence_or_resurrection(self):
        for provider in PROVIDERS:
            for index, outcome in enumerate((False, TimeoutError())):
                with self.subTest(provider=provider, outcome=outcome):
                    self.now += 61
                    self.observe(provider, stale=True)
                    self.probe.side_effect = outcome if isinstance(outcome, Exception) else None
                    self.probe.return_value = False
                    item = f"{provider}-{index}"
                    result = self.claim(provider, item)
                    self.assertEqual(result["state"], "blocked")
                    self.assertEqual(result["blocked_reason"],
                                     "no_fresh_eligible_route:harness_probe_failed")
                    self.assertEqual(self.router.report()["capacity"][provider]["status"], "stale")
                    self.probe.reset_mock()
                    self.probe.side_effect = None
                    self.probe.return_value = True
                    self.now += 61
                    with self.assertRaisesRegex(RoutingError, "stage_terminal"):
                        self.router.assign(item, "build", owner_id="worker", lease_seconds=300,
                                           expected_revision=result["revision"])
                    self.probe.assert_not_called()

    def test_quota_refusals_win_even_over_cached_probe_and_interactive_presence(self):
        for provider in PROVIDERS:
            self.claim(provider, provider)
            for source in ("quota:exhausted", "usage:over-ceiling"):
                self.observe(provider, available=False, source=source)
                self.observe(provider)  # A hook must not clear the refusal either.
                result = self.claim(provider, provider + source)
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(self.router.report()["capacity"][provider]["source"], source)
                with self.assertRaisesRegex(RoutingError, "no_fresh_eligible_route"):
                    self.router.require_execution_presence(provider)

    def test_quota_arriving_during_probe_wins(self):
        for provider in PROVIDERS:
            def exhaust(route):
                self.observe(route, available=False, source="quota:exhausted")
                return True
            self.probe.side_effect = exhaust
            self.assertEqual(self.claim(provider, provider)["state"], "blocked")

    def test_reserved_source_is_rejected_by_all_general_writers(self):
        for provider in PROVIDERS:
            observation = CapacityObservation(provider, self.now, self.now + 60,
                                              True, BROKER_PROBE_SOURCE)
            for trusted in (False, True):
                with self.assertRaisesRegex(RoutingError, "capacity_source_reserved"):
                    self.router.observe_capacity(observation, trusted=trusted)
                with self.router._db() as db:
                    with self.assertRaisesRegex(RoutingError, "capacity_source_reserved"):
                        upsert_observation(db, observation, trusted=trusted, now=self.now)
            self.router.observe_capacity(CapacityObservation(
                provider, self.now, self.now + 60, True, "pretend:ready"), trusted=False)
        # Existing untrusted-writer rule remains in effect without a broker probe.
        untrusted = StageRouter(self.router.path, clock=lambda: self.now)
        untrusted.register("untrusted", "build", allowed_routes=PROVIDERS)
        self.assertEqual(untrusted.assign("untrusted", "build", owner_id="model",
                         lease_seconds=60, expected_revision=0)["state"], "blocked")

    def test_success_and_failure_cache_survive_new_broker_instances(self):
        for ready in (True, False):
            for provider in PROVIDERS:
                self.now += 61
                self.probe.return_value = ready
                self.probe.reset_mock()
                self.claim(provider, f"{ready}-{provider}")
                another = StageRouter(self.router.path, clock=lambda: self.now,
                                      harness_probe=self.probe)
                another._refresh_harness_presence([provider])
                self.probe.assert_called_once_with(provider)
                self.now += 59
                another._refresh_harness_presence([provider])
                self.probe.assert_called_once_with(provider)
                self.now += 2
                another._refresh_harness_presence([provider])
                self.assertEqual(self.probe.call_count, 2)

    def test_concurrent_broker_observes_probe_reservation_without_running_again(self):
        other_probe = mock.Mock(return_value=True)
        another = StageRouter(self.router.path, clock=lambda: self.now,
                              harness_probe=other_probe)
        seen = []
        def run(provider):
            seen.append(another._refresh_harness_presence([provider]))
            another.register("waiting-" + provider, "build", allowed_routes=[provider])
            try:
                another.assign("waiting-" + provider, "build", owner_id="other",
                               lease_seconds=300, expected_revision=0)
            except RoutingError as exc:
                seen.append(str(exc))
            return True
        self.probe.side_effect = run
        for provider in PROVIDERS:
            self.assertEqual(self.claim(provider, provider)["state"], "owned")
            # The waiting stage is retried, not blocked, while the probe runs.
            self.assertEqual(another.get("waiting-" + provider, "build")["state"], "pending")
        self.assertEqual(seen, [x for p in PROVIDERS for x in
                                ({p: "harness_probe_pending"}, "harness_probe_pending")])
        other_probe.assert_not_called()

    def test_quota_refusal_blocks_through_fresh_until_then_probe_recovers(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.now += 61
                self.observe(provider, available=False, source="quota:exhausted")
                expires = self.now + 100
                self.probe.reset_mock()
                self.assertEqual(self.claim(provider, provider + "-fresh")["state"], "blocked")
                self.now = expires  # fresh_until is inclusive
                self.assertEqual(self.claim(provider, provider + "-edge")["state"], "blocked")
                self.probe.assert_not_called()
                self.now = expires + 1
                result = self.claim(provider, provider + "-expired")
                self.assertEqual((result["state"], result["owner_route"]), ("owned", provider))
                self.assertEqual(self.router.report()["capacity"][provider]["source"],
                                 BROKER_PROBE_SOURCE)
                self.probe.assert_called_once_with(provider)

    def test_presence_clears_refusal_only_after_fresh_until(self):
        for provider in PROVIDERS:
            self.observe(provider, available=False, source="usage:over-ceiling")
            expires = self.now + 100
            gate._write_presence(provider, str(self.router.path), expires)
            self.assertEqual(self.router.report()["capacity"][provider]["source"],
                             "usage:over-ceiling")
            gate._write_presence(provider, str(self.router.path), expires + 1)
            row = self.router.report()["capacity"][provider]
            self.assertEqual((row["source"], row["trusted"]), (PRESENCE_SOURCE, True))

    def test_refusal_expiry_is_judged_at_decision_time_not_observation_time(self):
        for provider in PROVIDERS:
            # Refusal expires exactly now; presence is stamped half a second ahead.
            self.router.observe_capacity(CapacityObservation(
                provider, self.now - 100, self.now, False, "usage:over-ceiling"),
                trusted=True)
            self.router.observe_capacity(CapacityObservation(
                provider, self.now + 0.5, self.now + 60, True, PRESENCE_SOURCE),
                trusted=True)
            self.assertEqual(self.router.report()["capacity"][provider]["source"],
                             "usage:over-ceiling")
            # A delayed presence stamped before an expired refusal still clears it.
            self.router.observe_capacity(CapacityObservation(
                provider, self.now - 100, self.now - 1, False, "usage:over-ceiling"),
                trusted=True)
            self.router.observe_capacity(CapacityObservation(
                provider, self.now - 2, self.now + 60, True, PRESENCE_SOURCE),
                trusted=True)
            self.assertEqual(self.router.report()["capacity"][provider]["source"],
                             PRESENCE_SOURCE)

    def test_unexpected_probe_error_is_a_cached_failure(self):
        for provider in PROVIDERS:
            self.observe(provider, stale=True)
            self.probe.side_effect = ValueError("synthetic defect")
            self.assertEqual(self.router._refresh_harness_presence([provider]),
                             {provider: "harness_probe_failed"})
            self.probe.side_effect = None
            self.assertEqual(self.router._refresh_harness_presence([provider]),
                             {provider: "harness_probe_failed"})
            self.assertEqual(self.probe.call_count, 1)
            self.probe.reset_mock()

    def test_fresh_interactive_presence_does_not_probe(self):
        for provider in PROVIDERS:
            self.observe(provider)
            self.assertEqual(self.claim(provider, provider)["state"], "owned")
        self.probe.assert_not_called()

    def test_hook_default_sqlite_connection_preserves_quota_and_refreshes_presence(self):
        for provider in PROVIDERS:
            self.observe(provider, stale=True)
            gate._write_presence(provider, str(self.router.path), self.now)
            self.assertEqual(self.router.report()["capacity"][provider]["status"], "available")
            self.observe(provider, available=False, source="quota:exhausted")
            gate._write_presence(provider, str(self.router.path), self.now)
            self.assertEqual(self.router.report()["capacity"][provider]["source"], "quota:exhausted")

    def test_mcp_claim_and_dispatch_refresh_and_reject_capacity_assertions(self):
        service = Service(self.root / "local")
        execution = SimpleNamespace(client_derived_routes=frozenset(), root=self.root / "execution",
                                    submit=mock.Mock(return_value={"job_id": "synthetic"}),
                                    status=mock.Mock(), result=mock.Mock())
        state = self.root / "state"
        state.mkdir()
        repo = str(self.root / "repo")
        Path(repo).mkdir()
        policy_path = Path(autoroute.policy_path(str(state)))
        policy_path.parent.mkdir()
        policy_path.write_text(json.dumps({
            "version": 1, "repos": {repo: {"classification": "synthetic",
                                            "allowed_routes": list(PROVIDERS)}}}))
        for provider in PROVIDERS:
            caller = "claude" if provider == "codex" else "codex"
            server = Server(caller, service, self.router, execution=execution, state_root=str(state))
            self.addCleanup(server.stop)
            self.assertNotIn("capacity_observe", server.tools)
            self.assertFalse(any("probe" in name for name in server.tools))
            item = "mcp-" + provider
            self.router.register(item, "build", allowed_routes=[provider])
            claim = server.tools["stage_claim"]["handler"]({
                "item_id": item, "stage": "build", "owner_id": "worker",
                "expected_revision": 0, "lease_seconds": 300})
            self.assertEqual(claim["state"], "owned")
            args = dict(item_id=item, stage="build", owner_id="worker", stage_revision=1,
                        provider=provider, repo=repo, brief="synthetic", base="HEAD",
                        model="test-model", effort="low")
            self.now += 61
            self.probe.reset_mock()
            self.probe.return_value = False
            response = server.tools["execution_dispatch"]["handler"](args)
            self.assertEqual(response, {"ok": False, "error": "harness_probe_failed"})
            self.probe.assert_called_once_with(provider)
            execution.submit.assert_not_called()
            self.now += 61
            self.probe.return_value = True
            response = server.tools["execution_dispatch"]["handler"](args)
            self.assertTrue(response["ok"], response)
            execution.submit.assert_called_once()
            execution.submit.reset_mock()


class ProbeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / "fake-cli"
        self.bin.write_text("synthetic, never executed")
        self.bin.chmod(0o700)
        self.claude = self.home / ".agent-bridge" / "claude-home"
        self.claude.mkdir(parents=True, mode=0o700)
        (self.home / ".agent-bridge" / "codex-home").mkdir(mode=0o700)
        self.calls = []
        self.tick = 100.0

    def response(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.tick += 0.5
        if "--version" in argv:
            output = b"Synthetic CLI 1.0"
        elif "--json" in argv:
            output = b'{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"max"}'
        else:
            output = b"Logged in using ChatGPT"
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"", spawn_failed=False,
                               timed_out=False, cap_exceeded=False, descendant_held_pipes=False)

    def probe(self, run=None):
        return HarnessReadinessProbe(dict.fromkeys(PROVIDERS, self.bin), home=self.home,
            env={"PATH": str(self.home / "fake-path"), "OPENAI_API_KEY": "never inherited"},
            claude_config_dir=self.claude, run=run or self.response, monotonic=lambda: self.tick)

    def test_commands_are_non_inference_pinned_and_bounded_in_lane_stores(self):
        probe = self.probe()
        for provider in PROVIDERS:
            self.calls.clear()
            self.assertTrue(probe(provider))
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(self.calls[0][0], [str(self.bin), "--version"])
            expected = (["login", "status"] if provider == "codex" else
                        ["--safe-mode", "--setting-sources", "", "auth", "status", "--json"])
            self.assertEqual(self.calls[1][0], [str(self.bin), *expected])
            for _, kwargs in self.calls:
                self.assertEqual(kwargs["stdin_data"], "")
                self.assertLessEqual(kwargs["timeout"], PROBE_TIMEOUT_SECONDS)
                self.assertGreater(kwargs["timeout"], 0)
                self.assertEqual(kwargs["stdout_cap"], PROBE_OUTPUT_BYTES)
                self.assertEqual(kwargs["stderr_cap"], PROBE_OUTPUT_BYTES)
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertEqual(kwargs["env"]["HOME"], str(self.home))
                selector = "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"
                self.assertEqual(kwargs["cwd"], kwargs["env"][selector])
            self.assertEqual(self.calls[0][1]["deadline"], self.calls[1][1]["deadline"])
            self.assertLess(self.calls[1][1]["timeout"], self.calls[0][1]["timeout"])

    def test_timeout_spawn_error_bad_auth_and_capture_failure_are_closed(self):
        for provider in PROVIDERS:
            for failure in (TimeoutError(), OSError(), subprocess.TimeoutExpired("fake", 4)):
                with self.subTest(provider=provider, failure=failure):
                    self.assertFalse(self.probe(mock.Mock(side_effect=failure))(provider))
            for field in ("timed_out", "spawn_failed", "cap_exceeded", "descendant_held_pipes"):
                def failing(argv, **kwargs):
                    result = self.response(argv, **kwargs)
                    setattr(result, field, True)
                    return result
                self.assertFalse(self.probe(failing)(provider))
            for output in (b"garbage", b"Not logged in", b'{"loggedIn":false}',
                           b'{"loggedIn":true,"authMethod":"api_key"}',
                           b'{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":{}}'):
                def bad_auth(argv, **kwargs):
                    result = self.response(argv, **kwargs)
                    if "--version" not in argv:
                        result.stdout = output
                    return result
                self.assertFalse(self.probe(bad_auth)(provider))

    def test_version_cannot_consume_a_second_timeout_budget(self):
        for provider in PROVIDERS:
            run = mock.Mock(side_effect=lambda argv, **kwargs: self.response(argv, **kwargs))
            def slow_version(argv, **kwargs):
                self.tick += PROBE_TIMEOUT_SECONDS
                return run(argv, **kwargs)
            self.assertFalse(self.probe(slow_version)(provider))
            self.assertEqual(run.call_count, 1)

    def test_config_pins_provider_resolution_once_without_running_a_cli(self):
        cfg = SimpleNamespace(python_executable=self.bin, codex_task_executable=self.bin,
                              claude_task_executable=self.bin, claude_config_dir=self.claude)
        env = {"HOME": str(self.home), "PATH": str(self.home / "synthetic-path")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("agent_bridge.orchestration.harness_probe.os.name", "posix"), \
                mock.patch("agent_bridge.orchestration.harness_probe.shutil.which",
                           return_value=str(self.bin)) as resolve, \
                mock.patch("agent_bridge.orchestration.harness_probe.Path.home",
                           return_value=self.home):
            probe = HarnessReadinessProbe.for_config(cfg)
        self.assertEqual(resolve.call_args_list, [mock.call(p) for p in PROVIDERS])
        self.assertEqual(probe.executables, dict.fromkeys(PROVIDERS, self.bin.resolve()))
        probe.run = self.response
        with mock.patch("agent_bridge.orchestration.harness_probe.shutil.which",
                        side_effect=AssertionError("must use pinned path")):
            for provider in PROVIDERS:
                self.assertTrue(probe(provider))

    def test_missing_binary_and_wrong_store_do_not_spawn(self):
        run = mock.Mock(side_effect=AssertionError("must not spawn"))
        probe = self.probe(run)
        probe.executables = dict.fromkeys(PROVIDERS, self.home / "missing")
        for provider in PROVIDERS:
            self.assertFalse(probe(provider))
        probe.executables = {"claude": self.bin}
        probe.claude_config_dir = self.home / ".claude"
        self.assertFalse(probe("claude"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
