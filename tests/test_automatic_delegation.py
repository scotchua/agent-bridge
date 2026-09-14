"""Offline tests for the guided, opt-in "automatic delegation" feature.

Covers answers/config compatibility, private-config generation and
permissions, MCP registration without disturbing consultation entries or
unrelated configuration, idempotency, uninstall, platform gates, and honest
failure/partial/success reporting. No test here contacts a model, applies a
patch, or makes a live provider call.
"""
from __future__ import annotations

import contextlib
import io
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
from agent_bridge.orchestration import (  # noqa: E402
    delegation, windows_preflight, windows_wsl_provision as wp)


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
    # The harness verdict, not just the process exit code. A row without these
    # is a v1 row and is refused by shape.
    "harness_ok": True, "harness_status": "complete", "harness_verdict": "read",
    "source_classification": "synthetic", "worktree_removed": True,
    "permission_to_apply": False, "permission_to_commit": False,
    "permission_to_push": False, "permission_to_merge": False,
}
BLOCKED_ROW = {
    "attempted": False, "reason": "execution_configuration_missing", "state": None,
    "returncode": None, "harness_ok": None, "harness_status": None,
    "harness_verdict": None,
    "source_classification": None, "worktree_removed": None,
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



def _write_private(path, text):
    """Fixtures write records the way production does: owner-only.

    The evidence loader refuses anything else, on purpose: a record other
    accounts could have written is not evidence about this machine.
    """
    descriptor = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(str(path), 0o600)


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
        worker = os.path.abspath(os.path.join(tempfile.gettempdir(), "worker"))
        validated = onboard.validate_answers(answers(automatic_delegation={
            "enabled": True, "local_worker_executable": worker}))
        self.assertEqual(validated["automatic_delegation"]["local_worker_executable"], worker)


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
        # Both bounded implementation harnesses are bundled in this checkout,
        # so the execution lane can be constructed for either direction on a
        # fresh, supported macOS install without any external skill install.
        self.assertTrue(availability["claude_task_executable"])
        self.assertTrue(availability["codex_task_executable"])
        self.assertTrue(availability["execution_complete"])
        self.assertFalse(availability["local_worker_configured"])

    def test_both_bundled_harnesses_run_through_the_configured_python(self):
        """Both harnesses are real, executable Python files, not placeholders.

        This does not contact any provider; it only proves each harness file
        parses and its own argument parser runs cleanly under the same
        interpreter guided config generation configures, which is what a
        fresh checkout needs before it can even attempt a live provider call.
        """
        cfg = delegation.build_config(tempfile.gettempdir(), str(ROOT), local_worker_executable=None)
        for key in ("codex_task_executable", "claude_task_executable"):
            proc = subprocess.run(
                [cfg["python_executable"], "-P", cfg[key], "--help"],
                capture_output=True, timeout=20, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--classification", proc.stdout)

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


@unittest.skipIf(os.name == "nt", "LaunchAgent rendering is a macOS/POSIX-only feature")
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

    def test_codex_bin_dir_defaults_to_claude_bin_dir_when_omitted(self):
        rendered = delegation.render_launch_agent(
            worker_binary="/repo/bin/agent-bridge-execution-worker",
            config_path="/home/user/.agent-bridge/orchestration/orchestration.json",
            python_executable="/usr/bin/python3", account="alice",
            claude_bin_dir="/opt/homebrew/bin",
            stdout_log="/home/user/.agent-bridge/orchestration/execution-worker.stdout.log",
            stderr_log="/home/user/.agent-bridge/orchestration/execution-worker.stderr.log")
        self.assertIn("<string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>", rendered.decode())

    def test_codex_bin_dir_is_added_to_path_when_different_from_claude(self):
        rendered = delegation.render_launch_agent(
            worker_binary="/repo/bin/agent-bridge-execution-worker",
            config_path="/home/user/.agent-bridge/orchestration/orchestration.json",
            python_executable="/usr/bin/python3", account="alice",
            claude_bin_dir="/opt/homebrew/bin", codex_bin_dir="/usr/local/bin",
            stdout_log="/home/user/.agent-bridge/orchestration/execution-worker.stdout.log",
            stderr_log="/home/user/.agent-bridge/orchestration/execution-worker.stderr.log")
        self.assertIn("<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>",
                     rendered.decode())

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


_GOOD_MANIFEST = {
    "schema_version": 1, "distro_release": "22.04.3",
    "rootfs_sha256": "a" * 64, "node_version": "20.11.0",
    "claude_version": "1.0.0", "codex_version": "1.0.0",
}


def _fake_windows_subprocess_run(*, ready: bool):
    """Bounded fake for windows_preflight's ``subprocess.run`` calls only."""
    def runner(argv, **kwargs):
        exe = argv[0]
        if exe == "cmd.exe":
            stdout = b"Microsoft Windows [Version 10.0.22631.3527]\r\n"
        elif exe == "wsl.exe":
            stdout = b"WSL version: 2.1.5.0\r\n" if ready else b"WSL version: 1.0.0.0\r\n"
        elif exe == "powershell.exe":
            stdout = b"Enabled\r\n" if ready else b"Disabled\r\n"
        elif exe == "systeminfo.exe":
            stdout = (b"Virtualization Enabled In Firmware: Yes\r\n" if ready
                      else b"Virtualization Enabled In Firmware: No\r\n")
        else:
            raise AssertionError(f"unexpected command spawned in a test: {argv!r}")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
    return runner


WINDOWS_DELEGATION_CHOICE = {
    "enabled": True,
    "windows_wsl_manifest_path": r"C:\Users\agent\wsl\manifest.json",
    "windows_wsl_rootfs_path": r"C:\Users\agent\wsl\rootfs.tar",
}


class WindowsPathCompatibilityTests(unittest.TestCase):
    def test_old_answers_without_windows_fields_stay_disabled(self):
        raw = {"version": 1, "directions": "both",
               "targets": {"codex": True, "claude_code": True, "claude_desktop": False},
               "privacy": {"mode": "baseline"}, "local_ollama": {"enabled": False},
               "automatic_delegation": {"enabled": False}}
        validated = onboard.validate_answers(raw)
        self.assertEqual(validated["automatic_delegation"], {"enabled": False})

    def test_enabled_without_windows_fields_is_still_accepted(self):
        validated = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
        self.assertNotIn("windows_wsl_manifest_path", validated["automatic_delegation"])
        self.assertNotIn("windows_wsl_rootfs_path", validated["automatic_delegation"])

    def test_valid_windows_paths_are_accepted_and_preserved(self):
        validated = onboard.validate_answers(answers(
            automatic_delegation=dict(WINDOWS_DELEGATION_CHOICE)))
        self.assertEqual(validated["automatic_delegation"]["windows_wsl_manifest_path"],
                         WINDOWS_DELEGATION_CHOICE["windows_wsl_manifest_path"])
        self.assertEqual(validated["automatic_delegation"]["windows_wsl_rootfs_path"],
                         WINDOWS_DELEGATION_CHOICE["windows_wsl_rootfs_path"])

    def test_manifest_without_rootfs_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must both be set or both omitted"):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": True, "windows_wsl_manifest_path": r"C:\a\manifest.json"}))

    def test_relative_windows_path_is_refused(self):
        with self.assertRaises(ValueError):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": True, "windows_wsl_manifest_path": r"manifest.json",
                "windows_wsl_rootfs_path": r"C:\a\rootfs.tar"}))

    def test_unc_windows_path_is_refused(self):
        with self.assertRaises(ValueError):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": True, "windows_wsl_manifest_path": r"\\server\share\manifest.json",
                "windows_wsl_rootfs_path": r"C:\a\rootfs.tar"}))

    def test_device_windows_path_is_refused(self):
        with self.assertRaises(ValueError):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": True, "windows_wsl_manifest_path": r"\\?\C:\a\manifest.json",
                "windows_wsl_rootfs_path": r"C:\a\rootfs.tar"}))

    def test_traversal_windows_path_is_refused(self):
        with self.assertRaises(ValueError):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": True, "windows_wsl_manifest_path": r"C:\a\..\manifest.json",
                "windows_wsl_rootfs_path": r"C:\a\rootfs.tar"}))

    def test_disabled_rejects_stray_windows_fields(self):
        with self.assertRaisesRegex(ValueError, "omit local_worker_executable"):
            onboard.validate_answers(answers(automatic_delegation={
                "enabled": False, "windows_wsl_manifest_path": r"C:\a\manifest.json"}))


