"""Client-derived admission is symmetric and opt-in for both providers."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import broker, config
from agent_bridge.errors import BrokerError
from agent_bridge.execution import claude_task, codex_task
from agent_bridge.orchestration import autoroute
from agent_bridge.orchestration import config as orchestration_config
from agent_bridge.orchestration.execution_queue import (
    ExecutionAdmissionError, ExecutionQueue, reserve_nothing)


class ClientDerivedParityTests(unittest.TestCase):
    def setUp(self):
        loaded = config.load()
        self.raw = copy.deepcopy(loaded.raw)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw["state_root"] = str(self.root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def bridge_config(self, routes=()):
        raw = copy.deepcopy(self.raw)
        raw["client_derived_routes"] = list(routes)
        return config.Config(raw, config.DEFAULT_CONFIG_PATH)

    def test_default_peer_refusal_is_symmetric(self):
        cfg = self.bridge_config()
        for caller, peer in broker.PEER_OF.items():
            with self.subTest(caller=caller), self.assertRaises(BrokerError):
                broker._validate_common(
                    cfg, {"prompt": "fixture", "source_classification": "client_derived"},
                    broker.START_FIELDS, peer)

    def test_peer_opt_in_accepts_both_spellings_in_both_directions(self):
        cfg = self.bridge_config(("peer",))
        for caller, peer in broker.PEER_OF.items():
            for label in ("client_derived", "client-derived"):
                with self.subTest(caller=caller, label=label):
                    broker._validate_common(
                        cfg, {"prompt": "fixture", "source_classification": label},
                        broker.START_FIELDS, peer)
                    self.assertIn("client_derived", cfg.peer_allowed_classifications(peer))

    def test_execution_default_refusal_and_opt_in_queue_symmetry(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        brief = self.root / "brief.md"
        brief.write_text("fixture", encoding="utf-8")
        for routes, accepted in (((), False), (("execution",), True)):
            queue = ExecutionQueue(self.root / ("queue-" + str(bool(routes))), None,
                                   model_reserved=reserve_nothing,
                                   client_derived_routes=frozenset(routes))
            for caller, provider in (("claude", "codex"), ("codex", "claude")):
                with self.subTest(routes=routes, caller=caller):
                    args = dict(caller=caller, provider=provider, repo=str(repo), brief=str(brief),
                                base="HEAD", classification="client_derived", model="fixture",
                                effort="low", item_id=caller + str(bool(routes)), stage="stage",
                                owner_id="owner", stage_revision=0,
                                verify_argv=[["git", "status"]])
                    if accepted:
                        self.assertEqual(queue.submit(**args)["state"], "queued")
                    else:
                        with self.assertRaises(ExecutionAdmissionError):
                            queue.submit(**args)

    def test_orchestration_config_loads_the_execution_opt_in(self):
        config_path = self.root / "orchestration.json"
        state = self.root / "orchestration-state"
        config_path.write_text(json.dumps({
            "config_version": "1", "state_root": str(state),
            "local_queue_root": str(state / "local"),
            "capacity_db": str(state / "capacity.sqlite3"),
            "worker_executable": str(self.root / "worker"),
            "worker_state": str(state / "worker-state"),
            "client_derived_routes": ["execution"],
        }), encoding="utf-8")
        self.assertEqual(orchestration_config.load(config_path).client_derived_routes,
                         frozenset({"execution"}))

    def test_harnesses_accept_only_execution_opt_in(self):
        # The path errors prove the client-derived classification passed each
        # harness's first admission check, without invoking a provider CLI.
        with self.assertRaisesRegex(claude_task.TaskError, "refuses client-derived"):
            claude_task.run_task(
                brief=Path("brief"), repo=Path("repo"), task_root=Path("tasks"),
                claude_bin=Path("claude"), claude_config_dir=Path("config"),
                classification="client_derived", model="fixture", effort="low", verify_argv=[])
        with self.assertRaisesRegex(claude_task.TaskError, "all paths"):
            claude_task.run_task(
                brief=Path("brief"), repo=Path("repo"), task_root=Path("tasks"),
                claude_bin=Path("claude"), claude_config_dir=Path("config"),
                classification="client_derived", model="fixture", effort="low",
                client_derived_routes=("execution",), verify_argv=[])
        with self.assertRaisesRegex(codex_task.TaskError, "refuses client-derived"):
            codex_task._run_admitted(
                brief=Path("brief"), repo=Path("repo"), task_root=Path("tasks"),
                codex_bin=Path("codex"), admission=None, codex_home=Path("home"),
                classification="client_derived", model="fixture", reasoning_effort="low", verify_argv=[])
        with self.assertRaisesRegex(codex_task.TaskError, "all paths"):
            codex_task._run_admitted(
                brief=Path("brief"), repo=Path("repo"), task_root=Path("tasks"),
                codex_bin=Path("codex"), admission=None, codex_home=Path("home"),
                classification="client_derived", model="fixture", reasoning_effort="low",
                client_derived_routes=("execution",), verify_argv=[])

    def test_autoroute_prefers_eligible_local_then_routes_to_the_peer(self):
        for caller, peer in broker.PEER_OF.items():
            with self.subTest(caller=caller):
                policy = autoroute.Policy(
                    repos={"/repo": autoroute.RepoPolicy(
                        "client_derived", ("claude", "codex", "local"), mechanical_ok=True)},
                    client_derived_routes=frozenset({"execution"}),
                    peer_classifications=autoroute.PEER_CLASSIFICATIONS | {"client_derived"})
                local = autoroute.decide(
                    autoroute.Signal(client=caller, repo="/repo", task_type="mechanical"), policy,
                    fresh_routes=frozenset({"local", peer}), load=autoroute.Load(0.1, True))
                self.assertEqual(local.route, "local")
                peer_result = autoroute.decide(
                    autoroute.Signal(client=caller, repo="/repo", task_type="implementation"), policy,
                    fresh_routes=frozenset({peer}), load=autoroute.Load(0.1, True))
                self.assertEqual(peer_result.route, peer)

    def test_room_policy_follows_the_peer_opt_in_symmetrically(self):
        from agent_bridge.chat.policy import RoomPolicy
        off = RoomPolicy(self.bridge_config())
        on = RoomPolicy(self.bridge_config(("peer",)))
        self.assertFalse(off.allow_client)
        self.assertTrue(on.allow_client)
        for target in ("claude", "codex"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    off.authorize(target, "client-derived")
                on.authorize(target, "client-derived")

    def test_audit_counts_opted_in_client_execution_as_eligible(self):
        from agent_bridge.orchestration import audit
        row = {"considered": {"classification": "client_derived", "client": "claude",
                              "allowed_routes": ["claude", "codex"], "mechanical_ok": False,
                              "task_type": "implementation"}}
        self.assertFalse(audit._eligible(row))
        row["considered"]["client_derived_execution"] = True
        self.assertTrue(audit._eligible(row))

    def test_shared_instructions_follow_the_peer_route_and_keep_identifier_rule(self):
        from agent_bridge import onboard
        base = {"privacy": {"mode": "baseline", "peers": {}}, "local_ollama": {"enabled": False},
                "automatic_delegation": {"enabled": False}}
        execution_only = onboard._shared_instructions({**base, "client_derived_routes": ["execution"]})
        self.assertIn("Never route client-derived", execution_only)
        peer = onboard._shared_instructions({**base, "client_derived_routes": ["peer"]})
        self.assertIn("same access for both", peer)
        self.assertIn("exact sensitive identifiers", peer)

    def test_secret_and_credential_labels_remain_refused(self):
        cfg = self.bridge_config(("peer", "execution"))
        for caller, peer in broker.PEER_OF.items():
            for label in ("secret", "credential"):
                with self.subTest(caller=caller, label=label), self.assertRaises(BrokerError):
                    broker._validate_common(
                        cfg, {"prompt": "fixture", "source_classification": label},
                        broker.START_FIELDS, peer)
