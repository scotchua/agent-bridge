"""Offline tests for the guided, opt-in "automatic delegation" feature.

Covers answers/config compatibility, private-config generation and
permissions, MCP registration without disturbing consultation entries or
unrelated configuration, idempotency, uninstall, platform gates, and honest
failure/partial/success reporting. No test here contacts a model, applies a
patch, or makes a live provider call.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge import config, onboard, setup_cmd, store  # noqa: E402
from agent_bridge.orchestration import delegation  # noqa: E402


def answers(**changes):
    base = {"version": 1, "directions": "both",
            "targets": {"codex": True, "claude_code": True, "claude_desktop": False},
            "privacy": {"mode": "baseline", "peers": {}}, "local_ollama": {"enabled": False},
            "automatic_delegation": {"enabled": False}}
    base.update(changes)
    return base


def active_in(directory):
    raw = store.read_json(config.DEFAULT_CONFIG_PATH)
    raw["state_root"] = os.path.join(directory, "state")
    raw["peers"]["codex"]["codex_home"] = os.path.join(directory, "isolated-codex")
    return config.Config(raw, config.DEFAULT_CONFIG_PATH)


PASSING_ROW = {
    "attempted": True, "reason": None, "state": "complete", "returncode": 0,
    "source_classification": "synthetic", "worktree_removed": True,
    "permission_to_apply": False, "permission_to_commit": False,
    "permission_to_push": False, "permission_to_merge": False,
}
BLOCKED_ROW = {
    "attempted": False, "reason": "execution_configuration_missing", "state": None,
    "returncode": None, "source_classification": None, "worktree_removed": None,
    "permission_to_apply": None, "permission_to_commit": None,
    "permission_to_push": None, "permission_to_merge": None,
}


def evidence(cfg, *, codex_to_claude=None, claude_to_codex=None, local_model=None):
    return {
        "verification_profile": delegation.VERIFICATION_PROFILE,
        "effective_config_sha256": delegation.config_sha256(cfg),
        "created_at": "2026-01-01T00:00:00.000+00:00",
        "directions": {"codex->claude": codex_to_claude or PASSING_ROW,
                       "claude->codex": claude_to_codex or PASSING_ROW},
        "local_model": local_model or {"status": "not_configured"},
        "no_patches_applied": True, "no_commits": True, "no_pushes": True,
        "no_merges": True, "no_paid_fallback": True, "no_client_data": True,
    }


class AnswersCompatibilityTests(unittest.TestCase):
    def test_old_answers_file_without_the_key_defaults_disabled(self):
        raw = {"version": 1, "directions": "both",
               "targets": {"codex": True, "claude_code": True, "claude_desktop": False},
               "privacy": {"mode": "baseline"}, "local_ollama": {"enabled": False}}
        validated = onboard.validate_answers(raw)
        self.assertEqual(validated["automatic_delegation"], {"enabled": False})

    def test_enabled_requires_explicit_boolean(self):
        with self.assertRaises(ValueError):
            onboard.validate_answers(answers(automatic_delegation={"enabled": "yes"}))

    def test_enabled_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            onboard.validate_answers(answers(automatic_delegation={"enabled": True, "typo": 1}))

    def test_disabled_rejects_stray_worker_path(self):
        with self.assertRaisesRegex(ValueError, "omit local_worker_executable"):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": False, "local_worker_executable": "/abs/path"}))

    def test_enabled_requires_absolute_worker_path(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": True, "local_worker_executable": "relative/path"}))

    def test_enabled_accepts_absolute_worker_path(self):
        validated = onboard.validate_answers(answers(automatic_delegation={
            "enabled": True, "local_worker_executable": "/abs/path/worker"}))
        self.assertEqual(validated["automatic_delegation"]["local_worker_executable"], "/abs/path/worker")


class ConfigGenerationTests(unittest.TestCase):
    def test_config_paths_are_absolute_and_outside_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            for key, value in cfg.items():
                if key in ("config_version", "interval_seconds"):
                    continue
                self.assertTrue(os.path.isabs(value), f"{key} must be absolute: {value}")
            self.assertTrue(cfg["state_root"].startswith(os.path.join(os.path.abspath(home), ".agent-bridge")))
            self.assertFalse(cfg["state_root"].startswith(str(ROOT)))
            self.assertTrue(cfg["worker_executable"].endswith(delegation.NO_WORKER_SENTINEL))
            self.assertNotIn("token", json.dumps(cfg).lower())
            self.assertNotIn("secret", json.dumps(cfg).lower())

    def test_harness_availability_reflects_the_current_checkout(self):
        cfg = delegation.build_config(tempfile.gettempdir(), str(ROOT), local_worker_executable=None)
        availability = delegation.harness_availability(cfg)
        self.assertTrue(availability["claude_task_executable"])
        # Honest, current limitation: no codex execution harness ships yet, so
        # the executor cannot be constructed for either direction.
        self.assertFalse(availability["codex_task_executable"])
        self.assertFalse(availability["execution_complete"])
        self.assertFalse(availability["local_worker_configured"])

    def test_local_worker_executable_marks_configured_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = os.path.join(tmp, "worker")
            Path(worker).write_text("#!/bin/sh\n")
            os.chmod(worker, 0o700)
            cfg = delegation.build_config(tmp, str(ROOT), local_worker_executable=worker)
            self.assertTrue(delegation.harness_availability(cfg)["local_worker_configured"])


class PlatformBoundaryTests(unittest.TestCase):
    def test_darwin_reports_a_verified_continuous_service(self):
        report = delegation.platform_boundary_report("darwin")
        self.assertTrue(report["continuous_service_verified"])

    def test_windows_and_linux_never_claim_a_verified_continuous_service(self):
        for name in ("win32", "linux"):
            report = delegation.platform_boundary_report(name)
            self.assertFalse(report["continuous_service_verified"])
            self.assertIn("not installed automatically", report["execution_worker_service"])
            self.assertEqual(report["registration_and_config"], "supported (portable MCP registration and private config generation)")


class LaunchAgentTests(unittest.TestCase):
    def test_render_requires_absolute_paths_and_carries_no_credentials(self):
        rendered = delegation.render_launch_agent(
            worker_binary="/repo/bin/agent-bridge-execution-worker",
            config_path="/home/user/.agent-bridge/orchestration/orchestration.json",
            python_executable="/usr/bin/python3", account="alice",
            claude_bin_dir="/opt/homebrew/bin",
            stdout_log="/home/user/.agent-bridge/orchestration/execution-worker.stdout.log",
            stderr_log="/home/user/.agent-bridge/orchestration/execution-worker.stderr.log")
        text = rendered.decode()
        for secret_marker in ("token", "password", "api_key", "oauth"):
            self.assertNotIn(secret_marker, text.lower())
        self.assertIn("<true/>", text)
        with self.assertRaises(delegation.DelegationConfigError):
            delegation.render_launch_agent(
                worker_binary="relative/path", config_path="/a", python_executable="/b",
                account="alice", claude_bin_dir="/c", stdout_log="/d", stderr_log="/e")

    def test_activate_refuses_non_darwin_and_root(self):
        with mock.patch("sys.platform", "linux"):
            self.assertEqual(delegation.activate_launch_agent("/tmp/x.plist")["status"], "unsupported_platform")
        with mock.patch("sys.platform", "darwin"), mock.patch.object(os, "getuid", return_value=0, create=True):
            self.assertEqual(delegation.activate_launch_agent("/tmp/x.plist")["status"], "refused_root")

    def test_activate_is_staged_by_default_and_never_bootstraps_without_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = os.path.join(tmp, "agent.plist")
            Path(plist).write_text("<plist/>")
            with mock.patch("sys.platform", "darwin"), \
                 mock.patch.object(os, "getuid", return_value=501, create=True), \
                 mock.patch.object(delegation, "_launchctl") as probe:
                probe.return_value = mock.Mock(returncode=1, stderr=b"")
                result = delegation.activate_launch_agent(plist, apply=False)
        self.assertEqual(result["status"], "staged")
        self.assertIn("bootstrap", result["command"])
        probe.assert_called_once()

    def test_activate_is_idempotent_when_already_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = os.path.join(tmp, "agent.plist")
            Path(plist).write_text("<plist/>")
            with mock.patch("sys.platform", "darwin"), \
                 mock.patch.object(os, "getuid", return_value=501, create=True), \
                 mock.patch.object(delegation, "_launchctl") as probe:
                probe.return_value = mock.Mock(returncode=0, stderr=b"")
                result = delegation.activate_launch_agent(plist, apply=True)
        self.assertEqual(result["status"], "already_active")
        probe.assert_called_once()


class EvidenceValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = delegation.build_config(self.tmp.name, str(ROOT), local_worker_executable=None)

    def test_full_pass_both_directions_reports_enabled(self):
        status = delegation.validate_evidence(
            evidence(self.cfg), self.cfg,
            required_directions=delegation.DIRECTIONS, local_worker_required=False)
        self.assertEqual(status["overall"], "enabled")
        self.assertEqual(status["local_model"], "not_configured")

    def test_one_blocked_required_direction_reports_partial(self):
        status = delegation.validate_evidence(
            evidence(self.cfg, claude_to_codex=BLOCKED_ROW), self.cfg,
            required_directions=delegation.DIRECTIONS, local_worker_required=False)
        self.assertEqual(status["claude->codex"], "blocked:execution_configuration_missing")
        self.assertEqual(status["overall"], "partial")

    def test_unselected_direction_is_not_graded(self):
        status = delegation.validate_evidence(
            evidence(self.cfg, claude_to_codex=BLOCKED_ROW), self.cfg,
            required_directions=("codex->claude",), local_worker_required=False)
        self.assertEqual(status["overall"], "enabled")
        self.assertEqual(status["claude->codex"], "not_required")

    def test_all_required_directions_blocked_reports_blocked(self):
        status = delegation.validate_evidence(
            evidence(self.cfg, codex_to_claude=BLOCKED_ROW, claude_to_codex=BLOCKED_ROW), self.cfg,
            required_directions=delegation.DIRECTIONS, local_worker_required=False)
        self.assertEqual(status["overall"], "blocked")

    def test_tampered_config_hash_is_refused(self):
        tampered = evidence(self.cfg)
        tampered["effective_config_sha256"] = "0" * 64
        with self.assertRaisesRegex(delegation.DelegationVerificationError, "staged orchestration config"):
            delegation.validate_evidence(tampered, self.cfg, required_directions=delegation.DIRECTIONS,
                                         local_worker_required=False)

    def test_permission_to_apply_true_is_never_acceptable(self):
        tampered_row = dict(PASSING_ROW, permission_to_apply=True)
        with self.assertRaises(delegation.DelegationVerificationError):
            delegation.validate_evidence(evidence(self.cfg, codex_to_claude=tampered_row), self.cfg,
                                         required_directions=delegation.DIRECTIONS, local_worker_required=False)

    def test_safety_flag_false_is_refused(self):
        tampered = evidence(self.cfg)
        tampered["no_paid_fallback"] = False
        with self.assertRaisesRegex(delegation.DelegationVerificationError, "no_paid_fallback"):
            delegation.validate_evidence(tampered, self.cfg, required_directions=delegation.DIRECTIONS,
                                         local_worker_required=False)

    def test_local_model_must_be_explicitly_not_configured_when_not_required(self):
        tampered = evidence(self.cfg, local_model={"status": "complete", "source_classification": "synthetic"})
        with self.assertRaises(delegation.DelegationVerificationError):
            delegation.validate_evidence(tampered, self.cfg, required_directions=delegation.DIRECTIONS,
                                         local_worker_required=False)

    def test_local_model_required_but_absent_is_refused(self):
        with self.assertRaises(delegation.DelegationVerificationError):
            delegation.validate_evidence(evidence(self.cfg), self.cfg, required_directions=delegation.DIRECTIONS,
                                         local_worker_required=True)

    def test_wrong_verification_profile_is_refused(self):
        tampered = evidence(self.cfg)
        tampered["verification_profile"] = "something-else"
        with self.assertRaises(delegation.DelegationVerificationError):
            delegation.validate_evidence(tampered, self.cfg, required_directions=delegation.DIRECTIONS,
                                         local_worker_required=False)

    def test_unexpected_shape_is_refused(self):
        tampered = evidence(self.cfg)
        tampered["extra"] = 1
        with self.assertRaises(delegation.DelegationVerificationError):
            delegation.validate_evidence(tampered, self.cfg, required_directions=delegation.DIRECTIONS,
                                         local_worker_required=False)


class ApplyIntegrationTests(unittest.TestCase):
    def _stage(self, tmp, choices):
        home = os.path.join(tmp, "home")
        candidate = os.path.join(tmp, "candidate.json")
        results = os.path.join(tmp, "results.json")
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        active = active_in(tmp)
        staged = setup_cmd.write_candidate(candidate, onboard.privacy_overlay(choices), active)
        plan = {"answers_sha256": onboard._sha(choices),
                "effective_config_sha256": config.effective_config_sha256(staged.raw),
                "directions": choices["directions"], "targets": choices["targets"],
                "pins": {peer: {key: staged.peer(peer).get(key) for key in ("executable", "allowed_versions")}
                         for peer in ("claude", "codex")}}
        store.atomic_write_json(candidate + ".onboarding-plan.json", plan)
        results_doc = {"verdict": "PASS", "effective_config_sha256": config.effective_config_sha256(staged.raw),
                       "configured_versions": {"claude": [], "codex": []},
                       "observed_versions": {"claude": [], "codex": []},
                       "controls_requested": {"timeout_canaries": 1}, "controls_executed": {"timeout_canaries": 1}}
        store.atomic_write_json(results, results_doc)
        return home, candidate, results

    def _apply_patches(self, tmp):
        return (
            mock.patch.object(setup_cmd, "validate_promotion"),
            mock.patch.object(setup_cmd, "promote_candidate", return_value={}),
            mock.patch.object(setup_cmd, "is_durable", return_value=True),
            mock.patch.object(setup_cmd, "find_all", return_value=[]),
            mock.patch.object(config, "local_config_path", return_value=os.path.join(tmp, "local.json")),
        )

    def test_enabled_without_evidence_is_refused_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            patches = self._apply_patches(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with self.assertRaisesRegex(ValueError, "delegation-results"):
                    onboard.apply(choices, candidate, results, str(ROOT), home=home)
            self.assertFalse(os.path.exists(onboard._paths(home)["delegation_config"]))

    def test_blocked_evidence_is_refused_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            delegation_results = os.path.join(tmp, "delegation.json")
            store.atomic_write_json(delegation_results, evidence(
                cfg, codex_to_claude=BLOCKED_ROW, claude_to_codex=BLOCKED_ROW))
            patches = self._apply_patches(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with self.assertRaisesRegex(ValueError, "proves nothing"):
                    onboard.apply(choices, candidate, results, str(ROOT), home=home,
                                 delegation_results=delegation_results)
            self.assertFalse(os.path.exists(onboard._paths(home)["delegation_config"]))

    def test_partial_pass_applies_registrations_and_reports_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            delegation_results = os.path.join(tmp, "delegation.json")
            store.atomic_write_json(delegation_results, evidence(cfg, claude_to_codex=BLOCKED_ROW))
            patches = self._apply_patches(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                onboard.apply(choices, candidate, results, str(ROOT), home=home,
                             delegation_results=delegation_results)
            paths = onboard._paths(home)
            self.assertTrue(os.path.isfile(paths["delegation_config"]))
            written = store.read_json(paths["delegation_config"])
            self.assertEqual(written, cfg)
            if os.name != "nt":
                self.assertEqual(os.stat(paths["delegation_config"]).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(os.path.dirname(paths["delegation_config"])).st_mode & 0o777, 0o700)
            self.assertIn('mcp_servers."agent-bridge-orchestration"', Path(paths["codex_toml"]).read_text())
            self.assertIn("agent-bridge-orchestration", store.read_json(paths["claude_json"])["mcpServers"])
            receipt = store.read_json(paths["delegation_receipt"])
            self.assertEqual(receipt["status"]["overall"], "partial")
            if sys.platform == "darwin":
                self.assertTrue(os.path.isfile(paths["launch_agent"]))
                self.assertNotIn("token", Path(paths["launch_agent"]).read_text().lower())

    def test_apply_is_idempotent_and_preserves_unrelated_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            Path(os.path.join(home, ".claude.json")).write_text(
                json.dumps({"mcpServers": {"keep": {"command": "else"}}, "other": 1}))
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            delegation_results = os.path.join(tmp, "delegation.json")
            store.atomic_write_json(delegation_results, evidence(cfg))
            patches = self._apply_patches(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                onboard.apply(choices, candidate, results, str(ROOT), home=home, delegation_results=delegation_results)
                onboard.apply(choices, candidate, results, str(ROOT), home=home, delegation_results=delegation_results)
            paths = onboard._paths(home)
            self.assertEqual(store.read_json(paths["claude_json"])["mcpServers"]["keep"]["command"], "else")
            self.assertEqual(store.read_json(paths["claude_json"])["other"], 1)
            receipt = store.read_json(paths["delegation_receipt"])
            self.assertEqual(receipt["status"]["overall"], "enabled")

    def test_edited_delegation_config_is_preserved_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            delegation_results = os.path.join(tmp, "delegation.json")
            store.atomic_write_json(delegation_results, evidence(cfg))
            patches = self._apply_patches(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                onboard.apply(choices, candidate, results, str(ROOT), home=home, delegation_results=delegation_results)
                paths = onboard._paths(home)
                edited = store.read_json(paths["delegation_config"])
                edited["interval_seconds"] = 999.0
                store.atomic_write_json(paths["delegation_config"], edited)
                with self.assertRaisesRegex(ValueError, "edited or is unrecognized"):
                    onboard.apply(choices, candidate, results, str(ROOT), home=home,
                                 delegation_results=delegation_results)
                self.assertEqual(store.read_json(paths["delegation_config"])["interval_seconds"], 999.0)

    def test_uninstall_removes_orchestration_registration_and_preserves_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            delegation_results = os.path.join(tmp, "delegation.json")
            store.atomic_write_json(delegation_results, evidence(cfg))
            patches = self._apply_patches(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                onboard.apply(choices, candidate, results, str(ROOT), home=home, delegation_results=delegation_results)
            paths = onboard._paths(home)
            with mock.patch.object(delegation, "_launchctl") as probe:
                probe.return_value = mock.Mock(returncode=1, stderr=b"")
                report = onboard.uninstall(choices, str(ROOT), home=home,
                                           apply_changes=True, delegation_only=True)
            self.assertNotIn('mcp_servers."agent-bridge-orchestration"', Path(paths["codex_toml"]).read_text())
            self.assertNotIn("agent-bridge-orchestration", store.read_json(paths["claude_json"])["mcpServers"])
            # Consultation stays installed; only orchestration is removed.
            self.assertIn('mcp_servers."claude-peer"', Path(paths["codex_toml"]).read_text())
            self.assertIn("codex-peer", store.read_json(paths["claude_json"])["mcpServers"])
            self.assertIn(paths["delegation_config"], report["retained_files"])
            self.assertTrue(os.path.isfile(paths["delegation_config"]))
            if sys.platform == "darwin":
                self.assertFalse(os.path.exists(paths["launch_agent"]))

    def test_uninstall_preserves_edited_launch_agent_and_reports_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            delegation_results = os.path.join(tmp, "delegation.json")
            store.atomic_write_json(delegation_results, evidence(cfg))
            patches = self._apply_patches(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                onboard.apply(choices, candidate, results, str(ROOT), home=home, delegation_results=delegation_results)
            paths = onboard._paths(home)
            if sys.platform != "darwin":
                self.skipTest("LaunchAgent handling is macOS-specific")
            Path(paths["launch_agent"]).write_bytes(Path(paths["launch_agent"]).read_bytes() + b"<!-- edited -->")
            edited = Path(paths["launch_agent"]).read_bytes()
            with mock.patch.object(delegation, "_launchctl") as probe:
                probe.return_value = mock.Mock(returncode=1, stderr=b"")
                report = onboard.uninstall(choices, str(ROOT), home=home, apply_changes=True)
            self.assertTrue(any("LaunchAgent" in c for c in report["preserved_conflicts"]))
            self.assertEqual(Path(paths["launch_agent"]).read_bytes(), edited)


class LauncherSecurityTests(unittest.TestCase):
    """Extends the existing hostile-cwd launcher coverage to the new binary."""

    def test_new_launcher_ignores_cwd_package_and_inherited_pythonpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            hostile = Path(temporary)
            marker = hostile / "hostile-imported"
            package = hostile / "agent_bridge"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from pathlib import Path\nimport os\n"
                "Path(os.environ['LAUNCHER_SECURITY_MARKER']).write_text('executed')\n",
                encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(hostile)
            environment["LAUNCHER_SECURITY_MARKER"] = str(marker)
            name = "agent-bridge-orchestration-verify" + (".cmd" if os.name == "nt" else "")
            launcher = ROOT / "bin" / name
            argv = ([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher), "--help"]
                    if os.name == "nt" else [str(launcher), "--help"])
            completed = subprocess.run(argv, cwd=hostile, env=environment, capture_output=True, timeout=20)
            self.assertFalse(marker.exists(), completed.stdout.decode())
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_launcher_source_matches_the_safe_pattern(self):
        text = (ROOT / "bin" / "agent-bridge-orchestration-verify").read_text(encoding="utf-8")
        self.assertIn('export PYTHONPATH="$REPO/src"', text)
        self.assertNotIn("$PYTHONPATH", text)
        self.assertIn('exec "$PY" -P -m ', text)


class OrchestrationServeSubcommandTests(unittest.TestCase):
    def test_serve_orchestration_is_caller_bound_and_distinct_from_consultation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "orchestration.json")
            state = os.path.join(tmp, "state")
            store.atomic_write_json(cfg_path, {
                "config_version": "1", "state_root": state,
                "local_queue_root": os.path.join(state, "local-queue"),
                "capacity_db": os.path.join(state, "capacity.sqlite3"),
                "worker_executable": os.path.join(tmp, "no-worker"),
                "worker_state": os.path.join(state, "worker-state"),
                "interval_seconds": 0.05,
            })
            requests = "\n".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
                                  for i, method in ((1, "initialize"), (2, "tools/list"))) + "\n"
            proc = subprocess.run([sys.executable, str(ROOT / "setup_bridge.py"), "serve-orchestration",
                                   "--caller", "codex", "--config", cfg_path],
                                  input=requests, capture_output=True, text=True, encoding="utf-8",
                                  cwd=tmp, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            messages = [json.loads(line) for line in proc.stdout.splitlines()]
            names = {t["name"] for m in messages if m.get("id") == 2 for t in m["result"]["tools"]}
            self.assertIn("orchestration_status", names)
            self.assertIn("work_route_local", names)
            self.assertFalse(names & {"claude_start", "codex_start"})
            self.assertFalse(names & {"execution_dispatch"})  # no execution config supplied


if __name__ == "__main__":
    unittest.main()
