"""The patch that comes back, and the ceiling it is read through.

The defect these exist for: ``capture_diff`` used ``subprocess.run`` with
``stdout=PIPE``, buffered the entire patch, and only then compared its length
to the limit. A repository that produced a gigabyte of diff consumed a
gigabyte of guest memory before anything asked whether that was allowed. A
limit downstream of the buffer is not a limit.

It also inherited ``subprocess.run``'s other problem: ``run`` waits for
end-of-file on the pipe, so a descendant that outlived git and kept the write
end open would hold the call open after the job was over.

These run real subprocesses against real temporary repositories, so they are
evidence about the mechanism rather than about a mock of it.
"""

from __future__ import annotations

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

from agent_bridge.orchestration import guest_runner as gr

GIT = shutil.which("git")


@unittest.skipIf(GIT is None, "git is not installed on this machine")
@unittest.skipIf(os.name == "nt", "in-guest git lifecycle: POSIX process groups")
class CaptureDiffTests(unittest.TestCase):
    """The guest pins git at /usr/bin/git. Here it is wherever it is."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = self.temp.name
        patcher = mock.patch.object(gr, "GIT_PATH", GIT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _repo(self, **files):
        gr._git(["init", "-q"], self.workdir)
        for name, payload in files.items():
            Path(self.workdir, name).write_bytes(payload)
        gr._git(["add", "-A"], self.workdir)
        gr._git(["commit", "-q", "--allow-empty", "-m", "base"], self.workdir)

    def test_an_unchanged_tree_produces_an_empty_patch(self):
        self._repo(**{"a.txt": b"one\n"})
        self.assertEqual(gr.capture_diff(self.workdir), b"")

    def test_a_change_appears_in_the_patch(self):
        self._repo(**{"a.txt": b"one\n"})
        Path(self.workdir, "a.txt").write_bytes(b"two\n")
        patch = gr.capture_diff(self.workdir)
        self.assertIn(b"a.txt", patch)
        self.assertIn(b"+two", patch)

    def test_a_new_file_appears_in_the_patch(self):
        self._repo(**{"a.txt": b"one\n"})
        Path(self.workdir, "new.txt").write_bytes(b"added\n")
        self.assertIn(b"new.txt", gr.capture_diff(self.workdir))

    def test_a_tree_with_no_git_directory_produces_nothing(self):
        self.assertEqual(gr.capture_diff(self.workdir), b"")

    def test_a_patch_above_the_ceiling_is_refused(self):
        self._repo()
        Path(self.workdir, "big.txt").write_bytes(
            b"x" * (gr.MAX_DIFF_BYTES + 1024 * 1024))
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.capture_diff(self.workdir)
        self.assertEqual(caught.exception.code, "diff_too_large")

    def test_a_patch_just_below_the_ceiling_comes_back_whole(self):
        self._repo()
        # Compressible text, written as lines so git emits a text diff.
        body = (b"line of content\n" * ((gr.MAX_DIFF_BYTES // 2) // 16))
        Path(self.workdir, "medium.txt").write_bytes(body)
        patch = gr.capture_diff(self.workdir)
        self.assertLessEqual(len(patch), gr.MAX_DIFF_BYTES)
        self.assertIn(b"medium.txt", patch)

    def test_the_ceiling_is_applied_while_reading_not_after(self):
        """The property the old version failed. Nothing may buffer past the
        ceiling before the refusal happens."""

        self._repo()
        Path(self.workdir, "big.txt").write_bytes(
            b"y" * (gr.MAX_DIFF_BYTES * 3))
        seen = {}
        real = gr._BoundedReader

        class _Watched(real):
            def __init__(self, stream, limit):
                seen.setdefault("limits", []).append(limit)
                real.__init__(self, stream, limit)

        with mock.patch.object(gr, "_BoundedReader", _Watched):
            with self.assertRaises(gr.GuestRunnerError):
                gr.capture_diff(self.workdir)
        self.assertIn(gr.MAX_DIFF_BYTES + 1, seen["limits"])

    def test_capture_diff_does_not_use_an_unbounded_pipe(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration" /
                  "guest_runner.py").read_text(encoding="utf-8")
        body = source[source.index("def capture_diff"):
                      source.index("class _OutputTooLarge")]
        self.assertNotIn("subprocess.run", body)
        self.assertIn("_bounded_git", body)

    def test_a_failing_git_is_a_named_refusal(self):
        self._repo()
        with mock.patch.object(gr, "_bounded_git",
                               lambda *a, **k: (1, b"", b"fatal")):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.capture_diff(self.workdir)
        self.assertEqual(caught.exception.code, "diff_failed")

    def test_a_timeout_is_its_own_refusal(self):
        self._repo()

        def slow(*_a, **_k):
            raise TimeoutError("git timed out")

        with mock.patch.object(gr, "_bounded_git", slow):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.capture_diff(self.workdir)
        self.assertEqual(caught.exception.code, "diff_timed_out")

    def test_no_patch_content_appears_in_a_refusal(self):
        self._repo()
        secret = b"TOPSECRETVALUE"
        Path(self.workdir, "big.txt").write_bytes(
            secret + b"z" * (gr.MAX_DIFF_BYTES + 1024))
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.capture_diff(self.workdir)
        self.assertNotIn(secret.decode(), str(caught.exception))


@unittest.skipIf(GIT is None or os.name == "nt", "POSIX process groups")
class BoundedGitLifecycleTests(unittest.TestCase):
    """The runner half: timeouts and descendants that outlive their parent."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_a_command_that_never_finishes_times_out_rather_than_hangs(self):
        with mock.patch.object(gr, "GIT_PATH", "/bin/sh"):
            with self.assertRaises(TimeoutError):
                gr._bounded_git(["-c", "sleep 30"], self.temp.name,
                                limit=1024, timeout=0.5)

    def test_a_descendant_holding_the_pipe_does_not_hold_the_call(self):
        """The failure ``subprocess.run`` has and this does not: git exits,
        a grandchild keeps the write end of stdout open, and a reader waiting
        for end-of-file waits forever."""

        script = "( sleep 30 & ) ; echo done ; exit 0"
        with mock.patch.object(gr, "GIT_PATH", "/bin/sh"):
            returncode, stdout, _stderr = gr._bounded_git(
                ["-c", script], self.temp.name, limit=1024, timeout=10.0)
        self.assertEqual(returncode, 0)
        self.assertIn(b"done", stdout)

    def test_output_above_the_limit_raises_rather_than_truncating(self):
        with mock.patch.object(gr, "GIT_PATH", "/bin/sh"):
            with self.assertRaises(gr._OutputTooLarge):
                gr._bounded_git(["-c", "yes AAAAAAAA"], self.temp.name,
                                limit=64 * 1024, timeout=20.0)

    def test_stderr_above_the_limit_also_fails_closed(self):
        with mock.patch.object(gr, "GIT_PATH", "/bin/sh"), \
             mock.patch.object(gr, "MAX_OUTPUT_BYTES", 64 * 1024):
            with self.assertRaises(gr._OutputTooLarge):
                gr._bounded_git(["-c", "yes ERROR 1>&2"], self.temp.name,
                                limit=64 * 1024, timeout=20.0)

    def test_a_bounded_run_never_inherits_this_processes_environment(self):
        os.environ["AGENT_BRIDGE_LEAK_CANARY"] = "leaked"
        self.addCleanup(os.environ.pop, "AGENT_BRIDGE_LEAK_CANARY", None)
        with mock.patch.object(gr, "GIT_PATH", "/usr/bin/env"):
            _code, stdout, _err = gr._bounded_git([], self.temp.name,
                                                  limit=64 * 1024, timeout=10.0)
        self.assertNotIn(b"AGENT_BRIDGE_LEAK_CANARY", stdout)


if __name__ == "__main__":
    unittest.main()
