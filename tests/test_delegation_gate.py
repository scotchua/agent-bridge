"""The delegation-first gate: receipts, judgment, hook wire, installation."""
from __future__ import annotations

import json
import ntpath
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import store  # noqa: E402
from agent_bridge.capacity_router import CapacityObservation, RoutingError, StageRouter  # noqa: E402
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot  # noqa: E402
from agent_bridge.orchestration import autoroute, gate, localfirst  # noqa: E402
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
        # Read is not an edit tool: it is the read gate's own kind (see
        # ReadClassificationTests), never classified as an implementation write.
        kind, paths = gate.classify("claude", "Read", {"file_path": "x"}, cwd)
        self.assertEqual(kind, "read")

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


class ShellWordTests(unittest.TestCase):
    def test_a_quote_opening_mid_word_protects_its_spaces(self):
        self.assertEqual(gate._shell_words("git --git-dir='/a b/.git' commit -m x"),
                         ["git", "--git-dir=/a b/.git", "commit", "-m", "x"])
        self.assertEqual(gate._shell_words('cp "x y" \'z w\''), ["cp", "x y", "z w"])

    def test_windows_keeps_backslashes_as_separators(self):
        with mock.patch.object(gate.os, "name", "nt"):
            self.assertEqual(gate._shell_words(r"echo {} > C:\Users\me\a.json"),
                             ["echo", "{}", ">", r"C:\Users\me\a.json"])
            self.assertEqual(gate._shell_words(r"git --git-dir='C:\p q\.git' commit"),
                             ["git", r"--git-dir=C:\p q\.git", "commit"])

    def test_an_unbalanced_quote_falls_back_to_whitespace(self):
        self.assertEqual(gate._shell_words("echo 'oops"), ["echo", "'oops"])


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
            route="claude", observed_at=1000.0, fresh_until=5000.0, available=True,
            source="test"), trusted=True)
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
                                                    fresh_until=time.time() + 3600, available=True,
                                                    source="test"), trusted=True)
        router.register("item-1", "implement", allowed_routes=["claude"])
        owned = router.assign("item-1", "implement", owner_id="claude-session", lease_seconds=600,
                              expected_revision=0)
        gate.record_decision(str(self.state), caller="claude", stage_record=owned,
                             repo=str(self.repo), reason="live", ttl_seconds=600, clock=time.time)
        return router, owned

    def test_a_denied_edit_is_reported_on_stdout_and_logged(self):
        # --no-automatic-routing is the pre-automatic posture: no receipt is a
        # deny, full stop. With automatic routing on (the default) this same
        # call creates a decision first, which the AutomaticGate tests cover.
        completed = self.run_hook("claude", {"hook_event_name": "PreToolUse", "tool_name": "Edit",
                                             "tool_input": {"file_path": str(self.repo / "a.py")},
                                             "cwd": str(self.repo)}, "--no-automatic-routing")
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
        command = gate.hook_command(str(ROOT), "claude", str(config)) + " --no-automatic-routing"
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
                route=route, observed_at=1000.0, fresh_until=5000.0, available=True,
                source="test"), trusted=True)

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


class EachClientHasItsOwnEditingTool(unittest.TestCase):
    """The trap that hid a livelock, stated where a test author will see it.

    Claude edits through ``Edit`` and friends; Codex edits through
    ``apply_patch``. Naming the wrong one does not fail: the gate classifies
    the call as not gated and allows it, which is correct behaviour and a
    silent trap for a test. Two tests were written that way, one of them the
    only guard against the two clients routing work at each other forever,
    and both passed while reaching none of the code they named.
    """

    def test_each_client_gates_its_own_editing_tool(self):
        for client, tool in (("claude", "Edit"), ("codex", "apply_patch")):
            kind, _ = gate.classify(client, tool, {"file_path": "/tmp/a.py",
                                                   "input": "*** Begin Patch\n"
                                                            "*** Update File: a.py\n"
                                                            "*** End Patch\n"},
                                    "/tmp")
            self.assertEqual(kind, "edit", f"{client} does not gate {tool}")

    def test_the_other_client_editing_tool_is_not_gated_at_all(self):
        """Not a defect. The reason a test must use the right name."""
        for client, foreign in (("claude", "apply_patch"), ("codex", "Edit")):
            kind, _ = gate.classify(client, foreign, {"file_path": "/tmp/a.py"},
                                    "/tmp")
            self.assertEqual(kind, "other",
                             f"{foreign} is unexpectedly gated for {client}; "
                             f"if that changed, the fixtures that rely on this "
                             f"asymmetry need revisiting")

    def test_the_two_edit_tool_sets_do_not_overlap(self):
        self.assertFalse(gate.EDIT_TOOLS["claude"] & gate.EDIT_TOOLS["codex"])

    def test_both_clients_gate_the_same_shell_tool(self):
        """Bash is shared, which is why shell fixtures work for both."""
        self.assertIn("Bash", gate.SHELL_TOOLS["claude"])
        self.assertIn("Bash", gate.SHELL_TOOLS["codex"])


class TheWindowsHookCommandQuoting(unittest.TestCase):
    r"""A static fix for a defect that would have silenced the Windows gate.

    ``hook_command`` quoted both paths with ``shlex.quote``, which is POSIX
    quoting: it wraps a value containing a space in single quotes, and
    ``cmd.exe`` does not treat single quotes as quoting at all. On any Windows
    account whose home contains a space, which is ``C:\Users\First Last`` and
    therefore most of them, the installed command was malformed and the hook
    never ran. A hook that never runs is a gate that never gates, and nothing
    would have said so.

    These tests exercise the quoting function, not a Windows host. The
    launcher still has not been run under either host on Windows, and the
    installer's ``not_covered`` list still says so.
    """

    def quoted(self, value, name):
        with mock.patch.object(gate.os, "name", name):
            return gate.quote_for_host_shell(value)

    def test_posix_quoting_is_unchanged(self):
        self.assertEqual(self.quoted("/home/some one/x", "posix"),
                         "'/home/some one/x'")

    def test_a_windows_path_with_a_space_gets_double_quotes(self):
        self.assertEqual(self.quoted(r"C:\Users\First Last\x.cmd", "nt"),
                         r'"C:\Users\First Last\x.cmd"')

    def test_no_single_quotes_ever_reach_a_windows_command(self):
        """The specific defect: cmd.exe cannot read them."""
        self.assertNotIn("'", self.quoted(r"C:\Users\First Last\x.cmd", "nt"))

    def test_every_cmd_delimiter_legal_in_a_path_is_quoted(self):
        r"""The first version of the quoted set had only the obvious ones.

        NTFS forbids only < > : " / \ | ? * , so a comma, a semicolon, an
        equals sign, a percent and an exclamation mark are all legal in a
        directory name and all significant to cmd.exe: it truncates the
        program name at the delimiter, or expands a variable, and the hook
        never runs. Found by sweeping for more instances of the pattern that
        had already produced four defects.
        """
        for character in (" ", "\t", ",", ";", "=", "%", "!", "&", "^", "(", ")"):
            path = "C:\\dev\\a" + character + "b\\hook.cmd"
            quoted = self.quoted(path, "nt")
            with self.subTest(character=character):
                self.assertNotEqual(quoted, path,
                                    f"a path containing {character!r} was left bare")
                self.assertTrue(quoted.startswith('"') and quoted.endswith('"'), quoted)

    def test_a_windows_path_without_metacharacters_is_left_alone(self):
        self.assertEqual(self.quoted(r"C:\Users\a\x.cmd", "nt"),
                         r"C:\Users\a\x.cmd")

    def test_a_cmd_builtin_write_is_recognised(self):
        r"""The heuristic listed only POSIX verbs, so `del` read as a read.

        Found while checking the Windows quoting, and the same class of
        oversight: one platform's conventions written into a cross-platform
        component. Still a heuristic, and still not a security boundary.
        """
        for command in (r"cmd /c del C:\Users\First Last\app.py",
                        "DEL app.py", "move a b", "ren a b", "rd /s /q build",
                        "attrib +r app.py"):
            self.assertTrue(gate.shell_writes(command), command)

    def test_a_powershell_write_cmdlet_is_recognised(self):
        for command in (r"Remove-Item .\app.py", r"remove-item .\app.py",
                        "Set-Content app.py 'x'", "Out-File -FilePath app.py"):
            self.assertTrue(gate.shell_writes(command), command)

    def test_windows_reads_are_still_reads(self):
        """A deny fails closed, but a heuristic that denies everything is no
        heuristic, so the ordinary read commands must stay reads."""
        for command in ("dir", "type app.py", "Get-Content app.py",
                        "Get-ChildItem", "where python", "findstr x app.py"):
            self.assertFalse(gate.shell_writes(command), command)

    def test_a_windows_tree_command_reaches_a_protected_ancestor(self):
        r"""The matching half: `rd /s` reaches a tree just as `rm -r` does.

        The tree-verb list decides whether naming an *ancestor* of a
        protected path counts as reaching it. It listed POSIX verbs only, so
        on Windows a recursive delete of a directory holding the gate's own
        state read as touching only that directory.
        """
        for command in (r"rd /s /q C:\Users\x\.agent-bridge",
                        r"Remove-Item -Recurse .\state",
                        "xcopy /s a b", "robocopy a b /e"):
            self.assertTrue(gate._TREE_VERBS.search(command), command)
        for command in ("dir", "Get-Content x", "type x"):
            self.assertFalse(gate._TREE_VERBS.search(command), command)

    def test_the_protected_path_check_resolves_and_folds_both_sides(self):
        r"""Windows paths are case-insensitive and take either separator.

        Without normalisation, a protected path named in a different case, or
        with forward slashes, compared unequal to the same path and the
        protected-path rule did not fire.
        """
        import inspect
        source = inspect.getsource(gate._under)
        self.assertIn("os.path.normcase(os.path.realpath(path))", source)
        self.assertIn("os.path.normcase(os.path.realpath(root))", source)

    def test_a_path_inside_the_root_is_under_it_on_any_platform(self):
        """The first version of this test failed on Windows CI, and was right
        to: it passed an unresolved root, and only ``path`` was resolved. On a
        Windows runner ``gettempdir`` can return a short 8.3 name, so the two
        sides were different spellings of one directory. Both are resolved
        now, so the caller no longer has to know."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "state")
            os.makedirs(os.path.join(root, "routing"))
            self.assertTrue(gate._under(os.path.join(root, "routing", "x.json"), root))
            self.assertTrue(gate._under(root, root))
            self.assertFalse(gate._under(os.path.join(tmp, "elsewhere", "x"), root))

    @unittest.skipUnless(os.name == "posix", "POSIX case semantics")
    def test_case_is_significant_on_posix(self):
        """/etc/Passwd really is a different file from /etc/passwd."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "state")
            os.makedirs(root)
            self.assertFalse(gate._under(os.path.join(tmp, "STATE", "x.json"), root))

    @unittest.skipUnless(os.name == "nt", "Windows case semantics")
    def test_case_is_not_significant_on_windows(self):
        """The actual fix, verified by the Windows runners rather than mocked.

        A protected path named in a different case is the same file on
        Windows, so it has to reach the protected-path rule.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "state")
            os.makedirs(root)
            self.assertTrue(gate._under(os.path.join(tmp, "STATE", "x.json"), root))
            self.assertTrue(gate._under(os.path.join(tmp, "state/x.json"), root))

    def test_flattening_an_argv_for_the_heuristic_is_a_different_direction(self):
        r"""``shlex.join`` elsewhere in this module is not the same defect.

        It flattens a tool call's argv list into text so the write heuristic
        can read it, rather than building a command for a shell to run, and
        the quoting it adds only makes the patterns easier to match. Checked
        with a Windows-shaped argv, including a path with a space.
        """
        argv = ["cmd", "/c", r"del C:\Users\First Last\app.py"]
        self.assertTrue(gate.shell_writes(gate._command_text({"command": argv})))
        self.assertTrue(gate.shell_writes(gate._command_text(
            {"command": ["sh", "-c", r"rm -f 'C:\Users\First Last\app.py'"]})))

    def test_the_command_builder_uses_it_for_both_paths(self):
        """Neither the launcher nor the config path may be POSIX-quoted."""
        import inspect
        source = inspect.getsource(gate.hook_command)
        self.assertEqual(source.count("quote_for_host_shell"), 2)
        self.assertNotIn("shlex.quote", source)


class TheHookPayloadIsUtf8WhateverTheLocaleSays(unittest.TestCase):
    r"""The worst defect this project has had: a silent, total bypass.

    Hook mode read its payload with ``sys.stdin.read()``, which decodes using
    the locale encoding. On Windows that is the ANSI code page, and both hosts
    emit raw UTF-8: Node's ``JSON.stringify`` and Rust's ``serde_json`` do not
    escape non-ASCII. So for a repository whose path contained any non-ASCII
    character the payload arrived as mojibake, ``enclosing_repos`` found no
    ``.git`` above the mangled path, and ``judge`` returned ``allow`` with
    ``outside_repository``. The gate printed an empty object and the edit
    proceeded ungated, for every user whose name or project path is not pure
    ASCII.

    Measured before the fix: the same payload naming a repository with an
    e-acute was denied ``routed_elsewhere`` under a UTF-8 stdin and allowed
    under ``cp1252``.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        (self.state / "routing").mkdir(parents=True)
        self.db = self.state / "capacity.sqlite3"
        self.config = self.base / "orchestration.json"
        self.config.write_text(json.dumps(
            {"state_root": str(self.state), "capacity_db": str(self.db)}), encoding="utf-8")
        # An e-acute and a CJK character, so the test is not about one codec.
        self.repo = self.base / "caf\u00e9-\u9879\u76ee"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, timeout=60,
                       capture_output=True)
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.state / "routing" / "routing-policy.json").write_text(
            json.dumps({"version": 1, "declared_available": ["codex"],
                        "repos": {str(self.repo): {
                            "classification": "internal_nonclient",
                            "allowed_routes": ["claude", "codex"]}}}, ensure_ascii=False),
            encoding="utf-8")

    def judge_with(self, encoding: str) -> dict:
        """Drive the gate as a subprocess with its stdio encoding forced.

        ``PYTHONIOENCODING`` is how this test reproduces on Linux what a
        Windows ANSI code page does by default. The payload goes in as raw
        UTF-8 bytes, which is what a host writes.
        """
        payload = json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": str(self.repo / "app.py")},
             "cwd": str(self.repo)}, ensure_ascii=False).encode("utf-8")
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
                       "PYTHONIOENCODING": encoding}
        environment.pop("PYTHONUTF8", None)
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", "claude", "--config", str(self.config)],
            input=payload, capture_output=True, timeout=120, env=environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def code(self, payload: dict) -> str:
        output = payload.get("hookSpecificOutput")
        if not output:
            return "ALLOWED"
        reason = str(output.get("permissionDecisionReason", ""))
        return reason.rsplit("[", 1)[-1].rstrip("]")

    def test_a_non_ascii_repository_is_judged_under_every_stdio_encoding(self):
        for encoding in ("utf-8", "cp1252", "latin-1", "ascii"):
            with self.subTest(encoding=encoding):
                self.assertEqual(self.code(self.judge_with(encoding)),
                                 "routed_elsewhere",
                                 f"the gate did not judge the call under {encoding}")

    def test_the_gate_reads_bytes_rather_than_the_locale(self):
        """Pinned in the source too, because the defect is invisible in the
        output on a host whose locale happens to be UTF-8, which is every
        Linux CI runner this project has."""
        import inspect
        source = inspect.getsource(gate._hook_input)
        self.assertIn('decode("utf-8")', source)
        self.assertIn("sys.stdin", source)
        self.assertNotIn("sys.stdin.read()", inspect.getsource(gate.main))

    def test_a_byte_order_mark_is_tolerated(self):
        payload = b"\xef\xbb\xbf" + json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": str(self.repo / "app.py")},
             "cwd": str(self.repo)}).encode("utf-8")
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", "claude", "--config", str(self.config)],
            input=payload, capture_output=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(self.code(json.loads(completed.stdout)), "routed_elsewhere")

    def test_bytes_that_are_not_utf8_are_a_deny_rather_than_a_guess(self):
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", "claude", "--config", str(self.config)],
            input=b"\xff\xfe{not even close}", capture_output=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.code(json.loads(completed.stdout)), "gate_error")


