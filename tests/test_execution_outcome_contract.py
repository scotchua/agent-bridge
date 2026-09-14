"""One semantic outcome contract, exercised across both executors.

The defect this file exists for: the queue decided a job was complete from a
zero exit code, while the Windows executor reported a different status field
with a different vocabulary. Two executors, two notions of "done", and a
verification failure recorded as a completed job.

These are real ``ExecutionQueue`` runs. The queue submits, claims, dispatches
and writes the receipt exactly as it does in production; only the runtime
underneath ``WindowsWslExecutor`` is injected, because a WSL guest is not
available here. That is the seam, and nothing above it is simulated.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import execution_queue as eq
from agent_bridge.orchestration import guest_runner as gr
from agent_bridge.orchestration import windows_delegation as wd
from agent_bridge.orchestration import windows_evidence as wev
from agent_bridge.orchestration import windows_wsl_provision as wp
from agent_bridge.orchestration import windows_wsl_runtime as wr

ROOTFS_BYTES = b"pinned-rootfs" * 32
MANIFEST = {
    "schema_version": 1, "distro_release": "12.9",
    "rootfs_sha256": hashlib.sha256(ROOTFS_BYTES).hexdigest(),
    "node_version": "20.19.0", "claude_version": "1.0.0",
    "codex_version": "0.1.0",
}


def _guest_response(**overrides):
    fields = dict(schema_version=gr.SCHEMA_VERSION, status="completed",
                  harness_status=gr.HARNESS_COMPLETE, reason="ok", exit_code=0,
                  stdout_b64="", stderr_b64="",
                  diff_b64=base64.b64encode(b"--- a\n+++ b\n").decode("ascii"),
                  verification=[{"program": "git", "returncode": 0,
                                 "stdout_sha256": "a" * 64,
                                 "stderr_sha256": "b" * 64,
                                 "duration_seconds": 0.1,
                                 "egress": EGRESS_RECEIPT}],
                  truncated=False, duration_seconds=0.5)
    fields.update(overrides)
    return json.dumps(fields).encode("utf-8")


def _runtime_result(stdout):
    return wr.RuntimeResult(
        status=wr.STATUS_COMPLETED, reason=wr.REASON_OK,
        distro_name="agent-bridge-abc123", exit_code=0, stdout=stdout,
        stderr=b"", canaries_passed=True, spawned=True,
        timings={"total_seconds": 1.0},
        cleanup=wr.CleanupReport("ok", "ok", "ok", "ok"), detail="")


#: The network posture a check ran under, as the guest records it.
EGRESS_RECEIPT = "verify-egress-denied:" + "c" * 64


def _ready_state():
    return wp.ProvisionState(
        is_windows=True, windows_build_ok=True, firmware_virtualization=True,
        features_enabled={name: True for name in wp.REQUIRED_FEATURES},
        reboot_pending=False, wsl_present=True, wsl_version_ok=True,
        guest_image_installed=True, boundary_verified=True,
        provider_lane_verified=True)


def _commit_repo(repo: Path) -> str:
    """Initialise, commit, and return HEAD. Never touches the user's git
    configuration: a global hook or template would leak into the fixture."""

    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@invalid")
    for args in (["init", "--quiet"], ["add", "-A"],
                 ["commit", "--quiet", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo)] + args, check=True, env=env,
                       shell=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, env=env, shell=False,
                         stdout=subprocess.PIPE)
    return out.stdout.decode().strip()


class WindowsQueueIntegrationTests(unittest.TestCase):
    """A real ExecutionQueue driving a real WindowsWslExecutor."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)

        self.repo = base / "repo"
        self.repo.mkdir()
        (self.repo / "main.py").write_text("print(1)\n", encoding="utf-8")
        # A real commit, not just an initialised directory. translate() now
        # resolves the admitted base and refuses a worktree sitting on a
        # different commit, so an empty repository would exercise nothing.
        self.base_sha = _commit_repo(self.repo)
        self.brief = base / "brief.txt"
        self.brief.write_text("Do the synthetic thing.\n", encoding="utf-8")

        rootfs = base / "rootfs.tar"
        rootfs.write_bytes(ROOTFS_BYTES)
        manifest = base / "manifest.json"
        manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        sidecar = base / "sidecar.json"
        sidecar.write_text(json.dumps({
            "schema_version": 1, "architecture": "arm64",
            "rootfs_sha256": hashlib.sha256(ROOTFS_BYTES).hexdigest()}),
            encoding="utf-8")
        self.config = wd.DelegationConfig(
            runtime_root=str(base / "runtime"), rootfs_path=str(rootfs),
            manifest_path=str(manifest), sidecar_path=str(sidecar))

        self.queue_root = base / "queue"
        self.queue_root.mkdir(mode=0o700)
        self.dispatched = []

    def _executor(self, response, **overrides):
        def run_job(job_request, **_kwargs):
            self.dispatched.append(job_request)
            return _runtime_result(response)

        fields = dict(
            provision_state=_ready_state(),
            provider_lane=wev.ProviderLane(
                verified=True, providers=("claude", "codex"),
                portability_observed=True, refresh_behaviour_observed=True),
            platform_name="win32", machine="arm64",
            auth_source=lambda provider: {"kind": gr.AUTH_KINDS[provider],
                                          "token": "sk-session-token"},
            run_job=run_job)
        fields.update(overrides)
        return wd.WindowsWslExecutor(self.config, **fields)

    def _run(self, response, **overrides):
        queue = eq.ExecutionQueue(self.queue_root, self._executor(response, **overrides),
                                  recover_interrupted=False)
        submitted = queue.submit(
            caller="codex", provider="claude", repo=str(self.repo),
            brief=str(self.brief), base="HEAD", classification="synthetic",
            model="sonnet", effort="low", item_id="item-1", stage="stage-1",
            owner_id="owner-1", stage_revision=0,
            verify_argv=[["git", "status"]], timeout_seconds=300)
        queue.run_once("worker-1")
        return queue.result(submitted["job_id"])

    # -- the contract -------------------------------------------------------

    def test_a_verified_guest_completion_becomes_a_complete_job(self):
        receipt = self._run(_guest_response())
        self.assertEqual(receipt["state"], "complete")
        self.assertEqual(receipt["harness"]["harness_status"],
                         eq.HARNESS_COMPLETE)
        self.assertTrue(receipt["harness"]["harness_ok"])
        self.assertEqual(receipt["harness"]["returncode"], 0)

    def test_a_semantic_verification_failure_becomes_a_failed_job(self):
        """The provider exited 0. The checks did not pass. Not a completion."""
        receipt = self._run(_guest_response(
            harness_status=gr.HARNESS_VERIFICATION_FAILED,
            reason="verification_failed", exit_code=0,
            verification=[{"program": "git", "returncode": 1,
                           "stdout_sha256": "a" * 64, "stderr_sha256": "b" * 64,
                           "duration_seconds": 0.1,
                           "egress": EGRESS_RECEIPT}]))
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["harness"]["harness_status"],
                         eq.HARNESS_VERIFICATION_FAILED)
        self.assertFalse(receipt["harness"]["harness_ok"])

    def test_a_guest_that_aborted_becomes_a_failed_job(self):
        receipt = self._run(_guest_response(
            status="aborted", harness_status=gr.HARNESS_FAILED,
            reason="provider_nonzero_exit", exit_code=2,
            verification=[], diff_b64=""))
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["harness"]["harness_status"],
                         eq.HARNESS_FAILED)

    def test_an_unparseable_guest_response_is_never_a_completion(self):
        receipt = self._run(b"not json at all")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["harness"]["reason"],
                         "guest_response_malformed")

    def test_a_closed_provider_lane_fails_the_job_without_dispatching(self):
        receipt = self._run(_guest_response(), provider_lane=wev.ProviderLane())
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["harness"]["detail"], "provider_lane_unverified")
        self.assertEqual(self.dispatched, [])

    # -- what actually reached the guest ------------------------------------

    def test_the_queues_own_request_is_what_gets_translated(self):
        self._run(_guest_response())
        payload = json.loads(self.dispatched[0].stdin_data)
        self.assertEqual(payload["mode"], gr.MODE_PROVIDER_JOB)
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["model"], "sonnet")
        self.assertEqual(payload["effort"], "low")
        self.assertEqual(payload["brief"], "Do the synthetic thing.\n")
        self.assertEqual(payload["verify_argv"], [["git", "status"]])
        gr.validate_request(payload)

    def test_the_admitted_repository_is_what_gets_packed(self):
        import io
        import tarfile
        self._run(_guest_response())
        payload = json.loads(self.dispatched[0].stdin_data)
        with tarfile.open(fileobj=io.BytesIO(
                base64.b64decode(payload["workspace_tar_b64"]))) as tar:
            self.assertEqual(sorted(tar.getnames()), ["main.py"])

    def test_the_patch_comes_back_beside_the_receipt(self):
        receipt = self._run(_guest_response())
        job_dir = self.queue_root / receipt["job_id"]
        self.assertEqual((job_dir / "guest.diff").read_bytes(), b"--- a\n+++ b\n")

    def test_no_receipt_ever_carries_the_session_or_the_source(self):
        receipt = self._run(_guest_response())
        serialised = json.dumps(receipt)
        self.assertNotIn("sk-session-token", serialised)
        self.assertNotIn("print(1)", serialised)


