"""Offline contracts exposed by the claude_task mutation sweep."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge.execution import claude_task


class ShaTests(unittest.TestCase):
    def test_hashes_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "invented-report.txt"
            path.write_bytes(b"Northstar Fabrication, 2026-03-14, 42.00\n")
            self.assertEqual(claude_task._sha(path), hashlib.sha256(path.read_bytes()).hexdigest())


class ContentSnapshotTests(unittest.TestCase):
    def test_counts_exact_dirty_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "invented-ledger.txt").write_bytes(b"42\n")
            with mock.patch.object(claude_task, "_relevant_paths", return_value=["invented-ledger.txt"]):
                snapshot = claude_task._content_snapshot(repo, {})
            self.assertEqual(snapshot["bytes"], 3)


class AtomicJsonTests(unittest.TestCase):
    def test_refuses_receipt_when_private_file_cannot_be_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "receipt.json"
            with mock.patch.object(claude_task.agent_platform, "enforce_owner_only_file",
                                   side_effect=PermissionError("owner-only unavailable")) as enforce:
                with self.assertRaises(PermissionError):
                    claude_task._atomic_json(target, {"status": "pending"})
            enforce.assert_called_once()
            self.assertFalse(target.exists())


class JsonTests(unittest.TestCase):
    def test_rejects_a_json_array_as_a_result_document(self):
        with self.assertRaisesRegex(claude_task.TaskError, "must be a JSON object"):
            claude_task._json(b"[]", "invented result")


class AuthTests(unittest.TestCase):
    def test_rejects_an_unsupported_auth_method(self):
        payload = {"loggedIn": True, "authMethod": "api-key", "subscriptionType": "team"}
        result = SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())
        with mock.patch.object(claude_task, "_run", return_value=result):
            with self.assertRaisesRegex(claude_task.TaskError, "supported claude.ai subscription"):
                claude_task._auth(Path("/invented/claude"), {})

    def test_nonzero_status_refuses_even_a_valid_auth_envelope(self):
        payload = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "team"}
        result = SimpleNamespace(returncode=9, stdout=json.dumps(payload).encode())
        with mock.patch.object(claude_task, "_run", return_value=result):
            with self.assertRaisesRegex(claude_task.TaskError, "auth preflight failed"):
                claude_task._auth(Path("/invented/claude"), {})


class NetnsConfinedTests(unittest.TestCase):
    def test_normal_exit_is_not_a_confinement_selftest_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / "tree"
            tree.mkdir()
            result = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            with mock.patch.object(claude_task.hostenv, "confined_argv", return_value=["fake"]) as confined, \
                 mock.patch.object(claude_task, "_run", return_value=result):
                actual = claude_task._netns_confined(["python", "-m", "unittest"], tree,
                                                      root / "scratch", {}, 5,
                                                      SimpleNamespace(name="mock-userns"))
            self.assertIs(actual, result)
            self.assertEqual(confined.call_args.args[0], ["python", "-m", "unittest"])
            self.assertEqual(actual.sandbox_backend, "mock-userns")


class ConfinementCanariesTests(unittest.TestCase):
    def test_includes_the_source_checkout_of_a_linked_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            common = root / "source" / ".git"
            gitdir = common / "worktrees" / "invented"
            gitdir.mkdir(parents=True)
            (gitdir / "commondir").write_text("../..\n")
            tree = root / "job" / "verification-worktree"
            tree.mkdir(parents=True)
            (tree / ".git").write_text(f"gitdir: {gitdir}\n")
            self.assertIn(str(common.parent.resolve()), claude_task._confinement_canaries(tree))


class SandboxExecConfinedTests(unittest.TestCase):
    def test_profile_can_read_a_pinned_virtual_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / "tree"
            tree.mkdir()
            scratch = root / "scratch"
            venv = root / "invented-venv"
            venv.mkdir()
            result = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            with mock.patch.object(claude_task.sys, "prefix", str(venv)), \
                 mock.patch.object(claude_task, "_run", return_value=result):
                claude_task._sandbox_exec_confined([sys.executable, "-m", "unittest"], tree,
                                                   scratch, {}, 5,
                                                   SimpleNamespace(name="mock-sandbox"))
            self.assertIn(f'(subpath "{venv.resolve()}")', (scratch / "verify.sb").read_text())


class MainTests(unittest.TestCase):
    def test_complete_result_has_success_exit_status(self):
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            args = [str(Path(temp) / "brief.md"), "--repo", str(Path(temp) / "repo"),
                    "--classification", "synthetic", "--verify-json", '["git","status"]']
            with mock.patch.object(claude_task, "run_task", return_value={"status": "complete"}), \
                 contextlib.redirect_stdout(output):
                code = claude_task.main(args)
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
