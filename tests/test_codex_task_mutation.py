import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import codex_task as task

SYNTHETIC_ROOT = Path(tempfile.gettempdir()) / "synthetic-codex-task"


class ShaTests(unittest.TestCase):
    def test_hashes_file_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invented-brief.txt"
            path.write_bytes(b"Synthetic account A: 17\n")
            self.assertEqual(task._sha(path), hashlib.sha256(path.read_bytes()).hexdigest())


class ContentSnapshotTests(unittest.TestCase):
    def test_file_count_at_limit_is_covered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "example.txt").write_text("Synthetic company\n")
            with mock.patch.object(task, "MAX_SNAPSHOT_FILES", 1), \
                 mock.patch.object(task, "_relevant_paths", return_value=["example.txt"]):
                snapshot = task._content_snapshot(root, {})
            self.assertEqual(snapshot["files"], 1)


class HashRegularFileTests(unittest.TestCase):
    def test_file_size_at_limit_is_hashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "example.txt"
            path.write_bytes(b"123")
            digest = hashlib.sha256()
            with mock.patch.object(task, "MAX_SNAPSHOT_FILE_BYTES", 3):
                self.assertEqual(task._hash_regular_file(path, digest, 3), 3)


class RequireConfinementTests(unittest.TestCase):
    def test_returns_selected_backend(self):
        backend = object()
        with mock.patch.object(task.hostenv, "confinement", return_value=backend) as selected:
            self.assertIs(task._require_confinement("synthetic"), backend)
        selected.assert_called_once_with("synthetic")


class AtomicJsonTests(unittest.TestCase):
    def test_writes_receipt_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            task._atomic_json(path, {"company": "Invented Studio", "amount": 17})
            self.assertEqual(json.loads(path.read_text()),
                             {"company": "Invented Studio", "amount": 17})


class AuthTests(unittest.TestCase):
    def test_accepts_successful_chatgpt_status(self):
        status = SimpleNamespace(returncode=0, stdout=b"Logged in using ChatGPT\n", stderr=b"")
        with mock.patch.object(task, "_run", return_value=status):
            self.assertEqual(task._auth(SYNTHETIC_ROOT / "codex", SYNTHETIC_ROOT / "home", {}),
                             {"auth_method": "chatgpt"})


class CommandTests(unittest.TestCase):
    def test_default_model_does_not_add_override(self):
        argv = task._command(SYNTHETIC_ROOT / "codex", "default", None,
                             SYNTHETIC_ROOT / "work", SYNTHETIC_ROOT / "message")
        self.assertNotIn("-m", argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)


class ParseEventsTests(unittest.TestCase):
    def test_thread_started_yields_thread_id(self):
        thread, errors, events = task._parse_events(
            b'{"type":"thread.started","thread_id":"synthetic-thread"}\n')
        self.assertEqual(thread, "synthetic-thread")
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 1)


class CacheArtifactTests(unittest.TestCase):
    def test_generated_bytecode_is_cache(self):
        self.assertTrue(task._is_cache_artifact("__pycache__/example.cpython-311.pyc"))


class CachePreventionEnvironmentTests(unittest.TestCase):
    def test_generation_and_verification_cache_prevention_is_deterministic(self):
        environment = task._env()
        self.assertEqual({name: environment[name] for name in task.CACHE_PREVENTION_ENV},
                         task.CACHE_PREVENTION_ENV)

    def test_verification_keeps_pytest_cache_plugin_available(self):
        environment = task._verification_env(task._env())
        self.assertNotIn("PYTEST_ADDOPTS", environment)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")


