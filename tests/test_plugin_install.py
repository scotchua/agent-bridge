"""Offline contracts for the Claude Code packaged install path."""
from __future__ import annotations

import contextlib
import io
import json
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

from agent_bridge import config, mcp_server, packaged, packaged_setup, revision, store  # noqa: E402


class PluginInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.mkdir(self.home, 0o700)
        self.plugin = os.path.join(self.tmp.name, "plugin")
        os.makedirs(os.path.join(self.plugin, ".claude-plugin"), mode=0o700)
        Path(os.path.join(self.plugin, ".claude-plugin", "plugin.json")).write_text(
            json.dumps({"name": "agent-bridge", "version": "1.0.0"}), encoding="utf-8")
        Path(os.path.join(self.plugin, "payload.py")).write_text("one\n", encoding="utf-8")
        self.base = json.loads((ROOT / "config" / "broker.json").read_text(encoding="utf-8"))
        self.base["state_root"] = os.path.join(self.home, ".agent-bridge")
        self.base["schema_path"] = str(ROOT / "schema" / "peer_response.v2.json")
        self.defaults = os.path.join(self.tmp.name, "broker.json")
        store.atomic_write_json(self.defaults, self.base)

    @contextlib.contextmanager
    def environment(self, *, packaged_mode: bool, extra: dict[str, str] | None = None):
        values = {"HOME": self.home, packaged.PACKAGED_ENV: "1" if packaged_mode else "",
                  packaged.PLUGIN_ROOT_ENV: self.plugin}
        if extra:
            values.update(extra)
        with mock.patch.dict(os.environ, values, clear=False), \
             mock.patch.object(config, "DEFAULT_CONFIG_PATH", self.defaults), \
             mock.patch.object(config, "REPO_ROOT", self.tmp.name):
            yield

    def test_packaged_config_precedence_and_clone_isolation(self) -> None:
        clone_one = os.path.join(self.tmp.name, "one", "config")
        clone_two = os.path.join(self.tmp.name, "two", "config")
        os.makedirs(clone_one, mode=0o700)
        os.makedirs(clone_two, mode=0o700)
        one, two = os.path.join(clone_one, "local.json"), os.path.join(clone_two, "local.json")
        store.atomic_write_json(one, {"limits": {"max_jobs": 11}})
        store.atomic_write_json(two, {"limits": {"max_jobs": 22}})
        with self.environment(packaged_mode=True):
            packaged.prepare()
            packaged.record_verified(packaged.package_identity())
            packaged.write_json(packaged.config_path(), {"limits": {"max_jobs": 33}})
            self.assertEqual(config.load().limit("max_jobs"), 33)
            explicit = os.path.join(self.tmp.name, "explicit.json")
            store.atomic_write_json(explicit, {"limits": {"max_jobs": 44}})
            self.assertEqual(config.load(explicit).limit("max_jobs"), 44)
            with mock.patch.dict(os.environ, {config.CONFIG_ENV: explicit}):
                self.assertEqual(config.load().limit("max_jobs"), 44)
        # Disabling the explicit packaged bit means its config is not even
        # considered. Each clone still selects only its own local overlay.
        with self.environment(packaged_mode=False), \
             mock.patch.object(config, "local_config_path", return_value=one):
            self.assertEqual(config.load().limit("max_jobs"), 11)
        with self.environment(packaged_mode=False), \
             mock.patch.object(config, "local_config_path", return_value=two):
            self.assertEqual(config.load().limit("max_jobs"), 22)

    def test_atomic_interruption_keeps_previous_config(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            path = packaged.config_path()
            packaged.write_json(path, {"limits": {"max_jobs": 1}})
            with mock.patch.object(store, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    packaged.write_json(path, {"limits": {"max_jobs": 2}})
            self.assertEqual(packaged.read_json(path)["limits"]["max_jobs"], 1)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not Windows ACL evidence")
    def test_insecure_or_symlinked_state_is_refused(self) -> None:
        with self.environment(packaged_mode=True):
            root = packaged.state_root()
            os.mkdir(root, 0o700)
            os.chmod(root, 0o775)
            with self.assertRaisesRegex(packaged.PackagedStateError, "writable"):
                packaged.prepare()
            os.chmod(root, 0o700)
            target = os.path.join(self.tmp.name, "target")
            os.mkdir(target, 0o700)
            os.symlink(target, os.path.join(root, "packaged"))
            with self.assertRaisesRegex(packaged.PackagedStateError, "symlink"):
                packaged.prepare()

    @unittest.skipIf(os.name == "nt", "POSIX owner ids are not Windows ACL evidence")
    def test_other_owner_is_refused_where_owner_ids_are_available(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            packaged.write_json(packaged.config_path(), {})
            with mock.patch.object(packaged.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(packaged.PackagedStateError, "owned"):
                    packaged.validate_for_read(packaged.config_path())

    def test_config_cannot_be_redirected_into_peer_writable_directory(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            peer_workdir = os.path.join(packaged.state_root(), "workspaces", "codex")
            os.makedirs(peer_workdir, mode=0o700)
            with self.assertRaisesRegex(packaged.PackagedStateError, "outside"):
                packaged.write_json(os.path.join(peer_workdir, "config.json"), {})

    def test_digest_change_requires_setup_and_known_rollback_is_accepted(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            first = packaged.package_identity()
            packaged.record_verified(first)
            self.assertTrue(packaged.verification_status()[0])
            payload = Path(os.path.join(self.plugin, "payload.py"))
            payload.write_text("two\n", encoding="utf-8")
            self.assertFalse(packaged.verification_status()[0])
            payload.write_text("one\n", encoding="utf-8")
            self.assertTrue(packaged.verification_status()[0])

    def test_server_refuses_an_unverified_plugin_change_before_loading_config(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            packaged.record_verified(packaged.package_identity())
            Path(os.path.join(self.plugin, "payload.py")).write_text("changed\n", encoding="utf-8")
            with mock.patch.object(mcp_server, "load_config", side_effect=AssertionError("must not load")):
                self.assertEqual(mcp_server.main(["--caller", "claude"]), 2)

    def test_packaged_revision_is_content_identity_not_git(self) -> None:
        with self.environment(packaged_mode=True):
            value = revision.capture()
            self.assertTrue(value.startswith("plugin:1.0.0:"))

    def test_launcher_fails_closed_without_a_supported_interpreter(self) -> None:
        empty = os.path.join(self.tmp.name, "empty-bin")
        os.mkdir(empty)
        for name in ("python3", "python"):
            path = os.path.join(empty, name)
            Path(path).write_text("#!/bin/sh\necho Python 3.10.0\nexit 1\n", encoding="utf-8")
            os.chmod(path, 0o700)
        # dirname/sed/head remain available to the launcher; both Python names
        # ahead of them deliberately fail the version probe.
        env = {"HOME": self.home, "PATH": empty + ":/usr/bin:/bin"}
        completed = subprocess.run(["/bin/sh", str(ROOT / "bin" / "agent-bridge-plugin-mcp"),
                                    "--caller", "claude"], env=env, text=True,
                                   capture_output=True, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr.strip(),
                         "agent-bridge requires Python 3.11 or newer. Install Python 3.11+ and run /agent-bridge:setup again.")

    def test_duplicate_codex_peer_is_detected_without_reading_values(self) -> None:
        config_path = os.path.join(self.home, ".claude.json")
        store.atomic_write_json(config_path, {"mcpServers": {"codex-peer": {"command": "private"}}})
        self.assertEqual(packaged_setup.find_competing_registration(self.home), config_path)

    def test_server_reports_an_unreadable_claude_config_as_unreadable(self) -> None:
        Path(os.path.join(self.home, ".claude.json")).write_text("{half", encoding="utf-8")
        with self.environment(packaged_mode=True):
            packaged.prepare()
            packaged.record_verified(packaged.package_identity())
            with mock.patch.object(mcp_server, "load_config", side_effect=AssertionError("must not load")), \
                 mock.patch.object(sys, "stderr", new=io.StringIO()) as err:
                self.assertEqual(mcp_server.main(["--caller", "claude"]), 2)
        self.assertIn("could not read the Claude configuration", err.getvalue())
        self.assertNotIn("conflicts", err.getvalue())

    def test_setup_refuses_native_windows_before_touching_state(self) -> None:
        with self.environment(packaged_mode=True), \
             mock.patch.object(packaged_setup.os, "name", "nt"), \
             mock.patch.object(packaged, "prepare", side_effect=AssertionError("must not prepare")):
            with self.assertRaisesRegex(ValueError, "clone install"):
                packaged_setup.setup(home=self.home)

    def test_plugin_metadata_is_valid_and_uses_plugin_root(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(marketplace["name"], "agent-bridge")
        self.assertEqual(marketplace["plugins"][0]["name"], "agent-bridge")
        self.assertEqual(marketplace["plugins"][0]["source"], "./")
        self.assertEqual(plugin["name"], "agent-bridge")
        self.assertEqual(plugin["version"], "1.0.0")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", plugin["mcpServers"]["codex-peer"]["command"])

    def test_windows_reparse_attribute_is_refused_by_the_shared_path_check(self) -> None:
        fake = mock.Mock(st_file_attributes=0x400)
        self.assertTrue(packaged._is_reparse(fake))

    def test_unverified_overlay_is_refused_by_config_load(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            packaged.write_json(packaged.config_path(), {"limits": {"max_jobs": 33}})
            with self.assertRaises(ValueError):
                config.load()

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not Windows ACL evidence")
    def test_group_or_world_readable_state_is_refused(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            packaged.write_json(packaged.config_path(), {})
            os.chmod(packaged.config_path(), 0o644)
            with self.assertRaisesRegex(packaged.PackagedStateError, "readable"):
                packaged.validate_for_read(packaged.config_path())
            os.chmod(packaged.config_path(), 0o600)
            os.chmod(packaged.directory(), 0o755)
            with self.assertRaisesRegex(packaged.PackagedStateError, "readable"):
                packaged.prepare()

    @unittest.skipIf(os.name == "nt", "FIFOs are POSIX only")
    def test_fifo_in_place_of_state_file_is_refused(self) -> None:
        with self.environment(packaged_mode=True):
            packaged.prepare()
            os.mkfifo(packaged.config_path(), 0o600)
            with self.assertRaisesRegex(packaged.PackagedStateError, "file type"):
                packaged.validate_for_read(packaged.config_path())

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not Windows ACL evidence")
    def test_group_writable_home_is_refused(self) -> None:
        os.chmod(self.home, 0o770)
        with self.environment(packaged_mode=True):
            with self.assertRaisesRegex(packaged.PackagedStateError, "home directory"):
                packaged.prepare()

    @unittest.skipIf(os.name == "nt", "symlink creation needs privileges on Windows")
    def test_plugin_digest_refuses_a_symlink(self) -> None:
        os.symlink(os.path.join(self.plugin, "payload.py"), os.path.join(self.plugin, "linked.py"))
        with self.environment(packaged_mode=True):
            with self.assertRaisesRegex(packaged.PackagedStateError, "symlinks"):
                packaged.package_identity()

    def test_project_scoped_registrations_are_detected(self) -> None:
        config_path = os.path.join(self.home, ".claude.json")
        store.atomic_write_json(config_path, {"projects": {"/some/project": {
            "mcpServers": {"codex-peer": {"command": "private"}}}}})
        self.assertEqual(packaged_setup.find_competing_registration(self.home), config_path)
        os.remove(config_path)
        project = os.path.join(self.tmp.name, "project")
        os.mkdir(project)
        mcp_json = os.path.join(project, ".mcp.json")
        store.atomic_write_json(mcp_json, {"mcpServers": {"codex-peer": {}}})
        self.assertEqual(packaged_setup.find_competing_registration(self.home, cwd=project), mcp_json)
        self.assertIsNone(packaged_setup.find_competing_registration(self.home, cwd=self.tmp.name))

    def test_launcher_python_with_unsafe_characters_is_refused(self) -> None:
        for value in ('/usr/bin/py"thon', "/usr/bin/py\\thon", "/usr/bin/py\nthon", "python3"):
            with self.assertRaises(ValueError):
                packaged_setup._check_launcher_python(value)
        packaged_setup._check_launcher_python("/usr/local/bin/python3")

    def _verification_record(self) -> dict:
        ok = {"status": "complete", "contract_valid": True}
        rows = [dict(ok, kind=f"one-turn {i}") for i in range(10)]
        rows += [dict(ok, kind=f"multi-turn {i} t1") for i in range(3)]
        rows.append({"kind": "schema-pressure", "status": "failed", "acceptable": True})
        return {"effective_config_sha256": "bound",
                "runner_arguments": {"direction": "claude-to-codex"},
                "blocked": [], "skipped_controls": [],
                "controls_requested": {"timeout": True}, "controls_executed": {"timeout": True},
                "rows": rows, "timeouts": [{"status": "timed_out", "orphans": 0}],
                "observed_versions": {"codex": ["1.0.0"]}}

    def _check_record(self, record: dict) -> None:
        path = os.path.join(self.tmp.name, "canary-results.json")
        store.atomic_write_json(path, record)
        candidate = mock.Mock(raw={})
        candidate.peer.return_value = {"allowed_versions": ["1.0.0"]}
        with mock.patch.object(config, "effective_config_sha256", return_value="bound"):
            packaged_setup._verify_result(path, candidate)

    def test_verification_accepts_a_complete_one_direction_record(self) -> None:
        self._check_record(self._verification_record())

    def test_verification_refuses_incomplete_evidence(self) -> None:
        cases = {
            "blocked": lambda r: r.update(blocked=["codex"]),
            "skipped": lambda r: r.update(skipped_controls=["timeout"]),
            "unexecuted control": lambda r: r.update(controls_executed={}),
            "no controls": lambda r: r.update(controls_requested={}, controls_executed={}),
            "schema pressure": lambda r: r["rows"][-1].update(acceptable=False),
            "timeout status": lambda r: r["timeouts"][0].update(status="failed"),
            "timeout orphans": lambda r: r["timeouts"][0].update(orphans=1),
            "no timeouts": lambda r: r.update(timeouts=[]),
        }
        for name, mutate in cases.items():
            record = self._verification_record()
            mutate(record)
            with self.subTest(name), self.assertRaises(ValueError):
                self._check_record(record)

    @unittest.skipIf(os.name == "nt", "POSIX launcher")
    def test_launcher_ignores_recorded_python_when_state_is_not_private(self) -> None:
        fake = os.path.join(self.tmp.name, "fake-python")
        Path(fake).write_text('#!/bin/sh\ncase "$2" in -c) exit 0;; esac\necho FAKE\n',
                              encoding="utf-8")
        os.chmod(fake, 0o700)
        empty = os.path.join(self.tmp.name, "empty-bin")
        os.mkdir(empty)
        with self.environment(packaged_mode=True):
            packaged.prepare()
            packaged.write_json(packaged.config_path(), {"launcher_python": fake})
        env = {"HOME": self.home, "PATH": empty + ":/usr/bin:/bin"}
        command = ["/bin/sh", str(ROOT / "bin" / "agent-bridge-plugin-mcp")]
        trusted = subprocess.run(command, env=env, text=True, capture_output=True, check=False,
                                 stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(trusted.stdout.strip(), "FAKE")
        os.chmod(os.path.join(self.home, ".agent-bridge", "packaged", "config.json"), 0o640)
        untrusted = subprocess.run(command, env=env, text=True, capture_output=True, check=False,
                                 stdin=subprocess.DEVNULL, timeout=60)
        self.assertNotIn("FAKE", untrusted.stdout)


if __name__ == "__main__":
    unittest.main()