class TheLauncherNeverFailsOpen(unittest.TestCase):
    """A hook that produces no decision is read as a non-blocking error.

    The gate always exits 0 and carries its decision in the JSON, but that
    contract only starts once the interpreter is running. Both launchers used
    to exit with the interpreter's status and nothing on stdout when it could
    not start, which a host treats as a failed hook and then runs the tool
    anyway. On a stock Windows account with no Python the bare name ``python``
    resolves to the Microsoft Store App Execution Alias stub, so this was the
    default case there, not an edge case.
    """

    def launcher(self) -> Path:
        return ROOT / "bin" / ("agent-bridge-gate-hook"
                               + (".cmd" if os.name == "nt" else ""))

    def run_launcher(self, *args, python: str | None = None):
        environment = dict(os.environ)
        if python is not None:
            environment["AGENT_BRIDGE_PYTHON"] = python
        argv = [str(self.launcher()), *args]
        if os.name == "nt":
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                    subprocess.list2cmdline(argv)]
        return subprocess.run(argv, input="{}", capture_output=True, text=True,
                              timeout=120, env=environment, cwd=str(ROOT))

    def test_an_interpreter_that_cannot_start_is_a_deny(self):
        completed = self.run_launcher("--client", "claude", "--state-root",
                                      "/nonexistent-state-root",
                                      python="definitely-not-an-interpreter")
        self.assertEqual(completed.returncode, 0,
                         f"a launch failure must still exit 0: {completed.stderr[-500:]}")
        payload = json.loads(completed.stdout)
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertTrue(reason.endswith("[gate_launcher_failed]"), reason)
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_an_interpreter_that_cannot_start_answers_empty_for_posttooluse(self):
        completed = self.run_launcher("--client", "claude", "--state-root",
                                      "/nonexistent-state-root", "--event", "PostToolUse",
                                      python="definitely-not-an-interpreter")
        self.assertEqual(completed.returncode, 0,
                         f"a launch failure must still exit 0: {completed.stderr[-500:]}")
        self.assertEqual(completed.stdout.strip(), "{}")

    def test_a_config_path_spelling_the_marker_does_not_spoof_posttooluse(self):
        # A confirmed adversarial-review finding: an earlier version of this
        # check searched for the literal text "--event PostToolUse" anywhere
        # in the flattened argument line, so a --config path that happened to
        # contain exactly that text (one argument, quoting preserved) would
        # misclassify this ordinary PreToolUse call (no --event given at all,
        # so it defaults to PreToolUse) as PostToolUse, at the one moment the
        # interpreter has already failed to start -- a silent {} instead of
        # the fail-closed deny this whole branch exists to guarantee.
        #
        # POSIX only. This exercises the real installed .sh launcher through
        # a real shell. On Windows, run_launcher's own re-invocation
        # (subprocess.list2cmdline, then cmd.exe /d /s /c on the result) is a
        # second, independent layer of quoting on top of the one this test
        # is actually trying to probe, and reproducing this specific
        # embedded-space value through both layers reliably is its own
        # unresolved cmd.exe quoting question, not evidence about the .cmd
        # launcher's own fix: every other Windows test in this class,
        # including the interpreter-failure PostToolUse case with no
        # embedded space, passes against the same .cmd file's positional
        # %~1/%~2 + shift loop.
        if os.name == "nt":
            self.skipTest("run_launcher's own double cmd.exe requoting for a "
                          "value containing an embedded space is untested here; "
                          "see this test's docstring")
        completed = self.run_launcher("--client", "claude", "--config",
                                      "some --event PostToolUse path.json",
                                      python="definitely-not-an-interpreter")
        self.assertEqual(completed.returncode, 0, completed.stderr[-500:])
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_subcommand_keeps_its_own_exit_status(self):
        """The subcommands are operator tools. Turning their failure into a
        fake hook decision would hide it."""
        completed = self.run_launcher("report", "--config", "/nonexistent.json")
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("hookSpecificOutput", completed.stdout)

    def test_both_launchers_set_utf8_mode(self):
        for name in ("agent-bridge-gate-hook", "agent-bridge-gate-hook.cmd"):
            text = (ROOT / "bin" / name).read_text(encoding="utf-8")
            self.assertIn("PYTHONUTF8", text, name)