class UnlinkBeneathTests(unittest.TestCase):
    def test_unlinks_regular_file_only(self):
        if os.open not in os.supports_dir_fd or os.lstat not in os.supports_dir_fd:
            self.skipTest("directory handles unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "example.pyc"
            path.write_bytes(b"synthetic")
            task._unlink_beneath(root, "example.pyc")
            self.assertFalse(path.exists())


class DropCachesTests(unittest.TestCase):
    def test_removes_only_reported_cache_artifact(self):
        if os.unlink not in os.supports_dir_fd or os.open not in os.supports_dir_fd \
                or os.lstat not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("directory handles unavailable")
        result = SimpleNamespace(returncode=0, stdout=b"__pycache__/example.pyc\0")
        with mock.patch.object(task, "_run", return_value=result), \
             mock.patch.object(task, "_unlink_beneath") as unlink:
            task._drop_caches(SYNTHETIC_ROOT / "tree", {})
        unlink.assert_called_once_with(SYNTHETIC_ROOT / "tree", "__pycache__/example.pyc")


class PatchTests(unittest.TestCase):
    def test_returns_nonempty_diff(self):
        ignored = SimpleNamespace(returncode=0, stdout=b"")
        add = SimpleNamespace(returncode=0, stdout=b"")
        diff = SimpleNamespace(returncode=0, stdout=b"synthetic diff")
        with mock.patch.object(task, "_drop_caches"), \
             mock.patch.object(task, "_run", side_effect=[ignored, add, diff]):
            self.assertEqual(task._patch(SYNTHETIC_ROOT / "tree", "HEAD", {}), b"synthetic diff")

    def test_unexpected_ignored_file_refuses_with_bounded_relative_path(self):
        ignored = b"generated/unexpected.cache\0"
        listing = SimpleNamespace(returncode=0, stdout=ignored)
        with mock.patch.object(task, "_drop_caches"), \
             mock.patch.object(task, "_run", return_value=listing):
            with self.assertRaises(task.TaskError) as caught:
                task._patch(SYNTHETIC_ROOT / "tree", "HEAD", {})
        self.assertEqual(caught.exception.ignored_artifact_paths, ["generated/unexpected.cache"])

    def test_ignored_path_list_is_bounded(self):
        entries = [f"cache-{number}".encode() for number in range(task.MAX_IGNORED_ARTIFACT_PATHS + 1)]
        listing = SimpleNamespace(returncode=0, stdout=b"\0".join(entries) + b"\0")
        with mock.patch.object(task, "_run", return_value=listing):
            paths = task._ignored_artifact_paths(SYNTHETIC_ROOT / "tree", {})
        self.assertEqual(len(paths), task.MAX_IGNORED_ARTIFACT_PATHS)

    def test_ignored_path_diagnostic_escapes_control_characters(self):
        listing = SimpleNamespace(returncode=0, stdout=b"unsafe\nname\0")
        with mock.patch.object(task, "_run", return_value=listing):
            paths = task._ignored_artifact_paths(SYNTHETIC_ROOT / "tree", {})
        self.assertEqual(paths, ["unsafe\\u000aname"])


class PinnedPythonTests(unittest.TestCase):
    def test_pins_python3_verification(self):
        self.assertEqual(task._pinned_python(["python3", "-m", "unittest"]),
                         [sys.executable, "-m", "unittest"])


class SandboxedTests(unittest.TestCase):
    def test_non_linux_backend_selects_sandbox_exec(self):
        backend = SimpleNamespace(name="synthetic-sandbox")
        result = object()
        with mock.patch.object(task, "_sandbox_exec_confined", return_value=result) as sandbox, \
             mock.patch.object(task, "_netns_confined") as netns:
            self.assertIs(task._sandboxed(["git", "status"], SYNTHETIC_ROOT / "tree",
                                         SYNTHETIC_ROOT / "scratch", {}, 2, backend), result)
        sandbox.assert_called_once()
        netns.assert_not_called()


class NetnsConfinedTests(unittest.TestCase):
    def test_successful_canary_check_returns_verification_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = SimpleNamespace(returncode=0)
            backend = SimpleNamespace(name="synthetic-netns")
            with mock.patch.object(task, "_run", return_value=result), \
                 mock.patch.object(task.hostenv, "confined_argv", return_value=["helper"]), \
                 mock.patch.object(task, "_confinement_canaries", return_value=[]):
                returned = task._netns_confined([sys.executable, "-m", "unittest"],
                                               root, root / "scratch", {}, 2, backend)
            self.assertIs(returned, result)
            self.assertEqual(result.sandbox_backend, "synthetic-netns")


class ConfinementCanariesTests(unittest.TestCase):
    def test_linked_worktree_adds_common_repository_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "worktree"; tree.mkdir()
            gitdir = root / "metadata" / "worktree"; gitdir.mkdir(parents=True)
            common = root / "metadata" / "common"; common.mkdir()
            (gitdir / "commondir").write_text("../common\n")
            (tree / ".git").write_text(f"gitdir: {gitdir}\n")
            self.assertIn(str(common.parent.resolve()), task._confinement_canaries(tree))


class SandboxExecConfinedTests(unittest.TestCase):
    def test_git_verification_uses_pinned_git_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "tree"; tree.mkdir()
            scratch = root / "scratch"
            backend = SimpleNamespace(name="synthetic-sandbox")
            result = SimpleNamespace(returncode=0)
            with mock.patch.object(task, "_git_bin", return_value=SYNTHETIC_ROOT / "git"), \
                 mock.patch.object(task, "_git_argv", return_value=["git-pinned", "status"]), \
                 mock.patch.object(task, "_run", return_value=result) as run:
                task._sandbox_exec_confined(["git", "status"], tree, scratch, {}, 2, backend)
            self.assertEqual(run.call_args.args[0][-2:], ["git-pinned", "status"])


class AncestorContaminationTests(unittest.TestCase):
    def test_checks_ancestor_instructions(self):
        job = SYNTHETIC_ROOT / "job"
        with mock.patch.object(task.preflight, "assert_workspace_clean") as check:
            task._assert_no_ancestor_contamination(job)
        check.assert_called_once_with(str(job))


class RunAdmittedTests(unittest.TestCase):
    def test_synthetic_classification_reaches_path_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(task.TaskError, "repo must be a primary git checkout"):
                task._run_admitted(brief=root / "brief.md", repo=root / "missing",
                                   task_root=root / "tasks", codex_bin=root / "codex",
                                   codex_home=root / "home", admission=object(),
                                   classification="synthetic")
