"""Boundary cases found by an offline mutation sweep of the gate."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import autoroute, gate  # noqa: E402
from agent_bridge.capacity_router import RoutingError  # noqa: E402
from test_delegation_gate import GateCase, ReadGateCase  # noqa: E402


class ReceiptNameMutationTests(unittest.TestCase):
    def test_receipt_name_uses_a_distinct_slot_for_another_clients_retain(self):
        shared = gate.receipt_name("/invented/repository")
        retained = gate.receipt_name("/invented/repository", "claude")
        self.assertEqual(retained, shared[:-5] + ".retain-claude.json")


class DecisionLockMutationTests(unittest.TestCase):
    def test_decision_lock_does_not_resolve_a_relative_repository(self):
        with mock.patch.object(gate, "repo_key", return_value="/invented/repository") as key, \
             mock.patch.object(gate.store, "file_lock") as file_lock:
            with gate.decision_lock("/invented/state", "relative-repository"):
                pass
        key.assert_not_called()
        file_lock.assert_not_called()


class RecordDecisionMutationTests(GateCase):
    def test_record_decision_rejects_a_lease_ending_at_the_decision_time(self):
        with self.assertRaisesRegex(RoutingError, "stage_lease_expired"):
            self.receipt(lease_until=1000.0)


class SingleDecisionMutationTests(unittest.TestCase):
    def test_single_decision_initializes_the_output_lock(self):
        out = gate._SingleDecision()
        with mock.patch.object(gate.sys, "stdout", io.StringIO()) as stream:
            self.assertTrue(out.emit("first"))
            self.assertFalse(out.emit("second"))
            self.assertEqual(stream.getvalue(), "first")

    def test_single_decision_emit_reports_its_first_write(self):
        out = gate._SingleDecision()
        with mock.patch.object(gate.sys, "stdout", io.StringIO()):
            self.assertIs(out.emit("decision"), True)


class WatchdogMutationTests(unittest.TestCase):
    def test_watchdog_seconds_rejects_zero(self):
        with mock.patch.dict(os.environ, {gate.HOOK_WATCHDOG_ENV: "0"}):
            self.assertEqual(gate._watchdog_seconds(), gate.HOOK_WATCHDOG_SECONDS)

    def test_arm_watchdog_returns_the_started_timer(self):
        timer = mock.Mock()
        with mock.patch.object(gate.threading, "Timer", return_value=timer):
            self.assertIs(gate._arm_watchdog(gate._SingleDecision()), timer)
        timer.start.assert_called_once_with()

    def test_arm_watchdog_fire_exits_after_emitting_a_deny(self):
        output = mock.Mock()
        output.emit.return_value = True
        with mock.patch.object(gate.threading, "Timer") as timer, \
             mock.patch.object(gate.os, "_exit") as exit_process:
            gate._arm_watchdog(output)
            timer.call_args.args[1]()
        exit_process.assert_called_once_with(0)


class CapacityDigestMutationTests(unittest.TestCase):
    def test_capacity_digest_returns_a_fingerprint_for_an_empty_table(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "capacity.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE capacity (route TEXT)")
                db.commit()
            finally:
                db.close()
            with mock.patch.object(gate, "capacity_fingerprint", return_value="empty-capacity"):
                self.assertEqual(gate.capacity_digest(str(path), 1000.0), "empty-capacity")


class WorktreePruneMutationTests(unittest.TestCase):
    def test_worktree_prune_accepts_a_shell_escaped_space_in_the_checkout_path(self):
        with tempfile.TemporaryDirectory() as home:
            base = Path(home)
            repo = base / "invented-repository"
            (repo / ".git").mkdir(parents=True)
            checkout_root = base / "linked checkouts"
            target = checkout_root / "finished"
            target.mkdir(parents=True)
            (target / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
            config = base / "orchestration.json"
            config.write_text(json.dumps({"worktree_prune_roots": [str(checkout_root)]}), encoding="utf-8")
            if os.name == "nt":
                command = f'git -C "{repo}" worktree remove "{target}"'
            else:
                command = (f"git -C {shlex.quote(str(repo))} worktree remove "
                           f"{str(target).replace(' ', chr(92) + ' ')}")
            decision = gate._worktree_prune_decision(command, str(repo), str(config), ())
            self.assertEqual(decision.code, "worktree_prune_root")


class ReadToolPathsMutationTests(unittest.TestCase):
    def test_read_tool_paths_ignores_a_non_string_path(self):
        self.assertEqual(gate._read_tool_paths({"file_path": 42}, "/invented/repository"), [])


class JudgeWriteMutationTests(GateCase):
    def write_decision(self, decide=None):
        return gate._judge_write(
            "claude", "edit", [str(self.repo / "new.py")], {}, str(self.repo),
            state_root=str(self.state), clock=self.clock, protected=(), capacity_db=None,
            decide=decide)

    def test_judge_write_expires_its_budget_at_the_exact_deadline(self):
        with mock.patch.object(gate.time, "monotonic", side_effect=[0.0, 0.0, 0.0, 7.0]), \
             mock.patch.object(gate, "decision_lock", return_value=contextlib.nullcontext()):
            decision = self.write_decision()
        self.assertEqual(decision.code, "gate_state_unavailable")

    def test_judge_write_redecides_an_overtaken_receipt(self):
        receipt = {"repo": str(self.repo), "caller": "claude", "owner_route": "claude",
                   "valid_until": 2000.0}
        decide = mock.Mock(return_value=SimpleNamespace(receipt=None))
        with mock.patch.object(gate, "read_receipt", return_value=receipt), \
             mock.patch.object(gate, "receipt_overtaken", return_value=True):
            self.write_decision(decide)
        decide.assert_called_once()

    def test_judge_write_accepts_the_receipt_reported_by_its_decider(self):
        receipt = {"repo": os.path.realpath(self.repo), "caller": "claude", "owner_route": "claude",
                   "valid_until": 2000.0, "item_id": "invented-item", "stage": "implementation"}
        decide = mock.Mock(return_value=SimpleNamespace(receipt=receipt))
        with mock.patch.object(gate, "read_receipt", return_value=None):
            decision = self.write_decision(decide)
        self.assertEqual(decision.code, "routing_receipt_valid")

    def test_judge_write_hides_unvetted_decider_exception_text(self):
        decide = mock.Mock(side_effect=ValueError("private synthetic detail"))
        decision = self.write_decision(decide)
        self.assertEqual(decision.code, "gate_auto_decision_failed")
        self.assertNotIn("private synthetic detail", decision.reason)


class AutomaticDeciderMutationTests(unittest.TestCase):
    def test_automatic_decider_passes_the_local_queue_root(self):
        with mock.patch("agent_bridge.orchestration.autodecide.ensure_decision") as ensure:
            gate.automatic_decider("claude", "/invented/state", "/invented/capacity",
                                   "/invented/queue")("/invented/repo", "implementation")
        self.assertEqual(ensure.call_args.kwargs["local_queue_root"], "/invented/queue")


class PresenceMutationTests(unittest.TestCase):
    def test_presence_failure_is_logged_at_the_rate_limit_boundary(self):
        with mock.patch.object(gate, "_write_presence", side_effect=OSError("offline")), \
             mock.patch.object(gate, "_last_presence_failure", return_value=940.0), \
             mock.patch.object(gate.store, "append_ledger") as append:
            self.assertFalse(gate.observe_presence_best_effort(
                "claude", "/invented/state", "/invented/capacity", clock=lambda: 1000.0))
        append.assert_called_once()


class ConfigMutationTests(unittest.TestCase):
    def test_gate_paths_selects_the_certified_delegate(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / "orchestration.json"
            config.write_text(json.dumps({"state_root": str(Path(home) / "state"),
                                          "capacity_db": str(Path(home) / "capacity.sqlite3"),
                                          "local_backend": "gemma_certified",
                                          "worker_executable": "/invented/legacy-worker",
                                          "gemma_delegate_executable": "/invented/certified-delegate"}),
                              encoding="utf-8")
            self.assertEqual(gate.gate_paths_from_config(str(config))[3],
                             "/invented/certified-delegate")

    def test_gemma_pilot_tasks_are_enabled_only_for_the_certified_backend(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / "orchestration.json"
            config.write_text(json.dumps({"local_backend": "gemma_certified",
                                          "gemma_pilot_tasks": ["extract"]}), encoding="utf-8")
            self.assertEqual(gate.gemma_pilot_tasks_from_config(str(config)), frozenset({"extract"}))


class HookInputMutationTests(unittest.TestCase):
    def test_hook_input_returns_decoded_utf8_bytes(self):
        stdin = SimpleNamespace(buffer=io.BytesIO("caf\u00e9".encode("utf-8")))
        with mock.patch.object(gate.sys, "stdin", stdin):
            self.assertEqual(gate._hook_input(), "caf\u00e9")


class ReportMutationTests(unittest.TestCase):
    def test_automatic_routing_state_returns_both_clients(self):
        with tempfile.TemporaryDirectory() as home:
            paths = {"claude_settings": str(Path(home) / "claude.json"),
                     "codex_hooks": str(Path(home) / "codex.json")}
            self.assertEqual(gate._automatic_routing_state(paths),
                             {"claude": "not_installed", "codex": "not_installed"})

    def test_policy_state_summarizes_a_valid_policy(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "routing-policy.json"
            path.write_text("{}", encoding="utf-8")
            policy = SimpleNamespace(repos={"/invented/repo": object()},
                                     default=SimpleNamespace(allowed_routes=()))
            with mock.patch.object(autoroute, "policy_path", return_value=str(path)), \
                 mock.patch.object(autoroute, "load_policy", return_value=policy):
                summary = gate._policy_state(home)
            self.assertEqual(summary["classified_repositories"], 1)
            self.assertIs(summary["readable"], True)

    def test_report_expires_a_receipt_at_its_exact_deadline(self):
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.object(gate, "list_receipts", return_value=[{"valid_until": 1000.0}]):
            result = gate.report(home, str(Path(home) / "state"), clock=lambda: 1000.0)
        self.assertEqual(result["receipts"][0]["status"], "expired")


class PlanInstallMutationTests(unittest.TestCase):
    def test_plan_install_for_claude_does_not_update_the_codex_feature_flag(self):
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.object(gate, "codex_hooks_flag_update") as flag:
            gate.plan_install(home, "/invented/bridge", "/invented/config.json", ("claude",))
        flag.assert_not_called()


class MainMutationTests(unittest.TestCase):
    def test_main_install_dispatches_to_install(self):
        installed = {"applied": False}
        with mock.patch.object(gate, "install", return_value=installed) as install, \
             mock.patch.object(gate, "_arm_watchdog") as watchdog, \
             mock.patch.object(gate.sys, "stdout", io.StringIO()):
            self.assertEqual(gate.main(["install", "--root", "/invented/bridge",
                                        "--config", "/invented/config.json"]), 0)
        install.assert_called_once()
        watchdog.assert_not_called()


class ReadThresholdMutationTests(ReadGateCase):
    def test_judge_read_path_gates_a_file_exactly_at_the_size_threshold(self):
        self.write_policy(read_gate_min_bytes=8500)
        self.make_ready()
        target = self.write_log(size=8500)
        self.assertEqual(self.read("claude", target).code, "local_digest_required")


if __name__ == "__main__":
    unittest.main()