class WindowsSpellingsOfAProtectedWrite(unittest.TestCase):
    r"""Four ways past the protected-path rule, all on Windows, all measured.

    The rule reads a command's operands and refuses one that reaches the
    gate's own state. Every mechanism it used to do that was written for
    POSIX, so on Windows the ordinary spellings walked through it:

    * ``cmd /c "del C:\Users\me\.agent-bridge\..."`` was allowed, because
      ``cmd`` was not a recognised shell, so the quoted command was treated as
      one word, and the colon split then severed the drive letter out of it;
    * ``bash.exe -c "..."`` was allowed where ``bash -c "..."`` was refused,
      because the basename still carried ``.exe``;
    * ``powershell -Command "..."`` was allowed, because neither the program
      nor the flag was recognised;
    * ``rm -rf /c/Users/me/.agent-bridge`` was allowed, because
      ``ntpath.isabs`` calls that absolute, so it was kept verbatim and later
      resolved against the current drive as ``C:\c\Users\me\...``, a
      different directory. This is the one that matters most: Claude Code's
      Bash tool on Windows runs through Git for Windows, so that is the
      spelling that shell produces.

    These tests run everywhere, with ``ntpath`` and ``os.name`` standing in
    for the platform, because the whole lesson of this round is that a
    Windows-only test which only ever runs on one runner is a test that stops
    being read. The gate's own Windows CI runners exercise the same code with
    the real ``ntpath``.
    """

    PROTECTED = (ntpath.normpath(r"C:\Users\me\.agent-bridge"),)

    def verdict(self, command: str, cwd: str = r"C:\proj") -> str:
        """Whether the protected-path branch of judge() would refuse."""
        with mock.patch.object(gate, "os") as fake:
            for attribute in dir(os):
                if not attribute.startswith("_"):
                    try:
                        setattr(fake, attribute, getattr(os, attribute))
                    except Exception:      # noqa: BLE001  a few are read-only
                        pass
            fake.path = ntpath
            fake.name = "nt"
            fake.sep = "\\"
            named = gate._command_paths(command, cwd)
            trees = bool(gate._TREE_VERBS.search(command))
            hit = [root for root in self.PROTECTED
                   if any(gate._reaches(path, root, through_ancestors=trees)
                          for path in named)]
        return "refused" if hit else "allowed"

    def test_a_nested_command_under_cmd_is_read(self):
        for command in (r'cmd /c "del C:\Users\me\.agent-bridge\routing\x.json"',
                        r'cmd.exe /C "del C:\Users\me\.agent-bridge\routing\x.json"',
                        r'cmd /k "del C:\Users\me\.agent-bridge\routing\x.json"'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "refused")

    def test_an_executable_suffix_does_not_hide_a_shell(self):
        for command in (r'bash.exe -c "rm -rf C:\Users\me\.agent-bridge"',
                        r'sh.exe -c "rm -rf C:\Users\me\.agent-bridge"'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "refused")

    def test_powershell_is_a_shell(self):
        for command in (
                r'powershell -Command "Remove-Item -Recurse C:\Users\me\.agent-bridge"',
                r'powershell.exe -command "Remove-Item C:\Users\me\.agent-bridge\x"',
                r'pwsh -Command "Remove-Item C:\Users\me\.agent-bridge\x"'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "refused")

    def test_a_git_bash_drive_path_names_the_same_directory(self):
        for command in (r"rm -rf /c/Users/me/.agent-bridge",
                        r"rm -rf //c/Users/me/.agent-bridge",
                        r"rm -rf /cygdrive/c/Users/me/.agent-bridge",
                        r"rm -rf /C/Users/me/.agent-bridge"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "refused")

    def test_the_plain_windows_spellings_still_work(self):
        """The cases that were already refused, so a fix cannot have traded
        one spelling for another."""
        for command in (r"del C:\Users\me\.agent-bridge\routing\x.json",
                        r"rm -rf c:\users\me\.agent-bridge",
                        r'del "C:\Users\me\.agent-bridge\routing\x.json"',
                        r"del C:/Users/me/.agent-bridge/routing/x.json"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "refused")

    def test_an_unrelated_write_is_still_allowed(self):
        """A rule that refuses everything is not a rule."""
        for command in (r"del C:\proj\app.py", r"rm -rf /c/other/place",
                        r'cmd /c "del C:\proj\build"', "type app.py"):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "allowed")

    def test_a_shell_command_flag_is_not_an_operand(self):
        r"""``cmd /c`` is a flag, not the drive root.

        The first version of the Git-Bash translation turned a bare ``/c``
        into ``C:\``, which is an ancestor of every protected path, so with a
        tree verb in the command every ``cmd /c`` was refused. Caught by the
        test that asks whether an unrelated write is still allowed, which is
        why that test is there.
        """
        self.assertEqual(gate._windows_drive_path("/c"), "/c")
        for command in (r'cmd /c "del C:\proj\build"',
                        r'cmd /k "rm -rf C:\proj\build"',
                        r'powershell -Command "Remove-Item C:\proj\build"'):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "allowed")

    # The two assertions below are about behaviour that differs by platform,
    # so each is stated twice: once for the platform it is true on, and once
    # for the platform it is false on. Writing only the POSIX half is a
    # mistake I have now made three times in this project, and it fails on
    # the Windows runners every time, which is the only reason it gets
    # caught. The rule I keep relearning: when a function branches on
    # os.name, so must its test.

    @unittest.skipUnless(os.name == "posix", "POSIX path semantics")
    def test_a_slash_c_path_is_left_alone_on_posix(self):
        """On POSIX, /c/Users/me really is that path."""
        self.assertEqual(gate._windows_drive_path("/c/Users/me"), "/c/Users/me")

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_a_slash_c_path_is_translated_on_windows(self):
        """And on Windows it is Git Bash's spelling of a drive.

        Asserted by the Windows runners rather than through a stand-in, which
        is what makes it a measurement.
        """
        self.assertEqual(gate._windows_drive_path("/c/Users/me"), r"C:\Users\me")
        self.assertEqual(gate._windows_drive_path("/cygdrive/d/x"), r"D:\x")
        self.assertEqual(gate._windows_drive_path("/c"), "/c")

    @unittest.skipUnless(os.name == "posix", "POSIX operand semantics")
    def test_a_colon_operand_is_split_on_posix(self):
        """``a:b`` can be two operands in a POSIX shell."""
        found = gate._command_paths("cp a:b /tmp/x", "/cwd")
        self.assertIn("/cwd/a", found)
        self.assertIn("/cwd/b", found)

    @unittest.skipUnless(os.name == "nt", "Windows operand semantics")
    def test_a_colon_operand_is_not_split_on_windows(self):
        """On Windows a colon is a drive or a stream, never a separator, and
        splitting on it severed the drive letter out of a nested command."""
        found = gate._command_paths("copy a:b x", r"C:\cwd")
        self.assertNotIn(r"C:\cwd\a", found)
        self.assertIn("a:b", [os.path.basename(entry) for entry in found]
                      + [entry for entry in found])


class EveryOperandOfACommandIsRead(unittest.TestCase):
    """The operand set itself, which nothing was asserting.

    A fix of mine silently dropped every command's own program name from the
    parsed operands, and 88 tests in this file plus the 559-check suite all
    passed. The cause is worth keeping: ``A and B`` returns A itself when A is
    falsy, A was the empty ``previous`` list, and the very next line appends
    to that same object, so the name held a list that had since become
    non-empty and therefore truthy.

    Nothing caught it because every test here asserts a *verdict*, and the
    program name is nearly always redundant with the working directory that
    ``classify`` adds alongside it. A test that pins the operands directly is
    the only kind that would have.
    """

    def test_the_program_name_and_the_operands_are_all_present(self):
        self.assertEqual(gate._command_paths("rm -f /tmp/x", "/cwd"),
                         ["/cwd/rm", "/tmp/x"])

    def test_a_bare_flag_names_nothing_but_its_value_does(self):
        found = gate._command_paths("git --git-dir=/tmp/g status", "/cwd")
        self.assertIn("/cwd/git", found)
        self.assertIn("/tmp/g", found)

    def test_a_redirection_target_is_an_operand(self):
        self.assertIn("/cwd/out.txt", gate._command_paths("echo hi > out.txt", "/cwd"))

    def test_a_nested_shell_command_contributes_its_own_operands(self):
        found = gate._command_paths('sh -c "rm -f /tmp/inner"', "/cwd")
        self.assertIn("/tmp/inner", found)

    def test_the_shells_own_flag_is_not_an_operand(self):
        """And this is what the dropped-name bug was introduced to do."""
        found = gate._command_paths('sh -c "rm -f /tmp/inner"', "/cwd")
        self.assertNotIn("/cwd/-c", found)


class TheSqliteUriEscapesEveryReservedCharacter(unittest.TestCase):
    """A percent in the database path made the gate deny every call.

    SQLite percent-decodes a ``file:`` URI path, so a directory named
    ``App%20Data`` was rewritten and the open failed. ``stage_binding`` turns
    that into ``stage_db_unavailable``, which is a deny on every gated call in
    both clients, and ``capacity_digest`` returns None. Fail-closed, and
    unusable. The ordering is the fix: percent must be escaped before the
    question mark and hash, or this function mangles its own escapes.
    """

    def test_a_percent_in_the_path_opens(self):
        with tempfile.TemporaryDirectory() as temporary:
            awkward = Path(temporary) / "App%20Data"
            awkward.mkdir()
            database = awkward / "capacity.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE capacity (route TEXT)")
            connection.commit()
            connection.close()
            uri = "file:" + gate._sqlite_uri_path(str(database)) + "?mode=ro"
            opened = sqlite3.connect(uri, uri=True)
            try:
                self.assertEqual(opened.execute("SELECT count(*) FROM capacity")
                                 .fetchone()[0], 0)
            finally:
                opened.close()

    def test_the_unescaped_form_is_what_used_to_fail(self):
        """Stated so the test cannot pass by the path simply working anyway."""
        with tempfile.TemporaryDirectory() as temporary:
            awkward = Path(temporary) / "App%20Data"
            awkward.mkdir()
            database = awkward / "capacity.sqlite3"
            sqlite3.connect(database).close()
            naive = "file:" + str(database) + "?mode=ro"
            with self.assertRaises(sqlite3.OperationalError):
                sqlite3.connect(naive, uri=True)

    def test_percent_is_escaped_before_the_others(self):
        escaped = gate._sqlite_uri_path("/tmp/a%b")
        self.assertIn("%25", escaped)
        self.assertNotIn("%2525", escaped)


class ABomPrefixedCodexTomlIsStillValidToml(unittest.TestCase):
    # A Windows editor or PowerShell's default encoding can prepend a UTF-8
    # BOM to config.toml. tomllib.loads sees the raw bytes only after this
    # module's own decode, so the BOM must already be gone by then.
    def test_codex_hooks_flag_update_reads_a_bom_prefixed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_bytes(b"\xef\xbb\xbf" + b'[other]\nkey = "value"\n')
            updated = gate.codex_hooks_flag_update(str(path))
            self.assertIsNotNone(updated)
            self.assertIn(b'key = "value"', updated)

    def test_codex_trust_state_reads_a_bom_prefixed_toml(self):
        with tempfile.TemporaryDirectory() as temporary:
            hooks_path = Path(temporary) / "hooks.json"
            ours = {"hooks": [{"command": f"...{gate.HOOK_NAME}..."}]}
            hooks_path.write_text(json.dumps({"hooks": {"PreToolUse": [ours]}}))
            codex_toml = Path(temporary) / "config.toml"
            # No matching trusted_hash entry: BOM-prefixed but otherwise-valid
            # TOML must parse (not raise) and yield "needs_review", never a
            # TOMLDecodeError from the leading BOM.
            codex_toml.write_bytes(b"\xef\xbb\xbf" + b'[hooks]\n')
            self.assertEqual(gate.codex_trust_state(str(codex_toml), str(hooks_path)), "needs_review")