class OutcomeContractTests(unittest.TestCase):
    """The queue holds every executor to the same shape."""

    def _made(self, **overrides):
        fields = dict(returncode=0, harness_ok=True,
                      harness_status=eq.HARNESS_COMPLETE,
                      harness_verdict="read")
        fields.update(overrides)
        return fields

    def test_a_well_formed_outcome_passes(self):
        eq.validate_outcome(self._made())

    def test_a_missing_field_is_refused(self):
        for key in eq.OUTCOME_REQUIRED_KEYS:
            outcome = self._made()
            del outcome[key]
            with self.assertRaises(eq.ExecutionAdmissionError) as caught:
                eq.validate_outcome(outcome)
            self.assertEqual(str(caught.exception), "execution_outcome_incomplete")

    def test_a_status_outside_the_vocabulary_is_refused(self):
        for value in ("completed", "done", "", None):
            with self.assertRaises(eq.ExecutionAdmissionError) as caught:
                eq.validate_outcome(self._made(harness_status=value,
                                                  harness_ok=False))
            self.assertEqual(str(caught.exception),
                             "execution_outcome_harness_status_invalid")

    def test_a_status_and_a_flag_that_disagree_are_refused(self):
        with self.assertRaises(eq.ExecutionAdmissionError) as caught:
            eq.validate_outcome(self._made(harness_ok=False))
        self.assertEqual(str(caught.exception), "execution_outcome_inconsistent")
        with self.assertRaises(eq.ExecutionAdmissionError):
            eq.validate_outcome(self._made(
                harness_status=eq.HARNESS_FAILED, harness_ok=True))

    def test_a_boolean_returncode_is_not_a_returncode(self):
        with self.assertRaises(eq.ExecutionAdmissionError) as caught:
            eq.validate_outcome(self._made(returncode=True))
        self.assertEqual(str(caught.exception),
                         "execution_outcome_returncode_invalid")

    def test_the_windows_and_posix_vocabularies_are_the_same_one(self):
        self.assertEqual(set(gr.HARNESS_STATUSES), set(eq.HARNESS_STATUSES))

    def test_success_needs_the_exit_code_and_the_verdict_to_agree(self):
        self.assertTrue(eq.outcome_is_success(self._made()))
        self.assertFalse(eq.outcome_is_success(self._made(returncode=1)))
        self.assertFalse(eq.outcome_is_success(self._made(
            harness_status=eq.HARNESS_VERIFICATION_FAILED, harness_ok=False)))


if __name__ == "__main__":
    unittest.main()
