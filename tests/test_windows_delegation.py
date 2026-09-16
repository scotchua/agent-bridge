"""Portable tests for the Windows delegation dispatch seam.

The runtime itself is injected, so these tests exercise the gates, the
translation into a guest job, and the outcome shape without a WSL guest, a
Windows host, or any subprocess. They are not evidence that delegation works
end to end.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr
from agent_bridge.orchestration import windows_delegation as wd
from agent_bridge.orchestration import windows_evidence as wev
from agent_bridge.orchestration import windows_rootfs as wrf
from agent_bridge.orchestration import windows_wsl as ww
from agent_bridge.orchestration import windows_wsl_provision as wp
from agent_bridge.orchestration import windows_wsl_runtime as wr
import platform_support

ROOTFS_BYTES = b"pinned-rootfs" * 32
ROOTFS_SHA256 = hashlib.sha256(ROOTFS_BYTES).hexdigest()

MANIFEST = {
    "schema_version": 1,
    "distro_release": "12.9",
    "rootfs_sha256": ROOTFS_SHA256,
    "node_version": "20.19.0",
    "claude_version": "1.0.0",
    "codex_version": "0.1.0",
}


def _ready_state(**overrides):
    fields = dict(
        is_windows=True, windows_build_ok=True, firmware_virtualization=True,
        features_enabled={name: True for name in wp.REQUIRED_FEATURES},
        reboot_pending=False, wsl_present=True, wsl_version_ok=True,
        guest_image_installed=True, boundary_verified=True,
        provider_lane_verified=True)
    fields.update(overrides)
    return wp.ProvisionState(**fields)


def _guest_response(**overrides):
    fields = dict(schema_version=gr.SCHEMA_VERSION, status="completed",
                  harness_status=gr.HARNESS_COMPLETE, reason="ok", exit_code=0,
                  stdout_b64="", stderr_b64="", diff_b64="", verification=[],
                  truncated=False, duration_seconds=0.5)
    fields.update(overrides)
    return json.dumps(fields).encode("utf-8")


def _check(**overrides):
    fields = dict(program="git", returncode=0, stdout_sha256="a" * 64,
                  stderr_sha256="b" * 64, duration_seconds=0.25,
                  egress="verify-egress-denied:" + "c" * 64)
    fields.update(overrides)
    return fields


def _write_private(path, text):
    """Write a fixture record the way production writes one: owner-only."""

    platform_support.write_private_bytes(path, text.encode("utf-8"))

def _open_lane(*providers):
    """A lane a live record would have opened. Never the default."""

    return wev.ProviderLane(verified=True, providers=providers or gr.PROVIDER_TOOLS,
                            portability_observed=True,
                            refresh_behaviour_observed=True)


def _auth_source(provider):
    return {"kind": gr.AUTH_KINDS[provider], "token": "sk-session-token"}


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _result(**overrides):
    fields = dict(status=wr.STATUS_COMPLETED, reason=wr.REASON_OK,
                  distro_name="agent-bridge-abc123", exit_code=0,
                  stdout=_guest_response(), stderr=b"",
                  canaries_passed=True, spawned=True,
                  timings={"total_seconds": 1.0},
                  cleanup=wr.CleanupReport("ok", "ok", "ok", "ok"), detail="")
    fields.update(overrides)
    return wr.RuntimeResult(**fields)


class _Fixture:
    """A temporary directory holding a manifest, a sidecar and a rootfs."""

    def __init__(self, *, rootfs=ROOTFS_BYTES, manifest=None, sidecar=True,
                 architecture="arm64"):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.rootfs_path = base / "rootfs.tar"
        self.rootfs_path.write_bytes(rootfs)
        self.manifest_path = base / "manifest.json"
        self.manifest_path.write_text(json.dumps(manifest or MANIFEST))
        self.sidecar_path = base / "sidecar.json"
        if sidecar:
            self.sidecar_path.write_text(json.dumps({
                "schema_version": 1, "architecture": architecture,
                "rootfs_sha256": hashlib.sha256(rootfs).hexdigest()}))
        self.config = wd.DelegationConfig(
            runtime_root="C:\\Users\\tester\\.agent-bridge\\wsl",
            rootfs_path=str(self.rootfs_path),
            manifest_path=str(self.manifest_path),
            sidecar_path=str(self.sidecar_path) if sidecar else None)

    def close(self):
        self.directory.cleanup()


#: A repository and a brief that exist on disk, because the executor now
#: packs the one and reads the other instead of taking a bespoke test field.
_WORKSPACE = None


#: The commit the fixture repository is sitting on. Resolved from a real git
#: repository rather than invented: translate() now resolves the admitted base
#: and refuses a worktree that is on a different commit, so a fake ``.git``
#: directory would not exercise the check it is meant to exercise.
_BASE_SHA = None


def setUpModule():
    global _WORKSPACE, _BASE_SHA
    _WORKSPACE = tempfile.TemporaryDirectory()
    base = Path(_WORKSPACE.name)
    repo = base / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (base / "brief.txt").write_bytes(b"Do the synthetic thing.\n")
    _BASE_SHA = _init_repo(repo)


def _init_repo(repo: Path) -> str:
    import subprocess
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present in CI and locally
        raise unittest.SkipTest("git is required for the base-binding fixtures")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@invalid")
    for args in (["init", "-q"], ["add", "-A"],
                 ["commit", "-q", "-m", "base"]):
        subprocess.run([git, "-C", str(repo)] + args, check=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = subprocess.run([git, "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, env=env, stdout=subprocess.PIPE)
    return out.stdout.decode().strip()


def tearDownModule():
    _WORKSPACE.cleanup()


def _request(**overrides):
    """The request the execution queue actually stores. No test-only fields."""

    root = Path(_WORKSPACE.name)
    brief = root / "brief.txt"
    fields = dict(provider="claude", repo=str(root / "repo"),
                  brief=str(brief),
                  # The digest the queue records at admission. translate()
                  # binds the brief it reads to this, so it is part of a real
                  # request rather than a test-only field.
                  brief_sha256=hashlib.sha256(brief.read_bytes()).hexdigest(),
                  base="HEAD",
                  classification="public", model="sonnet", effort="low",
                  verify_argv=[["git", "status"]], timeout_seconds=300)
    fields.update(overrides)
    return fields


class GateOrderTests(unittest.TestCase):
    """Cheapest and most fundamental first: a machine that is not Windows
    never reads a manifest."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def _executor(self, **overrides):
        fields = dict(provision_state=_ready_state(), provider_lane=_open_lane(),
                      platform_name="win32", machine="arm64",
                      auth_source=_auth_source,
                      run_job=lambda *a, **k: _result())
        fields.update(overrides)
        return wd.WindowsWslExecutor(self.fixture.config, **fields)

    def test_a_ready_windows_machine_passes_every_gate(self):
        self.assertEqual(self._executor().refusal(_request()), "")

    def test_a_non_windows_host_is_refused_first(self):
        self.assertEqual(self._executor(platform_name="darwin").refusal(_request()),
                         "unsupported_platform")

    def test_an_unverified_host_is_refused_and_names_the_evidence_failure(self):
        executor = self._executor(provision_state=None,
                                  evidence_reason="evidence_other_machine")
        self.assertEqual(executor.refusal(_request()),
                         "provisioning_unverified:evidence_other_machine")

    def test_an_unverified_host_with_no_stated_reason_still_refuses(self):
        self.assertEqual(self._executor(provision_state=None).refusal(_request()),
                         "provisioning_unverified:evidence_absent")

    def test_incomplete_provisioning_names_the_stage_it_stopped_at(self):
        executor = self._executor(
            provision_state=_ready_state(boundary_verified=False))
        self.assertEqual(executor.refusal(_request()),
                         "provisioning_incomplete:boundary_verification")

    def test_every_unfinished_stage_blocks_dispatch(self):
        for stage in wp.STAGE_ORDER[:-1]:
            executor = self._executor(provision_state=wp._state_at(_ready_state(), stage))
            self.assertTrue(executor.refusal(_request()).startswith(
                ("provisioning_incomplete", "unsupported_platform")), stage)