class TheTrustStateKeyMatchesCodexsOwnCodexHomeResolution(unittest.TestCase):
    """codex-rs canonicalizes CODEX_HOME (symlinks, Windows on-disk casing) only
    when the CODEX_HOME environment variable is set (utils/home-dir/src/lib.rs::
    find_codex_home); the default ``~/.codex`` gets no resolution at all before
    it is used to build a trust-state key (hooks/src/engine/discovery.rs). This
    module's own key must track that exact branch, or a real trust decision
    never shows as recorded because the two sides spell the same file two
    different ways.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        real_dir = Path(self.temp.name) / "real"
        real_dir.mkdir()
        self.link_dir = Path(self.temp.name) / "link"
        self.link_dir.symlink_to(real_dir)
        self.hooks_path = self.link_dir / "hooks.json"
        ours = {"hooks": [{"command": f"...{gate.HOOK_NAME}..."}]}
        self.hooks_path.write_text(json.dumps({"hooks": {"PreToolUse": [ours]}}))
        self.codex_toml = self.link_dir / "config.toml"

    def _write_state_for_key(self, key):
        escaped = key.replace("\\", "\\\\").replace('"', '\\"')
        self.codex_toml.write_text(f'[hooks.state."{escaped}"]\ntrusted_hash = "abc"\n')

    def test_with_codex_home_unset_the_key_is_not_resolved_through_the_symlink(self):
        # Codex's default ~/.codex branch applies no canonicalization; the key
        # it writes names the path as configured, symlink and all.
        self._write_state_for_key(f"{self.hooks_path}:pre_tool_use:0:0")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(gate.codex_trust_state(str(self.codex_toml), str(self.hooks_path)),
                             "recorded")

    def test_with_codex_home_unset_a_realpath_style_key_does_not_match(self):
        # The bug this guards against: unconditionally resolving through the
        # symlink would look for a key Codex never writes in this branch.
        self._write_state_for_key(f"{os.path.realpath(self.hooks_path)}:pre_tool_use:0:0")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(gate.codex_trust_state(str(self.codex_toml), str(self.hooks_path)),
                             "needs_review")

    def test_with_codex_home_set_the_key_is_resolved_through_the_symlink(self):
        # Codex calls std::fs::canonicalize on an explicit CODEX_HOME.
        self._write_state_for_key(f"{os.path.realpath(self.hooks_path)}:pre_tool_use:0:0")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.link_dir)}):
            self.assertEqual(gate.codex_trust_state(str(self.codex_toml), str(self.hooks_path)),
                             "recorded")

    def test_with_codex_home_set_the_unresolved_symlink_style_key_does_not_match(self):
        self._write_state_for_key(f"{self.hooks_path}:pre_tool_use:0:0")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.link_dir)}):
            self.assertEqual(gate.codex_trust_state(str(self.codex_toml), str(self.hooks_path)),
                             "needs_review")


# ---------------------------------------------------------------- read gate


class ReadClassificationTests(unittest.TestCase):
    """classify's fourth kind (design section 2.1). Claude's Read tool is
    matched by name, deterministically; Codex has no such tool, so its
    whole-file readers (cat, type, Get-Content, more, less, bat) are
    recognised from the shell command text instead, scoped to Codex only --
    Claude's Bash tool is judged exactly as it was before this phase."""

    def setUp(self):
        self.cwd = os.path.abspath(os.sep + "w")

    def test_claude_read_tool_is_classified_as_a_read(self):
        kind, paths = gate.classify("claude", "Read", {"file_path": "a.log"}, self.cwd)
        self.assertEqual((kind, paths), ("read", [os.path.join(self.cwd, "a.log")]))

    def test_claude_read_with_an_absolute_path_is_unchanged(self):
        absolute = os.path.abspath(os.sep + "abs" + os.sep + "a.log")
        kind, paths = gate.classify("claude", "Read", {"file_path": absolute}, self.cwd)
        self.assertEqual((kind, paths), ("read", [absolute]))

    def test_claude_read_offset_and_limit_do_not_change_the_classification(self):
        kind, paths = gate.classify(
            "claude", "Read", {"file_path": "a.log", "offset": 100, "limit": 20}, self.cwd)
        self.assertEqual((kind, paths), ("read", [os.path.join(self.cwd, "a.log")]))

    def test_codex_cat_is_classified_as_a_read(self):
        kind, paths = gate.classify("codex", "Bash", {"command": "cat a.log b.log"}, self.cwd)
        self.assertEqual(kind, "read")
        # _command_paths joins with a forward slash always, never
        # os.path.join, on every platform (its own documented contract);
        # matched here the same way this file's other _command_paths tests
        # already do, rather than a platform-native join that only agrees
        # with it on POSIX.
        cwd = self.cwd.rstrip("/")
        self.assertEqual(paths, [f"{cwd}/a.log", f"{cwd}/b.log"])

    def test_codex_windows_whole_file_readers_are_classified_as_reads(self):
        for command in ("type a.log", "Get-Content a.log", "more a.log", "less a.log", "bat a.log"):
            with self.subTest(command=command):
                kind, _ = gate.classify("codex", "Bash", {"command": command}, self.cwd)
                self.assertEqual(kind, "read")

    def test_codex_get_content_with_tail_or_totalcount_is_a_bounded_read(self):
        """A caller-scoped exception matching design section 2.1's own
        example ("Get-Content without -TotalCount or -Tail")."""
        for command in ("Get-Content a.log -Tail 5", "Get-Content a.log -TotalCount 10"):
            with self.subTest(command=command):
                kind, _ = gate.classify("codex", "Bash", {"command": command}, self.cwd)
                self.assertEqual(kind, "shell_read")

    def test_codex_head_tail_sed_grep_rg_are_exact_reads_not_gated(self):
        for command in ("head -n 40 a.log", "tail -n 40 a.log", "sed -n 1,5p a.log",
                        "grep ERROR a.log", "rg ERROR a.log"):
            with self.subTest(command=command):
                kind, _ = gate.classify("codex", "Bash", {"command": command}, self.cwd)
                self.assertEqual(kind, "shell_read")

    def test_claude_bash_cat_is_not_classified_as_a_read(self):
        """The whole-file-read heuristic is Codex-only (design section 2.1):
        Claude's own read gate is its Read tool, matched by name."""
        kind, _ = gate.classify("claude", "Bash", {"command": "cat a.log"}, self.cwd)
        self.assertEqual(kind, "shell_read")

    def test_a_write_shaped_command_is_classified_as_a_write_not_a_read(self):
        """shell_writes wins first: cat piped into a file still writes.

        This is classify's own contract, unchanged and still tested here on
        its own terms. It does NOT mean the read gate is skipped for a
        command like this: judge (not classify) independently checks
        _shell_whole_file_read for a Codex shell call regardless of which
        kind classify returned, specifically because shell_writes winning
        here used to make it look, wrongly, like the read never needed
        judging at all. See ReadGateWriteShapedOverlayTests for judge's own
        behavior on this exact command.
        """
        kind, _ = gate.classify("codex", "Bash", {"command": "cat a.log > b.log"}, self.cwd)
        self.assertEqual(kind, "shell")

    def test_a_redirection_before_the_command_name_is_still_recognized_as_a_read(self):
        """A previous version excluded the reader's own name by list
        position (dropping _command_paths()[0]), which assumed the program
        name always comes first. ``< a.log cat`` is ordinary POSIX syntax
        that puts the redirect target first instead: the old code kept
        ``cat`` and silently discarded ``a.log``, the real read target, a
        complete read-gate bypass for this construction."""
        kind, paths = gate.classify("codex", "Bash", {"command": "< a.log cat"}, self.cwd)
        self.assertEqual(kind, "read")
        cwd = self.cwd.rstrip("/")
        self.assertEqual(paths, [f"{cwd}/a.log"])

    def test_a_glued_redirection_before_the_command_name_is_still_a_read(self):
        kind, paths = gate.classify("codex", "Bash", {"command": "<a.log cat"}, self.cwd)
        self.assertEqual(kind, "read")
        cwd = self.cwd.rstrip("/")
        self.assertEqual(paths, [f"{cwd}/a.log"])

    def test_chained_reader_commands_each_lose_only_their_own_name(self):
        """Excluding _command_paths()[0] only ever removed the first
        command's name in a chain: cat a.log; cat b.log kept a bogus ``cat``
        path in the middle for every command after the first."""
        kind, paths = gate.classify(
            "codex", "Bash", {"command": "cat a.log ; cat b.log"}, self.cwd)
        self.assertEqual(kind, "read")
        cwd = self.cwd.rstrip("/")
        self.assertEqual(paths, [f"{cwd}/a.log", f"{cwd}/b.log"])

    def test_a_file_literally_named_like_a_reader_is_still_read(self):
        """Excluding a reader's name by VALUE, not by position, must still
        remove only the one occurrence that is the program name: ``cat
        cat`` reads a file that happens to be called ``cat``."""
        kind, paths = gate.classify("codex", "Bash", {"command": "cat cat"}, self.cwd)
        self.assertEqual(kind, "read")
        cwd = self.cwd.rstrip("/")
        self.assertEqual(paths, [f"{cwd}/cat"])

    def test_an_executable_suffixed_reader_is_recognized(self):
        """cat.exe/type.exe/more.com are how Git-for-Windows and MSYS2
        command captures spell these programs; a bare whitespace-bounded
        regex never matched the suffixed form."""
        for command in ("cat.exe a.log", "type.exe a.log", "more.com a.log"):
            with self.subTest(command=command):
                kind, _ = gate.classify("codex", "Bash", {"command": command}, self.cwd)
                self.assertEqual(kind, "read")

    def test_get_content_with_a_colon_bound_tail_or_totalcount_is_a_bounded_read(self):
        """PowerShell also colon-binds a flag's value (-Tail:5); missing it
        under-recognized the bounded form, gating a command that a
        space-separated spelling already exempts."""
        for command in ("Get-Content a.log -Tail:5", "Get-Content a.log -TotalCount:10"):
            with self.subTest(command=command):
                kind, _ = gate.classify("codex", "Bash", {"command": command}, self.cwd)
                self.assertEqual(kind, "shell_read")

    def test_a_reader_name_used_as_a_plain_argument_is_not_a_read(self):
        """echo cat prints the word "cat"; it does not read a file. A raw
        substring regex bounded only by whitespace matched this as a whole-
        file read, over-gating a command that reads nothing at all."""
        kind, _ = gate.classify("codex", "Bash", {"command": "echo cat"}, self.cwd)
        self.assertEqual(kind, "shell_read")

    def test_an_env_assignment_prefix_does_not_hide_the_reader_that_follows(self):
        kind, paths = gate.classify(
            "codex", "Bash", {"command": "LANG=C cat a.log"}, self.cwd)
        self.assertEqual(kind, "read")
        cwd = self.cwd.rstrip("/")
        self.assertIn(f"{cwd}/a.log", paths)
        self.assertNotIn(f"{cwd}/cat", paths)


