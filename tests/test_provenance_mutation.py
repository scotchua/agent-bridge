"""Offline behavior checks for implementation provenance."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_bridge import provenance  # noqa: E402


def expected_hash(files: dict[str, bytes],
                  order: list[str] | None = None) -> tuple[str, int]:
    entries = [f"{name}\0{hashlib.sha256(content).hexdigest()}"
               for name in (order if order is not None else sorted(files))
               for content in (files[name],)]
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest(), len(entries)


class GitProvenanceTests(unittest.TestCase):
    def test_git_returns_trimmed_stdout_with_safe_invocation(self):
        result = mock.Mock(returncode=0, stdout=b"invented-commit\n")
        with mock.patch.object(provenance.subprocess, "run", return_value=result) as run:
            self.assertEqual(provenance._git("/synthetic/repo", "rev-parse", "HEAD"),
                             "invented-commit")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(kwargs["cwd"], "/synthetic/repo")
        self.assertTrue(kwargs["capture_output"])
        self.assertFalse(kwargs["shell"])

    def test_git_failure_and_process_errors_are_unknown(self):
        with mock.patch.object(provenance.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout=b"failure")):
            self.assertIsNone(provenance._git("/synthetic/repo", "status"))
        for error in (OSError("missing git"), provenance.subprocess.TimeoutExpired("git", 1)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(provenance.subprocess, "run", side_effect=error):
                self.assertIsNone(provenance._git("/synthetic/repo", "status"))


class SourceHashTests(unittest.TestCase):
    def test_hashes_walk_order_relative_paths_and_allowed_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "zeta/b.py": b"invented python\n",
                "alpha/c.sh": b"synthetic shell\n",
                "alpha/a.json": b'{"company":"Fictional Juniper"}\n',
                "settings.toml": b"[invented]\namount = 17\n",
                "notes.md": b"synthetic notes\n",
            }
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for name in (".git", "__pycache__", ".pytest_cache", "runtime", "node_modules"):
                path = root / name / "skip.py"
                path.parent.mkdir()
                path.write_bytes(b"excluded")
            (root / "ignored.txt").write_bytes(b"excluded")
            walk_order = ["notes.md", "settings.toml", "alpha/a.json",
                          "alpha/c.sh", "zeta/b.py"]
            self.assertEqual(provenance.source_hash(str(root)),
                             expected_hash(files, walk_order))

    @unittest.expectedFailure
    def test_hash_uses_global_relative_path_order(self):
        # The documented global sort is absent: root files precede nested files.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "z.py").write_bytes(b"synthetic root")
            (root / "alpha").mkdir()
            (root / "alpha" / "a.py").write_bytes(b"synthetic nested")
            files = {"alpha/a.py": b"synthetic nested", "z.py": b"synthetic root"}
            self.assertEqual(provenance.source_hash(str(root)), expected_hash(files))

    def test_skips_unreadable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "readable.py").write_bytes(b"synthetic readable")
            (root / "unreadable.py").write_bytes(b"synthetic unreadable")
            real_open = open

            def selective_open(path, *args, **kwargs):
                if os.fspath(path) == str(root / "unreadable.py"):
                    raise OSError("synthetic read failure")
                return real_open(path, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=selective_open):
                actual = provenance.source_hash(str(root))
            self.assertEqual(actual, expected_hash({"readable.py": b"synthetic readable"}))

    def test_content_changes_hash_and_empty_tree_has_zero_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(provenance.source_hash(str(root)), expected_hash({}))
            path = root / "account.py"
            path.write_bytes(b"balance = 17\n")
            first = provenance.source_hash(str(root))
            self.assertEqual(first, expected_hash({"account.py": b"balance = 17\n"}))
            path.write_bytes(b"balance = 18\n")
            second = provenance.source_hash(str(root))
            self.assertEqual(second, expected_hash({"account.py": b"balance = 18\n"}))
            self.assertNotEqual(first[0], second[0])


class CollectProvenanceTests(unittest.TestCase):
    def test_collect_reports_clean_and_dirty_state_exactly(self):
        tree_hash = "a" * 64
        for status, dirty, count in (("", False, 0),
                                     ("  \n", False, 0),
                                     (" M alpha.py\n\n?? beta.json\n", True, 2)):
            with self.subTest(status=status), \
                 mock.patch.object(provenance, "_git", side_effect=["invented-commit", status]) as git, \
                 mock.patch.object(provenance, "source_hash", return_value=(tree_hash, 3)) as source_hash:
                self.assertEqual(provenance.collect("/synthetic/repo"), {
                    "repo_root": "/synthetic/repo", "git_commit": "invented-commit",
                    "git_available": True, "dirty_tree": dirty,
                    "dirty_file_count": count, "source_hash_sha256": tree_hash,
                    "source_file_count": 3,
                })
                self.assertEqual(git.call_args_list, [
                    mock.call("/synthetic/repo", "rev-parse", "HEAD"),
                    mock.call("/synthetic/repo", "status", "--porcelain"),
                ])
                source_hash.assert_called_once_with("/synthetic/repo")

    def test_collect_preserves_unknown_git_state(self):
        with mock.patch.object(provenance, "_git", side_effect=[None, None]), \
             mock.patch.object(provenance, "source_hash", return_value=("b" * 64, 2)):
            self.assertEqual(provenance.collect("/synthetic/repo"), {
                "repo_root": "/synthetic/repo", "git_commit": None,
                "git_available": False, "dirty_tree": None,
                "dirty_file_count": None, "source_hash_sha256": "b" * 64,
                "source_file_count": 2,
            })