class ClassificationGateTests(unittest.TestCase):
    """Public and synthetic by default; client-derived needs an approval."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=_ready_state(),
            provider_lane=_open_lane(), platform_name="win32", machine="arm64",
            auth_source=_auth_source, run_job=lambda *a, **k: _result())

    def test_the_default_classifications_are_public_and_synthetic(self):
        self.assertIn("public", wd.DEFAULT_ALLOWED_CLASSIFICATIONS)
        self.assertIn("synthetic", wd.DEFAULT_ALLOWED_CLASSIFICATIONS)
        for classification in ("public", "synthetic"):
            self.assertEqual(self.executor.refusal(
                _request(classification=classification)), "")

    def test_a_missing_classification_is_refused(self):
        for value in (None, "", 7):
            self.assertEqual(self.executor.refusal(_request(classification=value)),
                             "classification_missing")

    def test_an_unknown_classification_is_refused(self):
        self.assertEqual(self.executor.refusal(_request(classification="mystery")),
                         "classification_not_allowed")

    def test_client_derived_work_is_refused_without_an_explicit_approval(self):
        for classification in sorted(wd.APPROVAL_REQUIRED_CLASSIFICATIONS):
            self.assertEqual(
                self.executor.refusal(_request(classification=classification)),
                "classification_requires_explicit_approval", classification)

    def test_client_derived_work_runs_only_with_a_matching_approval(self):
        self.assertEqual(self.executor.refusal(_request(
            classification="client", approvals={"classification": "client"})), "")

    def test_an_approval_for_a_different_classification_does_not_transfer(self):
        self.assertEqual(self.executor.refusal(_request(
            classification="client", approvals={"classification": "synthetic"})),
            "classification_requires_explicit_approval")

    def test_a_malformed_approvals_field_grants_nothing(self):
        for approvals in ("client", ["client"], None):
            self.assertEqual(self.executor.refusal(_request(
                classification="client", approvals=approvals)),
                "classification_requires_explicit_approval")


class ProviderBlockerTests(unittest.TestCase):
    """Refused by name, not attempted and failed inside the guest."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.spawned = []
        self.executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=_ready_state(),
            platform_name="win32", machine="arm64",
            run_job=lambda *a, **k: self.spawned.append(a) or _result())

    def test_a_provider_job_is_refused_before_anything_is_spawned(self):
        for provider in ("claude", "codex"):
            outcome = self.executor(_request(provider=provider))
            self.assertEqual(outcome["detail"], "provider_lane_unverified")
            self.assertFalse(outcome["spawned"])
        self.assertEqual(self.spawned, [])

    def test_only_a_provider_can_be_dispatched_at_all(self):
        """There is no tool-shaped production request to smuggle work through."""
        for provider in ("python", "node", "bash", None, 7):
            outcome = self.executor(_request(provider=provider))
            self.assertEqual(outcome["detail"], "provider_not_supported")
        self.assertEqual(self.spawned, [])

    def test_a_lane_proven_live_for_one_provider_opens_only_that_one(self):
        executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=_ready_state(),
            provider_lane=_open_lane("claude"),
            platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request(provider="claude")), "")
        self.assertEqual(executor.refusal(_request(provider="codex")),
                         "provider_lane_unverified")

    def test_a_lane_recorded_without_both_observations_stays_closed(self):
        executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=_ready_state(),
            provider_lane=wev.ProviderLane(verified=True, providers=("claude",),
                                           portability_observed=True),
            platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request(provider="claude")),
                         "provider_lane_unverified")

    def test_an_executor_built_without_a_lane_has_no_lane(self):
        self.assertFalse(self.executor.provider_lane.enabled_for("claude"))

    def test_the_provider_clis_are_runnable_tools_but_still_gated(self):
        """The runner can execute them now; reaching them is a separate gate."""
        self.assertIn("claude", wd.SUPPORTED_TOOLS)
        self.assertIn("codex", wd.SUPPORTED_TOOLS)

    def test_the_blocker_names_what_is_unproven_rather_than_what_is_refused(self):
        text = wd.PROVIDER_EXECUTION_BLOCKER
        self.assertIn("API key", text)
        self.assertIn("subscription", text)
        self.assertIn("live", text)

    def test_there_is_no_host_side_session_source_yet(self):
        """Opening the lane without building the source must fail loudly."""
        executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=_ready_state(),
            provider_lane=_open_lane(), platform_name="win32", machine="arm64",
            run_job=lambda *a, **k: self.spawned.append(a) or _result())
        outcome = executor(_request())
        self.assertEqual(outcome["reason"], "provider_session_unavailable")
        self.assertEqual(self.spawned, [])


