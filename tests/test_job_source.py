"""Focused tests for the private, pinned POSIX job-source library."""
from __future__ import annotations

import copy
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge.execution import job_source


_GIT_HOME: Path | None = None


def git_env() -> dict[str, str]:
    assert _GIT_HOME is not None
    return {
        **os.environ,
        "HOME": str(_GIT_HOME),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Job Source Test",
        "GIT_AUTHOR_EMAIL": "job-source@example.invalid",
        "GIT_COMMITTER_NAME": "Job Source Test",
        "GIT_COMMITTER_EMAIL": "job-source@example.invalid",
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, shell=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=git_env()).stdout.strip()


def source_git(source: Path, *args: str) -> str:
    return subprocess.run(["git", "--git-dir", str(source), *args], check=True,
                          shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=git_env()).stdout.strip()


def operator_state(repo: Path) -> dict[str, object]:
    status = git(repo, "--no-optional-locks", "status", "--porcelain=v2",
                 "--untracked-files=all")
    git_dir = Path(git(repo, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    files: dict[str, str] = {}
    for path in sorted(git_dir.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(git_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "files": files,
        "status": status,
    }


@unittest.skipIf(os.name == "nt", "POSIX-only job-source tests")
class JobSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        global _GIT_HOME
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git_home = self.root / "git-home"
        self.git_home.mkdir(mode=0o700)
        _GIT_HOME = self.git_home
        self.repo = self.root / "operator"
        self.repo.mkdir()
        git(self.repo, "init", "--template=/dev/null")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-m", "base")
        self.jobs = self.root / "jobs"
        self.jobs.mkdir(mode=0o700)
        os.chmod(self.jobs, 0o700)

    def tearDown(self) -> None:
        global _GIT_HOME
        _GIT_HOME = None
        self.temp.cleanup()

    def job_root(self, name: str = "job") -> Path:
        root = self.jobs / name
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        return root

    def commit_unpushed(self, name: str = "unpushed.txt") -> str:
        (self.repo / name).write_text("private commit\n", encoding="utf-8")
        git(self.repo, "add", name)
        git(self.repo, "commit", "-m", "unpushed")
        return git(self.repo, "rev-parse", "HEAD")

    def test_unpushed_branch_is_pinned_and_operator_is_unchanged(self) -> None:
        commit = self.commit_unpushed()
        before = operator_state(self.repo)
        root = self.job_root()

        manifest = job_source.prepare_job_source(self.repo, "HEAD", root, limits=None)

        self.assertEqual(manifest["commit"], commit)
        self.assertEqual(manifest["base_spelling"], "HEAD")
        self.assertEqual(manifest["tree"], git(self.repo, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(manifest["object_format"], git(self.repo, "rev-parse", "--show-object-format=storage"))
        self.assertEqual(manifest["manifest_sha256"], hashlib.sha256(
            job_source.canonical_manifest_bytes(manifest)).hexdigest())
        self.assertEqual(operator_state(self.repo), before)
        self.assertEqual(job_source.verify_job_source(root, manifest), [])

        source = root / job_source.JOB_SOURCE_DIRNAME
        self.assertEqual(source_git(source, "rev-parse", job_source.BASE_REF), commit)
        self.assertEqual(source_git(source, "for-each-ref", "--format=%(refname)"),
                         job_source.BASE_REF)
        source_git(source, "fsck", "--connectivity-only", "--no-dangling", commit)
        config = (source / "config").read_text(encoding="utf-8")
        self.assertNotIn("remote", config)
        self.assertNotIn("alternate", config)
        self.assertIn("hooksPath", config)
        self.assertTrue((source / "hooks").is_dir())
        self.assertEqual(list((source / "hooks").iterdir()), [])
        self.assertFalse((source / "objects" / "info" / "alternates").exists())
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o700)
        self.assertEqual(manifest["config_sha256"], hashlib.sha256(
            (source / "config").read_bytes()).hexdigest())

    def test_detached_head_commit_is_pinned(self) -> None:
        git(self.repo, "checkout", "--detach")
        commit = self.commit_unpushed("detached.txt")

        manifest = job_source.prepare_job_source(self.repo, "HEAD", self.job_root(), limits=None)

        self.assertEqual(manifest["commit"], commit)
        self.assertEqual(job_source.verify_job_source(self.jobs / "job", manifest), [])

    def test_missing_object_is_refused(self) -> None:
        self.commit_unpushed()
        blob = git(self.repo, "rev-parse", "HEAD:unpushed.txt")
        git_dir = self.repo / ".git"
        object_path = git_dir / "objects" / blob[:2] / blob[2:]
        self.assertTrue(object_path.exists(), "fixture expects a loose blob")
        object_path.unlink()

        with self.assertRaisesRegex(job_source.JobSourceError, "source_object_missing"):
            job_source.prepare_job_source(self.repo, "HEAD", self.job_root(), limits=None)

    def test_limits_are_refused(self) -> None:
        self.commit_unpushed()
        with self.assertRaisesRegex(job_source.JobSourceError, "source_file_count_exceeded"):
            job_source.prepare_job_source(self.repo, "HEAD", self.job_root(),
                                          limits={"max_files": 0})

        with self.assertRaisesRegex(job_source.JobSourceError, "source_pack_exceeds_byte_limit"):
            job_source.prepare_job_source(self.repo, "HEAD", self.job_root("small-pack"),
                                          limits={"max_pack_bytes": 1})

        with self.assertRaisesRegex(job_source.JobSourceError, "source_file_size_exceeded"):
            job_source.prepare_job_source(self.repo, "HEAD", self.job_root("small-file"),
                                          limits={"max_file_bytes": 1})

    def test_raised_preparation_limits_do_not_affect_verification(self) -> None:
        large_tree = self.repo / "raised-limit"
        large_tree.mkdir()
        for number in range(job_source.MAX_SNAPSHOT_FILES + 1):
            (large_tree / f"{number:04d}").touch()
        git(self.repo, "add", "raised-limit")
        git(self.repo, "commit", "-m", "large tree")
        manifest = job_source.prepare_job_source(
            self.repo, "HEAD", self.job_root(),
            limits={"max_files": job_source.MAX_SNAPSHOT_FILES + 2},
        )
        self.assertEqual(job_source.verify_job_source(self.jobs / "job", manifest), [])

    def test_symlink_and_non_private_job_roots_are_refused(self) -> None:
        real = self.job_root("real")
        alias = self.jobs / "alias"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(job_source.JobSourceError, "job_source_root_invalid"):
            job_source.prepare_job_source(self.repo, "HEAD", alias, limits=None)

        public = self.jobs / "public"
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        with self.assertRaisesRegex(job_source.JobSourceError, "job_source_root_not_owner_only"):
            job_source.prepare_job_source(self.repo, "HEAD", public, limits=None)

    def test_verify_detects_ref_config_and_manifest_tampering(self) -> None:
        root = self.job_root()
        manifest = job_source.prepare_job_source(self.repo, "HEAD", root, limits=None)
        source = root / job_source.JOB_SOURCE_DIRNAME

        source_git(source, "update-ref", "-d", job_source.BASE_REF)
        reasons = job_source.verify_job_source(root, manifest)
        self.assertIn("base_ref_changed", reasons)

        # Make a fresh source for independent config and manifest checks.
        job_source.remove_job_source(root)
        manifest = job_source.prepare_job_source(self.repo, "HEAD", root, limits=None)
        source = root / job_source.JOB_SOURCE_DIRNAME
        with (source / "config").open("a", encoding="utf-8") as handle:
            handle.write("\n[adversarial]\n\tvalue = true\n")
        self.assertIn("config_changed", job_source.verify_job_source(root, manifest))

        tampered = copy.deepcopy(manifest)
        tampered["entries"][0]["path"] = "not-the-tracked-path"
        self.assertIn("manifest_mismatch", job_source.verify_job_source(root, tampered))

    def test_removal_is_contained_to_the_fixed_source_child(self) -> None:
        root = self.job_root()
        manifest = job_source.prepare_job_source(self.repo, "HEAD", root, limits=None)
        self.assertEqual(job_source.verify_job_source(root, manifest), [])
        keeper = root / "keep-me"
        keeper.write_text("not source data", encoding="utf-8")

        job_source.remove_job_source(root)

        self.assertTrue(root.is_dir())
        self.assertTrue(keeper.exists())
        self.assertFalse((root / job_source.JOB_SOURCE_DIRNAME).exists())


@unittest.skipUnless(os.name == "nt", "Windows-only platform guard test")
class WindowsJobSourceTest(unittest.TestCase):
    def test_public_operations_are_unsupported(self) -> None:
        manifest = {"not": "used"}
        for operation, args in (
            (job_source.prepare_job_source, ("operator", "HEAD", "job")),
            (job_source.verify_job_source, ("job", manifest)),
            (job_source.remove_job_source, ("job",)),
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(job_source.JobSourceError,
                                            "job_source_platform_unsupported"):
                    operation(*args)


if __name__ == "__main__":
    unittest.main()
