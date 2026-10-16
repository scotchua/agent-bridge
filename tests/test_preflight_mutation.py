"""Offline checks for preflight decisions and diagnostic evidence."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge import preflight
from agent_bridge.errors import BrokerError, ErrorCategory


class ClockAndContaminationTests(unittest.TestCase):
    def test_now_records_millisecond_precision(self) -> None:
        with mock.patch("datetime.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2031-04-05T10:11:12.123+00:00"
            self.assertEqual(preflight._now(), "2031-04-05T10:11:12.123+00:00")
        clock.now.return_value.isoformat.assert_called_once_with(timespec="milliseconds")

    def test_record_contamination_writes_only_when_requested(self) -> None:
        detail = {"reason": "invented instruction"}
        with mock.patch.object(preflight.store, "atomic_write_json") as write:
            preflight._record_contamination(None, detail)
            write.assert_not_called()
            preflight._record_contamination("/invented/report.json", detail)
            write.assert_called_once_with("/invented/report.json", detail)


class ObservedVersionTests(unittest.TestCase):
    def test_deadline_uses_bounded_runner_and_first_output_line(self) -> None:
        proc = SimpleNamespace(timed_out=False, spawn_failed=False, cap_exceeded=False,
                               descendant_held_pipes=False, stdout=b"peer 3.2\nextra\n", stderr=b"")
        with (mock.patch("agent_bridge.runner.run", return_value=proc) as run,
              mock.patch.object(preflight.subprocess, "run", side_effect=AssertionError("wrong lane"))):
            self.assertEqual(preflight.observed_version("/invented/peer", ["--version"],
                                                       deadline=42.0), "peer 3.2")
        self.assertEqual(run.call_args.kwargs["deadline"], 42.0)
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_deadline_refuses_timeout_and_each_runner_failure(self) -> None:
        for flag, category in (("timed_out", ErrorCategory.PEER_TIMEOUT),
                               ("spawn_failed", ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING),
                               ("cap_exceeded", ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING),
                               ("descendant_held_pipes", ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING)):
            with self.subTest(flag=flag):
                proc = SimpleNamespace(timed_out=False, spawn_failed=False, cap_exceeded=False,
                                       descendant_held_pipes=False, stdout=b"peer 3.2", stderr=b"")
                setattr(proc, flag, True)
                with mock.patch("agent_bridge.runner.run", return_value=proc):
                    with self.assertRaises(BrokerError) as raised:
                        preflight.observed_version("/invented/peer", ["--version"], deadline=42.0)
                self.assertEqual(raised.exception.category, category)


class DiscoverExecutableTests(unittest.TestCase):
    def test_known_peer_uses_path_lookup(self) -> None:
        with mock.patch.object(preflight.shutil, "which", return_value="/invented/bin/claude") as which:
            self.assertEqual(preflight.discover_executable("claude"), "/invented/bin/claude")
            self.assertIsNone(preflight.discover_executable("unknown"))
        which.assert_called_once_with("claude")


class CheckPeerTests(unittest.TestCase):
    @staticmethod
    def config(executable: str, allowed: list[str] | None = None) -> SimpleNamespace:
        return SimpleNamespace(peer=lambda peer: {"executable": executable,
                                                  "allowed_versions": allowed or []},
                               peer_extra_env=lambda peer: {})

    def test_missing_executable_is_refused_before_version_probe(self) -> None:
        with (mock.patch.object(preflight.os.path, "isfile", return_value=False),
              mock.patch.object(preflight.os, "access", return_value=False),
              mock.patch.object(preflight, "observed_version", return_value="peer 3.2") as version):
            with self.assertRaises(BrokerError) as raised:
                preflight.check_peer(self.config("/invented/missing"), "claude")
        self.assertEqual(raised.exception.category, ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING)
        version.assert_not_called()

    def test_allowed_version_report_is_pinned(self) -> None:
        with (mock.patch.object(preflight.os.path, "isfile", return_value=True),
              mock.patch.object(preflight.os, "access", return_value=True),
              mock.patch.object(preflight, "observed_version", return_value="peer 3.2")):
            report = preflight.check_peer(self.config("/invented/peer", ["peer 3.2"]), "claude")
        self.assertTrue(report["version_pinned"])
        self.assertEqual(report["allowed_versions"], ["peer 3.2"])


class WorkspaceCleanTests(unittest.TestCase):
    def test_clean_workspace_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "work")
            os.mkdir(workspace)
            preflight.assert_workspace_clean(workspace, stop_at=root)

    def test_instruction_in_workspace_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as handle:
                handle.write("invented instruction\n")
            with self.assertRaises(BrokerError) as raised:
                preflight.assert_workspace_clean(root, stop_at=root)
        self.assertEqual(raised.exception.category, ErrorCategory.WORKSPACE_CONTAMINATED)

    def test_stop_at_limits_ancestor_walk(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            boundary = os.path.join(root, "boundary")
            workspace = os.path.join(boundary, "work")
            os.makedirs(workspace)
            with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as handle:
                handle.write("invented instruction\n")
            preflight.assert_workspace_clean(workspace, stop_at=boundary)

    def test_boundary_directory_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "work")
            os.mkdir(workspace)
            with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as handle:
                handle.write("invented instruction\n")
            with self.assertRaises(BrokerError) as raised:
                preflight.assert_workspace_clean(workspace, stop_at=root)
        self.assertEqual(raised.exception.category, ErrorCategory.WORKSPACE_CONTAMINATED)


class WorkspaceEmptyTests(unittest.TestCase):
    def test_empty_workspace_is_accepted_and_nonempty_refused(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            preflight.assert_workspace_empty(workspace)
            with open(os.path.join(workspace, "invented.txt"), "w", encoding="utf-8") as handle:
                handle.write("sample\n")
            with self.assertRaises(BrokerError) as raised:
                preflight.assert_workspace_empty(workspace)
        self.assertEqual(raised.exception.category, ErrorCategory.WORKSPACE_CONTAMINATED)


class PeerHomeInventoryTests(unittest.TestCase):
    def test_inventory_counts_visible_entries_and_reports_auth_presence(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            system = os.path.join(home, "skills", ".system")
            os.makedirs(system)
            for name in ("visible", ".hidden"):
                os.mkdir(os.path.join(system, name))
            with open(os.path.join(home, "auth.json"), "w", encoding="utf-8") as handle:
                handle.write('{"invented": true}\n')
            report = preflight.peer_home_inventory(home)
        self.assertEqual(report["system_skills"], 1)
        self.assertEqual(report["user_skills"], 0)
        self.assertTrue(report["auth_json_present"])


if __name__ == "__main__":
    unittest.main()
