"""The delegation-first gate: receipts, judgment, hook wire, installation."""
from __future__ import annotations

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


class ReceiptIntegrityTests(GateCase):
    def test_the_audit_line_is_written_before_the_receipt(self):
        with mock.patch.object(gate.store, "append_ledger", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.receipt()
        self.assertFalse(Path(gate.receipt_path(str(self.state), str(self.repo))).exists())
        self.assertIsNone(gate.read_receipt(str(self.state), str(self.repo)))

    def test_non_finite_times_are_refused_on_both_sides(self):
        with self.assertRaisesRegex(RoutingError, "execution_stage_binding_invalid"):
            self.receipt(lease_until=float("inf"))
        with self.assertRaisesRegex(RoutingError, "clock_invalid"):
            gate.record_decision(str(self.state), caller="claude", stage_record=owned_stage(),
                                 repo=str(self.repo), reason="r", ttl_seconds=600, clock=lambda: float("nan"))
        written = self.receipt()
        path = Path(gate.receipt_path(str(self.state), str(self.repo)))
        path.write_text(json.dumps({**written, "valid_until": float("inf")}), encoding="utf-8")
        with self.assertRaises(ValueError):
            gate.read_receipt(str(self.state), str(self.repo))
        decision = self.judge("claude", "Edit", {"file_path": str(self.repo / "a")})
        self.assertEqual((decision.allowed, decision.code), (False, "gate_state_unavailable"))


class ClassificationTests(GateCase):
    def test_claude_edit_tools_name_their_files(self):
        cwd = os.path.abspath(os.sep + "w")
        kind, paths = gate.classify("claude", "Edit", {"file_path": "src/a.py"}, cwd)
        self.assertEqual((kind, paths), ("edit", [os.path.join(cwd, "src/a.py")]))
        absolute = os.path.abspath(os.sep + "abs" + os.sep + "b.py")
        kind, paths = gate.classify("claude", "MultiEdit", {"edits": [{"file_path": absolute}]}, cwd)
        self.assertEqual(paths, [absolute])
        kind, paths = gate.classify("claude", "NotebookEdit", {"notebook_path": "n.ipynb"}, cwd)
        self.assertEqual(paths, [os.path.join(cwd, "n.ipynb")])
        self.assertEqual(gate.classify("claude", "Read", {"file_path": "x"}, cwd), ("other", []))

    def test_codex_apply_patch_names_every_file_in_the_patch(self):
        patch = ("*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n"
                 "*** Add File: docs/new.md\n+hello\n*** Delete File: old.txt\n"
                 "*** Update File: m.py\n*** Move to: moved.py\n*** End Patch\n")
        self.assertEqual(gate.patch_paths(patch), ["src/a.py", "docs/new.md", "old.txt", "m.py", "moved.py"])
        cwd = os.path.abspath(os.sep + "w")
        kind, paths = gate.classify("codex", "apply_patch", {"input": patch}, cwd)
        self.assertEqual(kind, "edit")
        self.assertEqual(paths, [os.path.join(cwd, name) for name in
                                 ("src/a.py", "docs/new.md", "old.txt", "m.py", "moved.py")])
        kind, paths = gate.classify("codex", "apply_patch", {"unknown": 1}, cwd)
        self.assertEqual((kind, paths), ("edit", [cwd]))

    def test_shell_commands_are_sorted_by_a_stated_heuristic(self):
        writes = ["echo hi > out.txt", "cat a >> b", "sed -i 's/a/b/' f", "rm -rf build",
                  "git commit -m x", "git apply p.diff", "git push", "python3 - <<'EOF'\nprint(1)\nEOF",
                  "npm install left-pad", "mkdir -p x && touch y", "tee f", "black .", "python -c 'open(\"f\",\"w\")'",
                  ["bash", "-lc", "mv a b"], "ruff format src", "ruff check --fix src", "isort .",
                  "git tag v1.2.0", "git tag -a v1 -m x", "git tag -d v1", "prettier --write ."]
        reads = ["git status", "git diff", "ls -la", "cat file", "grep -rn foo src", "python3 -m pytest -q",
                 "echo 2>&1 | head", "git log --oneline -3", "pytest tests/test_x.py",
                 "ruff check src", "black --check .", "black --diff src/a.py", "isort --check-only .",
                 "git tag", "git tag --list 'v*'", "git tag -l", "git tag -n3", "git tag --contains abc",
                 "prettier --check ."]
        for command in writes:
            kind, paths = gate.classify("claude", "Bash", {"command": command}, "/w")
            # The working directory comes first; words that look like paths follow it.
            self.assertEqual((kind, paths[0]), ("shell", "/w"), command)
        for command in reads:
            self.assertEqual(gate.classify("claude", "Bash", {"command": command}, "/w"),
                             ("shell_read", []), command)
        kind, paths = gate.classify("codex", "local_shell", {"command": ["git", "commit", "-m", "x"]}, "/w")
        self.assertEqual((kind, paths[0]), ("shell", "/w"))
        # Codex names its shell tool Bash in hook input, and its patch text may travel in "command".
        kind, paths = gate.classify("codex", "Bash", {"command": "git commit -m x"}, "/w")
        self.assertEqual((kind, paths[0]), ("shell", "/w"))
        self.assertIn("Bash", gate.MATCHERS["codex"])
        kind, paths = gate.classify("codex", "apply_patch",
                                    {"command": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"}, "/w")
        self.assertEqual((kind, paths), ("edit", [os.path.join("/w", "a.py")]))
        for command in ("tar -xzf a.tgz", "tar xf a.tar", "tar -C out -xf a.tar", "tar -czf a.tgz src",
                        "unzip a.zip", "find . -name '*.pyc' -delete", "find src -exec rm {} \\;"):
            self.assertEqual(gate.classify("claude", "Bash", {"command": command}, "/w")[0], "shell", command)
        for command in ("tar -tzf a.tgz", "tar --list -f a.tar", "find . -name '*.py'"):
            self.assertEqual(gate.classify("claude", "Bash", {"command": command}, "/w")[0], "shell_read", command)


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

    def test_a_shell_write_aimed_at_another_repository_needs_that_receipt(self):
        self.receipt()          # only self.repo is receipted
        for command in (f"git -C {self.other} commit -m x", f"cd {self.other} && git commit -m x",
                        f"cp a.txt {self.other / 'b.txt'}", f"echo x > {self.other / 'c'}"):
            decision = self.judge("claude", "Bash", {"command": command})
            self.assertEqual(decision.code, "no_routing_receipt", (command, decision))
            self.assertIn(os.path.realpath(self.other), decision.repos)
        inside = self.judge("claude", "Bash", {"command": f"git -C {self.repo} commit -m x"})
        self.assertEqual(inside.code, "routing_receipt_valid")
        # A quoted path with spaces is one path, not a nested command to be split.
        spaced = self.base / "private repo"
        (spaced / ".git").mkdir(parents=True)
        for command in (f"git -C '{spaced}' commit -m x", f'cp a.txt "{spaced}/b.txt"',
                        ["git", "-C", str(spaced), "commit", "-m", "x"]):
            decision = self.judge("claude", "Bash", {"command": command})
            self.assertEqual(decision.code, "no_routing_receipt", (command, decision))
            self.assertIn(os.path.realpath(spaced), decision.repos)
        # A relative operand resolves against the working directory.
        relative = self.judge("claude", "Bash", {"command": "git -C other commit -m x"}, cwd=str(self.base))
        self.assertEqual(relative.code, "no_routing_receipt")
        self.assertIn(os.path.realpath(self.other), relative.repos)
        # The value of an inline option names its repository too.
        for command in (f"git --git-dir={self.other}/.git --work-tree={self.other} commit -m x",
                        f"git --git-dir='{spaced}/.git' commit -m x"):
            decision = self.judge("claude", "Bash", {"command": command})
            self.assertEqual(decision.code, "no_routing_receipt", (command, decision))
            self.assertTrue(decision.repos and set(decision.repos) & {os.path.realpath(self.other), os.path.realpath(spaced)},
                            (command, decision.repos))
        codex = gate.judge("codex", "Bash", {"command": f"git -C {self.other} push"}, str(self.repo),
                           state_root=str(self.state), clock=self.clock)
        self.assertEqual(codex.code, "no_routing_receipt")

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

    def test_the_gates_own_state_and_hook_files_are_protected_from_covered_tools(self):
        self.receipt()
        home = self.base / "home"
        database = str(self.base / "elsewhere" / "capacity.sqlite3")    # outside the state root
        protected = gate.protected_paths(str(self.state), str(self.base / "cfg.json"), str(home), database)
        forged = gate.receipt_path(str(self.state), str(self.repo))
        settings = os.path.join(str(home), ".claude", "settings.json")
        hooks = os.path.join(str(home), ".codex", "hooks.json")
        for root in (str(self.state), str(self.base / "cfg.json"), settings, hooks, database, database + "-wal"):
            self.assertIn(os.path.realpath(root), protected)
        cases = [("claude", "Write", {"file_path": forged}),
                 ("claude", "Edit", {"file_path": settings}),
                 ("claude", "Bash", {"command": f"echo '{{}}' > {forged}"}),
                 ("claude", "Bash", {"command": f"cp {self.base / 'mine.sqlite3'} {database}"}),
                 ("claude", "Write", {"file_path": database + "-wal"}),
                 # The directory above the database, moved or replaced wholesale.
                 ("claude", "Bash", {"command": f"mv {self.base / 'elsewhere'} {self.base / 'gone'}"}),
                 ("claude", "Bash", {"command": f"rm -rf {self.base / 'elsewhere'}"}),
                 ("claude", "Bash", {"command": f"cd {self.base} && rm -rf elsewhere"}),
                 ("claude", "Bash", {"command": f"tar --directory={self.base / 'elsewhere'} -xf a.tar"}),
                 ("claude", "Bash", {"command": f"rsync -a --delete mine/ --link-dest={self.base}/elsewhere/ {self.base}/x/"}),
                 ("claude", "Bash", {"command": f"cp -r {self.base / 'mine'} {self.base}/"}),
                 ("codex", "local_shell", {"command": ["rsync", "-a", "--delete", "mine/", str(self.base) + "/"]}),
                 ("codex", "apply_patch", {"input": f"*** Begin Patch\n*** Add File: {hooks}\n+x\n*** End Patch"}),
                 ("codex", "local_shell", {"command": ["sh", "-c", f"rm -f {hooks}"]})]
        for client, tool, tool_input in cases:
            decision = gate.judge(client, tool, tool_input, str(self.repo), state_root=str(self.state),
                                  clock=self.clock, protected=protected)
            self.assertEqual(decision.code, "gate_state_protected", (tool, decision))
            self.assertFalse(decision.allowed)
        # Reading the state is fine, and the repository itself is still judged by receipt.
        reading = gate.judge("claude", "Bash", {"command": f"cat {forged}"}, str(self.repo),
                             state_root=str(self.state), clock=self.clock, protected=protected)
        self.assertTrue(reading.allowed)
        # A write that only names an ancestor without acting on the tree is judged by receipt, not refused.
        commit = gate.judge("claude", "Bash", {"command": f"git -C {self.base} commit -m x"}, str(self.repo),
                            state_root=str(self.state), clock=self.clock, protected=protected)
        self.assertEqual(commit.code, "routing_receipt_valid")
        normal = gate.judge("claude", "Edit", {"file_path": str(self.repo / "a")}, str(self.repo),
                            state_root=str(self.state), clock=self.clock, protected=protected)
        self.assertEqual(normal.code, "routing_receipt_valid")

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
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecisionReason"], "why [x]")
        self.assertEqual(gate.hook_output(gate.Decision("allow", "x", "y")), {})


class StageBindingTests(GateCase):
    """The receipt points at live ownership; the router is asked on every allow."""

    def setUp(self):
        super().setUp()
        self.db = str(self.base / "capacity.sqlite3")
        self.router = StageRouter(self.db, clock=self.clock)
        self.router.observe_capacity(CapacityObservation(
            route="claude", observed_at=1000.0, fresh_until=5000.0, available=True, source="test"))
        self.router.register("item-1", "implement", allowed_routes=["claude"])
        self.owned = self.router.assign("item-1", "implement", owner_id="claude-session",
                                        lease_seconds=600, expected_revision=0)
        self.written = gate.record_decision(str(self.state), caller="claude", stage_record=self.owned,
                                            repo=str(self.repo), reason="r", ttl_seconds=600, clock=self.clock)

    def live(self, db=None, clock=None):
        return gate.judge("claude", "Edit", {"file_path": str(self.repo / "a")}, str(self.repo),
                          state_root=str(self.state), clock=clock or self.clock, capacity_db=db or self.db)

    def test_live_ownership_allows_and_survives_a_renewal(self):
        self.assertEqual(self.live().code, "routing_receipt_valid")
        self.router.renew("item-1", "implement", owner_id="claude-session", lease_seconds=600,
                          expected_revision=self.owned["revision"])
        self.assertEqual(self.live().code, "routing_receipt_valid")

    def test_a_completed_stage_no_longer_allows(self):
        self.router.complete("item-1", "implement", owner_id="claude-session",
                             expected_revision=self.owned["revision"])
        decision = self.live()
        self.assertEqual((decision.allowed, decision.code), (False, "stage_not_owned"))
        self.assertIn("routing_decide", decision.reason)

    def test_a_forged_receipt_for_a_stage_the_router_never_assigned_denies(self):
        gate.record_decision(str(self.state), caller="claude", stage_record=owned_stage(item_id="ghost"),
                             repo=str(self.other), reason="forged", ttl_seconds=600, clock=self.clock)
        decision = gate.judge("claude", "Write", {"file_path": str(self.other / "a")}, str(self.other),
                              state_root=str(self.state), clock=self.clock, capacity_db=self.db)
        self.assertEqual((decision.allowed, decision.code), (False, "stage_not_found"))

    def test_another_owner_on_the_row_denies(self):
        self.assertEqual(gate.stage_binding(self.db, {**self.written, "owner_id": "someone"}, 1000.0),
                         "stage_reassigned")
        self.assertEqual(gate.stage_binding(self.db, {**self.written, "owner_route": "codex"}, 1000.0),
                         "stage_reassigned")
        self.assertEqual(gate.stage_binding(self.db, self.written, 1000.0 + 601), "stage_lease_expired")
        self.assertIsNone(gate.stage_binding(self.db, self.written, 1000.0))

    def test_an_unreadable_router_database_denies(self):
        absent = self.live(db=str(self.base / "absent.sqlite3"))
        self.assertEqual((absent.allowed, absent.code), (False, "stage_db_unavailable"))
        garbage = self.base / "garbage.sqlite3"
        garbage.write_bytes(b"not a database at all, not even close, just bytes\n" * 40)
        broken = self.live(db=str(garbage))
        self.assertEqual((broken.allowed, broken.code), (False, "stage_db_unavailable"))
        self.assertTrue(garbage.exists())     # opened read-only; nothing was created or changed


class HookProcessTests(GateCase):
    """The launcher end to end: stdin in, one JSON line out, exit 0 always."""

    def run_hook(self, client, payload, *args):
        config = self.write_config()
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", client, "--config", str(config), *args],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        return completed

    def write_config(self):
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({"state_root": str(self.state),
                                      "capacity_db": str(self.state / "capacity.sqlite3")}), encoding="utf-8")
        return config

    def owned_now(self):
        """A stage the router really owns on the claude route, plus its receipt."""
        router = StageRouter(str(self.state / "capacity.sqlite3"))
        router.observe_capacity(CapacityObservation(route="claude", observed_at=time.time(),
                                                    fresh_until=time.time() + 3600, available=True, source="test"))
        router.register("item-1", "implement", allowed_routes=["claude"])
        owned = router.assign("item-1", "implement", owner_id="claude-session", lease_seconds=600,
                              expected_revision=0)
        gate.record_decision(str(self.state), caller="claude", stage_record=owned,
                             repo=str(self.repo), reason="live", ttl_seconds=600, clock=time.time)
        return router, owned

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
        self.owned_now()
        completed = self.run_hook("claude", {"tool_name": "Edit",
                                             "tool_input": {"file_path": str(self.repo / "a.py")},
                                             "cwd": str(self.repo)})
        self.assertEqual(completed.stdout.strip(), b"{}", completed.stderr)

    def test_a_receipt_without_a_live_stage_is_refused_by_the_process(self):
        # The receipt alone, written the way the tool writes it, but the stage router
        # was never told: the file is a pointer to ownership the router does not hold.
        gate.record_decision(str(self.state), caller="claude", stage_record=owned_stage(lease_until=time.time() + 600),
                             repo=str(self.repo), reason="forged", ttl_seconds=600, clock=time.time)
        completed = self.run_hook("claude", {"tool_name": "Edit",
                                             "tool_input": {"file_path": str(self.repo / "a.py")},
                                             "cwd": str(self.repo)})
        out = json.loads(completed.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[stage_db_unavailable]", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_the_installed_launcher_runs_the_same_judgment(self):
        if os.name == "nt":
            self.skipTest("the sh launcher is for POSIX hosts; the .cmd launcher is not exercised here")
        self.owned_now()
        config = self.write_config()
        command = gate.hook_command(str(ROOT), "claude", str(config))
        denied = subprocess.run(command, shell=True, input=json.dumps({
            "tool_name": "Write", "tool_input": {"file_path": str(self.other / "b.py")},
            "cwd": str(self.other)}).encode(), capture_output=True, timeout=60)
        self.assertEqual(denied.returncode, 0, denied.stderr)
        out = json.loads(denied.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[no_routing_receipt]", out["hookSpecificOutput"]["permissionDecisionReason"])
        allowed = subprocess.run(command, shell=True, input=json.dumps({
            "tool_name": "Write", "tool_input": {"file_path": str(self.repo / "b.py")},
            "cwd": str(self.repo)}).encode(), capture_output=True, timeout=60)
        self.assertEqual(allowed.stdout.strip(), b"{}", allowed.stderr)

    def test_garbage_input_denies_rather_than_crashing(self):
        config = self.write_config()
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "codex",
             "--config", str(config)], input=b"not json", capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0)
        out = json.loads(completed.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[gate_error]", reason)
        self.assertIn("(JSONDecodeError)", reason)     # the class, never the traceback
        self.assertNotIn("Traceback", completed.stdout.decode())

    def test_unusable_arguments_still_produce_a_deny(self):
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "nobody"],
            input=b"{}", capture_output=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0)
        out = json.loads(completed.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[gate_error]", out["hookSpecificOutput"]["permissionDecisionReason"])
        usage = subprocess.run([sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "install"],
                               capture_output=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(usage.returncode, 2)         # a person at the terminal still sees usage

    def test_input_without_a_tool_name_is_a_logged_deny(self):
        completed = self.run_hook("claude", {"cwd": str(self.repo)})
        out = json.loads(completed.stdout)
        self.assertIn("[hook_input_invalid]", out["hookSpecificOutput"]["permissionDecisionReason"])
        events = (self.state / "routing" / "gate-events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(events[-1])["code"], "hook_input_invalid")

    def test_an_internal_failure_is_logged_when_the_state_root_is_known(self):
        config = self.write_config()
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "codex",
             "--config", str(config)], input=b"[1, 2]", capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertIn("[gate_error]", json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        events = (self.state / "routing" / "gate-events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(events[-1])["code"], "gate_error")

    def test_a_config_without_a_capacity_db_denies(self):
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({"state_root": str(self.state)}), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "claude",
             "--config", str(config)],
            input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "a")}}).encode(),
            capture_output=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

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
        edit_tool = {"claude": ("Edit", {"file_path": str(self.repo / "a")}),
                     "codex": ("apply_patch", {"input": "*** Begin Patch\n*** Update File: a\n*** End Patch"})}
        tool, tool_input = edit_tool[owned["owner_route"]]
        decision = gate.judge(owned["owner_route"], tool, tool_input, str(self.repo),
                              state_root=str(self.state), clock=self.clock,
                              capacity_db=str(self.base / "capacity.sqlite3"))
        self.assertEqual((decision.allowed, decision.code), (True, "routing_receipt_valid"), decision)
        other = "codex" if owned["owner_route"] == "claude" else "claude"
        tool, tool_input = edit_tool[other]
        refused = gate.judge(other, tool, tool_input, str(self.repo), state_root=str(self.state),
                             clock=self.clock, capacity_db=str(self.base / "capacity.sqlite3"))
        self.assertEqual(refused.code, "routed_elsewhere")

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
        self.config.write_text(json.dumps({"state_root": str(self.state),
                                           "capacity_db": str(self.state / "capacity.sqlite3")}), encoding="utf-8")
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
        # Our own markers do not excuse a disabled flag either.
        from agent_bridge.onboard import BEGIN, END
        block = f"{BEGIN.format(name=gate.HOOKS_FLAG_NAME)}\n{END.format(name=gate.HOOKS_FLAG_NAME)}\n"
        self.toml.write_text("[features]\nhooks = false\n" + block, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "enable hooks explicitly"):
            self.install(apply=False)

    def test_a_one_client_run_keeps_the_other_clients_record(self):
        self.install(apply=True)
        receipt_path = self.home / ".agent-bridge" / "onboarding" / "gate-installation.json"
        both = json.loads(receipt_path.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(sorted(both), ["claude", "codex"])
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            gate.install(str(self.home), str(ROOT), str(self.config), ("claude",), apply=True)
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["entries"], both)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            gate.install(str(self.home), str(ROOT), str(self.config), ("claude",), apply=True, remove=True)
        after = json.loads(receipt_path.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(sorted(after), ["codex"])
        self.assertNotIn("hooks", json.loads(self.settings.read_text(encoding="utf-8")))
        self.assertIn(gate.HOOK_NAME, self.hooks.read_text(encoding="utf-8"))
        # Removing the same client again changes nothing and keeps the other's record.
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            gate.install(str(self.home), str(ROOT), str(self.config), ("claude",), apply=True, remove=True)
        self.assertEqual(sorted(json.loads(receipt_path.read_text(encoding="utf-8"))["entries"]), ["codex"])
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            gate.install(str(self.home), str(ROOT), str(self.config), ("codex",), apply=True, remove=True)
        self.assertFalse(receipt_path.exists())

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
        # A TOML basic string escapes backslashes, which a Windows path holds.
        key = f"{os.path.realpath(self.hooks)}:pre_tool_use:0:0".replace("\\", "\\\\")
        with open(self.toml, "a", encoding="utf-8") as handle:
            handle.write(f'\n[hooks.state."{key}"]\ntrusted_hash = "sha256:abc"\n')
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            self.assertEqual(gate.report(str(self.home), str(self.state), clock=self.clock)["codex_trust"], "recorded")


if __name__ == "__main__":
    unittest.main()