class ReceiptPathCaseFoldingTests(unittest.TestCase):
    """_judge_read_path's two ``receipt.get("path") == real_path`` checks
    used to be bare string equality, unlike _under's established pattern for
    the same kind of comparison. Never a bypass -- a false mismatch only
    means "no receipt yet", so the strict-side outcome is an extra digest --
    but needless churn on every case-insensitive-but-preserving filesystem,
    which is Windows (and, though os.path.normcase cannot help there, also
    macOS's own default volume)."""

    def test_the_receipt_path_comparisons_fold_case_and_separators(self):
        """Source-inspection, the same style test_the_protected_path_check_
        resolves_and_folds_both_sides already uses for _under: the actual
        cross-platform behavior is verified by the platform-gated tests
        below, run for real by the Windows/POSIX CI runners rather than
        mocked."""
        import inspect
        source = inspect.getsource(gate._judge_read_path)
        self.assertEqual(source.count("_same_path(receipt["), 2)

    @unittest.skipUnless(os.name == "posix", "POSIX case semantics")
    def test_case_is_significant_on_posix(self):
        self.assertFalse(gate._same_path("/tmp/App.log", "/tmp/app.log"))

    @unittest.skipUnless(os.name == "nt", "Windows case semantics")
    def test_case_is_not_significant_on_windows(self):
        """The actual fix, verified by the Windows runners rather than
        mocked: a digest receipt written for a path spelled in one case must
        still match the identical file read back in another, or on the
        other slash direction."""
        self.assertTrue(gate._same_path(r"C:\repo\App.log", r"C:\repo\app.log"))
        self.assertTrue(gate._same_path("C:/repo/App.log", r"C:\repo\App.log"))


class ReadGateCase(unittest.TestCase):
    """_judge_read_path/judge_read (design section 2.1, steps 3-9; step 1-2
    are the fast-path repo/enabled checks judge_read itself makes). In-process
    rather than through the installed launcher: several fixtures here (a
    corrupt calibration record, an aged heartbeat, a mocked load probe) are
    most reliably produced by writing the exact file or patching the exact
    seam, which a subprocess boundary would only obscure. The full loop
    through a real digest and a real installed hook is
    test_automatic_delegation_e2e.py's ReadGate class."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        (self.state / "routing").mkdir(parents=True)
        os.chmod(self.state, 0o700)
        self.repo = self.base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.local_root = self.base / "local-queue"
        self.local_root.mkdir()
        self.worker = self.base / "worker"
        self.worker.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(self.worker, 0o755)
        self.now = 2_000_000.0
        self.clock = lambda: self.now
        # autoroute.probe_load() reads the real host's load average via
        # os.getloadavg(), which does not exist on Windows -- Load(known=
        # False) there, always -- and can vary on Linux/macOS too;
        # readiness() already exists to defer local work on an unknown
        # reading (load_unknown), so any fixture here that assumes the lane
        # IS ready must not depend on the real host's own load. Patched to a
        # known, comfortably-under-ceiling reading by default; the two
        # tests that specifically exercise a load-based waiver override
        # this locally for their own assertion (the nested patch wins for
        # its own duration and this default resumes after it exits).
        load_patcher = mock.patch("agent_bridge.orchestration.autoroute.probe_load",
                                  return_value=autoroute.Load(ratio=0.1, known=True))
        load_patcher.start()
        self.addCleanup(load_patcher.stop)

    # ---------------------------------------------------------------- fixtures

    def write_policy(self, *, globs=("**/*.log",), enabled=True,
                     classification="internal_nonclient", mechanical_ok=True,
                     read_gate_min_bytes=None, digest_grace_seconds=None,
                     latency_budget_seconds=None, declared=("local",)):
        entry = {"classification": classification, "allowed_routes": ["claude", "codex"],
                "mechanical_ok": mechanical_ok, "mechanical_globs": list(globs)}
        local_first = {"enabled": enabled}
        for key, value in (("read_gate_min_bytes", read_gate_min_bytes),
                          ("digest_grace_seconds", digest_grace_seconds),
                          ("latency_budget_seconds", latency_budget_seconds)):
            if value is not None:
                local_first[key] = value
        document = {"version": 1, "declared_available": list(declared), "local_first": local_first,
                   "repos": {str(self.repo): entry}}
        path = Path(autoroute.policy_path(str(self.state)))
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)

    def write_calibration(self, *, sha=None, created_at=None, sizes=None):
        sha = sha if sha is not None else store.sha256_file(str(self.worker))
        created_at = created_at if created_at is not None else self.now
        sizes = sizes if sizes is not None else {
            "16000": {"median_s": 2.0, "outcomes": ["complete"] * 3},
        }
        store.atomic_write_json(localfirst.calibration_path(str(self.state)), {
            "version": 1, "created_at": created_at, "worker_sha256": sha, "sizes": sizes})

    def write_heartbeat(self, *, updated_at=None, verdict="admissible"):
        updated_at = updated_at if updated_at is not None else self.now
        if verdict == "admissible":
            resource = {"verdict": {"interactive": "admissible", "bulk": "deferred"}}
        else:
            resource = {"verdict": {"interactive": "deferred", "bulk": "deferred"}}
        store.atomic_write_json(localfirst.heartbeat_path(str(self.local_root)), {
            "version": 1, "updated_at": updated_at, "queue": {"resource": resource}})

    def make_ready(self):
        """Every readiness() condition passing: the ordinary case."""
        self.write_calibration()
        self.write_heartbeat()

    def write_log(self, name="app.log", *, size=8_500):
        target = self.repo / name
        target.write_text("x" * size, encoding="utf-8")
        return target

    def submit_job(self, *, status="queued", error=None):
        queue = LocalQueue(str(self.local_root), sampler=Sampler(), backend=FakeBackend(), clock=self.clock)
        result = queue.submit(task_type="log_triage", input="x" * 100,
                              params={"instruction": "i"}, priority="interactive",
                              classification="internal_nonclient", caller="codex", purpose="work")
        job_id = result["job_id"]
        if status != "queued" or error is not None:
            # sqlite3.Connection's own context manager only commits/rolls
            # back; it does not close the connection. On Windows a file
            # cannot be deleted while any handle to it is still open, so an
            # unclosed connection here made this fixture's own tempdir
            # cleanup fail with PermissionError on every CI Windows job.
            db = sqlite3.connect(str(self.local_root / "localq.sqlite3"))
            try:
                db.execute("UPDATE jobs SET status=?, error=? WHERE job_id=?",
                          (status, error, job_id))
                db.commit()
            finally:
                db.close()
        return job_id

    def write_receipt(self, target, *, job_id="job-1", created_at=None):
        info = os.stat(target)
        return localfirst.write_digest_receipt(
            str(self.state), path=str(target), repo=str(self.repo), size=info.st_size,
            mtime_ns=info.st_mtime_ns, offset=0, window_bytes=info.st_size,
            window_sha256="deadbeef", decode_replacements=0, task_type="log_triage",
            classification="internal_nonclient", caller="codex", job_id=job_id,
            intake_receipt_id="r1", clock=created_at or self.clock)

    def read(self, client, path, *, cwd=None, command=None):
        cwd = cwd or str(self.repo)
        if client == "claude":
            tool_name, tool_input = "Read", {"file_path": str(path)}
        else:
            tool_name, tool_input = "Bash", {"command": command or f"cat {path}"}
        return gate.judge(client, tool_name, tool_input, cwd, state_root=str(self.state),
                          clock=self.clock, local_queue_root=str(self.local_root),
                          worker_executable=str(self.worker))


class ReadGateFastPathTests(ReadGateCase):
    """Steps 1-5: allowed with nothing logged. No calibration or heartbeat
    fixture is written for these -- the fast path must never need them."""

    def test_outside_any_repository_is_allowed_and_unlogged(self):
        outside = self.base / "outside.log"
        outside.write_text("x" * 8_500, encoding="utf-8")
        self.write_policy()
        decision = self.read("claude", outside, cwd=str(self.base))
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.logged)

    def test_local_first_disabled_allows_a_matching_large_file(self):
        self.write_policy(enabled=False)
        target = self.write_log()
        decision = self.read("claude", target)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.logged)

    def test_a_repository_without_mechanical_ok_allows_a_matching_large_file(self):
        self.write_policy(mechanical_ok=False)
        target = self.write_log()
        decision = self.read("claude", target)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.logged)

    def test_a_non_matching_extension_is_allowed_even_when_large(self):
        self.write_policy(globs=("**/*.log",))
        target = self.repo / "app.py"
        target.write_text("x" * 8_500, encoding="utf-8")
        decision = self.read("claude", target)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.logged)

    def test_a_file_under_the_size_threshold_is_allowed(self):
        self.write_policy()
        target = self.write_log(size=100)
        decision = self.read("codex", target)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.logged)

    def test_a_protected_or_state_path_is_never_gated(self):
        """A digest could never satisfy an intent for the gate's own state
        (work_digest_file refuses it by the same rule): compelling one here
        would be a deny nothing could ever answer."""
        (self.state / ".git").mkdir(parents=True, exist_ok=True)
        self.write_policy(globs=("**/*",))
        target = self.state / "routing" / "leak.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * 8_500, encoding="utf-8")
        decision = self.read("claude", target, cwd=str(self.state))
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.logged)


class ReadGateReadinessWaiverTests(ReadGateCase):
    """Step 6: every readiness() reason waives rather than blocks (design
    section 2.1); each is its own fixture, matching Phase 1's own
    ReadinessTests style."""

    def assert_waived(self, decision, code):
        self.assertTrue(decision.allowed, decision)
        self.assertTrue(decision.logged)
        self.assertEqual(decision.code, "local_first_waived")
        self.assertEqual(decision.extra.get("waiver_reason"), code)
        self.assertEqual(decision.extra.get("bytes_estimate"), 8_500)

    def test_waived_when_calibration_is_missing(self):
        self.write_policy()
        self.write_heartbeat()
        target = self.write_log()
        self.assert_waived(self.read("claude", target), "calibration_missing")

    def test_waived_when_calibration_is_stale(self):
        self.write_policy()
        self.write_calibration(created_at=self.now - 40 * 86400)
        self.write_heartbeat()
        target = self.write_log()
        self.assert_waived(self.read("claude", target), "calibration_stale")

    def test_waived_when_the_worker_no_longer_matches_the_calibration(self):
        self.write_policy()
        self.write_calibration(sha="not-the-real-hash")
        self.write_heartbeat()
        target = self.write_log()
        self.assert_waived(self.read("claude", target), "calibration_worker_changed")

    def test_waived_when_the_heartbeat_is_missing(self):
        self.write_policy()
        self.write_calibration()
        target = self.write_log()
        self.assert_waived(self.read("claude", target), "executor_not_running")

    def test_waived_when_the_heartbeat_is_aged(self):
        self.write_policy()
        self.write_calibration()
        self.write_heartbeat(updated_at=self.now - 120)
        target = self.write_log()
        self.assert_waived(self.read("claude", target), "executor_not_running")

    def test_waived_when_the_heartbeat_carries_a_deferred_verdict(self):
        self.write_policy()
        self.write_calibration()
        self.write_heartbeat(verdict="deferred")
        target = self.write_log()
        self.assert_waived(self.read("claude", target), "resource_deferred")

    def test_waived_when_load_is_unknown(self):
        self.write_policy()
        self.make_ready()
        target = self.write_log()
        with mock.patch("agent_bridge.orchestration.autoroute.probe_load",
                        return_value=autoroute.Load(known=False)):
            self.assert_waived(self.read("claude", target), "load_unknown")

    def test_waived_when_load_is_high(self):
        self.write_policy()
        self.make_ready()
        target = self.write_log()
        with mock.patch("agent_bridge.orchestration.autoroute.probe_load",
                        return_value=autoroute.Load(ratio=5.0, known=True)):
            self.assert_waived(self.read("claude", target), "load_high")

    def test_waived_when_over_the_latency_budget(self):
        self.write_policy(latency_budget_seconds=1.0)
        self.write_calibration(sizes={"16000": {"median_s": 30.0, "outcomes": ["complete"] * 3}})
        self.write_heartbeat()
        target = self.write_log()
        with mock.patch("agent_bridge.orchestration.autoroute.probe_load",
                        return_value=autoroute.Load(ratio=0.1, known=True)):
            self.assert_waived(self.read("claude", target), "over_latency_budget")


class ReadGateReceiptTests(ReadGateCase):
    """Steps 7-9: a digest receipt's job status, the grace window, and
    writing a fresh intent."""

    def setUp(self):
        super().setUp()
        self.write_policy()
        self.make_ready()
        self.target = self.write_log()

    def test_allowed_with_digest_present_when_the_job_is_complete(self):
        job_id = self.submit_job(status="complete")
        self.write_receipt(self.target, job_id=job_id)
        decision = self.read("claude", self.target)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "local_digest_present")
        self.assertEqual(decision.extra.get("job_id"), job_id)

    def test_the_other_client_is_also_allowed_on_the_shared_receipt(self):
        """The receipt is shared by both clients (design section 2.5)."""
        job_id = self.submit_job(status="complete")
        self.write_receipt(self.target, job_id=job_id)
        self.assertEqual(self.read("claude", self.target).code, "local_digest_present")
        self.assertEqual(self.read("codex", self.target).code, "local_digest_present")

    def test_denied_pending_when_the_job_is_queued(self):
        job_id = self.submit_job(status="queued")
        self.write_receipt(self.target, job_id=job_id)
        decision = self.read("codex", self.target)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "local_digest_pending")
        self.assertIn(job_id, decision.reason)
        self.assertIn("work_result", decision.reason)

    def test_denied_pending_when_the_job_is_running(self):
        job_id = self.submit_job(status="running")
        self.write_receipt(self.target, job_id=job_id)
        self.assertEqual(self.read("codex", self.target).code, "local_digest_pending")

    def test_denied_pending_when_the_job_state_cannot_be_confirmed(self):
        """The receipt names a job the queue database does not have (or the
        database itself is unreadable): treated as not yet confirmed
        complete, the same fail-closed posture stage_db_unavailable already
        carries for the write gate, never a silent allow."""
        self.write_receipt(self.target, job_id="no-such-job")
        decision = self.read("codex", self.target)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "local_digest_pending")

    def test_waived_when_the_job_ended_failed_unknown_cancelled_or_expired(self):
        for state in ("failed", "unknown", "cancelled", "expired"):
            with self.subTest(state=state):
                target = self.write_log(name=f"{state}.log")
                job_id = self.submit_job(status=state)
                self.write_receipt(target, job_id=job_id)
                decision = self.read("claude", target)
                self.assertTrue(decision.allowed, decision)
                self.assertEqual(decision.code, "local_first_waived")
                self.assertEqual(decision.extra.get("waiver_reason"), f"digest_{state}")

    def test_waived_when_the_job_is_queued_and_deferred(self):
        job_id = self.submit_job(status="queued", error="deferred:resource_pressure")
        self.write_receipt(self.target, job_id=job_id)
        decision = self.read("claude", self.target)
        self.assertTrue(decision.allowed, decision)
        self.assertEqual(decision.code, "local_first_waived")
        self.assertEqual(decision.extra.get("waiver_reason"), "digest_deferred:resource_pressure")

    def test_waived_recent_digest_changed_file_within_the_grace_window(self):
        job_id = self.submit_job(status="complete")
        self.write_receipt(self.target, job_id=job_id)
        # Changes the file's identity, so the receipt no longer matches.
        self.target.write_text("y" * 9_000, encoding="utf-8")
        self.now += 60  # well within the default 900s grace window
        decision = self.read("claude", self.target)
        self.assertTrue(decision.allowed, decision)
        self.assertEqual(decision.code, "local_first_waived")
        self.assertEqual(decision.extra.get("waiver_reason"), "recent_digest_changed_file")

    def test_required_again_once_the_grace_window_has_passed(self):
        job_id = self.submit_job(status="complete")
        self.write_receipt(self.target, job_id=job_id)
        self.target.write_text("y" * 9_000, encoding="utf-8")
        self.now += 901  # past the default 900s digest_grace_seconds
        self.write_heartbeat()  # keeps the lane itself ready; isolates the grace check
        decision = self.read("claude", self.target)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "local_digest_required")

    def test_required_when_there_is_no_receipt_at_all(self):
        decision = self.read("claude", self.target)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "local_digest_required")
        self.assertIn("work_digest_file", decision.reason)
        # realpath, not the fixture's own raw path: on a Windows runner
        # whose account name has a short 8.3 alias (RUNNER~1 for
        # runneradmin), tempfile's own path and os.path.realpath's
        # normalization of it are two different strings for the identical
        # file, and _judge_read_path builds its reason from the resolved
        # one.
        self.assertIn(os.path.realpath(str(self.target)), decision.reason)

    def test_a_fresh_intent_is_written_and_matches_the_binding_fields(self):
        self.read("claude", self.target)
        intent = localfirst.read_digest_intent(str(self.state), str(self.target))
        self.assertIsNotNone(intent)
        info = os.stat(self.target)
        self.assertEqual(intent["path"], os.path.realpath(str(self.target)))
        self.assertEqual(intent["size"], info.st_size)
        self.assertEqual(intent["mtime_ns"], info.st_mtime_ns)
        self.assertEqual(intent["matched_glob"], "**/*.log")
        self.assertEqual(intent["classification"], "internal_nonclient")
        self.assertEqual(intent["client"], "claude")
        self.assertEqual(intent["next_call"], "work_digest_file")

    def test_a_digest_naming_a_different_file_does_not_retire_this_intent(self):
        """DIGEST_INTENT_BINDING: a receipt for a different size/mtime must
        not be mistaken for having satisfied this file's intent."""
        self.read("claude", self.target)
        other = self.write_log(name="other.log")
        localfirst.retire_digest_intent(
            str(self.state), str(self.target),
            binding={"path": os.path.realpath(str(self.target)),
                    "size": os.stat(other).st_size, "mtime_ns": os.stat(other).st_mtime_ns})
        self.assertIsNotNone(localfirst.read_digest_intent(str(self.state), str(self.target)))


