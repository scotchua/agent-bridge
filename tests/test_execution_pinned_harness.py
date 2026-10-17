"""Focused POSIX checks for worktrees backed by a queue-pinned source."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.execution import codex_task, claude_task, hostenv, job_source
from agent_bridge.orchestration.execution_queue import Harnesses, SubprocessHarnessExecutor


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Pinned Test",
           "GIT_AUTHOR_EMAIL": "pinned@example.invalid", "GIT_COMMITTER_NAME": "Pinned Test",
           "GIT_COMMITTER_EMAIL": "pinned@example.invalid"}
    return subprocess.run(["git", *args], cwd=repo, env=env, shell=False, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


@unittest.skipUnless(os.name == "posix", "pinned job sources are POSIX-only")
class PinnedHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.operator = self.root / "operator"
        self.operator.mkdir()
        git(self.operator, "init", "--quiet")
        (self.operator / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.operator, "add", "base.txt")
        git(self.operator, "commit", "--quiet", "-m", "base")
        self.job = self.root / "job"
        self.job.mkdir(mode=0o700)
        self.manifest = job_source.prepare_job_source(self.operator, "HEAD", self.job)
        (self.job / "job-source-manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8")
        self.commit = self.manifest["commit"]

    def test_pinned_manifest_refuses_missing_and_mismatched_base(self) -> None:
        for lane in (codex_task, claude_task):
            with self.assertRaisesRegex(lane.TaskError, "job_source_base_mismatch"):
                lane._pinned_source(self.job, "0" * 40)
        (self.job / "job-source-manifest.json").unlink()
        for lane in (codex_task, claude_task):
            with self.assertRaisesRegex(lane.TaskError, "job_source_manifest_missing"):
                lane._pinned_source(self.job, self.commit)

    def test_pinned_worktree_uses_only_the_bare_job_source(self) -> None:
        source = self.job / job_source.JOB_SOURCE_DIRNAME
        tree = self.root / "tree"
        env = codex_task._env()
        codex_task._add_pinned_worktree(source, tree, self.commit, env)
        checks: list[dict] = []
        marker = tree / ".git"
        original_marker = marker.read_bytes()
        codex_task._worktree_checkpoint(tree, source, self.commit, "generation", checks, env,
                                        marker_bytes=original_marker)
        self.assertEqual(checks, [{"checkpoint": "generation", "reasons": []}])
        marker.write_bytes(original_marker + b"\n")
        with self.assertRaisesRegex(codex_task.TaskError, "worktree_marker_changed"):
            codex_task._worktree_checkpoint(tree, source, self.commit, "marker-tampered", checks, env,
                                            marker_bytes=original_marker)
        marker.write_text("gitdir: /nowhere\n", encoding="utf-8")
        with self.assertRaisesRegex(codex_task.TaskError, "worktree_pointer_changed"):
            codex_task._worktree_checkpoint(tree, source, self.commit, "tampered", checks, env,
                                            marker_bytes=original_marker)
        # Remove through the source's Git directory, not the operator checkout.
        marker.write_text("gitdir: " + str(next((source / "worktrees").iterdir())) + "\n",
                          encoding="utf-8")
        self.assertTrue(codex_task._remove(self.operator, tree, env, git_dir=source))

    def test_manifest_tamper_has_a_closed_integrity_reason(self) -> None:
        source = self.job / job_source.JOB_SOURCE_DIRNAME
        with (source / "config").open("a", encoding="utf-8") as handle:
            handle.write("\n[bridge-test]\nvalue = tampered\n")
        checks: list[dict] = []
        with self.assertRaisesRegex(codex_task.TaskError, "config_changed"):
            codex_task._source_checkpoint(self.job, self.manifest, "before_generation_worktree", checks)
        self.assertEqual(checks[0]["reasons"], ["config_changed"])

    def test_manifest_file_tamper_is_checked_again_at_each_checkpoint(self) -> None:
        altered = {**self.manifest, "base_spelling": "changed"}
        (self.job / "job-source-manifest.json").write_text(json.dumps(altered), encoding="utf-8")
        checks: list[dict] = []
        with self.assertRaisesRegex(claude_task.TaskError, "manifest_mismatch"):
            claude_task._source_checkpoint(self.job, self.manifest, "before_generation_worktree", checks)
        self.assertEqual(checks, [{"checkpoint": "before_generation_worktree",
                                   "reasons": ["manifest_mismatch"]}])

    def test_executor_uses_the_job_directory_only_for_pinned_requests(self) -> None:
        harness = self.root / "harness.py"
        harness.write_text("# test harness\n", encoding="utf-8")
        executor = SubprocessHarnessExecutor(Harnesses(
            codex=harness, claude=harness, python=Path(sys.executable)))

        class Process:
            returncode = 0

            def communicate(self, *, timeout: int):
                return b'{"status":"complete"}\n', b""

        request = {"provider": "codex", "brief": str(self.root / "brief"),
                   "repo": str(self.operator), "base_commit": self.commit,
                   "classification": "synthetic", "model": "test", "effort": "low",
                   "timeout_seconds": 1, "verify_argv": []}
        with mock.patch("agent_bridge.orchestration.execution_queue.subprocess.Popen",
                        return_value=Process()) as popen:
            executor(request, self.job)
        argv = popen.call_args.args[0]
        self.assertIn("--job-source", argv)
        self.assertEqual(argv[argv.index("--job-source") + 1], str(self.job))
        self.assertEqual(popen.call_args.kwargs["cwd"], self.job)

        request.pop("base_commit")
        request["base"] = "HEAD"
        with mock.patch("agent_bridge.orchestration.execution_queue.subprocess.Popen",
                        return_value=Process()) as legacy_popen:
            executor(request, self.job)
        self.assertNotIn("--job-source", legacy_popen.call_args.args[0])
        self.assertEqual(legacy_popen.call_args.kwargs["cwd"], str(self.operator))


@unittest.skipUnless(os.name == "posix", "pinned job sources are POSIX-only")
class PinnedHarnessEndToEndTests(unittest.TestCase):
    """Proofs through the real lane entry points, not only their helpers."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Make a small disposable change.\n", encoding="utf-8")

    def _require_confinement(self) -> None:
        try:
            backend = hostenv.confinement("synthetic")
        except hostenv.HostCapabilityError as exc:
            self.skipTest("no verification confinement backend on this host: " + exc.code)
        # Some CI sandboxes expose sandbox-exec but deny sandbox_apply itself.
        # That is not a usable confinement backend, and matches the existing
        # lane tests' convention of skipping for a missing host capability.
        if backend.name == hostenv.MACOS_SANDBOX_EXEC:
            probe = subprocess.run([hostenv.SANDBOX_EXEC, "-p", "(version 1) (allow default)", "/usr/bin/true"],
                                   shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if probe.returncode:
                self.skipTest("sandbox-exec is unavailable to this test process")

    def _operator(self, name: str, *, read_tests: bool = False) -> Path:
        repo = self.root / name
        repo.mkdir()
        git(repo, "init", "--quiet")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        if read_tests:
            (repo / "test_read_worktree.py").write_text(
                "import unittest\n"
                "class ReadsOwnWorktree(unittest.TestCase):\n"
                "    def test_read(self):\n"
                "        self.assertEqual(open('base.txt', encoding='utf-8').read(), 'base\\n')\n",
                encoding="utf-8")
            (repo / "test_read_operator.py").write_text(
                "import unittest\n"
                f"OPERATOR = {str(repo)!r}\n"
                "class ReadsOperator(unittest.TestCase):\n"
                "    def test_read(self):\n"
                "        open(OPERATOR + '/base.txt', encoding='utf-8').read()\n",
                encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "--quiet", "-m", "base")
        return repo

    def _source(self, operator: Path, name: str) -> tuple[Path, dict]:
        root = self.root / name
        root.mkdir(mode=0o700)
        manifest = job_source.prepare_job_source(operator, "HEAD", root)
        (root / "job-source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root, manifest

    def _fake(self, lane, name: str) -> Path:
        """Copy the standard execution-lane fake used by the queue fixtures."""
        fake = self.root / name
        source = ROOT / "tests" / "fakes" / (
            "fake_exec_claude.py" if lane is claude_task else "fake_exec_codex.py")
        shutil.copy(source, fake)
        fake.chmod(0o755)
        return fake

    def _kwargs(self, lane, operator: Path, source: Path, fake: Path, task_name: str) -> dict:
        home = self.root / f"{task_name}-home"
        home.mkdir(mode=0o700)
        (home / "fake-exec.json").write_text(
            json.dumps({"mode": "ok", "write": {"generated.txt": "generated\n"}}), encoding="utf-8")
        values = dict(brief=self.brief, repo=operator, task_root=self.root / task_name,
                      classification="synthetic", verify_argv=[["git", "status"]],
                      job_source=source,
                      base=json.loads((source / "job-source-manifest.json").read_text(encoding="utf-8"))["commit"],
                      _fixture_home=home)
        if lane is claude_task:
            store = home / ".agent-bridge" / "claude-home"
            store.mkdir(mode=0o700, parents=True)
            values.update(claude_bin=fake, claude_config_dir=store, model="fake", effort="low")
        else:
            values.update(codex_bin=fake, codex_home=self.root / f"{task_name}-codex-home",
                          model="fake", reasoning_effort="low",
                          admission=mock.Mock(check_reported_version=lambda _: None,
                                              receipt=lambda: {}))
        return values

    def _run(self, lane, operator: Path, source: Path, fake: Path, task_name: str, *, mutate=None,
             verify_argv=None, pinned: bool = True):
        kwargs = self._kwargs(lane, operator, source, fake, task_name)
        fixture_home = kwargs.pop("_fixture_home")
        if verify_argv is not None:
            kwargs["verify_argv"] = verify_argv
        if not pinned:
            kwargs.pop("job_source")
        original = lane._run
        changed = False

        def wrapped(argv, *, cwd, env, timeout, input_bytes=None):
            nonlocal changed
            result = original(argv, cwd=cwd, env=env, timeout=timeout, input_bytes=input_bytes)
            if (mutate is not None and not changed and Path(cwd).name == "generation-worktree"
                    and str(fake) in [str(part) for part in argv]):
                changed = True
                mutate()
            return result

        home_patch = mock.patch.dict(os.environ, {
            "HOME": str(fixture_home), "USERPROFILE": str(fixture_home),
        })
        with home_patch, mock.patch.object(lane, "_run", wrapped if mutate is not None else original):
            if lane is claude_task:
                return lane.run_task(**kwargs)
            return lane._run_admitted(**kwargs)

    def _receipt(self, task_name: str) -> tuple[Path, dict]:
        path = next((self.root / task_name).glob("*/receipt.json"))
        return path, json.loads(path.read_text(encoding="utf-8"))

    def _mutate_operator(self, operator: Path, remote: Path) -> None:
        git(operator, "checkout", "--quiet", "-b", "operator-side")
        (operator / "operator-commit.txt").write_text("operator change\n", encoding="utf-8")
        git(operator, "add", "operator-commit.txt")
        git(operator, "commit", "--quiet", "-m", "operator change")
        (operator / "operator-untracked.txt").write_text("untracked\n", encoding="utf-8")
        git(remote.parent, "init", "--bare", "--quiet", str(remote))
        git(operator, "remote", "add", "temporary-proof", str(remote))
        git(operator, "push", "--quiet", "temporary-proof", "HEAD")

    def _move_base_ref(self, source: Path, manifest: dict) -> None:
        bare = source / job_source.JOB_SOURCE_DIRNAME
        tree = git(bare, "--git-dir", str(bare), "rev-parse", f"{manifest['commit']}^{{tree}}")
        moved = git(bare, "--git-dir", str(bare), "-c", "user.name=Pinned Test",
                    "-c", "user.email=pinned@example.invalid", "commit-tree", tree, "-m", "moved")
        git(bare, "--git-dir", str(bare), "update-ref", job_source.BASE_REF, moved)

    def _append_source_config(self, source: Path, manifest: dict) -> None:
        del manifest
        with (source / job_source.JOB_SOURCE_DIRNAME / "config").open("a", encoding="utf-8") as handle:
            handle.write("\n[proof]\nvalue = changed\n")

    def _apply_check(self, source: Path, commit: str, patch: Path, target: Path) -> None:
        subprocess.run(["git", "clone", "--quiet", str(source / job_source.JOB_SOURCE_DIRNAME), str(target)],
                       check=True, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "checkout", "--quiet", "--detach", commit], cwd=target, check=True,
                       shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=target, check=True,
                       shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_pinned_run_allows_operator_changes_and_records_closed_receipt(self) -> None:
        self._require_confinement()
        expected = ["before_generation_worktree", "generation_worktree_before_patch",
                    "before_verification_worktree", "verification_worktree_after_verification",
                    "after_verification", "after_cleanup"]
        for lane in (claude_task, codex_task):
            with self.subTest(lane=lane.__name__):
                operator = self._operator(lane.__name__)
                source, manifest = self._source(operator, lane.__name__ + "-source")
                fake = self._fake(lane, lane.__name__ + "-fake")
                task_name = lane.__name__ + "-tasks"
                result = self._run(lane, operator, source, fake, task_name,
                                   mutate=lambda: self._mutate_operator(operator, self.root / (lane.__name__ + "-remote.git")))
                self.assertEqual(result["status"], "complete")
                self._apply_check(source, manifest["commit"], Path(result["patch"]),
                                  self.root / (lane.__name__ + "-apply"))
                worktrees = git(operator, "worktree", "list", "--porcelain")
                self.assertEqual([line for line in worktrees.splitlines() if line.startswith("worktree ")],
                                 ["worktree " + str(operator)])
                registered = operator / ".git" / "worktrees"
                self.assertFalse(registered.exists() and any(registered.iterdir()))
                _, receipt = self._receipt(task_name)
                self.assertEqual(receipt["source_mode"], "pinned_job_source")
                self.assertEqual(receipt["job_source_manifest_sha256"], manifest["manifest_sha256"])
                self.assertEqual([item["checkpoint"] for item in receipt["job_source_checks"]], expected)
                self.assertTrue(all(not item["reasons"] for item in receipt["job_source_checks"]))
                self.assertTrue(receipt["source_integrity_match"])
                self.assertNotIn("source_before", receipt)
                self.assertNotIn("source_after", receipt)

    def test_pinned_source_tamper_stops_before_verification(self) -> None:
        self._require_confinement()
        for lane in (claude_task, codex_task):
            for reason, tamper in (("config_changed", self._append_source_config),
                                   ("base_ref_changed", self._move_base_ref)):
                with self.subTest(lane=lane.__name__, reason=reason):
                    label = lane.__name__ + "-" + reason
                    operator = self._operator(label + "-tamper")
                    source, manifest = self._source(operator, label + "-tamper-source")
                    fake = self._fake(lane, label + "-tamper-fake")
                    task_name = label + "-tamper-tasks"
                    def mutate(source=source, manifest=manifest, tamper=tamper):
                        tamper(source, manifest)
                    with self.assertRaisesRegex(lane.TaskError, reason):
                        self._run(lane, operator, source, fake, task_name, mutate=mutate)
                    job, receipt = self._receipt(task_name)
                    failed = next(item for item in receipt["job_source_checks"]
                                  if item["checkpoint"] == "before_verification_worktree")
                    self.assertIn(reason, failed["reasons"])
                    self.assertIn(reason, receipt["source_integrity_reasons"])
                    self.assertFalse((job.parent / "verify-1.stdout").exists())

    def test_pinned_generation_worktree_tamper_is_closed(self) -> None:
        self._require_confinement()
        for lane in (claude_task, codex_task):
            for mode, reason in (("head", "worktree_head_changed"),
                                 ("pointer", "worktree_pointer_changed")):
                with self.subTest(lane=lane.__name__, mode=mode):
                    label = lane.__name__ + "-" + mode
                    operator = self._operator(label + "-worktree")
                    source, _ = self._source(operator, label + "-worktree-source")
                    fake = self._fake(lane, label + "-worktree-fake")
                    task_name = label + "-worktree-tasks"
                    def tamper(mode=mode):
                        job = next((self.root / task_name).iterdir())
                        tree = job / "generation-worktree"
                        if mode == "head":
                            git(tree, "-c", "user.name=Pinned Test", "-c", "user.email=pinned@example.invalid",
                                "commit", "--allow-empty", "--quiet", "-m", "tampered")
                        else:
                            (tree / ".git").write_text("gitdir: /not-the-pinned-source\n", encoding="utf-8")
                    with self.assertRaisesRegex(lane.TaskError, reason):
                        self._run(lane, operator, source, fake, task_name, mutate=tamper)
                    _, receipt = self._receipt(task_name)
                    check = next(item for item in receipt["job_source_checks"]
                                 if item["checkpoint"] == "generation_worktree_before_patch")
                    self.assertIn(reason, check["reasons"])

    def test_pinned_cleanup_refusal_is_a_harness_failure(self) -> None:
        self._require_confinement()
        for lane in (claude_task, codex_task):
            with self.subTest(lane=lane.__name__):
                operator = self._operator(lane.__name__ + "-cleanup")
                source, _ = self._source(operator, lane.__name__ + "-cleanup-source")
                fake = self._fake(lane, lane.__name__ + "-cleanup-fake")
                task_name = lane.__name__ + "-cleanup-tasks"
                original = lane._remove
                def refuse(repo, tree, env, timeout=60, *, git_dir=None):
                    if git_dir is not None:
                        return False
                    return original(repo, tree, env, timeout, git_dir=git_dir)
                with mock.patch.object(lane, "_remove", refuse), self.assertRaisesRegex(lane.TaskError, "cleanup failed"):
                    self._run(lane, operator, source, fake, task_name)
                _, receipt = self._receipt(task_name)
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["error"], "WorktreeCleanupError")

    def test_legacy_receipt_keeps_operator_snapshot_shape(self) -> None:
        self._require_confinement()
        for lane in (claude_task, codex_task):
            with self.subTest(lane=lane.__name__):
                operator = self._operator(lane.__name__ + "-legacy")
                source, _ = self._source(operator, lane.__name__ + "-legacy-source")
                fake = self._fake(lane, lane.__name__ + "-legacy-fake")
                task_name = lane.__name__ + "-legacy-tasks"
                result = self._run(lane, operator, source, fake, task_name, pinned=False)
                self.assertEqual(result["source_mode"], "operator_checkout")
                _, receipt = self._receipt(task_name)
                self.assertIn("source_before", receipt)
                self.assertIn("source_after", receipt)

    def test_macos_pinned_verification_cannot_read_operator_checkout(self) -> None:
        self._require_confinement()
        backend = hostenv.confinement("synthetic")
        if backend.name != hostenv.MACOS_SANDBOX_EXEC:
            self.skipTest("sandbox-exec read confinement is unavailable")
        operator = self._operator("read-operator", read_tests=True)
        for module, command, expected in (
                ("test_read_operator", [["python3", "-m", "unittest", "test_read_operator"]], False),
                ("test_read_worktree", [["python3", "-m", "unittest", "test_read_worktree"]], True)):
            with self.subTest(module=module):
                source, _ = self._source(operator, module + "-source")
                fake = self._fake(claude_task, module + "-fake")
                task_name = module + "-tasks"
                result = self._run(claude_task, operator, source, fake, task_name, verify_argv=command)
                self.assertEqual(result["status"] == "complete", expected)
