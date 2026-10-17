"""POSIX queue admission binds execution jobs to private Git sources."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.execution import job_source
from agent_bridge.orchestration import execution_queue as eq


def git_env() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Admission Test",
            "GIT_AUTHOR_EMAIL": "admission@example.invalid",
            "GIT_COMMITTER_NAME": "Admission Test",
            "GIT_COMMITTER_EMAIL": "admission@example.invalid"}

def recording_popen(stdout: bytes) -> mock.Mock:
    """Record the harness argv and kwargs, then run a real child that prints
    ``stdout``, so the executor's bounded capture reads a real pipe."""
    real = subprocess.Popen

    def launch(argv, **kwargs):
        return real([sys.executable, "-c",
                     "import sys; sys.stdout.buffer.write(%r)" % stdout], **kwargs)
    return mock.Mock(side_effect=launch)



def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, env=git_env(), shell=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


def source_git(source: Path, *args: str) -> str:
    return subprocess.run(["git", "--git-dir", str(source), *args], check=True, shell=False,
                          env=git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


def operator_state(repo: Path) -> dict[str, object]:
    status = git(repo, "--no-optional-locks", "status", "--porcelain=v2", "--untracked-files=all")
    git_dir = repo / ".git"
    return {"status": status, "files": {
        str(path.relative_to(git_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(git_dir.rglob("*")) if path.is_file()}}


class FakeExecutor:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object], _directory: Path) -> dict[str, object]:
        self.requests.append(request)
        return {"returncode": 0, "harness_ok": True, "harness_status": "complete",
                "harness_verdict": "read"}


@unittest.skipUnless(os.name == "posix", "job-source admission is POSIX-only")
class ExecutionJobSourceAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "operator"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "--quiet", "-m", "base")
        self.base_commit = git(self.repo, "rev-parse", "HEAD")
        self.brief = self.root / "brief.md"
        self.brief.write_text("Implement this change.\n", encoding="utf-8")
        self.executor = FakeExecutor()
        self.queue = eq.ExecutionQueue(self.root / "queue", self.executor,
                                       model_reserved=eq.reserve_nothing)

    def submit(self, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "caller": "claude", "provider": "codex", "repo": str(self.repo),
            "brief": str(self.brief), "base": "HEAD", "classification": "synthetic",
            "model": "test-model", "effort": "low", "item_id": "item-1",
            "stage": "implement", "owner_id": "owner-1", "stage_revision": 1,
        }
        values.update(changes)
        return self.queue.submit(**values)  # type: ignore[arg-type]

    def request(self, job: dict[str, object]) -> dict[str, object]:
        return json.loads((self.queue.root / str(job["job_id"]) / "request.json").read_text())

    def test_head_moves_and_checkout_do_not_change_the_admitted_base(self) -> None:
        job = self.submit()
        request = self.request(job)
        (self.repo / "base.txt").write_text("later\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "later")
        git(self.repo, "checkout", "--quiet", "-b", "other-branch")
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "--quiet", "-m", "other")

        self.queue.run_once("worker")

        self.assertEqual(request["base"], "HEAD")
        self.assertEqual(request["base_commit"], self.base_commit)
        self.assertEqual(self.executor.requests[0]["base_commit"], self.base_commit)
        manifest = json.loads((self.queue.root / str(job["job_id"]) /
                               "job-source-manifest.json").read_text())
        self.assertEqual(job_source.verify_job_source(self.queue.root / str(job["job_id"]), manifest), [])

    def test_two_jobs_have_independent_verified_sources(self) -> None:
        first = self.submit(item_id="one")
        second = self.submit(item_id="two")
        first_dir = self.queue.root / str(first["job_id"])
        second_dir = self.queue.root / str(second["job_id"])
        self.assertNotEqual(first_dir, second_dir)
        for directory in (first_dir, second_dir):
            manifest = json.loads((directory / "job-source-manifest.json").read_text())
            self.assertEqual(job_source.verify_job_source(directory, manifest), [])

    def test_idempotent_retry_after_head_moves_keeps_the_first_source(self) -> None:
        first = self.submit(idempotency_key="same")
        first_request = self.request(first)
        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "--quiet", "-m", "later")
        retry = self.submit(idempotency_key="same")
        self.assertEqual(retry["job_id"], first["job_id"])
        self.assertEqual(self.request(retry)["base_commit"], first_request["base_commit"])
        self.assertEqual(len(list(self.queue.root.iterdir())), 1)

    def test_refused_source_leaves_no_job_directory(self) -> None:
        blob = git(self.repo, "rev-parse", "HEAD:base.txt")
        object_path = self.repo / ".git" / "objects" / blob[:2] / blob[2:]
        self.assertTrue(object_path.is_file())
        object_path.unlink()
        with self.assertRaisesRegex(eq.ExecutionAdmissionError,
                                    "job_source_refused:source_object_missing"):
            self.submit()
        self.assertEqual(list(self.queue.root.iterdir()), [])

    def test_default_limits_refusal_leaves_no_job_directory(self) -> None:
        # A patched library default reaches admission because submit supplies
        # no limits of its own.
        with mock.patch.object(job_source, "DEFAULT_LIMITS",
                               job_source.JobSourceLimits(max_files=0)):
            with self.assertRaisesRegex(eq.ExecutionAdmissionError,
                                        "job_source_refused:source_file_count_exceeded"):
                self.submit()
        self.assertEqual(list(self.queue.root.iterdir()), [])

    def test_tampered_sources_fail_before_the_executor(self) -> None:
        cases = ("ref", "config", "manifest")
        for kind in cases:
            with self.subTest(kind=kind):
                job = self.submit(item_id=kind)
                directory = self.queue.root / str(job["job_id"])
                if kind == "ref":
                    source_git(directory / job_source.JOB_SOURCE_DIRNAME,
                               "update-ref", "-d", job_source.BASE_REF)
                    expected = "job_source_integrity:base_ref_changed"
                elif kind == "config":
                    with (directory / job_source.JOB_SOURCE_DIRNAME / "config").open("a",
                                                                                         encoding="utf-8") as handle:
                        handle.write("\n[changed]\n\tvalue = true\n")
                    expected = "job_source_integrity:config_changed"
                else:
                    manifest = directory / "job-source-manifest.json"
                    changed = json.loads(manifest.read_text())
                    changed["commit"] = "0" * 40
                    manifest.write_text(json.dumps(changed), encoding="utf-8")
                    expected = "job_source_integrity:manifest_mismatch"
                self.queue.run_once("worker")
                result = self.queue.result(str(job["job_id"]))
                self.assertEqual(result["error_detail"], expected)
        self.assertEqual(self.executor.requests, [])

    def test_restart_retains_sources_for_queued_and_claimed_jobs(self) -> None:
        queued = self.submit(item_id="queued")
        queued_dir = self.queue.root / str(queued["job_id"])
        restarted = eq.ExecutionQueue(self.queue.root, self.executor,
                                      model_reserved=eq.reserve_nothing)
        self.assertTrue((queued_dir / job_source.JOB_SOURCE_DIRNAME).is_dir())
        restarted.run_once("worker")
        self.assertEqual(len(self.executor.requests), 1)

        claimed = self.submit(item_id="claimed")
        claimed_dir = restarted.root / str(claimed["job_id"])
        (claimed_dir / "claim.lock").write_bytes(b"")
        recovered = eq.ExecutionQueue(restarted.root, self.executor,
                                      model_reserved=eq.reserve_nothing)
        self.assertEqual(recovered.result(str(claimed["job_id"]))["state"], "blocked")
        self.assertTrue((claimed_dir / job_source.JOB_SOURCE_DIRNAME).is_dir())
        self.assertIsNone(recovered.run_once("worker"))

    def test_legacy_review_and_non_posix_requests_are_not_pinned(self) -> None:
        with mock.patch.object(eq, "_posix_job_sources_enabled", return_value=False):
            legacy = self.submit(item_id="legacy")
        legacy_request = self.request(legacy)
        self.assertNotIn("base_commit", legacy_request)
        self.queue.run_once("worker")
        self.assertEqual(self.executor.requests[-1]["base"], "HEAD")

        review = self.submit(item_id="review", kind="review", caller="claude", provider="codex",
                             head=self.base_commit, author_provider="local")
        review_dir = self.queue.root / str(review["job_id"])
        self.assertFalse((review_dir / job_source.JOB_SOURCE_DIRNAME).exists())
        self.assertNotIn("base_commit", self.request(review))

    def test_subprocess_harness_uses_pinned_or_legacy_base_as_recorded(self) -> None:
        executor = eq.SubprocessHarnessExecutor(eq.Harnesses(
            codex=self.brief, claude=self.brief, python=Path(sys.executable)))

        request: dict[str, object] = {
            "provider": "codex", "kind": "execution", "brief": str(self.brief),
            "repo": str(self.repo), "base": "HEAD", "base_commit": self.base_commit,
            "timeout_seconds": 1, "verify_argv": [], "classification": "synthetic",
            "model": "test-model", "effort": "low",
        }
        with mock.patch.object(eq.subprocess, "Popen",
                               new=recording_popen(b'{"ok":true,"status":"complete"}\n')) as popen:
            pinned_job = self.root / "pinned-job"
            pinned_job.mkdir(mode=0o700)
            executor(request, pinned_job)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--base") + 1], self.base_commit)

        request.pop("base_commit")
        with mock.patch.object(eq.subprocess, "Popen",
                               new=recording_popen(b'{"ok":true,"status":"complete"}\n')) as popen:
            # Capture files are created exclusively, so each run gets its own job dir.
            legacy_job = self.root / "legacy-job"
            legacy_job.mkdir(mode=0o700)
            executor(request, legacy_job)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--base") + 1], "HEAD")

    def test_orphan_source_is_ignored_and_reported(self) -> None:
        orphan = self.queue.root / "orphan"
        orphan.mkdir(mode=0o700)
        (orphan / job_source.JOB_SOURCE_DIRNAME).mkdir(mode=0o700)
        self.assertIsNone(self.queue.run_once("worker"))
        self.assertEqual(self.queue.state_report(), {"orphan_job_source": 1})
        restarted = eq.ExecutionQueue(self.queue.root, self.executor,
                                      model_reserved=eq.reserve_nothing)
        self.assertEqual(restarted.state_report(), {"orphan_job_source": 1})
        job = self.submit(idempotency_key="after-orphan")
        self.assertNotEqual(job["job_id"], "orphan")

    def test_admission_does_not_change_the_operator_repository(self) -> None:
        before = operator_state(self.repo)
        self.submit()
        self.assertEqual(operator_state(self.repo), before)


if __name__ == "__main__":
    unittest.main()
