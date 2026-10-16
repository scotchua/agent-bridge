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
            self.assertEqual(revision.report(loaded, checkout=root)["code_stale"], True)

    def test_unknown_head_fails_closed_as_stale(self):
        self.assertEqual(revision.report(None), {"code_revision": "unavailable", "code_stale": True})

    def test_consultation_status_exposes_only_revision_diagnostics(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = SimpleNamespace(
                state=lambda name: os.path.join(root, name),
                peer_allowed_classifications=lambda _peer: (),
                prompt_budget=lambda operation: 80 if operation == "start" else 40,
            )
            with mock.patch.object(revision, "capture", return_value="a" * 40), \
                 mock.patch.object(revision, "report", return_value={"code_revision": "a" * 40, "code_stale": True}):
                server = ConsultationServer("codex", cfg)
                response = server.call_tool({"name": "bridge_status", "arguments": {}})
            self.assertEqual(response["structuredContent"], {"ok": True, "code_revision": "a" * 40, "code_stale": True})


if __name__ == "__main__":
    unittest.main()
