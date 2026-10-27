"""Offline edge cases for orchestration configuration loading."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import operator_isolation  # noqa: E402,F401

from agent_bridge.orchestration import config


class OrchestrationConfigMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def document(self, **updates):
        doc = {
            "config_version": "1", "state_root": str(self.root / "state"),
            "local_queue_root": str(self.root / "queue"),
            "capacity_db": str(self.root / "capacity.sqlite3"),
            "worker_executable": str(self.root / "worker"),
            "worker_state": str(self.root / "worker-state"),
        }
        doc.update(updates)
        path = self.root / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_windows_paths_reject_invalid_host_path(self):
        path = self.document(
            windows_wsl_runtime_root="relative\\runtime",
            windows_wsl_rootfs_path="C:\\bridge\\rootfs.tar",
            windows_wsl_manifest_path="C:\\bridge\\manifest.json")
        with self.assertRaisesRegex(config.OrchestrationConfigError,
                                    "windows_wsl_runtime_root_invalid"):
            config.load(path)

    def test_absolute_path_rejects_non_string_value(self):
        with self.assertRaisesRegex(config.OrchestrationConfigError, "state_root_invalid"):
            config.load(self.document(state_root=17))

    def test_existing_file_returns_path_and_rejects_symlink(self):
        path = self.root / "delegate.py"
        path.write_text("# invented delegate\n", encoding="utf-8")
        self.assertEqual(config._existing_file(str(path), "delegate"), path)
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(config.OrchestrationConfigError,
                                        "delegate_must_be_an_existing_regular_file"):
                config._existing_file(str(path), "delegate")

    def test_existing_dir_returns_path_and_rejects_symlink(self):
        path = self.root / "receipts"
        path.mkdir()
        self.assertEqual(config._existing_dir(str(path), "receipts"), path)
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(config.OrchestrationConfigError,
                                        "receipts_must_be_an_existing_directory"):
                config._existing_dir(str(path), "receipts")

    def test_parse_worktree_prune_roots_requires_existing_directory(self):
        with self.assertRaisesRegex(config.OrchestrationConfigError,
                                    "worktree_prune_roots_invalid"):
            config.parse_worktree_prune_roots({
                "worktree_prune_roots": [str(self.root / "missing")]})

    def test_parse_worktree_prune_roots_resolves_and_keeps_roots(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        value = self.root / "checkout" / ".." / "checkout"
        self.assertEqual(config.parse_worktree_prune_roots({
            "worktree_prune_roots": [str(value)]}), (checkout.resolve(),))

    def test_client_data_health_rejects_zero_timeout(self):
        with self.assertRaisesRegex(config.OrchestrationConfigError,
                                    "client_data_health_timeout_seconds_invalid"):
            config._client_data_health({
                "client_data_health_command": [str(self.root / "health"), "status", "--json"],
                "client_data_health_path": str(self.root),
                "client_data_health_timeout_seconds": 0,
                "client_data_health_record": str(self.root / "record.json"),
            })

    def test_load_rejects_zero_interval(self):
        with self.assertRaisesRegex(config.OrchestrationConfigError,
                                    "interval_seconds_invalid"):
            config.load(self.document(interval_seconds=0))

    def test_load_rejects_capacity_database_as_state_root(self):
        with self.assertRaisesRegex(config.OrchestrationConfigError,
                                    "capacity_db_must_be_file"):
            config.load(self.document(capacity_db=str(self.root / "state")))

    def test_load_preserves_interval_and_client_routes(self):
        loaded = config.load(self.document(interval_seconds=8.5,
                                           client_derived_routes=["peer", "execution"]))
        self.assertEqual(loaded.interval_seconds, 8.5)
        self.assertEqual(loaded.client_derived_routes, frozenset({"peer", "execution"}))

    def test_load_preserves_worktree_prune_roots(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        loaded = config.load(self.document(worktree_prune_roots=[str(checkout)]))
        self.assertEqual(loaded.worktree_prune_roots, (checkout.resolve(),))


if __name__ == "__main__":
    unittest.main()