class ImageVerificationTests(unittest.TestCase):
    def _executor(self, fixture, **overrides):
        fields = dict(provision_state=_ready_state(), provider_lane=_open_lane(),
                      platform_name="win32", machine="arm64",
                      auth_source=_auth_source,
                      run_job=lambda *a, **k: _result())
        fields.update(overrides)
        return wd.WindowsWslExecutor(fixture.config, **fields)

    def test_a_matching_image_verifies(self):
        fixture = _Fixture()
        self.addCleanup(fixture.close)
        self.assertTrue(self._executor(fixture).verify_image().passed)

    def test_a_rootfs_that_changed_since_install_is_caught_at_dispatch(self):
        """Checked here as well as at install time, because the file can
        change in between and this is the last point before it is imported."""
        fixture = _Fixture()
        self.addCleanup(fixture.close)
        Path(fixture.rootfs_path).write_bytes(b"something else entirely")
        check = self._executor(fixture).verify_image()
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "rootfs_hash_mismatch")

    def test_an_image_for_the_wrong_architecture_is_refused(self):
        fixture = _Fixture(architecture="amd64")
        self.addCleanup(fixture.close)
        check = self._executor(fixture, machine="aarch64").verify_image()
        self.assertEqual(check.reason, "architecture_mismatch")

    def test_an_image_with_no_architecture_statement_is_refused(self):
        fixture = _Fixture(sidecar=False)
        self.addCleanup(fixture.close)
        self.assertEqual(self._executor(fixture).verify_image().reason,
                         "architecture_unknown")

    def test_an_unreadable_manifest_is_a_path_free_refusal(self):
        fixture = _Fixture()
        self.addCleanup(fixture.close)
        Path(fixture.manifest_path).unlink()
        with self.assertRaises(wd.DelegationRefused) as caught:
            self._executor(fixture).verify_image()
        self.assertEqual(caught.exception.reason, "manifest_unreadable")
        self.assertNotIn(str(fixture.manifest_path), caught.exception.detail)

    def test_a_malformed_manifest_does_not_quote_the_offending_value(self):
        fixture = _Fixture(manifest={**MANIFEST, "node_version": "latest"})
        self.addCleanup(fixture.close)
        with self.assertRaises(wd.DelegationRefused) as caught:
            self._executor(fixture).verify_image()
        self.assertEqual(caught.exception.reason, "manifest_invalid")
        self.assertNotIn("latest", caught.exception.detail)

    def test_a_failed_verification_aborts_without_spawning(self):
        fixture = _Fixture()
        self.addCleanup(fixture.close)
        Path(fixture.rootfs_path).write_bytes(b"changed")
        spawned = []
        executor = self._executor(
            fixture, run_job=lambda *a, **k: spawned.append(a) or _result())
        outcome = executor(_request())
        self.assertEqual(outcome["reason"], "image_unverified")
        self.assertFalse(outcome["spawned"])
        self.assertEqual(spawned, [])


class JobTranslationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.manifest = ww.parse_manifest(MANIFEST)

    def _build(self, **overrides):
        fields = dict(config=self.fixture.config, manifest=self.manifest,
                      tool="node", args=["--version"], stdin_data="",
                      distro_token="abc123")
        fields.update(overrides)
        return wd.build_job_request(**fields)

    def test_the_guest_argv_is_fixed_and_carries_nothing_variable(self):
        """A single unescaped value on an argv would be a command."""
        job = self._build(args=["--eval", "require('fs')"], stdin_data="a brief")
        self.assertEqual(job.job_spec["command"], [ww.GUEST_RUNNER_PATH, "--run"])
        for value in ("--eval", "require('fs')", "a brief"):
            self.assertNotIn(value, " ".join(job.job_spec["command"]))

    def test_the_variable_part_travels_in_the_piped_payload(self):
        job = self._build(args=["--eval", "1+1"], stdin_data="a brief")
        payload = json.loads(job.stdin_data)
        self.assertEqual(payload["args"], ["--eval", "1+1"])
        self.assertEqual(payload["stdin"], "a brief")

    def test_the_payload_is_valid_to_the_guests_own_validator(self):
        payload = json.loads(self._build().stdin_data)
        gr.validate_request(payload)

    def test_the_job_spec_passes_the_hosts_strict_validator(self):
        ww.validate_job_spec(self._build().job_spec)

    def test_the_pinned_runner_hash_is_the_hash_of_the_shipped_file(self):
        job = self._build()
        expected = hashlib.sha256(
            Path(gr.__file__).read_bytes()).hexdigest()
        self.assertEqual(job.guest_runner_sha256, expected)

    def test_an_unsupported_tool_is_refused_at_translation_too(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            self._build(tool="bash")
        self.assertEqual(caught.exception.reason, "tool_not_supported")

    def test_a_payload_the_guest_would_reject_fails_here_instead(self):
        """Better a named refusal on the host than a failure inside a sandbox
        that is about to be deleted."""
        with self.assertRaises(wd.DelegationRefused) as caught:
            self._build(args=["x" * (gr.MAX_ARG_BYTES + 1)])
        self.assertEqual(caught.exception.reason, "guest_request_invalid")
        self.assertEqual(caught.exception.detail, "args_too_large")

    def test_each_job_gets_its_own_distro_token(self):
        first = wd.build_job_request(config=self.fixture.config,
                                     manifest=self.manifest, tool="node",
                                     args=[], stdin_data="")
        second = wd.build_job_request(config=self.fixture.config,
                                      manifest=self.manifest, tool="node",
                                      args=[], stdin_data="")
        self.assertNotEqual(first.distro_token, second.distro_token)


class GuestResponseTests(unittest.TestCase):
    """The guest is inside the boundary; its output is untrusted input."""

    def test_a_well_formed_response_parses(self):
        parsed = wd.parse_guest_response(_guest_response())
        self.assertEqual(parsed["status"], "completed")

    def test_malformed_output_is_refused_not_partially_believed(self):
        for raw, reason in (
                (b"{not json", "guest_response_malformed"),
                (b'"a string"', "guest_response_malformed"),
                (_guest_response(schema_version=99),
                 "guest_response_schema_unsupported"),
                (_guest_response(status="weird"),
                 "guest_response_status_unknown"),
                (b'{"schema_version":1,"status":"completed"}',
                 "guest_response_keys_invalid")):
            with self.assertRaises(wd.DelegationRefused) as caught:
                wd.parse_guest_response(raw)
            self.assertEqual(caught.exception.reason, reason, raw)

    def test_an_extra_field_is_refused_because_it_is_not_the_pinned_guest(self):
        payload = json.loads(_guest_response())
        payload["surprise"] = 1
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(json.dumps(payload).encode())
        self.assertEqual(caught.exception.reason, "guest_response_keys_invalid")

    def test_every_field_type_is_checked(self):
        for override, reason in (
                ({"reason": ""}, "guest_response_reason_invalid"),
                ({"reason": "has spaces"}, "guest_response_reason_invalid"),
                ({"reason": "x" * 200}, "guest_response_reason_invalid"),
                ({"exit_code": "0"}, "guest_response_exit_code_invalid"),
                ({"exit_code": True}, "guest_response_exit_code_invalid"),
                ({"exit_code": 99999}, "guest_response_exit_code_invalid"),
                ({"truncated": "no"}, "guest_response_malformed"),
                ({"duration_seconds": "fast"}, "guest_response_malformed"),
                ({"duration_seconds": -1}, "guest_response_malformed")):
            with self.subTest(override=override):
                with self.assertRaises(wd.DelegationRefused) as caught:
                    wd.parse_guest_response(_guest_response(**override))
                self.assertEqual(caught.exception.reason, reason)

    def test_binary_payloads_are_decoded_not_carried_encoded(self):
        parsed = wd.parse_guest_response(_guest_response(
            stdout_b64=_b64(b"\x00\xffbinary"), diff_b64=_b64(b"--- a\n+++ b\n")))
        self.assertEqual(parsed["stdout"], b"\x00\xffbinary")
        self.assertEqual(parsed["diff"], b"--- a\n+++ b\n")

    def test_a_field_that_is_not_base64_is_refused(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(_guest_response(stdout_b64="not base64!!"))
        self.assertEqual(caught.exception.reason, "guest_response_not_base64")

    def test_a_decoded_field_over_its_bound_is_refused(self):
        oversized = _b64(b"x" * (gr.MAX_DIFF_BYTES + 1))
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(_guest_response(diff_b64=oversized))
        self.assertEqual(caught.exception.reason, "guest_response_too_large")

    def test_a_response_contradicting_itself_is_refused(self):
        for override, detail in (
                ({"truncated": True}, "truncated_completed"),
                ({"exit_code": 3}, "completed_nonzero")):
            with self.subTest(override=override):
                with self.assertRaises(wd.DelegationRefused) as caught:
                    wd.parse_guest_response(_guest_response(**override))
                self.assertEqual(caught.exception.detail, detail)

    def test_an_oversized_response_is_refused_before_it_is_parsed(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(b"x" * (gr.MAX_TOTAL_REQUEST_BYTES + 1))
        self.assertEqual(caught.exception.reason, "guest_response_too_large")


class OutcomeTests(unittest.TestCase):
    """The receipt records what happened AND returns the delegated result."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def _dispatch(self, result, job_dir=None):
        executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=_ready_state(),
            provider_lane=_open_lane(), platform_name="win32", machine="arm64",
            auth_source=_auth_source, run_job=lambda *a, **k: result)
        return executor(_request(), job_dir)

    def test_a_clean_run_reports_success(self):
        outcome = self._dispatch(_result())
        self.assertEqual(outcome["returncode"], 0)
        self.assertEqual(outcome["status"], wr.STATUS_COMPLETED)
        self.assertTrue(outcome["canaries_passed"])

    def test_the_real_result_comes_back_not_a_fingerprint_of_it(self):
        payload = _guest_response(stdout_b64=_b64(b"the answer"),
                                  diff_b64=_b64(b"--- a\n+++ b\n"))
        with tempfile.TemporaryDirectory() as tmp:
            outcome = self._dispatch(_result(stdout=payload), job_dir=Path(tmp))
            self.assertEqual(outcome["stdout_bytes"], len(b"the answer"))
            self.assertEqual(outcome["stdout_sha256"],
                             hashlib.sha256(b"the answer").hexdigest())
            self.assertEqual(outcome["diff_bytes"], len(b"--- a\n+++ b\n"))
            self.assertEqual(
                (Path(tmp) / "guest.stdout").read_bytes(), b"the answer")
            self.assertEqual(
                (Path(tmp) / "guest.diff").read_bytes(), b"--- a\n+++ b\n")

    def test_written_results_are_owner_only(self):
        payload = _guest_response(stdout_b64=_b64(b"answer"))
        with tempfile.TemporaryDirectory() as tmp:
            self._dispatch(_result(stdout=payload), job_dir=Path(tmp))
            platform_support.assert_owner_only(
                self, Path(tmp) / "guest.stdout", 0o600)

    def test_an_empty_stream_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._dispatch(_result(stdout=_guest_response()), job_dir=Path(tmp))
            self.assertFalse((Path(tmp) / "guest.stdout").exists())
            self.assertFalse((Path(tmp) / "guest.diff").exists())

    def test_the_receipt_hashes_the_delegated_output_not_the_envelope(self):
        payload = _guest_response(stdout_b64=_b64(b"inner"))
        outcome = self._dispatch(_result(stdout=payload))
        self.assertNotEqual(outcome["stdout_sha256"],
                            hashlib.sha256(payload).hexdigest())
        self.assertEqual(outcome["stdout_sha256"],
                         hashlib.sha256(b"inner").hexdigest())

    def test_an_aborted_runtime_result_is_not_reported_as_success(self):
        outcome = self._dispatch(_result(
            status=wr.STATUS_ABORTED, reason="canary_failed", exit_code=None,
            stdout=b"", canaries_passed=False))
        self.assertEqual(outcome["returncode"], 1)
        self.assertEqual(outcome["reason"], "canary_failed")

    def test_a_guest_that_failed_is_not_reported_as_success(self):
        """The host lifecycle can complete cleanly while the work inside it
        failed; those are different questions."""
        outcome = self._dispatch(_result(
            stdout=_guest_response(status="aborted",
                                   harness_status=gr.HARNESS_FAILED,
                                   reason="provider_nonzero_exit", exit_code=2)))
        self.assertEqual(outcome["returncode"], 1)
        self.assertFalse(outcome["harness_ok"])
        self.assertEqual(outcome["harness_status"], gr.HARNESS_FAILED)
        self.assertEqual(outcome["guest"]["reason"], "provider_nonzero_exit")

    def test_a_verification_failure_is_not_a_completed_job(self):
        """A provider that exited 0 while the checks failed is not success."""
        outcome = self._dispatch(_result(stdout=_guest_response(
            status="completed", harness_status=gr.HARNESS_VERIFICATION_FAILED,
            reason="verification_failed",
            verification=[_check(returncode=1)])))
        self.assertEqual(outcome["returncode"], 1)
        self.assertFalse(outcome["harness_ok"])
        self.assertEqual(outcome["harness_status"], gr.HARNESS_VERIFICATION_FAILED)
        self.assertEqual(outcome["guest"]["verification"][0]["returncode"], 1)

    def test_every_outcome_speaks_the_queues_contract(self):
        from agent_bridge.orchestration import execution_queue as eq
        for result in (_result(),
                       _result(stdout=_guest_response(
                           status="completed",
                           harness_status=gr.HARNESS_VERIFICATION_FAILED,
                           reason="verification_failed",
                           verification=[_check(returncode=1)])),
                       _result(status=wr.STATUS_ABORTED, reason="canary_failed",
                               exit_code=None, stdout=b"", canaries_passed=False),
                       _result(stdout=b"not json at all")):
            eq.validate_outcome(self._dispatch(result))

    def test_a_refusal_also_speaks_the_queues_contract(self):
        from agent_bridge.orchestration import execution_queue as eq
        executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=None, platform_name="win32")
        outcome = executor(_request())
        eq.validate_outcome(outcome)
        self.assertEqual(outcome["harness_status"], gr.HARNESS_ABORTED)

    def test_incomplete_cleanup_is_carried_into_the_outcome(self):
        outcome = self._dispatch(_result(
            status=wr.STATUS_ABORTED, reason=wr.REASON_CLEANUP_FAILED, stdout=b"",
            cleanup=wr.CleanupReport("failed+exit_1", "ok", "ok", "retained+x")))
        self.assertFalse(outcome["cleanup"]["complete"])
        self.assertEqual(outcome["reason"], wr.REASON_CLEANUP_FAILED)

    def test_a_malformed_guest_response_aborts_a_clean_looking_run(self):
        outcome = self._dispatch(_result(stdout=b"not json at all"))
        self.assertEqual(outcome["returncode"], 1)
        self.assertEqual(outcome["reason"], "guest_response_malformed")

    def test_the_outcome_is_json_serialisable_and_path_free(self):
        outcome = self._dispatch(_result())
        serialised = json.dumps(outcome)
        self.assertNotIn(str(self.fixture.rootfs_path), serialised)
        self.assertNotIn("C:\\Users", serialised)



class WorkspacePackingTests(unittest.TestCase):
    """The admitted worktree, packed for the guest. Bounded and deterministic."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)

    def _names(self):
        import io as _io
        import tarfile as _tarfile
        encoded, counts = wd.pack_workspace(self.repo)
        with _tarfile.open(fileobj=_io.BytesIO(base64.b64decode(encoded))) as tar:
            return sorted(tar.getnames()), counts

    def test_regular_files_are_packed_with_their_content(self):
        (self.repo / "a.py").write_text("print(1)\n", encoding="utf-8")
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "b.txt").write_text("two\n", encoding="utf-8")
        names, counts = self._names()
        self.assertEqual(names, ["a.py", "pkg/b.txt"])
        self.assertEqual(counts["files"], 2)

    def test_git_metadata_never_crosses_the_boundary(self):
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("token\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("x\n", encoding="utf-8")
        names, _ = self._names()
        self.assertEqual(names, ["keep.txt"])

    def test_a_symlink_is_never_followed_out_of_the_repository(self):
        platform_support.require_symlinks(self)
        import os as _os
        outside = Path(self.directory.name).parent / "outside-secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        _os.symlink(outside, self.repo / "escape.txt")
        (self.repo / "keep.txt").write_text("x\n", encoding="utf-8")
        names, counts = self._names()
        self.assertEqual(names, ["keep.txt"])
        self.assertEqual(counts["skipped"], 1)

    def test_a_fifo_is_skipped_rather_than_opened(self):
        import os as _os
        if not hasattr(_os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        _os.mkfifo(self.repo / "pipe")
        (self.repo / "keep.txt").write_text("x\n", encoding="utf-8")
        names, counts = self._names()
        self.assertEqual(names, ["keep.txt"])
        self.assertEqual(counts["skipped"], 1)

    def test_a_single_oversized_file_is_refused(self):
        (self.repo / "big.bin").write_bytes(
            b"x" * (wd.MAX_WORKSPACE_FILE_BYTES + 1))
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.pack_workspace(self.repo)
        self.assertEqual(caught.exception.reason, "workspace_file_too_large")

    def test_too_many_files_are_refused(self):
        for index in range(wd.MAX_WORKSPACE_FILES + 1):
            (self.repo / f"f{index}").write_text("x", encoding="utf-8")
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.pack_workspace(self.repo)
        self.assertEqual(caught.exception.reason, "workspace_too_many_files")

    def test_the_same_tree_packs_to_the_same_bytes(self):
        (self.repo / "a.py").write_text("print(1)\n", encoding="utf-8")
        first, _ = wd.pack_workspace(self.repo)
        second, _ = wd.pack_workspace(self.repo)
        self.assertEqual(first, second)

    def test_the_archive_is_acceptable_to_the_guests_own_validator(self):
        (self.repo / "a.py").write_text("print(1)\n", encoding="utf-8")
        encoded, _ = wd.pack_workspace(self.repo)
        self.assertEqual(gr.validate_workspace(encoded), encoded)


class BriefReadingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def test_the_brief_is_read_as_text(self):
        target = self.base / "b.txt"
        target.write_bytes(b"do the thing\n")
        self.assertEqual(wd.read_brief(target), "do the thing\n")

    def test_a_bom_prefixed_brief_does_not_embed_a_stray_character(self):
        # A Windows editor or PowerShell's default encoding can prepend a
        # UTF-8 BOM. Plain "utf-8" decodes this without raising, so the bug
        # is silent: a literal U+FEFF ends up embedded at the start of the
        # text sent to the provider.
        target = self.base / "b.txt"
        target.write_bytes(b"\xef\xbb\xbf" + b"do the thing\n")
        self.assertEqual(wd.read_brief(target), "do the thing\n")

    def test_a_symlinked_brief_is_refused(self):
        platform_support.require_symlinks(self)
        import os as _os
        real = self.base / "real.txt"
        real.write_text("x\n", encoding="utf-8")
        link = self.base / "link.txt"
        _os.symlink(real, link)
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.read_brief(link)
        self.assertEqual(caught.exception.reason, "brief_invalid")

    def test_a_brief_that_is_not_utf8_is_refused(self):
        target = self.base / "b.txt"
        target.write_bytes(b"\xff\xfe")
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.read_brief(target)
        self.assertEqual(caught.exception.detail, "not_utf8")

    def test_an_oversized_brief_is_refused(self):
        target = self.base / "b.txt"
        target.write_bytes(b"x" * (gr.MAX_BRIEF_BYTES + 1))
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.read_brief(target)
        self.assertEqual(caught.exception.reason, "brief_too_large")


class ProviderJobTranslationTests(unittest.TestCase):
    """The real queue request, translated. No bespoke fields."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.executor = wd.WindowsWslExecutor(
            self.fixture.config, provision_state=_ready_state(),
            provider_lane=_open_lane(), platform_name="win32", machine="arm64",
            auth_source=_auth_source, run_job=lambda *a, **k: _result())

    def _payload(self, **overrides):
        job, _base = self.executor.translate(_request(**overrides))
        return json.loads(job.stdin_data)

    def test_the_translated_payload_is_a_provider_job(self):
        payload = self._payload()
        self.assertEqual(payload["mode"], gr.MODE_PROVIDER_JOB)
        self.assertEqual(payload["provider"], "claude")
        self.assertNotIn("tool", payload)
        self.assertNotIn("args", payload)
        self.assertNotIn("stdin", payload)

    def test_the_stored_model_effort_and_checks_are_carried_over(self):
        payload = self._payload(model="opus", effort="high",
                                verify_argv=[["git", "diff"], ["pytest", "-q"]])
        self.assertEqual(payload["model"], "opus")
        self.assertEqual(payload["effort"], "high")
        self.assertEqual(payload["verify_argv"],
                         [["git", "diff"], ["pytest", "-q"]])

    def test_the_brief_travels_as_content_not_as_a_host_path(self):
        payload = self._payload()
        self.assertEqual(payload["brief"], "Do the synthetic thing.\n")
        self.assertNotIn(_WORKSPACE.name, json.dumps(payload))

    def test_the_repository_is_packed_into_the_archive(self):
        import io as _io
        import tarfile as _tarfile
        payload = self._payload()
        with _tarfile.open(fileobj=_io.BytesIO(
                base64.b64decode(payload["workspace_tar_b64"]))) as tar:
            self.assertEqual(sorted(tar.getnames()), ["main.py"])

    def test_the_guest_argv_stays_fixed_for_a_provider_job(self):
        job, _base = self.executor.translate(_request())
        self.assertEqual(job.job_spec["command"], [ww.GUEST_RUNNER_PATH, "--run"])
        self.assertNotIn("claude", " ".join(job.job_spec["command"]))

    def test_the_payload_is_valid_to_the_guests_own_validator(self):
        gr.validate_request(self._payload())

    def test_a_check_the_guest_would_refuse_fails_on_the_host(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            self.executor.translate(_request(verify_argv=[["bash", "-c", "x"]]))
        self.assertEqual(caught.exception.reason, "guest_request_invalid")
        self.assertEqual(caught.exception.detail, "verify_program_not_allowed")

    def test_a_request_with_no_checks_is_refused_before_it_runs(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            self.executor.translate(_request(verify_argv=[]))
        self.assertEqual(caught.exception.detail, "verify_argv_required")

    def test_the_verification_timeout_never_exceeds_the_job_timeout(self):
        payload = self._payload()
        self.assertLessEqual(payload["verify_timeout_seconds"],
                             payload["timeout_seconds"])

    def test_a_missing_repository_is_a_named_refusal_not_a_crash(self):
        outcome = self.executor(_request(repo=str(Path(_WORKSPACE.name) / "gone")))
        self.assertEqual(outcome["reason"], "workspace_unreadable")
        self.assertFalse(outcome["spawned"])

    def test_the_session_never_reaches_the_argv_or_the_job_spec(self):
        job, _base = self.executor.translate(_request())
        self.assertNotIn("sk-session-token", json.dumps(job.job_spec))
        self.assertIn("sk-session-token", job.stdin_data)


class VerificationEvidenceTests(unittest.TestCase):
    """The guest's account of what it ran is untrusted input too."""

    def test_well_formed_evidence_is_carried_through(self):
        parsed = wd.parse_guest_response(_guest_response(
            harness_status=gr.HARNESS_VERIFICATION_FAILED,
            reason="verification_failed",
            verification=[_check(), _check(program="pytest", returncode=1)]))
        self.assertEqual(len(parsed["verification"]), 2)
        self.assertEqual(parsed["verification"][1]["program"], "pytest")

    def test_evidence_that_is_not_a_list_is_refused(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(_guest_response(verification={}))
        self.assertEqual(caught.exception.detail, "not_a_list")

    def test_a_program_the_guest_was_never_allowed_to_run_is_refused(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(_guest_response(
                verification=[_check(program="bash")]))
        self.assertEqual(caught.exception.detail, "program")

    def test_a_digest_that_is_not_a_digest_is_refused(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(_guest_response(
                verification=[_check(stdout_sha256="nope")]))
        self.assertEqual(caught.exception.detail, "digest")

    def test_a_completion_claimed_over_failing_evidence_is_refused(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(_guest_response(
                harness_status=gr.HARNESS_COMPLETE,
                verification=[_check(returncode=1)]))
        self.assertEqual(caught.exception.detail, "complete_with_failure")

    def test_a_verification_failure_with_no_failing_check_is_refused(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.parse_guest_response(_guest_response(
                harness_status=gr.HARNESS_VERIFICATION_FAILED,
                reason="verification_failed", verification=[_check()]))
        self.assertEqual(caught.exception.detail, "failure_without_evidence")

    def test_a_harness_status_outside_the_vocabulary_is_refused(self):
        for value in ("finished", "", None, 1):
            with self.assertRaises(wd.DelegationRefused) as caught:
                wd.parse_guest_response(_guest_response(harness_status=value))
            self.assertEqual(caught.exception.reason,
                             "guest_response_harness_status_invalid")

    def test_the_two_status_fields_must_agree(self):
        for status, harness, detail in (
                ("completed", gr.HARNESS_FAILED, "completed_status"),
                ("completed", gr.HARNESS_ABORTED, "completed_status"),
                ("aborted", gr.HARNESS_COMPLETE, "aborted_status")):
            with self.assertRaises(wd.DelegationRefused) as caught:
                wd.parse_guest_response(_guest_response(
                    status=status, harness_status=harness,
                    exit_code=0 if status == "completed" else 1))
            self.assertEqual(caught.exception.detail, detail)


class DocumentedLimitationTests(unittest.TestCase):
    def test_no_live_validation_is_claimed(self):
        self.assertIn("no job has been dispatched through this executor on a live",
                      wd.NO_LIVE_VALIDATION)
        self.assertIn("has been run against a live Windows host", wd.__doc__)



class VerifiedExecutorTests(unittest.TestCase):
    """The production constructor: ready only on a machine-bound record."""

    def setUp(self):
        self.fixture = _Fixture()
        self.runtime = tempfile.TemporaryDirectory()
        self.config = wd.DelegationConfig(
            runtime_root=self.runtime.name,
            rootfs_path=str(self.fixture.rootfs_path),
            manifest_path=str(self.fixture.manifest_path),
            sidecar_path=str(self.fixture.sidecar_path))

    def tearDown(self):
        self.runtime.cleanup()
        self.fixture.close()

    def _write_evidence(self, **overrides):
        from agent_bridge.orchestration import windows_evidence as wev
        fields = dict(
            recorded_at="2026-09-14T00:00:00Z",
            host_fingerprint="c" * 64, wsl_version="2.3.26.0",
            rootfs_sha256=ROOTFS_SHA256,
            guest_runner_sha256=wd.guest_runner_sha256(),
            canaries=wr.CANARY_ORDER, boundary_verified=True,
            provider_lane=wev.ProviderLane())
        fields.update(overrides)
        _write_private(wev.evidence_path(self.runtime.name),
                       json.dumps(wev.Evidence(**fields).as_dict()))

    def test_with_no_record_the_executor_exists_and_refuses_by_name(self):
        executor = wd.verified_executor(self.config, platform_name="win32",
                                        machine="arm64")
        self.assertIsNone(executor.provision_state)
        self.assertEqual(executor.refusal(_request()),
                         "provisioning_unverified:evidence_absent")

    def test_an_unreadable_manifest_refuses_without_touching_the_record(self):
        config = wd.DelegationConfig(
            runtime_root=self.runtime.name,
            rootfs_path=str(self.fixture.rootfs_path),
            manifest_path=str(Path(self.runtime.name) / "absent.json"))
        executor = wd.verified_executor(config, platform_name="win32")
        self.assertEqual(executor.refusal(_request()),
                         "provisioning_unverified:manifest_unreadable")

    def test_a_matching_record_makes_the_boundary_ready(self):
        """Ready means the boundary is proven. The lane is a second gate."""
        self._write_evidence(provider_lane=_open_lane())
        executor = wd.verified_executor(self.config, fingerprint="c" * 64,
                                        platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request()), "")

    def test_a_record_for_a_different_image_does_not_make_it_ready(self):
        self._write_evidence(rootfs_sha256="0" * 64)
        executor = wd.verified_executor(self.config, fingerprint="c" * 64,
                                        platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request()),
                         "provisioning_unverified:evidence_image_changed")

    def test_a_record_for_a_different_guest_runner_does_not_make_it_ready(self):
        self._write_evidence(guest_runner_sha256="0" * 64)
        executor = wd.verified_executor(self.config, fingerprint="c" * 64,
                                        platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request()),
                         "provisioning_unverified:evidence_runner_changed")

    def test_a_verified_boundary_still_does_not_open_the_provider_lane(self):
        """The boundary and the lane are separate facts, and stay separate.

        Since provider enrolment became a rung of its own, a record with a
        verified boundary and a closed lane stops one step short of ready, so
        the refusal names the missing rung rather than the lane. The property
        is unchanged: verifying containment does not authorize handing a
        session in.
        """

        self._write_evidence()
        executor = wd.verified_executor(self.config, fingerprint="c" * 64,
                                        platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request(provider="claude")),
                         "provisioning_incomplete:provider_enrolment")

    def test_a_lane_closed_on_an_otherwise_ready_record_names_the_lane(self):
        """Once the record says the lane was observed, the lane speaks for
        itself: an observation that covered a different provider refuses by
        name rather than by stage."""

        self._write_evidence(provider_lane=_open_lane("codex"))
        executor = wd.verified_executor(self.config, fingerprint="c" * 64,
                                        platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request(provider="claude")),
                         "provider_lane_unverified")

    def test_a_record_missing_a_canary_does_not_make_it_ready(self):
        self._write_evidence(canaries=wr.CANARY_ORDER[:-1])
        executor = wd.verified_executor(self.config, fingerprint="c" * 64,
                                        platform_name="win32", machine="arm64")
        self.assertEqual(executor.refusal(_request()),
                         "provisioning_unverified:evidence_canaries_incomplete")


class ABomPrefixedManifestOrSidecarIsStillValidJson(unittest.TestCase):
    # A Windows editor or PowerShell's default encoding can prepend a UTF-8
    # BOM to manifest.json/sidecar.json. Both files are read with
    # Path.read_text(encoding=...); the BOM must be stripped there, not left
    # for json.loads to reject.
    def test_load_manifest_reads_a_bom_prefixed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(MANIFEST).encode())
            manifest = wd.load_manifest(str(path))
            self.assertEqual(manifest.node_version, MANIFEST["node_version"])

    def test_load_sidecar_reads_a_bom_prefixed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sidecar.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode())
            self.assertEqual(wd.load_sidecar(str(path)), {"a": 1})


if __name__ == "__main__":
    unittest.main()
