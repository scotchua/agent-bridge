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

    def test_schedule_is_nightly_and_uses_launchctl_wrapper(self) -> None:
        with mock.patch.object(drift, "_schedule_launchctl") as launcher, mock.patch.object(drift, "discover_config", return_value=str(self.config)):
            path = drift.install_schedule(home=str(self.home)); data = plistlib.loads(Path(path).read_bytes()); drift.remove_schedule(home=str(self.home))
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertIn("--update", data["ProgramArguments"]); self.assertIn("--scheduled", data["ProgramArguments"]); self.assertEqual(launcher.call_count, 2)
        self.assertEqual(launcher.call_args_list[1].kwargs, {"remove": True})

    def test_quick_check_missing_baseline_is_attention_not_drift(self) -> None:
        current = self.inventory()
        with mock.patch.object(drift, "inventory", return_value=current):
            report = drift.quick_check(str(self.config), home=str(self.home))
        self.assertEqual(report["changed"], [])
        self.assertEqual(report["baseline"], "missing")
        self.assertEqual(report["attention"], ["Run setup_bridge.py doctor once to record a baseline."])
        self.assertTrue(report["ok"])

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