class WindowsPreflightWiringTests(unittest.TestCase):
    def test_ready_when_all_checks_and_manifest_pass(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run",
                        side_effect=_fake_windows_subprocess_run(ready=True)), \
             mock.patch.object(onboard.store, "sha256_file", return_value="a" * 64), \
             mock.patch.object(onboard.store, "read_json", return_value=dict(_GOOD_MANIFEST)):
            summary = onboard._windows_preflight_summary(WINDOWS_DELEGATION_CHOICE, platform="win32")
        self.assertEqual(summary["status"], windows_preflight.STATUS_PREREQUISITES_READY)
        self.assertTrue(summary["prerequisites_ready"])
        self.assertIn("does not enable", summary["prerequisites_ready_meaning"])
        for check in summary["checks"].values():
            self.assertTrue(check.startswith("ready: "))

    def test_missing_manifest_path_is_reported_as_not_ready(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run",
                        side_effect=_fake_windows_subprocess_run(ready=True)):
            summary = onboard._windows_preflight_summary({"enabled": True}, platform="win32")
        self.assertEqual(summary["status"], windows_preflight.STATUS_PREREQUISITES_NOT_READY)
        self.assertFalse(summary["prerequisites_ready"])
        self.assertTrue(summary["checks"]["manifest"].startswith("not ready: "))

    def test_failed_prerequisite_checks_are_reported_as_not_ready(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run",
                        side_effect=_fake_windows_subprocess_run(ready=False)), \
             mock.patch.object(onboard.store, "sha256_file", return_value="a" * 64), \
             mock.patch.object(onboard.store, "read_json", return_value=dict(_GOOD_MANIFEST)):
            summary = onboard._windows_preflight_summary(WINDOWS_DELEGATION_CHOICE, platform="win32")
        self.assertEqual(summary["status"], windows_preflight.STATUS_PREREQUISITES_NOT_READY)
        self.assertFalse(summary["prerequisites_ready"])
        self.assertTrue(summary["checks"]["wsl_version"].startswith("not ready: "))
        self.assertTrue(summary["checks"]["virtual_machine_platform"].startswith("not ready: "))
        self.assertTrue(summary["checks"]["firmware_virtualization"].startswith("not ready: "))

    def test_no_preflight_command_is_run_on_non_windows_platforms(self):
        def _forbidden(*args, **kwargs):
            raise AssertionError("preflight must never spawn a process on non-Windows platforms")
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run",
                        side_effect=_forbidden):
            for platform_name in ("darwin", "linux"):
                summary = onboard._windows_preflight_summary(WINDOWS_DELEGATION_CHOICE, platform=platform_name)
                self.assertIsNone(summary)

    def test_ready_prerequisites_are_not_the_same_as_delegation_enabled(self):
        ready_report = {"status": windows_preflight.STATUS_PREREQUISITES_READY,
                        "prerequisites_ready": True, "checks": {}, "detail": ""}
        with mock.patch.object(onboard, "_windows_preflight_summary", return_value=ready_report), \
             mock.patch.object(onboard.delegation, "platform_boundary_report",
                              return_value={"platform": "windows",
                                            "execution_worker_service": "not installed automatically",
                                            "continuous_service_verified": False,
                                            "resource_sampler": "n/a", "registration_and_config": "supported"}):
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            result = onboard.status(choices)
        self.assertTrue(result["windows_preflight"]["prerequisites_ready"])
        self.assertEqual(result["execution_delegation"], "blocked")
        self.assertEqual(result["execution_delegation_detail"]["reason"],
                         "windows_wsl_configuration_missing")
        self.assertEqual(result["execution_delegation_detail"]["provider_lane"],
                         "closed")
        self.assertIn("verification record", result["execution_delegation_note"])
        self.assertIn("boundary-verification", result["execution_delegation_note"])


