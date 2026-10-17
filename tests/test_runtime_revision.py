"""Offline revision-status checks for long-running bridge processes."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge import revision
from agent_bridge import drift
from agent_bridge import mcp_server
from agent_bridge.mcp_server import Server as ConsultationServer


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, shell=False)


class RevisionTests(unittest.TestCase):
    def test_fake_checkout_reports_current_then_stale(self):
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            with open(os.path.join(root, "file"), "w", encoding="utf-8") as handle:
                handle.write("one\n")
            _git(root, "add", "file")
            _git(root, "commit", "-m", "first")
            loaded = revision.capture(root)
            self.assertEqual(revision.report(loaded, checkout=root)["code_stale"], False)
            with open(os.path.join(root, "file"), "a", encoding="utf-8") as handle:
                handle.write("two\n")
            _git(root, "add", "file")
            _git(root, "commit", "-m", "second")
            # Production status calls intentionally cache HEAD briefly; this
            # fixture needs to observe the commit made in the same process.
            revision._head_cache.clear()
            self.assertEqual(revision.report(loaded, checkout=root)["code_stale"], True)

    def test_unknown_head_fails_closed_as_stale(self):
        self.assertEqual(revision.report(None), {"code_revision": "unavailable", "code_stale": True})

    def test_doctor_counts_each_live_process_marker_and_removes_dead_ones(self):
        with tempfile.TemporaryDirectory() as root:
            for pid in (101, 102, 103):
                revision.record(os.path.join(root, f"consultation-server-claude-{pid}.json"),
                                "a" * 40, pid=pid, started_at="2026-01-01T00:00:00+00:00")
            bridge = SimpleNamespace(state_root=root)
            def alive(pid):
                return pid != 103
            def marker(path, **_kwargs):
                return {"code_revision": "a" * 40,
                        "code_stale": path.endswith("-101.json")}
            with mock.patch.object(drift, "_bridge", return_value=bridge), \
                 mock.patch.object(drift.host_platform, "process_alive", side_effect=alive), \
                 mock.patch.object(drift.revision, "read_record", side_effect=marker):
                status = drift.runtime_revision_status(home=root)
            self.assertEqual(status["stale_clients"],
                             ["consultation (Claude): 1 of 2 sessions stale"])
            self.assertFalse(os.path.exists(
                os.path.join(root, "consultation-server-claude-103.json")))

    def test_marker_write_failure_does_not_stop_consultation_server(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = SimpleNamespace(
                state_root=root,
                state=lambda name: os.path.join(root, name),
                peer_allowed_classifications=lambda _peer: (),
                prompt_budget=lambda operation: 80 if operation == "start" else 40,
            )
            with mock.patch.object(mcp_server, "Config", SimpleNamespace), \
                 mock.patch.object(revision, "record", side_effect=OSError), \
                 mock.patch.object(revision, "capture", return_value="a" * 40):
                server = ConsultationServer("codex", cfg)
            self.assertEqual(set(server.tools),
                             {"claude_start", "claude_continue", "claude_poll",
                              "claude_read", "claude_close"})


if __name__ == "__main__":
    unittest.main()
