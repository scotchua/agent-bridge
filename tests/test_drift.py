"""Offline contracts for doctor drift checks and safe Codex updates.

Every test has an empty temporary PATH and HOME.  The subprocess guard makes
an accidental return to host PATH discovery fail before it can touch a CLI.
"""
from __future__ import annotations

import ast
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge import drift


def completed(text: str = "", code: int = 0):
    return subprocess.CompletedProcess([], code, text.encode(), b"")


class IsolatedDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.bin = self.home / "empty-bin"; self.bin.mkdir()
        self.config = self.home / "orchestration.json"; self.config.write_text("{}")
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.bin)}, clear=False)
        self.env.start()
        real_run = subprocess.run
        def guarded(argv, *args, **kwargs):
            for arg in argv:
                if isinstance(arg, str) and os.path.isabs(arg):
                    self.assertTrue(Path(arg).resolve().is_relative_to(self.home.resolve()), f"host subprocess path: {arg}")
            return real_run(argv, *args, **kwargs)
        self.guard = mock.patch("agent_bridge.drift.subprocess.run", side_effect=guarded)
        self.guard.start()
        def guarded_run(argv, **kwargs):
            for arg in argv:
                if isinstance(arg, str) and os.path.isabs(arg):
                    self.assertTrue(Path(arg).resolve().is_relative_to(self.home.resolve()), f"host _run path: {arg}")
            return completed()
        self.run_guard = mock.patch.object(drift, "_run", side_effect=guarded_run)
        self.run_guard.start()

    def tearDown(self) -> None:
        self.run_guard.stop(); self.guard.stop(); self.env.stop(); self.temp.cleanup()

    def prefix(self, name: str) -> Path:
        prefix = self.home / name
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib" / "node_modules" / "@openai" / "codex").mkdir(parents=True)
        for name in ("codex", "npm"):
            path = prefix / "bin" / name
            path.write_text("#!/bin/sh\nexit 0\n"); path.chmod(0o700)
        return prefix

    def bridge(self, executable: Path):
        value = mock.Mock()
        value.peer.return_value = {"executable": str(executable), "model": "good"}
        value.peer_reasoning_effort.return_value = "low"
        return value

    def runner(self, calls: list[list[str]], *, fail_staged: bool = False, fail_post: bool = False):
        live_versions: dict[str, str] = {}
        def run(argv, **kwargs):
            calls.append(list(argv))
            joined = " ".join(argv)
            if " view @openai/codex time --json" in joined:
                old = "2020-01-01T00:00:00.000Z"
                return completed(json.dumps({"1.0": old, "2.0": old, "3.0-beta.1": old}))
            if " list -g " in joined:
                return completed('{"version":"1.0"}')
            if " install -g " in joined:
                staging = argv[argv.index("--prefix") + 1]
                if "codex-staging" in staging:
                    binary = Path(staging, "bin", "codex"); binary.parent.mkdir(parents=True, exist_ok=True); binary.write_text("x"); binary.chmod(0o700)
                else:
                    live_versions[str(Path(staging).resolve())] = argv[-1].rsplit("@", 1)[1]
                return completed()
            if argv[-2:] == ["exec", "--help"] or argv[-3:] == ["exec", "resume", "--help"]:
                return completed(" ".join(drift.CODEX_FLAGS))
            if argv[-2:] == ["debug", "models"]:
                return completed(json.dumps({"models": [{"slug": "good", "supported_reasoning_levels": [{"effort": "low"}]}]}))
            if len(argv) > 1 and argv[1] == "exec":
                return completed("" if fail_staged and "codex-staging" in argv[0] else "DRIFT_DOCTOR_WORD")
            if argv[-1:] == ["--version"]:
                version = live_versions.get(str(Path(argv[0]).parent.parent.resolve()), "1.0")
                return completed("" if fail_post and version == "2.0" else version)
            return completed()
        return run

    def test_quarantine_selects_newest_stable_eligible_release(self) -> None:
        prefix, calls = self.prefix("one"), []
        result = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"), bridge=self.bridge(prefix / "bin" / "codex"), runner=self.runner(calls))
        installs = [argv[-1] for argv in calls if "install" in argv]
        self.assertIn("@openai/codex@2.0", installs)
        self.assertNotIn("@openai/codex@3.0-beta.1", installs)
        self.assertTrue(any(item["status"] == "updated" for item in result["updates"]))

    def test_young_release_and_failing_view_do_not_install(self) -> None:
        prefix, calls = self.prefix("one"), []
        report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"), bridge=self.bridge(prefix / "bin" / "codex"), runner=self.runner(calls), npm_view=lambda *a: (None, None))
        self.assertFalse(any("install" in argv for argv in calls))
        self.assertEqual(report["updates"][0]["status"], "npm_view_failed_or_no_eligible_release")

    def test_staged_failure_leaves_real_prefix_untouched(self) -> None:
        prefix, calls = self.prefix("one"), []
        drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"), bridge=self.bridge(prefix / "bin" / "codex"), runner=self.runner(calls, fail_staged=True))
        self.assertEqual([argv for argv in calls if "install" in argv and str(prefix) in argv], [])

    def test_failed_update_is_rolled_back_to_recorded_version(self) -> None:
        prefix, calls = self.prefix("one"), []
        report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"), bridge=self.bridge(prefix / "bin" / "codex"), runner=self.runner(calls, fail_post=True))
        self.assertEqual(report["updates"][0]["status"], "rolled_back")
        self.assertTrue(report["updates"][0]["rollback_checks"]["ok"])
        self.assertTrue(any(argv[-1] == "@openai/codex@1.0" for argv in calls if "install" in argv))

    def test_post_switch_failure_without_previous_is_not_updated_or_prequalified(self) -> None:
        prefix, calls = self.prefix("one"), []
        doctor = {"ok": True, "changed": [], "needs_you": [], "checks": {}}
        with mock.patch.object(drift, "_npm_version_for_executable", return_value=None), \
             mock.patch.object(drift, "doctor", return_value=dict(doctor)) as checked_doctor:
            report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"),
                                  bridge=self.bridge(prefix / "bin" / "codex"),
                                  runner=self.runner(calls, fail_post=True))
        self.assertEqual(report["updates"][0]["status"], "post_switch_failed_no_rollback")
        self.assertNotEqual(report["updates"][0]["status"], "updated")
        self.assertIn(str(prefix), " ".join(report["needs_you"]))
        self.assertIn("2.0", " ".join(report["needs_you"]))
        self.assertFalse(checked_doctor.call_args.kwargs["prequalified_codex"])

    def test_rollback_failure_is_reported_with_both_versions(self) -> None:
        prefix, calls = self.prefix("one"), []
        base = self.runner(calls, fail_post=True)
        def fail_rollback(argv, **kwargs):
            if "install" in argv and argv[-1] == "@openai/codex@1.0":
                calls.append(list(argv))
                return completed("", 1)
            return base(argv, **kwargs)
        report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"),
                              bridge=self.bridge(prefix / "bin" / "codex"), runner=fail_rollback)
        self.assertEqual(report["updates"][0]["status"], "rollback_failed")
        message = " ".join(report["needs_you"])
        self.assertIn("1.0", message); self.assertIn("2.0", message); self.assertIn(str(prefix), message)

    def test_post_switch_version_must_match_candidate(self) -> None:
        def wrong_version(argv, **kwargs):
            if argv[-1:] == ["--version"]: return completed("1.0")
            if "debug" in argv: return completed('{"models": []}')
            return completed(" ".join(drift.CODEX_FLAGS))
        checks = drift._post_switch_checks("/ignored", candidate="2.0", bridge=None,
                                           default_model=None, runner=wrong_version)
        self.assertFalse(checks["ok"])

    def test_old_staging_prefixes_are_pruned(self) -> None:
        root = self.home / ".agent-bridge" / "codex-staging"
        for index in range(4):
            item = root / str(index); item.mkdir(parents=True); os.utime(item, (time.time() + index, time.time() + index))
        drift._prune_staging(str(self.home))
        self.assertEqual(len(list(root.iterdir())), 2)

    def test_pinned_consumer_blocks_switch_without_writing_cache(self) -> None:
        prefix, calls = self.prefix("one"), []
        source = self.home / ".claude/plugins/cache/market/firm-tools/9.0/skills/codex-job-runner/bridge.py"
        source.parent.mkdir(parents=True); source.write_text('PINNED_CODEX_VERSION = "1.0"\n')
        before = source.read_bytes()
        report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"), bridge=self.bridge(prefix / "bin" / "codex"), runner=self.runner(calls))
        expected = "firm-tools codex-job-runner pins 1.0; update that pin to 2.0 first (source platform/firm-claude-plugins), then the nightly update will proceed."
        self.assertIn(expected, report["needs_you"]); self.assertEqual(source.read_bytes(), before)

    def test_doctor_reports_consumer_mismatch(self) -> None:
        source = self.home / ".claude/plugins/cache/market/firm-tools/9.0/skills/codex-job-runner/bridge.py"
        source.parent.mkdir(parents=True); source.write_text('PINNED_CODEX_VERSION = "1.0"\n')
        current = {"version": 1, "at": 1, "config_path": str(self.config), "executables": {"claude": {"path": None}, "codex": {"path": None}}, "ollama": {}, "gemma": {"delegate": {}, "validator": {}}, "paths": {}, "hooks": {"claude": {"present": True, "launcher_exists": True}, "codex": {"present": True, "launcher_exists": True, "trust": "recorded"}}, "codex_default_model": None, "consumers": drift._consumer_pins(str(self.home))}
        with mock.patch.object(drift, "inventory", return_value=current), mock.patch.object(drift, "_npm_version_for_executable", return_value="2.0"):
            report = drift.doctor(str(self.config), home=str(self.home))
        self.assertIn("pins 1.0; update that pin to 2.0 first", " ".join(report["needs_you"]))
        self.assertEqual(report["consumers"][0]["version"], "1.0")

    def test_doctor_reports_a_launcher_record_failure_without_aborting(self) -> None:
        current = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        with mock.patch.object(drift, "inventory", return_value=current), \
             mock.patch.object(drift.python_runtime, "record", return_value=None):
            report = drift.doctor(str(self.config), home=str(self.home),
                                  bridge=self.bridge(self.home / "codex"),
                                  runner=lambda *_args, **_kwargs: completed())
        self.assertIn("launcher_python_record_failed", report["needs_you"])

    def test_consumer_cli_prefix_compares_with_npm_version(self) -> None:
        source = self.home / ".claude/plugins/cache/market/firm-tools/9.0/skills/codex-job-runner/bridge.py"
        source.parent.mkdir(parents=True)
        for pin, matches in (("codex-cli 0.160.1", True), (" codex-cli 0.160.1 ", True),
                             ("codex-cli 0.160.2", False), ("codex-cli 0.160.1-beta.1", False)):
            with self.subTest(pin=pin):
                source.write_text(f'PINNED_CODEX_VERSION = "{pin}"\n')
                current = self.inventory()
                current["consumers"] = drift._consumer_pins(str(self.home))
                with mock.patch.object(drift, "inventory", return_value=current), \
                     mock.patch.object(drift, "_npm_version_for_executable", return_value="0.160.1"):
                    report = drift.doctor(str(self.config), home=str(self.home))
                self.assertEqual(report["checks"]["consumer_pins"]["ok"], matches)
                self.assertEqual(any("update that pin" in line for line in report["needs_you"]), not matches)
                self.assertEqual(report["consumers"][0]["version"], pin)

    def test_prefixed_consumer_still_blocks_an_update(self) -> None:
        prefix, calls = self.prefix("one"), []
        source = self.home / ".claude/plugins/cache/market/firm-tools/9.0/skills/codex-job-runner/bridge.py"
        source.parent.mkdir(parents=True)
        source.write_text('PINNED_CODEX_VERSION = "codex-cli 1.0"\n')
        report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"),
                              bridge=self.bridge(prefix / "bin" / "codex"), runner=self.runner(calls))
        self.assertIn("pins 1.0; update that pin to 2.0", " ".join(report["needs_you"]))
        self.assertFalse(any("install" in argv and any(
            Path(arg).resolve() == prefix.resolve() for arg in argv if os.path.isabs(arg))
            for argv in calls))

    def test_model_check_and_paid_check_order(self) -> None:
        missing = drift._model_check("/ignored", configured_model="missing", configured_effort="low", default_model=None, runner=lambda *a, **k: completed('{"models": []}'))
        unsupported = drift._model_check("/ignored", configured_model="good", configured_effort="high", default_model=None, runner=lambda *a, **k: completed('{"models":[{"slug":"good","supported_reasoning_levels":[{"effort":"low"}]}]}'))
        unknown = drift._model_check("/ignored", configured_model="good", configured_effort="low", default_model=None, runner=lambda *a, **k: None)
        self.assertFalse(missing["ok"]); self.assertFalse(unsupported["ok"]); self.assertIsNone(unknown["ok"])
        calls: list[list[str]] = []
        def catalog_only(argv, **kwargs):
            calls.append(argv)
            return completed('{"models": []}') if "debug" in argv else completed(" ".join(drift.CODEX_FLAGS))
        checks = drift._staging_checks("/ignored", bridge=self.bridge(Path("/ignored")), default_model=None, runner=catalog_only)
        self.assertFalse(checks["ok"]); self.assertFalse(any("DRIFT_DOCTOR_WORD" in str(call) for call in calls))

    def test_doctor_skips_both_codex_paid_checks_when_model_check_fails(self) -> None:
        current = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        def free_failure(argv, **kwargs):
            if "debug" in argv: return completed('{"models": []}')
            return completed(" ".join(drift.CODEX_FLAGS if "codex" in argv[0] else drift.CLAUDE_FLAGS))
        with mock.patch.object(drift, "inventory", return_value=current), \
             mock.patch.object(drift, "run_cli_check") as paid:
            report = drift.doctor(str(self.config), home=str(self.home), bridge=self.bridge(self.home / "codex"), runner=free_failure)
        self.assertFalse(any(call.args[0] == "codex" for call in paid.call_args_list))
        self.assertEqual(report["checks"]["codex"]["reason"], "model_check_failed")
        self.assertEqual(report["checks"]["codex_default"]["reason"], "model_check_failed")
        self.assertNotIn("codex", report["requalified"])

    def test_flag_check_uses_whole_flags_and_constants_cover_launchers(self) -> None:
        check = drift._help_has_flags("/ignored", runner=lambda argv, **k: completed("--json"))
        self.assertFalse(check["ok"]); self.assertIn("--model", check["missing"])
        collision = drift._help_has_flags("/ignored", runner=lambda argv, **k: completed("--model-provider"))
        self.assertIn("--model", collision["missing"])
        codex, claude = self.launcher_flags()
        self.assertEqual(codex, set(drift.CODEX_FLAGS))
        self.assertEqual(claude, set(drift.CLAUDE_FLAGS))

    def test_missing_passed_flag_from_constant_fails_source_contract(self) -> None:
        codex, _ = self.launcher_flags()
        declared = set(drift.CODEX_FLAGS) - {"--model"}
        self.assertFalse(codex <= declared)

    def test_isolation_guard_trips_on_host_path(self) -> None:
        with self.assertRaises(AssertionError): subprocess.run(["/outside-temporary-root/codex", "--version"], check=False)
        with self.assertRaises(AssertionError): drift._run(["/outside-temporary-root/codex", "--version"], cwd=str(self.home), text="")

    def test_non_writable_prefix_is_skipped(self) -> None:
        prefix = self.prefix("readonly"); prefix.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"), bridge=self.bridge(prefix / "bin" / "codex"), runner=self.runner([]))
            self.assertEqual(report["updates"][0]["status"], "skipped_not_writable")
        finally: prefix.chmod(stat.S_IRWXU)

    def test_update_uses_each_prefix_npm_and_qualifies_one_candidate_once(self) -> None:
        first, second, calls = self.prefix("one"), self.prefix("two"), []
        report = drift.update(str(self.config), home=str(self.home),
                              path_env=os.pathsep.join((str(first / "bin"), str(second / "bin"))),
                              bridge=self.bridge(first / "bin" / "codex"), runner=self.runner(calls))
        direct = [argv for argv in calls if "install" in argv and "codex-staging" not in argv[argv.index("--prefix") + 1]]
        self.assertEqual({Path(argv[argv.index("--prefix") + 1]).resolve() for argv in direct}, {first.resolve(), second.resolve()})
        staged = [argv for argv in calls if "install" in argv and "codex-staging" in argv[argv.index("--prefix") + 1]]
        self.assertEqual(len(staged), 1)
        staged_execs = [argv for argv in calls if argv[0].endswith("codex-staging/2.0/bin/codex") and "--skip-git-repo-check" in argv]
        self.assertEqual(len(staged_execs), 2)
        installs = [argv for argv in calls if "install" in argv]
        self.assertTrue(all(argv[-1].startswith("@openai/codex@") for argv in installs))
        self.assertTrue(all(Path(argv[0]).name not in {"claude", "ollama"} for argv in calls))
        self.assertTrue(all(item["status"] == "updated" for item in report["updates"]))

    def test_npm_failure_does_not_rollback_or_change_install(self) -> None:
        prefix, calls = self.prefix("failure"), []
        base = self.runner(calls)
        def fail_live_install(argv, **kwargs):
            if "install" in argv and "codex-staging" not in argv[argv.index("--prefix") + 1]:
                calls.append(list(argv)); return completed("network down", 1)
            return base(argv, **kwargs)
        report = drift.update(str(self.config), home=str(self.home), path_env=str(prefix / "bin"),
                              bridge=self.bridge(prefix / "bin" / "codex"), runner=fail_live_install)
        self.assertEqual(report["updates"][0]["status"], "npm_failed")
        self.assertFalse(any(argv[-1] == "@openai/codex@1.0" for argv in calls if "install" in argv))
        self.assertIn("left untouched", " ".join(report["needs_you"]))

    def test_failed_component_is_not_absorbed_into_baseline(self) -> None:
        old, changed = self.inventory(), self.inventory(); changed["executables"]["codex"]["sha256"] = "new"
        with mock.patch.object(drift, "inventory", side_effect=[old, changed, changed]), \
             mock.patch.object(drift, "run_cli_check", return_value={"ok": False, "reason": "failed"}), \
             mock.patch.object(drift, "run_gemma_check", return_value={"ok": True}):
            drift.doctor(str(self.config), home=str(self.home)); first = drift.doctor(str(self.config), home=str(self.home)); second = drift.doctor(str(self.config), home=str(self.home))
        self.assertIn("codex", first["changed"]); self.assertIn("codex", second["changed"])

    def test_changed_identity_during_check_is_not_written_to_baseline(self) -> None:
        old, checked, replaced = self.inventory(), self.inventory(), self.inventory()
        checked["executables"]["codex"]["sha256"] = "checked"
        replaced["executables"]["codex"]["sha256"] = "replaced"
        drift._write_baseline(str(self.home), old)
        with mock.patch.object(drift, "inventory", side_effect=[checked, replaced]), \
             mock.patch.object(drift, "run_cli_check", return_value={"ok": True}):
            report = drift.doctor(str(self.config), home=str(self.home), bridge=self.bridge(self.home / "codex"))
        self.assertIn("codex", report["changed_during_checks"])
        self.assertEqual(drift._read_baseline(str(self.home))["executables"]["codex"], old["executables"]["codex"])

    def test_doctor_live_components_checks_only_codex_and_preserves_claude_baseline(self) -> None:
        old = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        current = json.loads(json.dumps(old))
        current["executables"]["claude"]["sha256"] = "claude-new"
        current["executables"]["codex"]["sha256"] = "codex-new"
        drift._write_baseline(str(self.home), old)
        calls: list[list[str]] = []
        def runner(argv, **kwargs):
            calls.append(list(argv))
            if "debug" in argv:
                return completed('{"models": [{"slug": "good", "supported_reasoning_levels": [{"effort": "low"}]}]}')
            if "--help" in argv:
                return completed(" ".join(drift.CLAUDE_FLAGS if "claude" in argv[0] else drift.CODEX_FLAGS))
            if "codex" in argv[0] and "exec" in argv:
                return completed("DRIFT_DOCTOR_WORD")
            return completed()
        with mock.patch.object(drift, "inventory", side_effect=[current, current]):
            report = drift.doctor(str(self.config), home=str(self.home), bridge=self.bridge(self.home / "codex"),
                                  runner=runner, live_components={"codex"})
        self.assertIn("codex", report["requalified"])
        self.assertFalse(any("claude" in call[0] and "exec" in call for call in calls))
        baseline = drift._read_baseline(str(self.home))
        self.assertEqual(baseline["executables"]["claude"], old["executables"]["claude"])
        self.assertEqual(baseline["executables"]["codex"], current["executables"]["codex"])

    def test_doctor_live_components_skips_gemma_when_only_claude_is_reserved(self) -> None:
        old = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        current = json.loads(json.dumps(old))
        current["executables"]["claude"]["sha256"] = "claude-new"
        current["gemma"]["delegate"] = {"path": "/new-gemma", "sha256": "new", "pin_matches": True}
        drift._write_baseline(str(self.home), old)
        def runner(argv, **kwargs):
            if "debug" in argv:
                return completed('{"models": [{"slug": "good", "supported_reasoning_levels": [{"effort": "low"}]}]}')
            if "--help" in argv:
                return completed(" ".join(drift.CLAUDE_FLAGS if "claude" in argv[0] else drift.CODEX_FLAGS))
            return completed()
        with mock.patch.object(drift, "inventory", side_effect=[current, current]), \
             mock.patch.object(drift, "run_gemma_check") as gemma:
            drift.doctor(str(self.config), home=str(self.home), bridge=self.bridge(self.home / "codex"),
                         runner=runner, live_components={"claude"})
        gemma.assert_not_called()

    def test_passing_gemma_check_records_ollama_inventory(self) -> None:
        old, current = self.inventory(), self.inventory()
        current["ollama"] = {"version": "1", "model_present": True}
        drift._write_baseline(str(self.home), old)
        with mock.patch.object(drift, "inventory", return_value=current), \
             mock.patch.object(drift, "run_gemma_check", return_value={"ok": True}) as gemma:
            report = drift.doctor(str(self.config), home=str(self.home),
                                  bridge=self.bridge(self.home / "codex"))
        gemma.assert_called_once_with(str(self.config))
        self.assertIn("gemma", report["requalified"])
        baseline = drift._read_baseline(str(self.home))
        self.assertEqual(baseline["ollama"], current["ollama"])
        self.assertNotIn("ollama", drift.changes(current, baseline))

    def test_failed_gemma_check_preserves_ollama_baseline(self) -> None:
        old, current = self.inventory(), self.inventory()
        current["ollama"] = {"version": "1", "model_present": True}
        # A passing CLI check forces a baseline merge despite Gemma's failure.
        current["executables"]["claude"]["sha256"] = "new"
        drift._write_baseline(str(self.home), old)
        with mock.patch.object(drift, "inventory", return_value=current), \
             mock.patch.object(drift, "run_cli_check", return_value={"ok": True}), \
             mock.patch.object(drift, "run_gemma_check", return_value={"ok": False}) as gemma:
            report = drift.doctor(str(self.config), home=str(self.home),
                                  bridge=self.bridge(self.home / "codex"))
        gemma.assert_called_once_with(str(self.config))
        self.assertNotIn("gemma", report["requalified"])
        baseline = drift._read_baseline(str(self.home))
        self.assertEqual(baseline["executables"]["claude"], current["executables"]["claude"])
        self.assertEqual(baseline["ollama"], old["ollama"])
        self.assertIn("ollama", drift.changes(current, baseline))

    def test_schedule_is_nightly_and_uses_launchctl_wrapper(self) -> None:
        with mock.patch.object(drift, "_schedule_launchctl") as launcher, mock.patch.object(drift, "discover_config", return_value=str(self.config)):
            path = drift.install_schedule(home=str(self.home)); data = plistlib.loads(Path(path).read_bytes()); drift.remove_schedule(home=str(self.home))
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertIn("--update", data["ProgramArguments"]); self.assertIn("--scheduled", data["ProgramArguments"]); self.assertEqual(launcher.call_count, 2)
        self.assertEqual(launcher.call_args_list[1].kwargs, {"remove": True})

    def test_requalify_only_schedule_never_updates(self) -> None:
        with mock.patch.object(drift, "_schedule_launchctl"), mock.patch.object(drift, "discover_config", return_value=str(self.config)):
            path = drift.install_schedule(home=str(self.home), update=False); data = plistlib.loads(Path(path).read_bytes()); drift.remove_schedule(home=str(self.home))
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertIn("--requalify-only", data["ProgramArguments"]); self.assertIn("--scheduled", data["ProgramArguments"])
        self.assertNotIn("--update", data["ProgramArguments"])

    def test_scheduled_requalify_only_runs_doctor_not_update(self) -> None:
        report = {"ok": True, "needs_you": [], "changed": [], "updates": []}
        with mock.patch.object(drift, "_read_baseline", return_value={"recorded": True}), \
             mock.patch.object(drift, "_home", return_value=str(self.home)), \
             mock.patch.object(drift, "update") as updater, \
             mock.patch.object(drift, "doctor", return_value=report) as doc:
            code = drift.main(["--scheduled", "--requalify-only", "--quiet", "--config", str(self.config)])
        self.assertEqual(code, 0); updater.assert_not_called()
        self.assertEqual(doc.call_args.args, (str(self.config),))

    def test_requalify_only_refuses_to_combine_with_update(self) -> None:
        with mock.patch.object(drift, "update") as updater, mock.patch.object(drift, "doctor") as doc, \
             self.assertRaises(SystemExit) as stop:
            drift.main(["--scheduled", "--requalify-only", "--update", "--config", str(self.config)])
        self.assertEqual(stop.exception.code, 2); updater.assert_not_called(); doc.assert_not_called()

    def test_install_schedule_flag_passes_requalify_only_through(self) -> None:
        with mock.patch.object(drift, "install_schedule", return_value="plist") as install:
            drift.main(["--install-schedule", "--requalify-only"])
        self.assertEqual(install.call_args.kwargs, {"update": False})

    def test_quick_check_missing_baseline_is_attention_not_drift(self) -> None:
        current = self.inventory()
        with mock.patch.object(drift, "inventory", return_value=current):
            report = drift.quick_check(str(self.config), home=str(self.home))
        self.assertEqual(report["changed"], [])
        self.assertEqual(report["baseline"], "missing")
        self.assertEqual(report["attention"], ["Run setup_bridge.py doctor once to record a baseline."])
        self.assertTrue(report["ok"])

    def quick_with_baseline(self, current: dict, baseline: dict, *, config: str) -> dict:
        drift._quick_cache.clear()
        with mock.patch.object(drift, "inventory", return_value=current), \
             mock.patch.object(drift, "_read_baseline", return_value=baseline), \
             mock.patch.object(drift, "discover_config", return_value=config):
            return drift.quick_check(str(self.config), home=str(self.home))

    def test_quick_check_version_change_alone_needs_no_attention(self) -> None:
        baseline = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        current = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        current["executables"]["codex"]["version"] = "2"
        report = self.quick_with_baseline(current, baseline, config=str(self.config))
        self.assertFalse(report["ok"])
        self.assertEqual(report["changed"], ["codex"])
        self.assertEqual(report["attention"], [])

    def test_quick_check_only_vendor_tree_content_move_is_informational(self) -> None:
        for name in ("claude", "codex"):
            with self.subTest(name=name):
                baseline = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
                baseline["executables"][name].update(
                    native_kind="vendor_tree_v1" if name == "codex" else "file_v1", native_sha256="native")
                current = json.loads(json.dumps(baseline))
                current["executables"][name]["path"] = str(self.home / "moved" / name)
                report = self.quick_with_baseline(current, baseline, config=str(self.config))
                self.assertEqual(report["changed"], [] if name == "codex" else [name])
                self.assertEqual(report["moved"], [name] if name == "codex" else [])
                self.assertEqual(report["ok"], name == "codex")
                self.assertEqual(report["attention"], [])

    def test_quick_check_content_changes_and_unprovable_moves_still_hold(self) -> None:
        for path_changed in (False, True):
            for field, value in (("native_sha256", "different"), ("native_sha256", None),
                                 ("sha256", "different"), ("version", "different")):
                with self.subTest(path_changed=path_changed, field=field, value=value):
                    baseline = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
                    baseline["executables"]["codex"]["native_sha256"] = "native"
                    current = json.loads(json.dumps(baseline))
                    if path_changed:
                        current["executables"]["codex"]["path"] = str(self.home / "moved" / "codex")
                    current["executables"]["codex"][field] = value
                    report = self.quick_with_baseline(current, baseline, config=str(self.config))
                    self.assertEqual(report["changed"], ["codex"])
                    self.assertEqual(report["moved"], [])
                    self.assertFalse(report["ok"])

    def test_quick_check_move_requires_both_native_hashes(self) -> None:
        for before, after in ((None, "native"), ("native", None), (None, None)):
            with self.subTest(before=before, after=after):
                baseline = self.inventory(codex=self.home / "codex")
                baseline["executables"]["codex"]["native_sha256"] = before
                current = json.loads(json.dumps(baseline))
                current["executables"]["codex"].update(path=str(self.home / "moved"), native_sha256=after)
                report = self.quick_with_baseline(current, baseline, config=str(self.config))
                self.assertEqual(report["changed"], ["codex"])
                self.assertFalse(report["ok"])
                self.assertEqual(report["moved"], [])

    def test_quick_check_legacy_baseline_requires_same_path(self) -> None:
        for moved in (False, True):
            with self.subTest(moved=moved):
                baseline = self.inventory(codex=self.home / "codex")
                current = json.loads(json.dumps(baseline))
                current["executables"]["codex"]["native_sha256"] = "native"
                if moved:
                    current["executables"]["codex"]["path"] = str(self.home / "moved")
                report = self.quick_with_baseline(current, baseline, config=str(self.config))
                self.assertEqual(report["changed"], ["codex"] if moved else [])
                self.assertEqual(report["ok"], not moved)
                self.assertEqual(report["moved"], [])

    def test_doctor_records_move_and_legacy_native_hash_without_paid_checks(self) -> None:
        for moved in (False, True):
            with self.subTest(moved=moved):
                baseline = self.inventory(codex=self.home / "codex")
                if moved:
                    baseline["executables"]["codex"].update(native_kind="vendor_tree_v1", native_sha256="native")
                current = json.loads(json.dumps(baseline))
                current["executables"]["codex"]["native_sha256"] = "native"
                if moved:
                    current["executables"]["codex"]["path"] = str(self.home / "moved")
                drift._write_baseline(str(self.home), baseline)
                with mock.patch.object(drift, "inventory", return_value=current), \
                     mock.patch.object(drift, "run_cli_check") as paid:
                    report = drift.doctor(str(self.config), home=str(self.home))
                paid.assert_not_called()
                self.assertEqual(report["changed"], [])
                self.assertEqual(report["moved"], ["codex"] if moved else [])
                self.assertEqual(drift._read_baseline(str(self.home))["executables"], current["executables"])

    def test_doctor_rejects_native_payload_replaced_during_checks(self) -> None:
        baseline = self.inventory(codex=self.home / "codex")
        baseline["executables"]["codex"]["native_sha256"] = "old"
        current = json.loads(json.dumps(baseline))
        current["executables"]["codex"]["native_sha256"] = "checked"
        replaced = json.loads(json.dumps(current))
        replaced["executables"]["codex"]["native_sha256"] = "replaced"
        drift._write_baseline(str(self.home), baseline)
        with mock.patch.object(drift, "inventory", side_effect=[current, replaced]), \
             mock.patch.object(drift, "run_cli_check", return_value={"ok": True}):
            report = drift.doctor(str(self.config), home=str(self.home))
        self.assertIn("codex", report["changed_during_checks"])
        self.assertEqual(drift._read_baseline(str(self.home))["executables"]["codex"], baseline["executables"]["codex"])

    def npm_launcher(self, prefix: str = "") -> Path:
        root = self.home / prefix / "node_modules" / "@openai" / "codex"
        (root / "bin").mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text('{"name":"@openai/codex"}')
        launcher = root / "bin" / "codex.js"
        launcher.write_text((ROOT / "tests/fixtures/codex_npm_launcher.js").read_text())
        return launcher

    def test_native_fingerprint_follows_optional_package_and_vendor_fallback(self) -> None:
        launcher = self.npm_launcher()
        for target in ("aarch64-apple-darwin", "x86_64-apple-darwin", "aarch64-unknown-linux-musl",
                       "x86_64-unknown-linux-musl", "aarch64-pc-windows-msvc", "x86_64-pc-windows-msvc"):
            for optional in (False, True):
                with self.subTest(target=target, optional=optional):
                    root = self.home / "optional" if optional else launcher.parent.parent
                    package_json = root / "package.json"
                    native = root / "vendor" / target / "bin" / ("codex.exe" if "windows" in target else "codex")
                    native.parent.mkdir(parents=True, exist_ok=True)
                    native.write_bytes(b"synthetic native payload")
                    decoy = root / "vendor" / target / "codex" / native.name
                    decoy.parent.mkdir(parents=True, exist_ok=True)
                    decoy.write_bytes(b"wrong payload")
                    response = {"target": target, "package_json": str(package_json) if optional else None}
                    probe = mock.Mock(return_value=completed(json.dumps(response)))
                    with mock.patch.object(drift.shutil, "which", return_value=str(self.bin / "node")):
                        from agent_bridge.execution.codex_promotion import tree_digest
                        self.assertEqual(drift._codex_native_sha256(str(launcher), probe),
                                         tree_digest(root / "vendor" / target))
                        native.unlink()
                        self.assertIsNone(drift._codex_native_sha256(str(launcher), probe))
                    self.assertIn("createRequire", probe.call_args.args[0][2])
                    self.assertEqual(probe.call_args.args[0][3], str(launcher))

    def test_unresolved_native_fingerprint_is_null(self) -> None:
        launcher = self.npm_launcher()
        for result in (None, completed("", 1), completed("bad json"), completed('{"target":"unknown"}')):
            with self.subTest(result=result), \
                 mock.patch.object(drift.shutil, "which", return_value=str(self.bin / "node")):
                self.assertIsNone(drift._codex_native_sha256(str(launcher), lambda *a, **k: result))
        with mock.patch.object(drift.shutil, "which", return_value=None):
            self.assertIsNone(drift._codex_native_sha256(str(launcher)))
        launcher.write_text("// unsupported future launcher")
        probe = mock.Mock()
        self.assertIsNone(drift._codex_native_sha256(str(launcher), probe))
        probe.assert_not_called()

    def test_missing_optional_payload_does_not_select_bundled_fallback(self) -> None:
        launcher = self.npm_launcher()
        target = "aarch64-apple-darwin"
        fallback = launcher.parent.parent / "vendor" / target / "bin" / "codex"
        fallback.parent.mkdir(parents=True)
        fallback.write_bytes(b"bundled decoy")
        response = {"target": target, "package_json": str(self.home / "optional" / "package.json")}
        with mock.patch.object(drift.shutil, "which", return_value=str(self.bin / "node")):
            self.assertIsNone(drift._codex_native_sha256(
                str(launcher), lambda *a, **k: completed(json.dumps(response))))

    def test_direct_native_binary_uses_its_own_digest(self) -> None:
        native = self.home / "codex"
        for magic in (b"\x7fELF", b"MZ00", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
            with self.subTest(magic=magic):
                native.write_bytes(magic + b"synthetic payload")
                self.assertEqual(drift._codex_native_sha256(str(native)), drift._sha256(str(native)))
        native.write_bytes(b"#!/bin/sh\n# unresolved external wrapper")
        self.assertIsNone(drift._codex_native_sha256(str(native)))

    def test_inventory_records_native_hash_and_null_when_unresolved(self) -> None:
        launcher = self.npm_launcher()
        for native in ("native", None):
            with self.subTest(native=native), \
                 mock.patch.object(drift, "_codex_native_identity", return_value=("vendor_tree_v1", native)), \
                 mock.patch.object(drift, "_ollama", return_value={}):
                current = drift.inventory(str(self.config), home=str(self.home),
                                          bridge=self.bridge(launcher), runner=self.runner([]))
            entry = current["executables"]["codex"]
            self.assertIn("native_sha256", entry)
            self.assertEqual(entry["native_sha256"], native)
            self.assertEqual(entry["native_kind"], "vendor_tree_v1")
            self.assertEqual(entry["sha256"], drift._sha256(str(launcher)))

    def npm_inventory(self, launcher: Path) -> dict:
        probe = completed(json.dumps({"target": "aarch64-apple-darwin", "package_json": None}))
        with mock.patch.object(drift, "_executable", side_effect=lambda name, _: str(launcher.resolve()) if name == "codex" else None), \
             mock.patch.object(drift.shutil, "which", return_value=str(self.bin / "node")), \
             mock.patch.object(drift, "_ollama", return_value={}), \
             mock.patch.object(drift, "_hook_state", return_value={}):
            return drift.inventory(str(self.config), home=str(self.home), bridge=self.bridge(launcher),
                                   runner=lambda argv, **kw: probe if "-e" in argv else completed("1.0"))

    def vendor_tree(self, launcher: Path) -> Path:
        tree = launcher.parent.parent / "vendor" / "aarch64-apple-darwin"
        for name in ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg",
                     "codex-resources/zsh/bin/zsh", "codex-resources/voice/bin/codex-voice-host",
                     "codex-resources/voice/lib/one.dylib"):
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic native payload")
        return tree

    def test_vendor_helpers_changed_at_same_path_or_move_still_hold(self) -> None:
        first, second = self.npm_launcher("first"), self.npm_launcher("second")
        tree, moved_tree = self.vendor_tree(first), self.vendor_tree(second)
        baseline = self.npm_inventory(first)
        report = self.quick_with_baseline(self.npm_inventory(second), baseline, config=str(self.config))
        self.assertEqual(report["changed"], [])
        self.assertEqual(report["moved"], ["codex"])
        for relative in ("bin/codex-code-mode-host", "codex-path/rg", "codex-resources/zsh/bin/zsh",
                         "codex-resources/voice/bin/codex-voice-host", "codex-resources/voice/lib/one.dylib"):
            for launcher, root in ((first, tree), (second, moved_tree)):
                with self.subTest(relative=relative, moved=launcher == second):
                    helper = root / relative
                    original = helper.read_bytes()
                    helper.write_bytes(b"changed synthetic helper")
                    current = self.npm_inventory(launcher)
                    self.assertEqual(current["executables"]["codex"]["sha256"], baseline["executables"]["codex"]["sha256"])
                    report = self.quick_with_baseline(current, baseline, config=str(self.config))
                    self.assertEqual(report["changed"], ["codex"])
                    self.assertEqual(report["moved"], [])
                    self.assertFalse(report["ok"])
                    helper.write_bytes(original)

    def test_direct_vendor_binary_helpers_changed_at_same_path_or_move_hold(self) -> None:
        trees = [self.vendor_tree(self.npm_launcher(prefix)) for prefix in ("first", "second")]
        for tree in trees:
            (tree / "bin/codex").write_bytes(b"\xcf\xfa\xed\xfe synthetic codex")
            (tree / "codex-package.json").write_text('{"name":"synthetic codex"}')
        baseline = self.npm_inventory(trees[0] / "bin/codex")
        entry = baseline["executables"]["codex"]
        unchanged = self.quick_with_baseline(self.npm_inventory(trees[1] / "bin/codex"), baseline,
                                             config=str(self.config))
        self.assertEqual(unchanged["changed"], [])
        self.assertEqual(unchanged["moved"], ["codex"])
        for tree in trees:
            for relative in ("bin/codex-code-mode-host", "codex-path/rg", "codex-package.json"):
                with self.subTest(moved=tree == trees[1], relative=relative):
                    helper = tree / relative
                    original = helper.read_bytes()
                    helper.write_bytes(b"different synthetic helper")
                    current = self.npm_inventory(tree / "bin/codex")
                    self.assertEqual(current["executables"]["codex"]["sha256"], entry["sha256"])
                    report = self.quick_with_baseline(current, baseline, config=str(self.config))
                    self.assertEqual(report["changed"], ["codex"])
                    self.assertEqual(report["moved"], [])
                    self.assertFalse(report["ok"])
                    helper.write_bytes(original)
        self.assertEqual(entry["native_kind"], "vendor_tree_v1")

    def test_direct_vendor_binary_and_npm_launcher_share_tree_digest(self) -> None:
        launcher = self.npm_launcher()
        tree = self.vendor_tree(launcher)
        native = tree / "bin/codex"
        native.write_bytes(b"\xcf\xfa\xed\xfe synthetic codex")
        for marker in (False, True):
            with self.subTest(marker=marker):
                if marker:
                    (tree / "codex-package.json").write_text("{}")
                launched = self.npm_inventory(launcher)["executables"]["codex"]
                direct = self.npm_inventory(native)["executables"]["codex"]
                self.assertIsNotNone(direct["native_sha256"])
                self.assertEqual(direct["native_sha256"], launched["native_sha256"])
                self.assertEqual(direct["native_kind"], "vendor_tree_v1")
                self.assertEqual(drift._codex_native_sha256(str(native)), launched["native_sha256"])

    def test_direct_native_package_uses_grandparent_root(self) -> None:
        outer = self.home / "outer"
        tree = outer / "package"
        native = tree / "bin/codex"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"MZ00 synthetic codex")
        for root in (outer, tree):
            (root / "codex-package.json").write_text("{}")
        entry = self.npm_inventory(native)["executables"]["codex"]
        self.assertEqual(entry["native_kind"], "vendor_tree_v1")
        self.assertEqual(entry["native_sha256"], drift._native_tree_sha256(tree))
        deep = tree / "one/two/three/four/codex"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(native.read_bytes())
        deep_entry = self.npm_inventory(deep)["executables"]["codex"]
        self.assertEqual(deep_entry["native_kind"], "file_v1")
        self.assertEqual(deep_entry["native_sha256"], drift._sha256(str(deep)))

    def test_direct_vendor_binary_bin_marker_does_not_hide_helper_changes(self) -> None:
        trees = [self.vendor_tree(self.npm_launcher(prefix)) for prefix in ("first", "second")]
        for tree in trees:
            (tree / "bin/codex").write_bytes(b"MZ00 synthetic codex")
            (tree / "bin/codex-package.json").write_text("{}")
        baseline = self.npm_inventory(trees[0] / "bin/codex")
        for tree in trees:
            with self.subTest(moved=tree == trees[1]):
                helper = tree / "codex-path/rg"
                original = helper.read_bytes()
                helper.write_bytes(b"different synthetic helper")
                current = self.npm_inventory(tree / "bin/codex")
                report = self.quick_with_baseline(current, baseline, config=str(self.config))
                self.assertEqual(report["changed"], ["codex"])
                self.assertEqual(report["moved"], [])
                self.assertFalse(report["ok"])
                helper.write_bytes(original)
        for tree in trees:
            entry = self.npm_inventory(tree / "bin/codex")["executables"]["codex"]
            self.assertEqual(entry["native_kind"], "vendor_tree_v1")
            self.assertEqual(entry["native_sha256"], drift._native_tree_sha256(tree))
        marker = trees[0] / "bin/codex-package.json"
        marker.write_text('{"extra":"marker changed"}')
        self.assertEqual(drift.changes(self.npm_inventory(trees[0] / "bin/codex"), baseline), ["codex"])

    def test_direct_native_marker_outside_grandparent_does_not_qualify(self) -> None:
        for relative, marker_relative in (("bin/codex", "bin/codex-package.json"),
                                          ("bin/deeper/codex", "codex-package.json"),
                                          ("other/codex", "codex-package.json"),
                                          ("bin/other", "codex-package.json")):
            with self.subTest(relative=relative, marker=marker_relative):
                binaries = []
                for prefix in ("first", "second"):
                    root = self.home / prefix / relative.replace("/", "-")
                    native = root / relative
                    native.parent.mkdir(parents=True)
                    native.write_bytes(b"MZ00 synthetic codex")
                    (root / marker_relative).write_text("{}")
                    entry = self.npm_inventory(native)["executables"]["codex"]
                    self.assertEqual(entry["native_kind"], "file_v1")
                    self.assertEqual(entry["native_sha256"], drift._sha256(str(native)))
                    binaries.append(native)
                baseline = self.npm_inventory(binaries[0])
                report = self.quick_with_baseline(self.npm_inventory(binaries[1]), baseline,
                                                 config=str(self.config))
                self.assertEqual(report["changed"], ["codex"])
                self.assertEqual(report["moved"], [])
                self.assertFalse(report["ok"])

    def test_identical_generic_native_shim_moves_hold(self) -> None:
        for shim in ("scoop/codex.exe", ".bunx/codex", "volta/volta-shim"):
            with self.subTest(shim=shim):
                binaries = [self.home / prefix / shim for prefix in ("first", "second")]
                for index, binary in enumerate(binaries):
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(b"MZ00 identical generic shim")
                    binary.with_suffix(".shim").write_text(f"path = synthetic-install-{index}")
                baseline = self.npm_inventory(binaries[0])
                current = self.npm_inventory(binaries[1])
                before, after = (value["executables"]["codex"] for value in (baseline, current))
                self.assertEqual(after["native_sha256"], before["native_sha256"])
                self.assertEqual(drift.changes(self.npm_inventory(binaries[0]), baseline), [])
                report = self.quick_with_baseline(current, baseline, config=str(self.config))
                self.assertEqual(report["changed"], ["codex"])
                self.assertEqual(report["moved"], [])
                self.assertFalse(report["ok"])
                self.assertEqual(after["native_kind"], "file_v1")
                binaries[0].write_bytes(b"MZ00 changed generic shim")
                self.assertEqual(drift.changes(self.npm_inventory(binaries[0]), baseline), ["codex"])

    def test_file_and_legacy_native_kinds_never_prove_moves(self) -> None:
        entry = {"path": "first", "version": "1", "sha256": "file", "native_sha256": "native"}
        for kind in (None, "binary_v1", "file_v1", "vendor_tree_v1"):
            with self.subTest(kind=kind):
                before = {**entry, **({"native_kind": kind} if kind else {})}
                self.assertTrue(drift._same_executable(before, before))
                self.assertEqual(drift._same_executable({**before, "path": "second"}, before),
                                 kind == "vendor_tree_v1")

    def test_round_zero_baseline_upgrades_only_at_same_path(self) -> None:
        for moved in (False, True):
            with self.subTest(moved=moved):
                baseline = self.inventory(codex=self.home / "codex")
                baseline["executables"]["codex"]["native_sha256"] = "old-single-file"
                current = json.loads(json.dumps(baseline))
                current["executables"]["codex"].update(native_sha256="new-tree", native_kind="vendor_tree_v1")
                if moved:
                    current["executables"]["codex"]["path"] = str(self.home / "moved")
                report = self.quick_with_baseline(current, baseline, config=str(self.config))
                self.assertEqual(report["changed"], ["codex"] if moved else [])
                self.assertEqual(report["moved"], [])
                self.assertEqual(report["ok"], not moved)
                if not moved:
                    drift._write_baseline(str(self.home), baseline)
                    with mock.patch.object(drift, "inventory", return_value=current), \
                         mock.patch.object(drift, "run_cli_check") as paid:
                        drift.doctor(str(self.config), home=str(self.home))
                    paid.assert_not_called()
                    self.assertEqual(drift._read_baseline(str(self.home))["executables"], current["executables"])
                for field in ("sha256", "version"):
                    changed = json.loads(json.dumps(current))
                    changed["executables"]["codex"][field] = "real change"
                    self.assertEqual(drift.changes(changed, baseline), ["codex"])

    def test_unknown_executable_identity_fields_fail_closed(self) -> None:
        entry = {"path": "same", "version": "1", "sha256": "shim", "native_sha256": "native"}
        for before, after in ((entry, {**entry, "future": "value"}),
                              ({**entry, "future": "value"}, entry),
                              ({**entry, "future": "one"}, {**entry, "future": "two"}),
                              (entry, {**entry, "future": None})):
            with self.subTest(before=before, after=after):
                self.assertFalse(drift._same_executable(after, before))
        self.assertFalse(drift._same_executable({**entry, "native_kind": ["unknown"]}, entry))

    def test_claude_native_proof_requires_native_magic(self) -> None:
        path = self.home / "claude"
        for contents, native in ((b"#!/bin/sh\nexternal-claude", False),
                                 (b"\xcf\xfa\xed\xfe synthetic binary", True)):
            with self.subTest(native=native):
                path.write_bytes(contents)
                with mock.patch.object(drift, "_ollama", return_value={}):
                    baseline = drift.inventory(str(self.config), home=str(self.home),
                                               bridge=self.bridge(path), runner=self.runner([]))
                entry = baseline["executables"]["claude"]
                self.assertEqual(entry["native_sha256"], entry["sha256"] if native else None)
                current = json.loads(json.dumps(baseline))
                current["executables"]["claude"]["path"] = str(self.home / "moved-claude")
                self.assertEqual(drift.changes(current, baseline), ["claude"])

    def test_claude_native_move_holds(self) -> None:
        binaries = [self.home / name / "claude" for name in ("first", "second")]
        inventories = []
        for binary in binaries:
            binary.parent.mkdir()
            binary.write_bytes(b"\xcf\xfa\xed\xfe identical synthetic claude")
            with mock.patch.object(drift, "_executable", side_effect=lambda name, _: str(binary) if name == "claude" else None), \
                 mock.patch.object(drift, "_ollama", return_value={}), \
                 mock.patch.object(drift, "_hook_state", return_value={}):
                inventories.append(drift.inventory(str(self.config), home=str(self.home),
                                                   bridge=self.bridge(binary), runner=self.runner([])))
        before, after = (value["executables"]["claude"] for value in inventories)
        self.assertEqual(after["native_sha256"], before["native_sha256"])
        report = self.quick_with_baseline(inventories[1], inventories[0], config=str(self.config))
        self.assertEqual(report["changed"], ["claude"])
        self.assertEqual(report["moved"], [])
        self.assertFalse(report["ok"])
        self.assertEqual(after["native_kind"], "file_v1")

    def test_round_zero_claude_baseline_upgrades_only_at_same_path(self) -> None:
        for native in (False, True):
            for moved in (False, True):
                with self.subTest(native=native, moved=moved):
                    baseline = self.inventory(claude=self.home / "claude")
                    baseline["executables"]["claude"]["native_sha256"] = "claude"
                    current = json.loads(json.dumps(baseline))
                    current["executables"]["claude"].update(native_kind="file_v1",
                                                          native_sha256="claude" if native else None)
                    if moved:
                        current["executables"]["claude"]["path"] = str(self.home / "moved-claude")
                    report = self.quick_with_baseline(current, baseline, config=str(self.config))
                    self.assertEqual(report["changed"], ["claude"] if moved else [])
                    self.assertEqual(report["moved"], [])
                    self.assertEqual(report["ok"], not moved)
                    for field in ("sha256", "version"):
                        changed = json.loads(json.dumps(current))
                        changed["executables"]["claude"][field] = "real change"
                        self.assertEqual(drift.changes(changed, baseline), ["claude"])
                    if not moved:
                        drift._write_baseline(str(self.home), baseline)
                        with mock.patch.object(drift, "inventory", return_value=current), \
                             mock.patch.object(drift, "run_cli_check") as paid:
                            drift.doctor(str(self.config), home=str(self.home))
                        paid.assert_not_called()
                        self.assertEqual(drift._read_baseline(str(self.home))["executables"], current["executables"])

    def test_transient_same_path_native_probe_failure_is_not_cached(self) -> None:
        for moved in (False, True):
            for other_change in (False, True):
                with self.subTest(moved=moved, other_change=other_change):
                    baseline = self.inventory(codex=self.home / "codex")
                    baseline["executables"]["codex"].update(native_kind="vendor_tree_v1", native_sha256="tree")
                    failed = json.loads(json.dumps(baseline))
                    failed["executables"]["codex"]["native_sha256"] = None
                    if moved:
                        failed["executables"]["codex"]["path"] = str(self.home / "moved")
                    if other_change:
                        failed["executables"]["codex"]["sha256"] = "changed shim"
                    drift._quick_cache.clear()
                    with mock.patch.object(drift, "inventory", side_effect=[failed, baseline]) as inventory, \
                         mock.patch.object(drift, "_read_baseline", return_value=baseline):
                        first = drift.quick_check(str(self.config), home=str(self.home))
                        second = drift.quick_check(str(self.config), home=str(self.home))
                    self.assertEqual(first["changed"], ["codex"])
                    self.assertEqual(second["changed"], ["codex"] if moved or other_change else [])
                    self.assertEqual(inventory.call_count, 1 if moved or other_change else 2)

    def test_native_tree_rehashes_every_inventory_and_tree_change(self) -> None:
        from agent_bridge.execution import codex_promotion
        launcher = self.npm_launcher(); tree = self.vendor_tree(launcher)
        helper = tree / "codex-path/rg"
        with mock.patch.object(codex_promotion, "tree_digest", wraps=codex_promotion.tree_digest) as digest:
            before = drift._native_tree_sha256(tree)
            self.assertIsNotNone(before)
            self.assertEqual(drift._native_tree_sha256(tree), before)
            self.assertEqual(digest.call_count, 2)
            info = helper.stat()
            helper.write_bytes(b"x" * info.st_size)
            os.utime(helper, ns=(info.st_atime_ns, info.st_mtime_ns))
            after = drift._native_tree_sha256(tree)
            self.assertNotEqual(after, before)
            self.assertEqual(digest.call_count, 3)
            for action in (lambda: helper.rename(helper.with_name("renamed")),
                           lambda: (tree / "extra").write_bytes(b"new"),
                           lambda: (tree / "extra").unlink(),
                           lambda: (tree / "empty-directory").mkdir()):
                previous = after
                action()
                after = drift._native_tree_sha256(tree)
                self.assertIsNotNone(after)
                self.assertNotEqual(after, previous)
            self.assertEqual(digest.call_count, 7)
            if os.name == "posix":
                (tree / "extra-mode").write_bytes(b"mode")
                previous = drift._native_tree_sha256(tree)
                (tree / "extra-mode").chmod(0o500)
                self.assertNotEqual(drift._native_tree_sha256(tree), previous)
                self.assertEqual(digest.call_count, 9)

    def test_windows_coarse_stat_semantics_cannot_reuse_stale_tree_digest(self) -> None:
        from types import SimpleNamespace
        launcher = self.npm_launcher()
        tree = self.vendor_tree(launcher)
        helper = tree / "codex-path/rg"
        real_lstat = Path.lstat
        creation_times = {}
        def windows_lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            creation = creation_times.setdefault(str(path), info.st_ctime_ns)
            return SimpleNamespace(st_size=info.st_size, st_mtime_ns=info.st_mtime_ns // 2_000_000_000 * 2_000_000_000,
                                   st_ctime_ns=creation, st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode)
        with mock.patch.object(Path, "lstat", windows_lstat):
            baseline = self.npm_inventory(launcher)
            signature = drift._native_tree_signature(tree.resolve())
            info = helper.stat()
            helper.write_bytes(b"x" * info.st_size)
            os.utime(helper, ns=(info.st_atime_ns, info.st_mtime_ns))
            self.assertEqual(drift._native_tree_signature(tree.resolve()), signature)
            current = self.npm_inventory(launcher)
        self.assertNotEqual(current["executables"]["codex"]["native_sha256"],
                            baseline["executables"]["codex"]["native_sha256"])
        report = self.quick_with_baseline(current, baseline, config=str(self.config))
        self.assertEqual(report["changed"], ["codex"])
        self.assertEqual(report["moved"], [])
        self.assertFalse(report["ok"])

    def test_native_tree_read_failure_and_mutation_during_hash_are_unprovable(self) -> None:
        from agent_bridge.execution import codex_promotion
        tree = self.vendor_tree(self.npm_launcher())
        self.assertIsNotNone(drift._native_tree_sha256(tree))
        with mock.patch.object(drift.os, "scandir", side_effect=PermissionError("unreadable")):
            self.assertIsNone(drift._native_tree_sha256(tree))
        with mock.patch.object(codex_promotion, "tree_digest", side_effect=PermissionError("unreadable")):
            self.assertIsNone(drift._native_tree_sha256(tree))
        original = codex_promotion.tree_digest
        def mutating(root):
            value = original(root)
            (root / "new-helper").write_bytes(b"changed during hash")
            return value
        with mock.patch.object(codex_promotion, "tree_digest", side_effect=mutating):
            self.assertIsNone(drift._native_tree_sha256(tree))
        self.assertIsNotNone(drift._native_tree_sha256(tree))

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks and FIFO")
    def test_native_tree_links_modes_and_refused_types(self) -> None:
        from agent_bridge.execution.codex_promotion import tree_digest
        tree = self.vendor_tree(self.npm_launcher())
        before = drift._native_tree_sha256(tree)
        helper = tree / "codex-path/rg"
        helper.chmod(0o500)
        after = drift._native_tree_sha256(tree)
        self.assertNotEqual(before, after)
        self.assertEqual(after, tree_digest(tree))
        link = tree / "link"
        link.symlink_to("bin/codex")
        before = drift._native_tree_sha256(tree)
        self.assertEqual(before, tree_digest(tree))
        link.unlink(); link.symlink_to("codex-path/rg")
        self.assertNotEqual(drift._native_tree_sha256(tree), before)
        for target in ("missing", "../outside", str(helper)):
            link.unlink(); link.symlink_to(target)
            self.assertIsNone(drift._native_tree_sha256(tree))
        link.unlink()
        os.mkfifo(tree / "fifo")
        self.assertIsNone(drift._native_tree_sha256(tree))

    def test_quick_check_conditions_that_cannot_repair_themselves_need_attention(self) -> None:
        baseline = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        config = str(self.config)
        cases = {}
        missing_path = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        missing_path["paths"] = {"state_root": {"path": str(self.home / "gone"), "exists": False}}
        cases["missing_path"] = (missing_path, config, "Configured path state_root is missing")
        cases["missing_config"] = (self.inventory(codex=self.home / "codex", claude=self.home / "claude"),
                                   None, "No installed orchestration config")
        for name in ("delegate", "validator"):
            pin = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
            pin["gemma"][name] = {"path": "/configured/gemma", "pin_matches": False}
            cases["pin_" + name] = (pin, config, f"Gemma {name} no longer matches its pinned checksum")
        for name in ("claude", "codex"):
            gone = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
            gone["executables"][name]["path"] = None
            cases["uninstalled_" + name] = (gone, config, f"{name.capitalize()} is no longer installed")
        for label, (current, resolved, message) in cases.items():
            with self.subTest(label=label):
                report = self.quick_with_baseline(current, baseline, config=resolved)
                self.assertFalse(report["ok"])
                self.assertEqual(len(report["attention"]), 1)
                self.assertIn(message, report["attention"][0])

    def test_scheduled_baseline_uses_only_free_checks(self) -> None:
        current = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        calls: list[list[str]] = []
        def free_runner(argv, **kwargs):
            calls.append(argv)
            if "debug" in argv:
                return completed('{"models": [{"slug": "good", "supported_reasoning_levels": [{"effort": "low"}]}]}')
            return completed(" ".join(drift.CODEX_FLAGS if "codex" in argv[0] else drift.CLAUDE_FLAGS))
        with mock.patch.object(drift, "inventory", return_value=current), \
             mock.patch.object(drift, "run_cli_check") as paid, \
             mock.patch.object(drift, "run_gemma_check") as gemma:
            report = drift.scheduled_baseline(str(self.config), home=str(self.home),
                                              bridge=self.bridge(self.home / "codex"), runner=free_runner)
        self.assertTrue(report["recorded"])
        self.assertIsNotNone(drift._read_baseline(str(self.home)))
        paid.assert_not_called(); gemma.assert_not_called()
        self.assertFalse(any("DRIFT_DOCTOR_WORD" in " ".join(call) for call in calls))

    def test_scheduled_baseline_requires_passing_free_checks(self) -> None:
        current = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
        cases = (
            ("codex_flags", {"ok": False, "missing": ["--model"]}, None, None, "Codex free flag check failed"),
            ("claude_flags", None, {"ok": False, "missing": ["--permission-mode"]}, None, "Claude free flag check failed"),
            ("codex_models", None, None, {"ok": False, "reason": "model unavailable"}, "model unavailable"),
        )
        for expected, codex_flags, claude_flags, models, message in cases:
            with self.subTest(expected=expected), \
                 mock.patch.object(drift, "inventory", return_value=current), \
                 mock.patch.object(drift, "_help_has_flags", return_value=codex_flags or {"ok": True}), \
                 mock.patch.object(drift, "_claude_help_has_flags", return_value=claude_flags or {"ok": True}), \
                 mock.patch.object(drift, "_model_check", return_value=models or {"ok": True}):
                report = drift.scheduled_baseline(str(self.config), home=str(self.home), bridge=self.bridge(self.home / "codex"))
            self.assertFalse(report["ok"])
            self.assertFalse(report["recorded"])
            self.assertIn(message, " ".join(report["needs_you"]))
            self.assertIsNone(drift._read_baseline(str(self.home)))
            drift._quick_cache.clear()
            with mock.patch.object(drift, "inventory", return_value=current):
                quick = drift.quick_check(str(self.config), home=str(self.home))
            self.assertEqual(quick["attention"], ["Run setup_bridge.py doctor once to record a baseline."])

    def test_scheduled_baseline_requires_matching_configured_gemma_pins(self) -> None:
        for name in ("delegate", "validator"):
            with self.subTest(name=name):
                current = self.inventory(codex=self.home / "codex", claude=self.home / "claude")
                current["gemma"][name] = {"path": "/configured/gemma", "pin_matches": False}
                with mock.patch.object(drift, "inventory", return_value=current), \
                     mock.patch.object(drift, "_help_has_flags", return_value={"ok": True}), \
                     mock.patch.object(drift, "_claude_help_has_flags", return_value={"ok": True}), \
                     mock.patch.object(drift, "_model_check", return_value={"ok": True}):
                    report = drift.scheduled_baseline(str(self.config), home=str(self.home), bridge=self.bridge(self.home / "codex"))
                self.assertFalse(report["ok"])
                self.assertIn(f"Gemma {name} pin", " ".join(report["needs_you"]))
                self.assertIsNone(drift._read_baseline(str(self.home)))

    def test_unknown_installed_consumer_version_is_not_a_mismatch(self) -> None:
        current = self.inventory(); current["consumers"] = [{"name": "firm-tools codex-job-runner", "version": "1.0"}]
        with mock.patch.object(drift, "inventory", return_value=current), \
             mock.patch.object(drift, "_npm_version_for_executable", return_value=None):
            report = drift.doctor(str(self.config), home=str(self.home))
        self.assertEqual(report["checks"]["consumer_pins"]["status"], "unknown")
        self.assertNotIn("pins 1.0", " ".join(report["needs_you"]))

    def test_ollama_digest_membership(self) -> None:
        class Reply:
            def __init__(self, value): self.value = value
            def read(self): return json.dumps(self.value).encode()
            def __enter__(self): return self
            def __exit__(self, *args): return False
        with mock.patch("agent_bridge.drift.urllib.request.urlopen", side_effect=[Reply({"version": "1"}), Reply({"models": [{"digest": "sha256:no"}, {"digest": "sha256:yes"}]})]): self.assertTrue(drift._ollama({"gemma_model_digest": "yes"})["model_present"])

    def test_update_never_escalates(self) -> None:
        self.assertNotIn("su" + "do", Path(drift.__file__).read_text())

    def inventory(self, *, codex: Path | None = None, claude: Path | None = None) -> dict:
        return {"version": 1, "at": 1.0, "config_path": str(self.config),
                "executables": {"claude": {"path": str(claude) if claude else None, "version": "1", "sha256": "claude"},
                                "codex": {"path": str(codex) if codex else None, "version": "1", "sha256": "codex"}},
                "ollama": {}, "gemma": {"delegate": {}, "validator": {}}, "paths": {},
                "hooks": {"claude": {"present": True, "launcher_exists": True},
                          "codex": {"present": True, "launcher_exists": True, "trust": "recorded"}},
                "codex_default_model": "good", "consumers": []}

    def launcher_flags(self) -> tuple[set[str], set[str]]:
        aliases = {"-m": "--model", "-s": "--sandbox", "-c": "--config", "-C": "--cd", "-p": "--print"}
        locations = (("backends/codex_backend.py", "build_argv", "codex"),
                     ("execution/codex_task.py", "_command", "codex"),
                     ("backends/claude_backend.py", "build_argv", "claude"),
                     ("execution/claude_task.py", "_command", "claude"),
                     ("drift.py", "run_cli_check", "codex"))
        found = {"codex": set(), "claude": set()}
        for relative, function, cli in locations:
            tree = ast.parse((ROOT / "src/agent_bridge" / relative).read_text(encoding="utf-8"))
            node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function)
            flags = {aliases.get(item.value, item.value) for item in ast.walk(node)
                     if isinstance(item, ast.Constant) and isinstance(item.value, str)
                     and item.value.startswith("-") and item.value != "-"}
            for name in (("codex", "claude") if cli == "both" else (cli,)):
                found[name].update(flags)
        guest = ast.parse((ROOT / "src/agent_bridge/orchestration/guest_runner.py").read_text(encoding="utf-8"))
        for function in ("provider_argv", "auth_probe_argv"):
            node = next(item for item in guest.body if isinstance(item, ast.FunctionDef) and item.name == function)
            branch = next(item for item in node.body if isinstance(item, ast.If))
            for name, statements in (("claude", branch.body), ("codex", branch.orelse)):
                flags = {aliases.get(item.value, item.value) for statement in statements for item in ast.walk(statement)
                         if isinstance(item, ast.Constant) and isinstance(item.value, str)
                         and item.value.startswith("-") and item.value != "-"}
                found[name].update(flags)
        return found["codex"], found["claude"]


if __name__ == "__main__": unittest.main()