class WindowsPlanReportingTests(unittest.TestCase):
    def test_plan_includes_windows_preflight_and_blocked_execution_note(self):
        ready_report = {"status": windows_preflight.STATUS_PREREQUISITES_READY,
                        "prerequisites_ready": True, "checks": {}, "detail": ""}
        with mock.patch.object(onboard, "_windows_preflight_summary", return_value=ready_report):
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            result = onboard.plan(choices, str(ROOT))
        automatic = result["automatic_delegation"]
        self.assertEqual(automatic["windows_preflight"], ready_report)
        self.assertIn("is off", automatic["execution_delegation_note"])
        self.assertEqual(automatic["execution_delegation"]["state"], "blocked")

    def test_plan_omits_windows_preflight_on_non_windows(self):
        with mock.patch.object(onboard, "_windows_preflight_summary", return_value=None):
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            result = onboard.plan(choices, str(ROOT))
        self.assertIsNone(result["automatic_delegation"]["windows_preflight"])
        self.assertIsNone(result["automatic_delegation"]["execution_delegation_note"])


class WindowsApplyRefusalTests(unittest.TestCase):
    """Applying on Windows must still report execution delegation blocked."""

    def _stage(self, tmp, choices):
        home = os.path.join(tmp, "home")
        candidate = os.path.join(tmp, "candidate.json")
        results = os.path.join(tmp, "results.json")
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        active = active_in(tmp)
        staged = setup_cmd.write_candidate(candidate, onboard.privacy_overlay(choices), active)
        plan_doc = {"answers_sha256": onboard._sha(choices),
                   "effective_config_sha256": config.effective_config_sha256(staged.raw),
                   "directions": choices["directions"], "targets": choices["targets"],
                   "pins": {peer: {key: staged.peer(peer).get(key) for key in ("executable", "allowed_versions")}
                            for peer in ("claude", "codex")}}
        store.atomic_write_json(candidate + ".onboarding-plan.json", plan_doc)
        results_doc = {"verdict": "PASS", "effective_config_sha256": config.effective_config_sha256(staged.raw),
                      "configured_versions": {"claude": [], "codex": []},
                      "observed_versions": {"claude": [], "codex": []},
                      "controls_requested": {"timeout_canaries": 1}, "controls_executed": {"timeout_canaries": 1}}
        store.atomic_write_json(results, results_doc)
        return home, candidate, results

    def test_successful_preflight_still_reports_execution_delegation_blocked_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            home, candidate, results = self._stage(tmp, choices)
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            delegation_results = os.path.join(tmp, "delegation.json")
            store.atomic_write_json(delegation_results, evidence(cfg))
            ready_report = {"status": windows_preflight.STATUS_PREREQUISITES_READY,
                            "prerequisites_ready": True, "checks": {}, "detail": ""}
            windows_boundary = {"platform": "windows",
                               "execution_worker_service": "not installed automatically",
                               "continuous_service_verified": False,
                               "resource_sampler": "n/a", "registration_and_config": "supported"}
            with mock.patch.object(setup_cmd, "validate_promotion"), \
                 mock.patch.object(setup_cmd, "promote_candidate", return_value={}), \
                 mock.patch.object(setup_cmd, "is_durable", return_value=True), \
                 mock.patch.object(setup_cmd, "find_all", return_value=[]), \
                 mock.patch.object(config, "local_config_path", return_value=os.path.join(tmp, "local.json")), \
                 mock.patch.object(onboard.delegation, "platform_boundary_report", return_value=windows_boundary), \
                 mock.patch.object(onboard, "_windows_preflight_summary", return_value=ready_report):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    onboard.apply(choices, candidate, results, str(ROOT), home=home,
                                 delegation_results=delegation_results)
            report = json.loads(buffer.getvalue())["automatic_delegation"]
            self.assertEqual(report["status"]["overall"], "enabled")
            self.assertEqual(report["windows_preflight"], ready_report)
            self.assertEqual(report["execution_delegation"], "blocked")
            self.assertIn("remains blocked", report["execution_delegation_note"])


