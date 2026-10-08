"""Offline tests for the opt-in upgrade guide."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import onboard, store, upgrade  # noqa: E402
from agent_bridge.chat.grok import ALLOWED_COMMANDS  # noqa: E402


class UpgradeTests(unittest.TestCase):
    def _receipt(self, home: str) -> None:
        store.atomic_write_json(onboard._paths(home)["receipt"], {"version": 1})

    def _chat(self, home: str) -> None:
        os.makedirs(os.path.join(home, ".agent-bridge", "chat"), exist_ok=True)

    def _run(self, home: str, answers: list[str], *, platform: str = "linux") -> tuple[int, list[str]]:
        messages: list[str] = []
        iterator = iter(answers)
        with mock.patch.object(upgrade.os, "geteuid", return_value=501, create=True):
            result = upgrade.run(home=home, platform=platform, ask=lambda _prompt: next(iterator), out=messages.append)
        return result, messages

    def _manifest(self, home: str, version: str = "1") -> str:
        path = os.path.join(home, ".agent-bridge", "grok", "bot-manifest.json")
        store.atomic_write_json(path, {"product": "Grok Bot", "version": version,
                                       "tools": sorted(ALLOWED_COMMANDS),
                                       "allowlist": sorted(ALLOWED_COMMANDS)})
        return path

    def test_first_install_exits_without_writing(self):
        with tempfile.TemporaryDirectory() as home:
            result, messages = self._run(home, [])
            self.assertEqual(result, 0)
            self.assertIn("first install", "\n".join(messages))
            self.assertFalse(os.path.exists(os.path.join(home, ".agent-bridge")))

    def test_detection_fixtures_distinguish_available_on_and_uncertain(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            self._chat(home)
            bad = os.path.join(home, ".agent-bridge", "grok", "bot-manifest.json")
            store.atomic_write_bytes(bad, b"not json")
            with mock.patch.object(upgrade.shutil, "which", side_effect=lambda name: "/bin/hermes" if name == "hermes" else None):
                states = upgrade.detect(home, platform="linux")
            self.assertEqual(states["bridge_base"]["state"], "on")
            self.assertEqual(states["agent_room"]["state"], "on")
            self.assertEqual(states["hermes"]["state"], "available")
            self.assertEqual(states["grok"]["state"], "uncertain")

    def test_default_no_writes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            with mock.patch.object(upgrade.shutil, "which", return_value=None):
                result, _ = self._run(home, ["", "", ""])
            self.assertEqual(result, 0)
            self.assertFalse(os.path.exists(upgrade._upgrade_path(home)))
            self.assertFalse(os.path.exists(upgrade.launcher_path(home, "linux")))

    def test_hermes_without_agent_room_is_not_enabled(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            with mock.patch.object(upgrade.shutil, "which", side_effect=lambda name: "/opt/hermes" if name == "hermes" else None):
                result, messages = self._run(home, ["n", "y", "n", "y"])
            self.assertEqual(result, 0)
            self.assertIn("This needs Agent Room. Not now.", messages)
            self.assertFalse(os.path.exists(upgrade.launcher_path(home, "linux")))

    def test_launcher_and_manifest_contents(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            with mock.patch.object(upgrade.shutil, "which", return_value=None):
                result, _ = self._run(home, ["y", "y", "1.2.3", "y"])
            self.assertEqual(result, 0)
            launcher = Path(upgrade.launcher_path(home, "linux")).read_text()
            self.assertIn("--grok-state-dir", launcher)
            self.assertIn(os.path.join(home, ".agent-bridge", "grok"), launcher)
            manifest = store.read_json(os.path.join(home, ".agent-bridge", "grok", "bot-manifest.json"))
            self.assertEqual(manifest["version"], "1.2.3")
            self.assertEqual(set(manifest["tools"]), ALLOWED_COMMANDS)

    def test_preview_no_writes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            with mock.patch.object(upgrade.shutil, "which", return_value=None):
                result, messages = self._run(home, ["y", "n", "n"])
            self.assertEqual(result, 0)
            self.assertIn("Preview: files that would be created or replaced:", messages)
            self.assertFalse(os.path.exists(upgrade.launcher_path(home, "linux")))
            self.assertFalse(os.path.exists(upgrade._upgrade_path(home)))

    def test_missing_baseline_offers_doctor_and_nightly_separately(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            config = os.path.join(home, "orchestration.json")
            Path(config).write_text("{}")
            prompts: list[str] = []
            def ask(prompt: str) -> str:
                prompts.append(prompt)
                if "baseline" in prompt:
                    return ""
                if "nightly" in prompt:
                    return ""
                if "Apply" in prompt:
                    return "y"
                return "n"
            with mock.patch.object(upgrade.os, "geteuid", return_value=501, create=True), \
                 mock.patch.object(upgrade.drift, "discover_config", return_value=config), \
                 mock.patch.object(upgrade.drift, "_read_baseline", return_value=None), \
                 mock.patch.object(upgrade.drift, "scheduled_baseline", return_value={"ok": True}) as baseline, \
                 mock.patch.object(upgrade.drift, "doctor") as doctor, \
                 mock.patch.object(upgrade.drift, "install_schedule") as schedule:
                result = upgrade.run(home=home, platform="darwin", ask=ask, out=lambda _message: None)
            self.assertEqual(result, 0)
            self.assertIn("Run doctor once to record a baseline? [Y/n] ", prompts)
            self.assertIn("Install the nightly doctor schedule? [y/N] ", prompts)
            baseline.assert_called_once_with(config, home=os.path.abspath(home))
            doctor.assert_not_called()
            schedule.assert_not_called()

    def test_manifest_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            path = self._manifest(home, "old")
            original = Path(path).read_bytes()
            with self.assertRaisesRegex(ValueError, "will not be overwritten"):
                upgrade._commit(home, {path: b"new"}, [path], platform="linux", refuse_replace={path})
            self.assertEqual(Path(path).read_bytes(), original)

    def test_failed_write_rolls_back_everything(self):
        with tempfile.TemporaryDirectory() as home:
            first = os.path.join(home, ".agent-bridge", "first")
            second = os.path.join(home, ".agent-bridge", "second")
            store.atomic_write_bytes(first, b"old first")
            store.atomic_write_bytes(second, b"old second")
            original_writer = store.atomic_write_bytes
            def fail_second(path: str, data: bytes, **kwargs: object) -> None:
                if path == second and data == b"new second":
                    raise OSError("synthetic failure")
                original_writer(path, data, **kwargs)
            with mock.patch.object(upgrade.store, "atomic_write_bytes", side_effect=fail_second):
                with self.assertRaises(OSError):
                    upgrade._commit(home, {first: b"new first", second: b"new second"}, [], platform="linux")
            self.assertEqual(Path(first).read_bytes(), b"old first")
            self.assertEqual(Path(second).read_bytes(), b"old second")

    def test_late_failed_write_rolls_back_created_and_replaced_files(self):
        with tempfile.TemporaryDirectory() as home:
            first = os.path.join(home, ".agent-bridge", "first")
            late = os.path.join(home, ".agent-bridge", "late")
            store.atomic_write_bytes(first, b"old")
            original_writer = store.atomic_write_bytes
            def fail_after_write(path: str, data: bytes, **kwargs: object) -> None:
                original_writer(path, data, **kwargs)
                if path == late:
                    raise OSError("late synthetic failure")
            with mock.patch.object(upgrade.store, "atomic_write_bytes", side_effect=fail_after_write):
                with self.assertRaises(OSError):
                    upgrade._commit(home, {first: b"new", late: b"late"}, [late], platform="linux")
            self.assertEqual(Path(first).read_bytes(), b"old")
            self.assertFalse(os.path.exists(late))

    def test_undo_removes_only_unchanged_created_files(self):
        with tempfile.TemporaryDirectory() as home:
            created = os.path.join(home, ".agent-bridge", "created")
            changed = os.path.join(home, ".agent-bridge", "changed")
            store.atomic_write_bytes(created, b"created")
            store.atomic_write_bytes(changed, b"changed")
            store.atomic_write_json(upgrade._upgrade_path(home), {"created": [
                {"path": created, "sha256": upgrade._sha256(b"created")},
                {"path": changed, "sha256": upgrade._sha256(b"before")},
            ]})
            messages: list[str] = []
            self.assertEqual(upgrade.undo(home, out=messages.append), 0)
            self.assertFalse(os.path.exists(created))
            self.assertTrue(os.path.exists(changed))
            self.assertFalse(os.path.exists(upgrade._upgrade_path(home)))

    def test_undo_without_a_record_is_a_successful_noop(self):
        with tempfile.TemporaryDirectory() as home:
            messages: list[str] = []
            self.assertEqual(upgrade.undo(home, out=messages.append), 0)
            self.assertEqual(messages, ["Nothing to undo: no upgrade record was found."])

    def test_malformed_undo_record_remains_an_error(self):
        with tempfile.TemporaryDirectory() as home:
            store.atomic_write_bytes(upgrade._upgrade_path(home), b"not json")
            messages: list[str] = []
            self.assertEqual(upgrade.undo(home, out=messages.append), 1)
            self.assertIn("cannot be read safely", messages[0])

    def test_unsafe_windows_path_prints_one_error_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            before = self._outside_snapshot(home)
            with mock.patch.object(upgrade.config, "REPO_ROOT", "C:\\bad%repo"), \
                 mock.patch.object(upgrade.shutil, "which", return_value=None):
                result, messages = self._run(home, ["y", "n"], platform="win32")
            self.assertEqual(result, 1)
            self.assertEqual(messages[-1], "Upgrade could not prepare the launcher: Windows launcher paths cannot contain a double quote or percent sign.")
            self.assertFalse(os.path.exists(upgrade.launcher_path(home, "win32")))
            self.assertFalse(os.path.exists(upgrade._upgrade_path(home)))
            self.assertEqual(self._outside_snapshot(home), before)

    def test_guide_only_features_do_not_change_configuration(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            with mock.patch.object(upgrade.shutil, "which", side_effect=lambda name: "/bin/ollama" if name == "ollama" else None):
                result, messages = self._run(home, ["n", "n", "y"], platform="darwin")
            self.assertEqual(result, 0)
            self.assertIn("Next step: docs/orchestration-mcp.md#certified-gemma-local-backend", messages)
            self.assertFalse(os.path.exists(upgrade.launcher_path(home, "linux")))
            self.assertFalse(os.path.exists(os.path.join(home, ".agent-bridge", "grok", "bot-manifest.json")))

    def test_existing_hermes_is_reported_not_asked_and_preserved(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            launcher = upgrade.launcher_path(home, "linux")
            with mock.patch.object(upgrade.shutil, "which", side_effect=lambda name: "/old/hermes" if name == "hermes" else None):
                first_result, _ = self._run(home, ["y", "y", "n", "y"])
            self.assertEqual(first_result, 0)
            self._chat(home)
            with mock.patch.object(upgrade.shutil, "which", side_effect=lambda name: "/new/hermes" if name == "hermes" else None):
                result, messages = self._run(home, ["y", "2", "y"])
            self.assertEqual(result, 0)
            self.assertIn("You already have: agent_room, hermes.", messages)
            text = Path(launcher).read_text()
            self.assertIn("--hermes-executable", text)
            self.assertIn("/new/hermes", text)
            self.assertIn("--grok-state-dir", text)
            self.assertIn("Preview: launcher will include Hermes, Grok.", messages)

    def test_windows_launcher_detects_existing_hermes_from_quoted_tokens(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            launcher = upgrade.launcher_path(home, "win32")
            store.atomic_write_bytes(launcher, upgrade.launcher_bytes(home, "win32", hermes_path="C:\\Program Files\\Hermes\\hermes.exe", grok=False))
            with mock.patch.object(upgrade.shutil, "which", return_value=None):
                states = upgrade.detect(home, platform="win32")
            self.assertEqual(states["hermes"]["state"], "on")

    def test_existing_grok_is_preserved_when_adding_hermes(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            self._chat(home)
            self._manifest(home)
            launcher = upgrade.launcher_path(home, "linux")
            store.atomic_write_bytes(launcher, upgrade.launcher_bytes(home, "linux", hermes_path=None, grok=True))
            with mock.patch.object(upgrade.shutil, "which", side_effect=lambda name: "/new/hermes" if name == "hermes" else None):
                result, messages = self._run(home, ["y", "y"])
            self.assertEqual(result, 0)
            text = Path(launcher).read_text()
            self.assertIn("--hermes-executable", text)
            self.assertIn("--grok-state-dir", text)
            self.assertIn("Preview: launcher will include Hermes, Grok.", messages)

    def _outside_snapshot(self, home: str) -> dict[str, bytes | None]:
        result: dict[str, bytes | None] = {}
        base = Path(home, ".agent-bridge")
        for path in Path(home).rglob("*"):
            if path == base or base in path.parents:
                continue
            result[str(path.relative_to(home))] = None if path.is_dir() else path.read_bytes()
        return result

    def test_all_writes_stay_under_agent_bridge(self):
        with tempfile.TemporaryDirectory() as home:
            self._receipt(home)
            Path(home, "user-file").write_bytes(b"do not touch")
            os.makedirs(Path(home, "user-dir"), exist_ok=True)
            Path(home, "user-dir", "nested").write_bytes(b"also do not touch")
            before = self._outside_snapshot(home)
            with mock.patch.object(upgrade.shutil, "which", return_value=None):
                result, _ = self._run(home, ["n", "n", "y"])
            self.assertEqual(result, 0)
            self.assertEqual(self._outside_snapshot(home), before)


if __name__ == "__main__":
    unittest.main()
