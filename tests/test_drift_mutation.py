"""Offline contracts for drift helpers missed by the mutation sweep."""
from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_bridge import drift


class DriftMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_config_candidates_preserves_receipt_then_default(self) -> None:
        from agent_bridge.orchestration import delegation, gate
        receipt = self.home / "receipt.json"
        configured = self.home / "fictional-config.json"
        fallback = self.home / "fallback.json"
        with mock.patch.object(gate, "install_paths", return_value={"receipt": str(receipt)}), \
             mock.patch.object(delegation, "paths_for", return_value={"config": str(fallback)}), \
             mock.patch.object(drift.store, "read_json", return_value={"config": str(configured)}):
            self.assertEqual(drift._config_candidates(str(self.home)), [str(configured), str(fallback)])

    def test_discover_config_finds_existing_candidate(self) -> None:
        candidate = self.home / "fictional-config.json"
        candidate.write_text("{}")
        with mock.patch.object(drift, "_config_candidates", return_value=[str(candidate)]):
            self.assertEqual(drift.discover_config(home=str(self.home)), str(candidate))

    def test_default_config_path_returns_discovered_path(self) -> None:
        candidate = str(self.home / "fictional-config.json")
        with mock.patch.object(drift, "discover_config", return_value=candidate):
            self.assertEqual(drift.default_config_path(str(self.home)), candidate)

    def test_sha256_hashes_existing_file(self) -> None:
        source = self.home / "invented-binary"
        source.write_bytes(b"invented binary contents")
        self.assertEqual(drift._sha256(str(source)), hashlib.sha256(source.read_bytes()).hexdigest())

    def test_version_reads_first_line_without_injected_runner(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"9.8.7\nextra\n")
        with mock.patch.object(drift.subprocess, "run", return_value=completed) as run:
            self.assertEqual(drift._version(str(self.home / "invented-cli")), "9.8.7")
        run.assert_called_once()
        self.assertEqual(drift._version(str(self.home / "invented-cli"), runner=lambda *a, **k: completed), "9.8.7")

    def test_bridge_returns_loaded_configuration(self) -> None:
        configured = object()
        with mock.patch.object(drift.bridge_config, "load", return_value=configured):
            self.assertIs(drift._bridge(), configured)

    def test_executable_returns_configured_file(self) -> None:
        executable = self.home / "invented-cli"
        executable.write_bytes(b"invented")
        bridge = mock.Mock()
        bridge.peer.return_value = {"executable": str(executable)}
        self.assertEqual(drift._executable("codex", bridge), str(executable.resolve()))

    def test_normal_digest_removes_prefix_and_lowercases(self) -> None:
        self.assertEqual(drift._normal_digest("sha256:ABCD"), "abcd")

    def test_codex_default_model_reads_isolated_home(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "invented-model"\n')
        self.assertEqual(drift._codex_default_model(str(self.home)), "invented-model")

    def test_stable_removes_nested_timestamps(self) -> None:
        self.assertEqual(drift._stable({"at": 10, "nested": [{"at": 20, "value": 30}]}),
                         {"nested": [{"value": 30}]})

    def test_merge_good_baseline_retains_old_failed_codex(self) -> None:
        old = {"at": 1, "paths": {}, "hooks": {}, "config_path": "invented-config",
               "codex_default_model": "invented-model", "executables": {"claude": {"version": "1"}, "codex": {"version": "1"}},
               "ollama": {}, "gemma": {"delegate": {}, "validator": {}}}
        current = {**old, "at": 2, "executables": {"claude": {"version": "1"}, "codex": {"version": "2"}}}
        merged = drift._merge_good_baseline(old, current, set())
        self.assertEqual(merged["executables"]["codex"], {"version": "1"})
        self.assertEqual(merged["at"], 2)

    def test_run_returns_completed_process(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"invented output")
        with mock.patch.object(drift.subprocess, "run", return_value=completed):
            result = drift._run([str(self.home / "invented-cli")], cwd=str(self.home), text="invented input")
        self.assertIs(result, completed)

    def test_quick_run_returns_bounded_probe(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"invented output")
        with mock.patch.object(drift, "_run", return_value=completed) as run:
            self.assertIs(drift._quick_run(["invented-cli"], cwd=str(self.home), text="", timeout=100), completed)
        self.assertEqual(run.call_args.kwargs["timeout"], drift.QUICK_CHECK_TIMEOUT)

    def test_quick_check_cache_expires_at_limit(self) -> None:
        key = ("invented-config", str(self.home))
        drift._quick_cache[key] = (100 - drift.CACHE_SECONDS, {"cached": True})
        self.addCleanup(drift._quick_cache.pop, key, None)
        current = {"paths": {}, "gemma": {"delegate": {}, "validator": {}}}
        with mock.patch.object(drift, "discover_config", return_value="invented-config"), \
             mock.patch.object(drift, "inventory", return_value=current) as inventory, \
             mock.patch.object(drift, "_read_baseline", return_value=None), \
             mock.patch.object(drift.time, "monotonic", return_value=100):
            result = drift.quick_check(home=str(self.home))
        inventory.assert_called_once()
        self.assertNotIn("cached", result)

    def test_quick_check_force_refresh_bypasses_cache_without_baseline_write(self) -> None:
        key = ("invented-config", str(self.home))
        drift._quick_cache[key] = (100.0, {"cached": True})
        self.addCleanup(drift._quick_cache.pop, key, None)
        current = {"paths": {}, "gemma": {"delegate": {}, "validator": {}}}
        with mock.patch.object(drift, "discover_config", return_value="invented-config"), \
             mock.patch.object(drift, "inventory", return_value=current) as inventory, \
             mock.patch.object(drift, "_read_baseline", return_value=None), \
             mock.patch.object(drift, "_write_baseline") as baseline, \
             mock.patch.object(drift.time, "monotonic", return_value=101.0):
            result = drift.quick_check(home=str(self.home), force_refresh=True)
        inventory.assert_called_once()
        baseline.assert_not_called()
        self.assertEqual(result["cache_age_seconds"], 0.0)
        self.assertIn("checked_at", result)

    def test_eligible_release_accepts_cutoff(self) -> None:
        now = datetime.datetime(2034, 3, 5, 12, tzinfo=datetime.timezone.utc).timestamp()
        published = "2034-03-02T12:00:00Z"
        output = subprocess.CompletedProcess([], 0, json.dumps({"9.8.7": published}).encode())
        result = drift._eligible_release(self.home / "npm", self.home, quarantine_hours=72,
                                         runner=lambda *a, **k: output, now=now)
        self.assertEqual(result, ("9.8.7", published))

    def test_newer_rejects_equal_version(self) -> None:
        self.assertIs(drift._newer("9.8.7", "9.8.7"), False)

    def test_quarantine_hours_accepts_zero_setting(self) -> None:
        with mock.patch.object(drift.store, "read_json", return_value={"quarantine_hours": 0}):
            self.assertEqual(drift._quarantine_hours(str(self.home), None), 0.0)

    def test_schedule_launchctl_returns_activation_result(self) -> None:
        from agent_bridge.orchestration import delegation
        result = {"status": "invented-active"}
        with mock.patch.object(drift.sys, "platform", "darwin"), \
             mock.patch.object(delegation, "activate_launch_agent", return_value=result) as activate:
            self.assertIs(drift._schedule_launchctl(str(self.home / "invented.plist")), result)
        activate.assert_called_once()

    def test_remove_schedule_reports_removed_file(self) -> None:
        schedule = self.home / "invented.plist"
        schedule.write_text("invented")
        with mock.patch.object(drift.os, "geteuid", return_value=501, create=True), \
             mock.patch.object(drift, "schedule_path", return_value=str(schedule)), \
             mock.patch.object(drift, "_schedule_launchctl"):
            self.assertTrue(drift.remove_schedule(home=str(self.home)))
            self.assertIs(drift.remove_schedule(home=str(self.home)), False)
        self.assertFalse(schedule.exists())
        with mock.patch.object(drift.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(drift, "schedule_path", return_value=str(schedule)), \
             mock.patch.object(drift, "_schedule_launchctl"):
            with self.assertRaises(PermissionError):
                drift.remove_schedule(home=str(self.home))

    def test_npm_for_prefix_finds_node_adjacent_npm(self) -> None:
        prefix = self.home / "invented-prefix"
        bin_dir = self.home / "invented-node-bin"
        bin_dir.mkdir()
        node = bin_dir / "node"
        npm = bin_dir / "npm"
        npm.write_text("invented")
        with mock.patch.object(drift.shutil, "which", return_value=str(node)):
            self.assertEqual(drift._npm_for_prefix(prefix, prefix / "bin" / "codex"), npm)

    def test_npm_version_extracts_version_from_offline_output(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b'{"version":"9.8.7"}')
        with mock.patch.object(drift, "_run", return_value=completed):
            self.assertEqual(drift._npm_version(self.home / "npm", self.home), "9.8.7")

    def test_report_attention_skips_notification_for_clear_report(self) -> None:
        with mock.patch.object(drift.subprocess, "run") as run:
            drift.report_attention(str(self.home), {"ok": True, "changed": [], "attention": []})
        run.assert_not_called()

    def test_notify_attention_delegates_to_report_attention(self) -> None:
        report = {"changed": ["invented-component"]}
        with mock.patch.object(drift, "report_attention") as notify:
            drift._notify_attention(str(self.home), report)
        notify.assert_called_once_with(str(self.home), report)

    def test_notify_updated_uses_local_notification_command(self) -> None:
        with mock.patch.object(drift.subprocess, "run") as run:
            drift._notify_updated("9.8.7")
        self.assertIn("9.8.7", " ".join(run.call_args.args[0]))

    def test_main_returns_success_for_clean_report(self) -> None:
        report = {"ok": True, "changed": [], "needs_you": []}
        with mock.patch.object(drift, "doctor", return_value=report), mock.patch("builtins.print"):
            self.assertEqual(drift.main(["--json"]), 0)


if __name__ == "__main__":
    unittest.main()