class WindowsSetupLadderWiringTests(unittest.TestCase):
    """Onboarding must describe guided setup, not assume WSL is present."""

    def _summary(self, *, ready, choice=None, manifest_ok=True):
        patches = [
            mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run",
                       side_effect=_fake_windows_subprocess_run(ready=ready)),
            mock.patch.object(onboard.store, "sha256_file",
                              return_value=("a" if manifest_ok else "b") * 64),
            mock.patch.object(onboard.store, "read_json", return_value=dict(_GOOD_MANIFEST)),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return onboard._windows_setup_summary(
                choice if choice is not None else WINDOWS_DELEGATION_CHOICE,
                platform="win32")

    def test_a_machine_with_nothing_installed_starts_at_the_firmware_or_feature_stage(self):
        summary = self._summary(ready=False)
        self.assertIn(summary["stage"],
                      (wp.STAGE_FIRMWARE_VIRTUALIZATION, wp.STAGE_WINDOWS_FEATURES))
        self.assertFalse(summary["delegation_may_be_enabled"])

    def test_it_names_the_next_step_in_plain_language_with_its_boundaries(self):
        summary = self._summary(ready=False)
        step = summary["next"]
        self.assertTrue(step["title"])
        self.assertTrue(step["detail"])
        self.assertIn(step["actor"], (wp.ACTOR_INSTALLER, wp.ACTOR_INSTALLER_ELEVATED,
                                      wp.ACTOR_USER))
        self.assertIn("requires_admin", step)
        self.assertIn("reboots", step)

    def test_it_reports_the_admin_and_restart_boundaries_that_are_still_ahead(self):
        summary = self._summary(ready=False)
        self.assertTrue(summary["requires_admin_ahead"])
        self.assertTrue(summary["requires_reboot_ahead"])

    def test_the_image_and_verification_stages_are_always_ahead_of_a_ready_machine(self):
        summary = self._summary(ready=True)
        self.assertIn(wp.STAGE_BOUNDARY_VERIFICATION, summary["remaining_stages"])

    def test_a_fully_prepared_machine_still_stops_at_boundary_verification(self):
        summary = self._summary(ready=True)
        self.assertEqual(summary["stage"], wp.STAGE_BOUNDARY_VERIFICATION)
        self.assertFalse(summary["delegation_may_be_enabled"])
        self.assertIn("boundary", summary["gate"])

    def test_an_unconfigured_guest_image_is_its_own_stage_not_an_assumption(self):
        summary = self._summary(ready=True, choice={"enabled": True})
        self.assertEqual(summary["stage"], wp.STAGE_GUEST_IMAGE)
        self.assertIn(wp.STAGE_GUEST_IMAGE, summary["remaining_stages"])

    def test_a_mismatched_rootfs_hash_is_not_an_installed_image(self):
        summary = self._summary(ready=True, manifest_ok=False)
        self.assertEqual(summary["stage"], wp.STAGE_GUEST_IMAGE)

    def test_it_carries_the_evidence_behind_every_conclusion(self):
        summary = self._summary(ready=True)
        names = {check["name"] for check in summary["evidence"]["checks"]}
        self.assertIn("boundary_verified", names)
        self.assertIn("guest_image", names)
        self.assertIn("feature:VirtualMachinePlatform", names)
        self.assertIn("feature:Microsoft-Windows-Subsystem-Linux", names)

    def test_it_states_the_boundaries_it_cannot_cross(self):
        summary = self._summary(ready=True)
        self.assertIn("administrator", summary["limitations"])
        self.assertIn("firmware", summary["limitations"])
        self.assertIn("restart", summary["limitations"])

    def test_it_runs_no_command_on_a_non_windows_platform(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("setup reporting must not spawn a process off Windows")
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run",
                        side_effect=forbidden):
            for platform_name in ("darwin", "linux"):
                self.assertIsNone(onboard._windows_setup_summary(
                    WINDOWS_DELEGATION_CHOICE, platform=platform_name))

    def test_plan_and_status_both_carry_the_setup_ladder(self):
        stub = {"stage": wp.STAGE_WINDOWS_FEATURES, "delegation_may_be_enabled": False}
        with mock.patch.object(onboard, "_windows_preflight_summary", return_value=None), \
             mock.patch.object(onboard, "_windows_setup_summary", return_value=stub), \
             mock.patch.object(onboard.delegation, "platform_boundary_report",
                              return_value={"platform": "windows",
                                            "execution_worker_service": "not installed automatically",
                                            "continuous_service_verified": False,
                                            "resource_sampler": "n/a",
                                            "registration_and_config": "supported"}):
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            self.assertEqual(onboard.plan(choices, str(ROOT))["automatic_delegation"]["windows_setup"], stub)
            self.assertEqual(onboard.status(choices)["windows_setup"], stub)

    def test_the_plan_describes_reversible_per_user_activation_on_windows(self):
        with mock.patch.object(onboard, "_windows_preflight_summary", return_value=None), \
             mock.patch.object(onboard, "_windows_setup_summary", return_value=None), \
             mock.patch.object(onboard.delegation, "platform_boundary_report",
                              return_value={"platform": "windows",
                                            "execution_worker_service": "not installed automatically",
                                            "continuous_service_verified": False,
                                            "resource_sampler": "n/a",
                                            "registration_and_config": "supported"}):
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            activation = onboard.plan(choices, str(ROOT))["automatic_delegation"]["windows_activation"]
        self.assertEqual(activation["task_name"], "AgentBridgeExecutionWorker")
        self.assertIn("no administrator", activation["scope"].replace("\n", " "))
        self.assertIn("remove it yourself", activation["reversible"])
        self.assertIn("live", activation["note"])

    def test_the_plan_offers_no_windows_activation_on_a_mac(self):
        with mock.patch.object(onboard, "_windows_preflight_summary", return_value=None), \
             mock.patch.object(onboard, "_windows_setup_summary", return_value=None):
            choices = onboard.validate_answers(answers(automatic_delegation={"enabled": True}))
            plan_report = onboard.plan(choices, str(ROOT))["automatic_delegation"]
        self.assertIsNone(plan_report["windows_activation"])

    def test_the_gate_is_the_ladder_not_the_preflight(self):
        # prerequisites_ready is about local evidence; it must never be the
        # thing that turns delegation on.
        summary = self._summary(ready=True)
        self.assertFalse(summary["delegation_may_be_enabled"])



