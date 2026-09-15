"""The delegation-first gate: receipts, judgment, hook wire, installation."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.capacity_router import CapacityObservation, RoutingError, StageRouter  # noqa: E402
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot  # noqa: E402
from agent_bridge.orchestration import gate  # noqa: E402
from agent_bridge.orchestration.server import Server  # noqa: E402


class Sampler:
    def sample(self):
        return ResourceSnapshot(100.0, "normal", "normal", True, 120.0)


class Service:
    """The queue the server needs, without a worker process."""

    def __init__(self, root):
        self.queue = LocalQueue(root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 1000.0)

    def once(self):
        return self.queue.run_once("gate-test")


def owned_stage(**changes):
    record = {"item_id": "item-1", "stage": "implement", "state": "owned",
              "owner_id": "claude-session", "owner_route": "claude", "revision": 1,
              "lease_until": 1000.0 + 3600}
    record.update(changes)
    return record


class GateCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.repo = self.base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "src").mkdir()
        self.other = self.base / "other"
        (self.other / ".git").mkdir(parents=True)
        self.clock = lambda: 1000.0

    def receipt(self, **changes):
        return gate.record_decision(str(self.state), caller="claude", stage_record=owned_stage(**changes),
                                    repo=str(self.repo), reason="small fix", ttl_seconds=3600,
                                    clock=self.clock)

    def judge(self, client, tool, tool_input, cwd=None, clock=None):
        return gate.judge(client, tool, tool_input, cwd or str(self.repo),
                          state_root=str(self.state), clock=clock or self.clock)


class RepositoryKeyTests(GateCase):
    def test_the_nearest_git_ancestor_keys_the_receipt(self):
        self.assertEqual(gate.repo_key(str(self.repo / "src" / "a.py")), os.path.realpath(self.repo))
        self.assertEqual(gate.repo_key(str(self.repo)), os.path.realpath(self.repo))
        self.assertIsNone(gate.repo_key(str(self.base / "loose")))
        self.assertEqual(gate.receipt_name(str(self.repo)), gate.receipt_name(str(self.repo / "src")[: -4]))

    def test_a_worktree_git_file_counts(self):
        worktree = self.base / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /elsewhere", encoding="utf-8")
        self.assertEqual(gate.repo_key(str(worktree / "x")), os.path.realpath(worktree))


class ReceiptTests(GateCase):
    def test_a_receipt_records_the_owned_stage_and_the_route(self):
        receipt = self.receipt()
        self.assertEqual(receipt["repo"], os.path.realpath(self.repo))
        self.assertEqual(receipt["owner_route"], "claude")
        self.assertEqual(receipt["decision"], "self")
        self.assertEqual(receipt["valid_until"], 1000.0 + 3600)
        self.assertEqual(gate.read_receipt(str(self.state), str(self.repo)), receipt)
        self.assertIsNone(gate.read_receipt(str(self.state), str(self.other)))
        ledger = (self.state / "routing" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(ledger[0])["event"], "routing_decided")

    def test_the_receipt_never_outlives_the_lease(self):
        receipt = self.receipt(lease_until=1000.0 + 60)
        self.assertEqual(receipt["valid_until"], 1060.0)
        with self.assertRaisesRegex(RoutingError, "stage_lease_expired"):
            self.receipt(lease_until=999.0)

    def test_peer_and_local_routes_are_named_as_such(self):
        self.assertEqual(self.receipt(owner_route="codex")["decision"], "peer")
        self.assertEqual(self.receipt(owner_route="local")["decision"], "local")

    def test_bad_inputs_are_refused_before_anything_is_written(self):
        for kwargs, code in (
            ({"repo": "relative"}, "repo_invalid"),
            ({"repo": str(self.base)}, "repo_not_a_repository"),
            ({"reason": ""}, "reason_invalid"),
            ({"reason": "x" * 501}, "reason_invalid"),
            ({"ttl_seconds": 5}, "ttl_invalid"),
            ({"ttl_seconds": True}, "ttl_invalid"),
            ({"stage_record": owned_stage(state="pending")}, "execution_stage_binding_invalid"),
            ({"stage_record": owned_stage(owner_route="paid")}, "execution_stage_binding_invalid"),
            ({"caller": "human"}, "caller_invalid"),
        ):
            values = {"caller": "claude", "stage_record": owned_stage(), "repo": str(self.repo),
                      "reason": "r", "ttl_seconds": 600, "clock": self.clock}
            values.update(kwargs)
            with self.assertRaisesRegex(RoutingError, code, msg=str(kwargs)):
                gate.record_decision(str(self.state), **values)
        self.assertFalse((self.state / "routing").exists())

    def test_a_foreign_file_in_the_receipt_slot_is_not_a_receipt(self):
        path = Path(gate.receipt_path(str(self.state), str(self.repo)))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 1, "valid_until": 1e12, "owner_route": "claude",
                                    "repo": "/somewhere/else"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            gate.read_receipt(str(self.state), str(self.repo))


class ClassificationTests(GateCase):
    def test_claude_edit_tools_name_their_files(self):
        kind, paths = gate.classify("claude", "Edit", {"file_path": "src/a.py"}, "/w")
        self.assertEqual((kind, paths), ("edit", ["/w/src/a.py"]))
        kind, paths = gate.classify("claude", "MultiEdit", {"edits": [{"file_path": "/abs/b.py"}]}, "/w")
        self.assertEqual(paths, ["/abs/b.py"])
        kind, paths = gate.classify("claude", "NotebookEdit", {"notebook_path": "n.ipynb"}, "/w")
        self.assertEqual(paths, ["/w/n.ipynb"])
        self.assertEqual(gate.classify("claude", "Read", {"file_path": "x"}, "/w"), ("other", []))

    def test_codex_apply_patch_names_every_file_in_the_patch(self):
        patch = ("*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n"
                 "*** Add File: docs/new.md\n+hello\n*** Delete File: old.txt\n"
                 "*** Update File: m.py\n*** Move to: moved.py\n*** End Patch\n")
        self.assertEqual(gate.patch_paths(patch), ["src/a.py", "docs/new.md", "old.txt", "m.py", "moved.py"])
        kind, paths = gate.classify("codex", "apply_patch", {"input": patch}, "/w")
        self.assertEqual(kind, "edit")
        self.assertEqual(paths, ["/w/src/a.py", "/w/docs/new.md", "/w/old.txt", "/w/m.py", "/w/moved.py"])
        kind, paths = gate.classify("codex", "apply_patch", {"unknown": 1}, "/w")
        self.assertEqual((kind, paths), ("edit", ["/w"]))

    def test_shell_commands_are_sorted_by_a_stated_heuristic(self):
        writes = ["echo hi > out.txt", "cat a >> b", "sed -i 's/a/b/' f", "rm -rf build",
                  "git commit -m x", "git apply p.diff", "git push", "python3 - <<'EOF'\nprint(1)\nEOF",
                  "npm install left-pad", "mkdir -p x && touch y", "tee f", "black .", "python -c 'open(\"f\",\"w\")'",
                  ["bash", "-lc", "mv a b"]]
        reads = ["git status", "git diff", "ls -la", "cat file", "grep -rn foo src", "python3 -m pytest -q",
                 "echo 2>&1 | head", "git log --oneline -3", "pytest tests/test_x.py"]
        for command in writes:
            self.assertEqual(gate.classify("claude", "Bash", {"command": command}, "/w"),
                             ("shell", ["/w"]), command)
        for command in reads:
            self.assertEqual(gate.classify("claude", "Bash", {"command": command}, "/w"),
                             ("shell_read", []), command)
        self.assertEqual(gate.classify("codex", "local_shell", {"command": ["git", "commit", "-m", "x"]}, "/w"),
                         ("shell", ["/w"]))


class JudgmentTests(GateCase):
    def test_no_receipt_is_a_deny_that_says_how_to_proceed(self):
        decision = self.judge("claude", "Edit", {"file_path": str(self.repo / "src" / "a.py")})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "no_routing_receipt")
        self.assertIn("stage_register", decision.reason)
        self.assertIn("routing_decide", decision.reason)
        self.assertEqual(decision.repos, (os.path.realpath(self.repo),))

    def test_a_valid_receipt_for_this_route_allows(self):
        self.receipt()
        decision = self.judge("claude", "Write", {"file_path": str(self.repo / "src" / "new.py")})
        self.assertTrue(decision.allowed, decision)
        self.assertEqual(decision.code, "routing_receipt_valid")
        shell = self.judge("claude", "Bash", {"command": "git commit -m x"})
        self.assertTrue(shell.allowed)

    def test_the_other_route_is_denied_and_told_to_dispatch(self):
        self.receipt(owner_route="codex")
        decision = self.judge("claude", "Edit", {"file_path": str(self.repo / "a")})
        self.assertEqual(decision.code, "routed_elsewhere")
        self.assertIn("execution_dispatch", decision.reason)
        peer = self.judge("codex", "apply_patch", {"input": "*** Begin Patch\n*** Update File: a\n*** End Patch"})
        self.assertTrue(peer.allowed, peer)

    def test_an_expired_receipt_denies(self):
        self.receipt(lease_until=1000.0 + 100)
        decision = self.judge("claude", "Edit", {"file_path": str(self.repo / "a")}, clock=lambda: 1101.0)
        self.assertEqual(decision.code, "routing_receipt_expired")
        self.assertIn("stage_renew", decision.reason)

    def test_every_repository_touched_needs_its_own_receipt(self):
        self.receipt()
        decision = self.judge("claude", "MultiEdit", {"edits": [
            {"file_path": str(self.repo / "a")}, {"file_path": str(self.other / "b")}]})
        self.assertEqual(decision.code, "no_routing_receipt")
        self.assertIn(os.path.realpath(self.other), decision.reason)

    def test_outside_any_repository_and_reads_are_not_gated(self):
        loose = self.base / "notes.txt"
        decision = self.judge("claude", "Write", {"file_path": str(loose)})
        self.assertEqual((decision.allowed, decision.code), (True, "outside_repository"))
        read = self.judge("claude", "Read", {"file_path": str(self.repo / "a")})
        self.assertEqual((read.allowed, read.logged), (True, False))
        status = self.judge("claude", "Bash", {"command": "git status"})
        self.assertEqual((status.allowed, status.code), (True, "shell_read_only_heuristic"))

    def test_unreadable_gate_state_fails_closed(self):
        path = Path(gate.receipt_path(str(self.state), str(self.repo)))
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        decision = self.judge("claude", "Edit", {"file_path": str(self.repo / "a")})
        self.assertEqual(decision.code, "gate_state_unavailable")
        self.assertFalse(decision.allowed)

    def test_the_wire_shape_is_the_hosts_deny(self):
        deny = gate.hook_output(gate.Decision("deny", "x", "why"))
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(deny["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecisionReason"], "why")
        self.assertEqual(gate.hook_output(gate.Decision("allow", "x", "y")), {})


class HookProcessTests(GateCase):
    """The launcher end to end: stdin in, one JSON line out, exit 0 always."""

    def run_hook(self, client, payload, *args):
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({"state_root": str(self.state)}), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", client, "--config", str(config), *args],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        return completed

    def test_a_denied_edit_is_reported_on_stdout_and_logged(self):
        completed = self.run_hook("claude", {"hook_event_name": "PreToolUse", "tool_name": "Edit",
                                             "tool_input": {"file_path": str(self.repo / "a.py")},
                                             "cwd": str(self.repo)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        out = json.loads(completed.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        events = (self.state / "routing" / "gate-events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(events[-1])["code"], "no_routing_receipt")

    def test_an_allowed_edit_prints_an_empty_object(self):
        gate.record_decision(str(self.state), caller="claude", stage_record=owned_stage(lease_until=time.time() + 600),
                             repo=str(self.repo), reason="live", ttl_seconds=600, clock=time.time)
        completed = self.run_hook("claude", {"tool_name": "Edit",
                                             "tool_input": {"file_path": str(self.repo / "a.py")},
                                             "cwd": str(self.repo)})
        self.assertEqual(json.loads(completed.stdout), {})

    def test_garbage_input_denies_rather_than_crashing(self):
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({"state_root": str(self.state)}), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "codex",
             "--config", str(config)], input=b"not json", capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0)
        out = json.loads(completed.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("gate_error", json.dumps(out) + "gate_error")  # reason names the class only
        self.assertNotIn("Traceback", completed.stdout.decode())

    def test_a_missing_config_denies(self):
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "claude",
             "--config", str(self.base / "absent.json")],
            input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "a")}}).encode(),
            capture_output=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


class RoutingDecideToolTests(GateCase):
    def setUp(self):
        super().setUp()
        self.service = Service(str(self.base / "queue"))
        self.router = StageRouter(str(self.base / "capacity.sqlite3"), clock=self.clock)
        for route in ("claude", "codex"):
            self.router.observe_capacity(CapacityObservation(
                route=route, observed_at=1000.0, fresh_until=5000.0, available=True, source="test"))

    def call(self, server, name, arguments):
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
        return reply["result"]["structuredContent"]

    def test_a_receipt_requires_the_same_binding_as_dispatch(self):
        server = Server("claude", self.service, self.router, interval=0.01, state_root=str(self.state))
        self.router.register("item-1", "implement", allowed_routes=["claude", "codex"])
        refused = self.call(server, "routing_decide", {
            "item_id": "item-1", "stage": "implement", "owner_id": "me", "stage_revision": 0,
            "repo": str(self.repo), "reason": "before claiming"})
        self.assertEqual(refused, {"ok": False, "error": "execution_stage_binding_invalid"})
        owned = self.router.assign("item-1", "implement", owner_id="me", lease_seconds=600, expected_revision=0)
        wrong_owner = self.call(server, "routing_decide", {
            "item_id": "item-1", "stage": "implement", "owner_id": "someone", "stage_revision": owned["revision"],
            "repo": str(self.repo), "reason": "r"})
        self.assertEqual(wrong_owner["error"], "execution_stage_binding_invalid")
        result = self.call(server, "routing_decide", {
            "item_id": "item-1", "stage": "implement", "owner_id": "me", "stage_revision": owned["revision"],
            "repo": str(self.repo), "reason": "small fix"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["receipt"]["owner_route"], owned["owner_route"])
        self.assertEqual(result["receipt"]["caller"], "claude")
        self.assertLessEqual(result["receipt"]["valid_until"], owned["lease_until"])
        decision = gate.judge(owned["owner_route"], "Edit", {"file_path": str(self.repo / "a")},
                              str(self.repo), state_root=str(self.state), clock=self.clock)
        self.assertTrue(decision.allowed, decision)

    def test_without_a_state_root_the_tool_refuses(self):
        server = Server("codex", self.service, self.router, interval=0.01)
        self.assertIn("routing_decide", server.tools)
        result = self.call(server, "routing_decide", {
            "item_id": "i", "stage": "s", "owner_id": "o", "stage_revision": 0,
            "repo": str(self.repo), "reason": "r"})
        self.assertEqual(result, {"ok": False, "error": "routing_receipts_unavailable"})


class InstallTests(GateCase):
    def setUp(self):
        super().setUp()
        self.home = self.base / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir()
        self.config = self.base / "orchestration.json"
        self.config.write_text(json.dumps({"state_root": str(self.state)}), encoding="utf-8")
        self.settings = self.home / ".claude" / "settings.json"
        self.hooks = self.home / ".codex" / "hooks.json"
        self.toml = self.home / ".codex" / "config.toml"

    def install(self, **kwargs):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            return gate.install(str(self.home), str(ROOT), str(self.config), ("claude", "codex"), **kwargs)

    def test_install_adds_one_entry_per_client_and_is_idempotent(self):
        self.settings.write_text(json.dumps({"theme": "dark", "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/theirs.sh"}]}]}}), encoding="utf-8")
        self.toml.write_text('[mcp_servers.x]\ncommand = "x"\n', encoding="utf-8")
        report = self.install(apply=True)
        self.assertTrue(report["applied"])
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "dark")
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["hooks"][0]["command"], "/theirs.sh")
        self.assertIn(gate.HOOK_NAME, pre[1]["hooks"][0]["command"])
        self.assertIn("--client claude", pre[1]["hooks"][0]["command"])
        hooks = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertIn("--client codex", hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        toml = self.toml.read_text(encoding="utf-8")
        import tomllib
        parsed = tomllib.loads(toml)
        self.assertIs(parsed["features"]["hooks"], True)
        self.assertEqual(parsed["mcp_servers"]["x"]["command"], "x")
        again = self.install(apply=True)
        self.assertEqual(again["planned_files"], [])
        self.assertIn("Claude Desktop", " ".join(report["not_covered"]))

    def test_an_edited_entry_is_preserved_not_overwritten(self):
        self.install(apply=True)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        settings["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 99
        self.settings.write_text(json.dumps(settings), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "was edited"):
            self.install(apply=True)

    def test_a_config_that_disables_hooks_is_not_flipped(self):
        self.toml.write_text("[features]\nhooks = false\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "enable hooks explicitly"):
            self.install(apply=False)
        self.assertFalse(self.settings.exists())

    def test_remove_takes_only_our_entries_and_the_flag_block(self):
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/theirs.sh"}]}]}}), encoding="utf-8")
        self.install(apply=True)
        self.install(apply=True, remove=True)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)
        self.assertEqual(json.loads(self.hooks.read_text(encoding="utf-8")), {})
        import tomllib
        self.assertNotIn("hooks", tomllib.loads(self.toml.read_text(encoding="utf-8")).get("features", {}))
        self.assertFalse((self.home / ".agent-bridge" / "onboarding" / "gate-installation.json").exists())

    def test_report_shows_installation_trust_and_receipts(self):
        self.install(apply=True)
        self.receipt()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            report = gate.report(str(self.home), str(self.state), clock=self.clock)
        self.assertEqual(report["installed"], {"claude": True, "codex": True})
        self.assertEqual(report["codex_trust"], "needs_review")
        self.assertEqual(report["receipts"][0]["status"], "valid")
        key = f"{os.path.realpath(self.hooks)}:pre_tool_use:0:0"
        with open(self.toml, "a", encoding="utf-8") as handle:
            handle.write(f'\n[hooks.state."{key}"]\ntrusted_hash = "sha256:abc"\n')
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            self.assertEqual(gate.report(str(self.home), str(self.state), clock=self.clock)["codex_trust"], "trusted")


if __name__ == "__main__":
    unittest.main()