class ReadGatePolicyAndMultiPathTests(ReadGateCase):
    def test_unreadable_policy_denies_a_gated_shape_read_inside_a_repository(self):
        path = Path(autoroute.policy_path(str(self.state)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        target = self.write_log()
        decision = self.read("claude", target)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "gate_auto_decision_failed")
        self.assertIn("policy_unreadable", decision.reason)

    def test_unreadable_policy_still_allows_a_read_outside_any_repository(self):
        path = Path(autoroute.policy_path(str(self.state)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        outside = self.base / "outside.log"
        outside.write_text("x" * 8_500, encoding="utf-8")
        decision = self.read("claude", outside, cwd=str(self.base))
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.logged)

    def test_two_files_named_in_one_call_denies_on_the_first_gated_one(self):
        self.write_policy()
        self.make_ready()
        small = self.write_log(name="small.log", size=100)     # fast-path allow
        large = self.write_log(name="large.log", size=8_500)   # gated, no receipt
        decision = self.read("codex", None, command=f"cat {small} {large}")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "local_digest_required")
        self.assertIn(os.path.realpath(str(large)), decision.reason)


class ReadGateWriteShapedOverlayTests(ReadGateCase):
    """judge's overlay of judge_read onto a Codex "shell" classification
    (design section 2.1 combined with classify's own, unchanged,
    tested-elsewhere write-precedence contract). Before this overlay
    existed, a command shaped like both a write and a whole-file read (``cat
    a.log > b.log``) never reached judge_read at all: classify picks the
    write shape, judge trusted that kind alone, and the read gate's entire
    purpose was defeated for any ordinary Codex command whose text happened
    to also carry a write-shaped suffix -- not an edge case, since a
    trailing redirect or a piped ``tee`` is ordinary shell usage."""

    def grant_write_access(self, client="codex"):
        """A valid, unexpired routing receipt naming ``client`` as owner, so
        the write side of a "shell" classification allows on its own --
        exactly the condition under which the read gate previously went
        unchecked entirely."""
        gate.record_decision(
            str(self.state), caller=client,
            stage_record=owned_stage(owner_route=client, lease_until=self.now + 3600),
            repo=str(self.repo), reason="test fixture",
            ttl_seconds=3600, clock=self.clock)

    def test_a_write_shaped_command_that_also_reads_a_file_whole_still_requires_a_digest(self):
        self.write_policy()
        self.make_ready()
        self.grant_write_access()
        target = self.write_log()
        decision = self.read("codex", None, command=f"cat {target} > {self.repo / 'out.log'}")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "local_digest_required")
        self.assertIn(os.path.realpath(str(target)), decision.reason)

    def test_the_write_side_deny_still_wins_when_there_is_no_routing_receipt_at_all(self):
        """Without grant_write_access, the write side denies first on its
        own, pre-existing grounds; a command that cannot proceed at all
        gains nothing from also being judged as a read (and judge_read would
        otherwise record a fresh digest intent for a call going nowhere)."""
        self.write_policy()
        self.make_ready()
        target = self.write_log()
        decision = self.read("codex", None, command=f"cat {target} > {self.repo / 'out.log'}")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "no_routing_receipt")

    def test_a_write_shaped_read_is_allowed_once_the_digest_is_present(self):
        self.write_policy()
        self.make_ready()
        self.grant_write_access()
        target = self.write_log()
        job_id = self.submit_job(status="complete")
        self.write_receipt(target, job_id=job_id)
        decision = self.read("codex", None, command=f"cat {target} > {self.repo / 'out.log'}")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "local_digest_present")

    def test_claude_bash_is_not_subject_to_the_overlay(self):
        """The whole-file-read heuristic stays Codex-only (design section
        2.1): the identical write-shaped, reads-a-big-file command from
        Claude's Bash tool is judged as a write only, exactly as before this
        overlay existed."""
        self.write_policy()
        self.make_ready()
        self.grant_write_access(client="claude")
        target = self.write_log()
        decision = gate.judge(
            "claude", "Bash", {"command": f"cat {target} > {self.repo / 'out.log'}"},
            str(self.repo), state_root=str(self.state), clock=self.clock,
            local_queue_root=str(self.local_root), worker_executable=str(self.worker))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "routing_receipt_valid")