class WindowsDelegationStateTests(unittest.TestCase):
    """The verdict comes from the machine's record, never from a constant."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        from agent_bridge.orchestration import windows_delegation as wd
        from agent_bridge.orchestration import windows_evidence as wev
        from agent_bridge.orchestration import windows_wsl_runtime as wr
        self.wd, self.wev, self.wr = wd, wev, wr
        self.rootfs = self.root / "rootfs.tar"
        self.rootfs.write_bytes(b"pinned" * 64)
        import hashlib
        self.rootfs_sha256 = hashlib.sha256(self.rootfs.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({
            "schema_version": 1, "distro_release": "12.9",
            "rootfs_sha256": self.rootfs_sha256, "node_version": "20.19.0",
            "claude_version": "1.0.0", "codex_version": "0.1.0"}))
        self.choice = {
            "windows_wsl_runtime_root": str(self.root),
            "windows_wsl_rootfs_path": str(self.rootfs),
            "windows_wsl_manifest_path": str(self.manifest)}

    def tearDown(self):
        self.temp.cleanup()

    def _write_evidence(self, **overrides):
        fields = dict(
            recorded_at="2026-09-14T00:00:00Z",
            host_fingerprint=self.wev.host_fingerprint(),
            wsl_version="2.3.26.0", rootfs_sha256=self.rootfs_sha256,
            guest_runner_sha256=self.wd.guest_runner_sha256(),
            canaries=self.wr.CANARY_ORDER, boundary_verified=True,
            provider_lane=self.wev.ProviderLane())
        fields.update(overrides)
        _write_private(self.wev.evidence_path(str(self.root)),
                       json.dumps(self.wev.Evidence(**fields).as_dict()))

    def test_an_unconfigured_machine_is_blocked_by_name(self):
        verdict = onboard._windows_delegation_state({})
        self.assertEqual(verdict["state"], "blocked")
        self.assertEqual(verdict["reason"], "windows_wsl_configuration_missing")

    def test_a_configured_machine_with_no_record_is_blocked_by_name(self):
        verdict = onboard._windows_delegation_state(self.choice)
        self.assertEqual(verdict["state"], "blocked")
        self.assertEqual(verdict["reason"], "evidence_absent")

    def test_a_verified_machine_is_not_reported_as_blocked(self):
        self._write_evidence()
        verdict = onboard._windows_delegation_state(self.choice)
        self.assertEqual(verdict["state"], "ready")
        self.assertEqual(verdict["verified_at"], "2026-09-14T00:00:00Z")

    def test_a_verified_machine_still_has_a_closed_provider_lane(self):
        self._write_evidence()
        verdict = onboard._windows_delegation_state(self.choice)
        self.assertEqual(verdict["provider_lane"], "closed")
        self.assertEqual(verdict["provider_lane_providers"], [])

    def test_the_lane_opens_only_where_the_record_says_it_was_observed(self):
        self._write_evidence(provider_lane=self.wev.ProviderLane(
            verified=True, providers=("claude",), portability_observed=True,
            refresh_behaviour_observed=True))
        verdict = onboard._windows_delegation_state(self.choice)
        self.assertEqual(verdict["provider_lane"], "open")
        self.assertEqual(verdict["provider_lane_providers"], ["claude"])

    def test_a_replaced_image_puts_a_verified_machine_back_to_blocked(self):
        self._write_evidence()
        self.manifest.write_text(json.dumps({
            "schema_version": 1, "distro_release": "12.9",
            "rootfs_sha256": "0" * 64, "node_version": "20.19.0",
            "claude_version": "1.0.0", "codex_version": "0.1.0"}))
        verdict = onboard._windows_delegation_state(self.choice)
        self.assertEqual(verdict["state"], "blocked")
        self.assertEqual(verdict["reason"], "evidence_image_changed")

    def test_an_unreadable_manifest_is_blocked_by_name(self):
        verdict = onboard._windows_delegation_state(
            {**self.choice, "windows_wsl_manifest_path": str(self.root / "gone")})
        self.assertEqual(verdict["state"], "blocked")
        self.assertEqual(verdict["reason"], "manifest_unreadable")



if __name__ == "__main__":
    unittest.main()
