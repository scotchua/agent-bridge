"""Harness defects that failed every provider job on a stock macOS worker.

- `python`/`python3` verification ran whatever the worker PATH found: no
  `python` at all (sandbox-exec exit 71), or /usr/bin's Python 3.9.
- Any test run left `__pycache__`, which refused the whole job as
  "ignored untracked files" although caches can never enter a patch.
- A 2 MB stream cap was below one ordinary Codex event stream.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import claude_task, codex_task  # noqa: E402

LANES = (codex_task, claude_task)


class PinnedPythonTests(unittest.TestCase):
    def test_python_names_run_the_worker_interpreter(self):
        for lane in LANES:
            for name in ("python", "python3"):
                with self.subTest(lane=lane.__name__, name=name):
                    self.assertEqual(lane._pinned_python([name, "-m", "pytest", "-q"]),
                                     [sys.executable, "-m", "pytest", "-q"])

    def test_other_programs_are_unchanged(self):
        for lane in LANES:
            for command in (["git", "status"], ["npm", "test"], ["/usr/bin/python3", "-m", "pytest"], []):
                with self.subTest(lane=lane.__name__, command=command):
                    self.assertEqual(lane._pinned_python(list(command)), list(command))


class CacheDropTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name) / "tree"
        self.tree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.tree, check=True)
        (self.tree / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\nsecret.env\n")
        (self.tree / "pkg").mkdir()
        (self.tree / "pkg" / "mod.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.tree, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                        "commit", "-qm", "base"], cwd=self.tree, check=True)
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tree, check=True,
                                   capture_output=True, text=True).stdout.strip()

    def _caches(self):
        (self.tree / "pkg" / "__pycache__").mkdir()
        (self.tree / "pkg" / "__pycache__" / "mod.cpython-314.pyc").write_bytes(b"\0")
        (self.tree / ".pytest_cache").mkdir()
        (self.tree / ".pytest_cache" / "README.md").write_text("cache\n")
        (self.tree / "stray.pyc").write_bytes(b"\0")

    def _reset(self):
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=self.tree, check=True)
        subprocess.run(["git", "reset", "-q"], cwd=self.tree, check=True)
        subprocess.run(["git", "clean", "-qfdx"], cwd=self.tree, check=True)

    def test_caches_alone_no_longer_refuse_the_patch(self):
        for lane in LANES:
            with self.subTest(lane=lane.__name__):
                (self.tree / "pkg" / "mod.py").write_text("x = 2\n")
                self._caches()
                patch = lane._patch(self.tree, self.base, lane._env())
                self.assertIn(b"x = 2", patch)
                self.assertNotIn(b"__pycache__", patch)
                self.assertNotIn(b"pytest_cache", patch)
                self.assertFalse((self.tree / "pkg" / "__pycache__" / "mod.cpython-314.pyc").exists())
                self.assertFalse((self.tree / "stray.pyc").exists())
                self._reset()

    def test_any_other_ignored_file_still_refuses(self):
        for lane in LANES:
            with self.subTest(lane=lane.__name__):
                (self.tree / "pkg" / "mod.py").write_text("x = 3\n")
                self._caches()
                (self.tree / "secret.env").write_text("hidden\n")
                with self.assertRaises(lane.TaskError):
                    lane._patch(self.tree, self.base, lane._env())
                self._reset()

    def test_unexpected_content_hidden_in_a_cache_directory_still_refuses(self):
        for lane in LANES:
            for hidden in ("pkg/__pycache__/notes.txt", ".pytest_cache/v/exfil.json"):
                with self.subTest(lane=lane.__name__, hidden=hidden):
                    (self.tree / "pkg" / "mod.py").write_text("x = 4\n")
                    self._caches()
                    (self.tree / hidden).parent.mkdir(parents=True, exist_ok=True)
                    (self.tree / hidden).write_text("hidden\n")
                    with self.assertRaises(lane.TaskError):
                        lane._patch(self.tree, self.base, lane._env())
                    self.assertTrue((self.tree / hidden).exists())
                    self._reset()

    def test_tracked_files_under_cache_names_are_never_deleted(self):
        tracked = self.tree / "vendor" / "__pycache__" / "kept.pyc"
        tracked.parent.mkdir(parents=True)
        tracked.write_bytes(b"tracked")
        subprocess.run(["git", "add", "-f", str(tracked)], cwd=self.tree, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                        "commit", "-qm", "tracked cache"], cwd=self.tree, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tree, check=True,
                              capture_output=True, text=True).stdout.strip()
        for lane in LANES:
            with self.subTest(lane=lane.__name__):
                (self.tree / "pkg" / "mod.py").write_text("x = 5\n")
                patch = lane._patch(self.tree, base, lane._env())
                self.assertTrue(tracked.exists())
                self.assertNotIn(b"kept.pyc", patch)
                subprocess.run(["git", "checkout", "-q", "--", "pkg"], cwd=self.tree, check=True)
                subprocess.run(["git", "reset", "-q"], cwd=self.tree, check=True)

    def test_leading_space_names_are_not_stripped_onto_a_tracked_file(self):
        tracked = self.tree / "kept.pyc"
        tracked.write_bytes(b"tracked")
        subprocess.run(["git", "add", "-f", "kept.pyc"], cwd=self.tree, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                        "commit", "-qm", "tracked pyc"], cwd=self.tree, check=True)
        for lane in LANES:
            with self.subTest(lane=lane.__name__):
                spaced = self.tree / " kept.pyc"
                spaced.write_bytes(b"ignored")
                lane._drop_caches(self.tree, lane._env())
                self.assertTrue(tracked.exists())
                self.assertEqual(tracked.read_bytes(), b"tracked")
                self.assertFalse(spaced.exists())

    def test_a_symlinked_cache_directory_is_not_followed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "keep.pyc").write_bytes(b"keep")
        (self.tree / "pkg" / "__pycache__").symlink_to(outside, target_is_directory=True)
        for lane in LANES:
            with self.subTest(lane=lane.__name__):
                lane._drop_caches(self.tree, lane._env())
                self.assertTrue((outside / "keep.pyc").exists())


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"),
                     "macOS sandbox-exec backend")
class SandboxedPythonEndToEndTests(unittest.TestCase):
    def test_bare_python_runs_the_worker_interpreter_inside_the_sandbox(self):
        for lane in LANES:
            with self.subTest(lane=lane.__name__), tempfile.TemporaryDirectory() as temp:
                tree = Path(temp) / "tree"
                tree.mkdir()
                env = {**lane._env(), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
                result = lane._sandboxed(["python", "-c", "import sys, unittest; print(sys.executable)"],
                                         tree, Path(temp) / "scratch", env, 60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.decode().strip(), sys.executable)


class StreamCapTests(unittest.TestCase):
    def test_stream_cap_admits_an_ordinary_event_stream(self):
        for lane in LANES:
            with self.subTest(lane=lane.__name__):
                self.assertGreaterEqual(lane.MAX_STREAM_BYTES, 16_000_000)


if __name__ == "__main__":
    unittest.main()