class RecordEventReservedFieldTests(unittest.TestCase):
    """Decision.extra is a plain dict merged straight into the ledger
    record. Nothing in this codebase populates a key that collides with one
    record_event already sets itself (a read decision's own extra keys --
    waiver_reason, bytes_estimate, job_id, matched_glob -- are all distinct
    from them), but nothing stopped a future Decision from doing so either;
    this is the guard, not a reachable-today exploit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        (self.state / "routing").mkdir(parents=True)

    def _events(self):
        ledger = os.path.join(gate.receipt_dir(str(self.state)), gate.EVENT_LEDGER)
        with open(ledger, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_a_colliding_extra_key_does_not_overwrite_the_reserved_field(self):
        decision = gate.Decision(
            "deny", "local_digest_required", "test", ("repo",), logged=True,
            extra={"code": "hijacked", "at": 999, "client": "hijacked-client",
                  "bytes_estimate": 8_500})
        gate.record_event(str(self.state), "codex", "Bash", decision, clock=lambda: 12345.0)
        event = self._events()[0]
        self.assertEqual(event["code"], "local_digest_required")
        self.assertEqual(event["client"], "codex")
        self.assertEqual(event["at"], 12345.0)
        self.assertEqual(event["bytes_estimate"], 8_500)


class RunHookReadEventTests(ReadGateCase):
    """run_hook, not judge: only run_hook writes the event ledger, so this is
    where "that reason in the event" (design section 2.1/2.5) is actually
    checked against the persisted record rather than just the return value."""

    def _events(self):
        ledger = os.path.join(gate.receipt_dir(str(self.state)), gate.EVENT_LEDGER)
        with open(ledger, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_a_waiver_reason_and_bytes_estimate_reach_the_event_ledger(self):
        self.write_policy()
        self.write_heartbeat()  # calibration missing -> waived
        target = self.write_log()
        payload = {"tool_name": "Read", "tool_input": {"file_path": str(target)},
                  "cwd": str(self.repo)}
        decision = gate.run_hook("claude", str(self.state), payload, clock=self.clock,
                                 local_queue_root=str(self.local_root),
                                 worker_executable=str(self.worker))
        self.assertEqual(decision.code, "local_first_waived")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "local_first_waived")
        self.assertEqual(events[0]["waiver_reason"], "calibration_missing")
        self.assertEqual(events[0]["bytes_estimate"], 8_500)

    def test_local_digest_required_reaches_the_event_ledger_with_the_matched_glob(self):
        self.write_policy()
        self.make_ready()
        target = self.write_log()
        payload = {"tool_name": "Read", "tool_input": {"file_path": str(target)},
                  "cwd": str(self.repo)}
        decision = gate.run_hook("claude", str(self.state), payload, clock=self.clock,
                                 local_queue_root=str(self.local_root),
                                 worker_executable=str(self.worker))
        self.assertEqual(decision.code, "local_digest_required")
        events = self._events()
        self.assertEqual(events[-1]["code"], "local_digest_required")
        self.assertEqual(events[-1]["matched_glob"], "**/*.log")
        self.assertEqual(events[-1]["bytes_estimate"], 8_500)

    def test_a_fast_path_allow_writes_no_event_at_all(self):
        self.write_policy(enabled=False)
        target = self.write_log()
        payload = {"tool_name": "Read", "tool_input": {"file_path": str(target)},
                  "cwd": str(self.repo)}
        gate.run_hook("claude", str(self.state), payload, clock=self.clock,
                      local_queue_root=str(self.local_root), worker_executable=str(self.worker))
        ledger = os.path.join(gate.receipt_dir(str(self.state)), gate.EVENT_LEDGER)
        self.assertFalse(os.path.exists(ledger))

    def test_two_files_both_requiring_a_digest_each_log_their_own_event(self):
        """judge_read returns only one Decision as the call's overall
        outcome (the first deny), but a call naming two gated-shape files
        must not silently drop the second one's own event: its
        digest_intent audit-ledger row is written regardless (a per-path
        side effect inside _judge_read_path), and before also_log existed,
        only the first path's outcome ever became a gate event -- so the
        audit's own adds-up invariant broke the moment the second file's
        intent later expired with nothing counting it."""
        self.write_policy()
        self.make_ready()
        small_gated = self.write_log(name="a.log", size=8_500)
        large_gated = self.write_log(name="b.log", size=9_500)
        payload = {"tool_name": "Bash",
                  "tool_input": {"command": f"cat {small_gated} {large_gated}"},
                  "cwd": str(self.repo)}
        decision = gate.run_hook("codex", str(self.state), payload, clock=self.clock,
                                 local_queue_root=str(self.local_root),
                                 worker_executable=str(self.worker))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "local_digest_required")
        events = self._events()
        required = [event for event in events if event["code"] == "local_digest_required"]
        self.assertEqual(len(required), 2)
        self.assertEqual({event["bytes_estimate"] for event in required}, {8_500, 9_500})

    def test_two_files_already_digested_both_log_their_own_event(self):
        """The all-allow side of the same gap: two gated-shape files that
        both already have a complete digest used to log only the first
        (decisions[0]), silently under-counting compelled/digested for the
        second even though nothing was denied."""
        self.write_policy()
        self.make_ready()
        first = self.write_log(name="a.log", size=8_500)
        second = self.write_log(name="b.log", size=9_500)
        job_id = self.submit_job(status="complete")
        self.write_receipt(first, job_id=job_id)
        self.write_receipt(second, job_id=job_id)
        payload = {"tool_name": "Bash", "tool_input": {"command": f"cat {first} {second}"},
                  "cwd": str(self.repo)}
        decision = gate.run_hook("codex", str(self.state), payload, clock=self.clock,
                                 local_queue_root=str(self.local_root),
                                 worker_executable=str(self.worker))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "local_digest_present")
        events = self._events()
        present = [event for event in events if event["code"] == "local_digest_present"]
        self.assertEqual(len(present), 2)
        self.assertEqual({event["bytes_estimate"] for event in present}, {8_500, 9_500})

    def test_the_audit_adds_up_after_a_multi_path_call_requires_two_digests(self):
        """The acceptance criterion the design's own build plan names,
        exercised through the real judge_read/record_event code path rather
        than hand-constructed ledger rows -- the only way the multi-path
        under-logging gap this class's other two tests target could actually
        have been caught."""
        from agent_bridge.orchestration import audit

        self.write_policy()
        self.make_ready()
        small_gated = self.write_log(name="a.log", size=8_500)
        large_gated = self.write_log(name="b.log", size=9_500)
        payload = {"tool_name": "Bash",
                  "tool_input": {"command": f"cat {small_gated} {large_gated}"},
                  "cwd": str(self.repo)}
        gate.run_hook("codex", str(self.state), payload, clock=self.clock,
                      local_queue_root=str(self.local_root), worker_executable=str(self.worker))
        document = audit.report(str(self.state), home=str(self.base),
                                since_hours=0.0, clock=self.clock)
        lf = document["local_first"]
        self.assertEqual(lf["compelled"]["count"], 2)
        self.assertEqual(lf["outstanding"]["count"], 2)
        self.assertTrue(lf["adds_up"])


class TestBuildRunnerMatchTests(unittest.TestCase):
    """Phase 5's own matcher (design section 2.9): narrow, by-name, reusing
    the same tokenizer the read gate's whole-file-read heuristic already
    trusts rather than a second parser."""

    def matched(self, command):
        return gate._shell_reader_words(command, gate._TEST_BUILD_RUNNERS)

    def test_a_bare_runner_is_matched(self):
        self.assertEqual(self.matched("pytest -q"), ["pytest"])

    def test_the_default_names_are_unchanged(self):
        # Passing no `names` argument at all still matches the whole-file
        # readers, not the test/build runners: the generalization did not
        # change any existing caller's behaviour.
        self.assertEqual(gate._shell_reader_words("cat a.log"), ["cat"])
        self.assertEqual(gate._shell_reader_words("pytest -q"), [])

    def test_chained_after_a_cd_is_matched(self):
        self.assertEqual(self.matched("cd /repo && pytest -q"), ["pytest"])

    def test_used_as_a_plain_argument_is_not_matched(self):
        # The same "echo cat" negative the whole-file-read heuristic already
        # guards against: a runner name is only a match in command-name
        # position, not anywhere in the text.
        self.assertEqual(self.matched("echo pytest"), [])

    def test_a_windows_executable_suffix_does_not_hide_it(self):
        self.assertEqual(self.matched("rake.bat test"), ["rake.bat"])

    def test_a_general_purpose_wrapper_is_not_matched(self):
        # The documented, deliberate gap (NOT_COVERED): npm/make/cargo/etc.
        # run many non-test/build subcommands too, so a name-only match
        # would mislabel most of what they actually do.
        for command in ("npm test", "npm install", "make test", "make clean",
                        "cargo test", "cargo build", "go test ./...", "go vet ./...",
                        "python -m pytest", "python3 -m unittest discover"):
            self.assertEqual(self.matched(command), [], command)


class ResponseByteLengthTests(unittest.TestCase):
    def test_stdout_and_stderr_are_summed(self):
        self.assertEqual(gate._response_byte_length({"stdout": "abc", "stderr": "de"}), 5)

    def test_missing_fields_count_as_nothing(self):
        self.assertEqual(gate._response_byte_length({}), 0)

    def test_a_non_dict_response_is_zero_not_a_crash(self):
        self.assertEqual(gate._response_byte_length(None), 0)
        self.assertEqual(gate._response_byte_length("not a dict"), 0)
        self.assertEqual(gate._response_byte_length(["stdout"]), 0)

    def test_a_non_string_field_is_ignored_not_a_crash(self):
        self.assertEqual(gate._response_byte_length({"stdout": 123, "stderr": "ok"}), 2)

    def test_multibyte_text_counts_encoded_bytes_not_characters(self):
        self.assertEqual(gate._response_byte_length({"stdout": "café", "stderr": ""}),
                         len("café".encode("utf-8")))


class RecordInlineMeasurementTests(GateCase):
    """gate.record_inline_measurement and run_post_hook (design section 2.9):
    measurement only, and only for a Claude Bash call matched by
    _TEST_BUILD_RUNNERS."""

    def ledger_rows(self):
        path = os.path.join(gate.receipt_dir(str(self.state)), gate.INLINE_LEDGER)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_a_matching_claude_bash_call_is_recorded(self):
        gate.record_inline_measurement(str(self.state), "claude", "Bash",
                                       {"command": "pytest -q"},
                                       {"stdout": "1 passed", "stderr": ""}, clock=self.clock)
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_runner"], "pytest")
        self.assertEqual(rows[0]["bytes"], len(b"1 passed"))
        self.assertEqual(rows[0]["client"], "claude")
        self.assertEqual(rows[0]["at"], self.clock())

    def test_a_non_matching_command_is_not_recorded(self):
        gate.record_inline_measurement(str(self.state), "claude", "Bash",
                                       {"command": "ls -la"}, {"stdout": "x"}, clock=self.clock)
        self.assertEqual(self.ledger_rows(), [])

    def test_a_non_bash_tool_is_not_recorded_even_if_the_input_matches(self):
        gate.record_inline_measurement(str(self.state), "claude", "Read",
                                       {"command": "pytest -q"}, {"stdout": "x"}, clock=self.clock)
        self.assertEqual(self.ledger_rows(), [])

    def test_codex_is_never_recorded(self):
        # Phase 5 installs no PostToolUse entry for Codex at all (Phase 0
        # could not confirm the event exists there); this function's own
        # client check is a second, independent line of defense, not
        # something that relies solely on the installer keeping it out.
        gate.record_inline_measurement(str(self.state), "codex", "Bash",
                                       {"command": "pytest -q"}, {"stdout": "x"}, clock=self.clock)
        self.assertEqual(self.ledger_rows(), [])

    def test_run_post_hook_never_raises_on_a_garbage_payload(self):
        for payload in ({}, {"tool_name": 123}, {"tool_name": "Bash", "tool_input": None},
                        {"tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_response": None}):
            gate.run_post_hook("claude", str(self.state), payload, clock=self.clock)
        # The one payload above with a real match still gets recorded: a
        # broad except in run_post_hook must not swallow the normal path.
        self.assertEqual(len(self.ledger_rows()), 1)

    def test_run_post_hook_survives_an_unwritable_state_root(self):
        gate.run_post_hook("claude", "/nonexistent/does/not/exist", {
            "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
            "tool_response": {"stdout": "x"}}, clock=self.clock)   # must not raise


class PostToolUseHookModeTests(GateCase):
    """main()'s --event PostToolUse branch, through the module's own argv
    entry point (subprocess, not the installed launcher script -- that is
    exercised separately in test_automatic_delegation_e2e.py)."""

    def write_config(self):
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({"state_root": str(self.state),
                                      "capacity_db": str(self.state / "capacity.sqlite3")}), encoding="utf-8")
        return config

    def run_post(self, payload):
        config = self.write_config()
        return subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "claude",
             "--config", str(config), "--event", "PostToolUse"],
            input=json.dumps(payload).encode(), capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})

    def test_a_matching_call_is_measured_and_the_reply_is_empty(self):
        completed = self.run_post({"tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                                   "tool_response": {"stdout": "1 passed", "stderr": ""}})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), b"{}")
        path = os.path.join(gate.receipt_dir(str(self.state)), gate.INLINE_LEDGER)
        with open(path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bytes"], len(b"1 passed"))

    def test_garbage_input_still_answers_empty_not_a_crash(self):
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "claude",
             "--config", str(self.write_config()), "--event", "PostToolUse"],
            input=b"not json", capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), b"{}")
        self.assertNotIn(b"Traceback", completed.stdout)

    def test_no_event_flag_still_defaults_to_pretooluse(self):
        # Backward compatibility: a hook command installed before Phase 5
        # existed never passes --event at all.
        config = self.write_config()
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "claude",
             "--config", str(config)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                              "cwd": str(self.repo)}).encode(),
            capture_output=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        out = json.loads(completed.stdout)
        # A PreToolUse judgment happened (some hookSpecificOutput-shaped
        # decision or an unlogged allow), not Phase 5's bare {} reply.
        self.assertIsInstance(out, dict)

    def test_an_argparse_failure_still_answers_empty_for_posttooluse(self):
        # A confirmed adversarial-review finding: main()'s except SystemExit
        # handler (reached when argparse itself rejects the arguments, e.g.
        # an invalid --client, before args.event even exists) used to print
        # a PreToolUse-shaped deny unconditionally, regardless of --event --
        # violating "PostToolUse never denies" for a call this malformed.
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "nobody",
             "--config", str(self.write_config()), "--event", "PostToolUse"],
            input=b"{}", capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), b"{}")

    def test_an_argparse_failure_without_the_event_flag_still_denies(self):
        # The companion case: the same malformed call with no --event at all
        # (or --event PreToolUse) must keep denying exactly as before.
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate", "--client", "nobody",
             "--config", str(self.write_config())],
            input=b"{}", capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        out = json.loads(completed.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class PostToolUseInstallTests(unittest.TestCase):
    """plan_install/install's Phase 5 additions: a second, independently
    tracked PostToolUse entry, opt-in via include_post."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir()
        self.config = self.base / "orchestration.json"
        self.config.write_text(json.dumps({"state_root": str(self.base / "state"),
                                           "capacity_db": str(self.base / "state" / "capacity.sqlite3")}),
                               encoding="utf-8")
        self.settings = self.home / ".claude" / "settings.json"
        self.receipt_path = self.home / ".agent-bridge" / "onboarding" / "gate-installation.json"

    def install(self, clients=("claude",), **kwargs):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            return gate.install(str(self.home), str(ROOT), str(self.config), clients, **kwargs)

    def settings_hooks(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]

    def test_include_post_adds_a_bash_only_post_entry(self):
        report = self.install(apply=True, include_post=True)
        self.assertTrue(report["applied"])
        self.assertEqual(report["post_clients"], ["claude"])
        hooks = self.settings_hooks()
        self.assertIn("PreToolUse", hooks)
        self.assertIn("PostToolUse", hooks)
        post = hooks["PostToolUse"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["matcher"], "Bash")
        self.assertIn("--event PostToolUse", post[0]["hooks"][0]["command"])
        pre_command = hooks["PreToolUse"][0]["hooks"][0]["command"]
        self.assertNotIn("--event PostToolUse", pre_command)
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(receipt["post_entries"]), ["claude"])

    def test_without_include_post_no_post_entry_is_added(self):
        self.install(apply=True, include_post=False)
        hooks = self.settings_hooks()
        self.assertIn("PreToolUse", hooks)
        self.assertNotIn("PostToolUse", hooks)
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertNotIn("post_entries", receipt)

    def test_a_plain_reinstall_does_not_remove_an_existing_post_entry(self):
        self.install(apply=True, include_post=True)
        report = self.install(apply=True, include_post=False)
        # Nothing changed at all: neither the Pre nor the Post entry needed
        # a rewrite, so a plain re-install is a true no-op.
        self.assertEqual(report["planned_files"], [])
        hooks = self.settings_hooks()
        self.assertIn("PostToolUse", hooks)
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(receipt["post_entries"]), ["claude"])

    def test_include_post_is_idempotent(self):
        self.install(apply=True, include_post=True)
        report = self.install(apply=True, include_post=True)
        self.assertEqual(report["planned_files"], [])

    def test_remove_takes_out_both_entries_together(self):
        self.install(apply=True, include_post=True)
        report = self.install(apply=True, remove=True)
        self.assertTrue(report["applied"])
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("hooks", settings)
        self.assertFalse(self.receipt_path.exists())

    def test_pre_side_removal_is_not_lost_when_the_post_side_is_already_gone(self):
        # CRITICAL regression (confirmed by adversarial review): plan_install's
        # composition used to let a real Pre-side change vanish whenever the
        # Post-side call was itself a no-op, because that call's return value
        # unconditionally overwrote the Pre-side call's already-computed
        # bytes. Reproduced via drift: the receipt still records a Post entry
        # for claude, but the live settings.json has already lost it (a hand
        # edit, or an earlier run of this very bug) -- exactly the state
        # where the Post-side hooks_file_update call, on --remove, finds
        # nothing left to do and returns None.
        self.install(apply=True, include_post=True)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        del settings["hooks"]["PostToolUse"]
        self.settings.write_text(json.dumps(settings), encoding="utf-8")

        report = self.install(apply=True, remove=True)

        self.assertTrue(report["applied"])
        after = json.loads(self.settings.read_text(encoding="utf-8"))
        # The live PreToolUse entry must actually be gone: the bug left it
        # installed and active while the receipt below claimed removal
        # had succeeded.
        self.assertNotIn("PreToolUse", after.get("hooks", {}))
        self.assertFalse(self.receipt_path.exists())

    def test_codex_is_never_given_a_post_entry_even_when_asked(self):
        report = self.install(clients=("claude", "codex"), apply=True, include_post=True)
        self.assertEqual(report["post_clients"], ["claude"])
        hooks_json = json.loads((self.home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertNotIn("PostToolUse", hooks_json.get("hooks", {}))

    def test_an_edited_post_entry_is_preserved_not_overwritten(self):
        self.install(apply=True, include_post=True)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        settings["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"] = 99
        self.settings.write_text(json.dumps(settings), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "was edited"):
            self.install(apply=True, include_post=True)

    def test_report_shows_post_installation_state(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            before = gate.report(str(self.home), str(self.base / "state"))
        self.assertEqual(before["installed_post"], {"claude": False, "codex": False})
        self.assertEqual(before["inline_measurement"], {"claude": "counted", "codex": "not_countable"})
        self.install(apply=True, include_post=True)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}):
            after = gate.report(str(self.home), str(self.base / "state"))
        self.assertEqual(after["installed_post"], {"claude": True, "codex": False})


if __name__ == "__main__":
    unittest.main()
