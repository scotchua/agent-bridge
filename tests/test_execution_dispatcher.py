"""Offline tests for durable cross-provider implementation dispatch."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import platform_support
from agent_bridge.capacity_router import CapacityObservation, StageRouter
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot
from agent_bridge.orchestration import autoroute
from agent_bridge.orchestration.execution_queue import (
    UNEXPECTED_FAILURE_DETAIL, ExecutionAdmissionError, ExecutionQueue, _atomic_json,
    _harness_summary, outcome_is_success,
    reserve_nothing,
)
from agent_bridge.orchestration.server import Server


class FakeExecutor:
    """Shaped like SubprocessHarnessExecutor's outcome, semantics included.

    The exit code and the harness's own verdict are separate inputs here
    precisely because they can disagree on a real run: a harness whose
    verification commands failed still exits 0 today.
    """

    def __init__(self, returncode=0, harness_ok=True, harness_status="complete"):
        self.returncode = returncode
        self.harness_ok = harness_ok
        self.harness_status = harness_status
        self.requests = []

    def __call__(self, request, job_dir):
        self.requests.append(request)
        return {"returncode": self.returncode, "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64, "stdout_bytes": 10, "stderr_bytes": 0,
                "harness_ok": self.harness_ok,
                "harness_status": self.harness_status,
                "harness_verdict": "read"}


class Sampler:
    def sample(self):
        return ResourceSnapshot(100.0, "normal", "normal", True, 120.0)


class LocalService:
    def __init__(self, root):
        self.queue = LocalQueue(root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 100.0)

    def once(self):
        return None


class ExecutionDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.brief = self.root / "brief.md"
        self.brief.write_text("Synthetic implementation brief", encoding="utf-8")
        self.fake = FakeExecutor()
        self.queue = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 100.0,
                                    model_reserved=reserve_nothing)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, **changes):
        values = {"caller": "codex", "provider": "claude", "repo": str(self.repo),
                  "brief": str(self.brief), "base": "HEAD", "classification": "synthetic",
                  "model": "sonnet", "effort": "medium", "verify_argv": [["pytest", "-q"]],
                  "timeout_seconds": 90, "paid_fallback": False, "item_id": "item-1",
                  "stage": "implement", "owner_id": "owner-1", "stage_revision": 1}
        values.update(changes)
        return self.queue.submit(**values)

    def test_opposite_provider_only_and_no_paid_fallback(self):
        with self.assertRaisesRegex(ExecutionAdmissionError, "provider_not_eligible"):
            self.submit(provider="codex")
        with self.assertRaisesRegex(ExecutionAdmissionError, "paid_fallback_forbidden"):
            self.submit(paid_fallback=True)
        claude_to_codex = self.submit(caller="claude", provider="codex", model="gpt-5.6-terra",
                                      verify_argv=[])
        self.assertEqual(claude_to_codex["provider"], "codex")

    def test_atomic_receipt_closes_descriptor_and_preserves_acl_failure(self):
        target = self.root / "queue" / "acl-test.json"
        target.parent.mkdir(exist_ok=True)
        real_close = os.close
        with mock.patch(
            "agent_bridge.orchestration.execution_queue.host_platform.enforce_owner_only_file",
            side_effect=PermissionError("owner-only ACL unavailable"),
        ), mock.patch(
            "agent_bridge.orchestration.execution_queue.os.close",
            wraps=real_close,
        ) as close:
            with self.assertRaisesRegex(PermissionError, "owner-only ACL unavailable"):
                _atomic_json(target, {"state": "test"})
        self.assertTrue(close.called)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".pending-*")), [])

    def test_classification_and_paths_fail_closed(self):
        with self.assertRaisesRegex(ExecutionAdmissionError, "classification_not_eligible"):
            self.submit(classification="client_derived")
        with self.assertRaisesRegex(ExecutionAdmissionError, "repo_and_brief_must_be_absolute"):
            self.submit(brief="brief.md")
        with self.assertRaisesRegex(ExecutionAdmissionError, "claude_verification_required"):
            self.submit(verify_argv=[])

    def test_a_repo_symlink_is_resolved_once_at_admission_not_followed_later(self):
        # An adversarial review found that submit() stored the caller's raw
        # `repo` argument unresolved. autoroute.Policy.for_repo (the
        # eligibility check upstream in mcp.py) already resolves through
        # os.path.realpath before deciding whether a route may see this
        # repository, so a caller could point `repo` through a symlink, get
        # admitted (and classified) against the approved target the symlink
        # happened to name at that moment, then repoint the symlink to an
        # unapproved repository before the separately-scheduled worker
        # actually ran run_once() -- the harness would then execute against
        # whatever the symlink resolves to *now*, not what was approved at
        # admission. brief already gets an analogous protection (a symlink
        # is refused outright, and its content is rehashed at run_once); a
        # repository is ordinarily reached through a symlink, so refusing it
        # outright would be a needless behavior change -- resolving it once,
        # immediately, is what actually closes the gap.
        platform_support.require_symlinks(self)
        approved = self.repo
        other = self.root / "other-repo"
        other.mkdir()
        (other / ".git").mkdir()
        link = self.root / "link-to-repo"
        link.symlink_to(approved, target_is_directory=True)
        result = self.submit(repo=str(link))
        stored = json.loads((self.queue.root / result["job_id"] / "request.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(stored["repo"], str(approved.resolve()))
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        self.queue.run_once("worker-1")
        self.assertEqual(self.fake.requests[0]["repo"], str(approved.resolve()))

    def test_a_command_the_harness_would_refuse_is_refused_at_admission(self):
        # Live jobs d8e5763d, 7a79ae7f and 19869b0e were admitted with commands
        # the harness refuses, ran, and failed as a bare "TaskError".
        with self.assertRaisesRegex(
                ExecutionAdmissionError,
                "verify_argv_rejected: Python verification is limited to "
                "python -m pytest or python -m unittest"):
            self.submit(verify_argv=[["python3", "-c", "print(1)"]])
        with self.assertRaisesRegex(ExecutionAdmissionError,
                                    "verify_argv_rejected: verification executable"):
            self.submit(verify_argv=[["/usr/bin/test", "-e", "x"]])
        with self.assertRaisesRegex(ExecutionAdmissionError, "verify_argv_invalid"):
            self.submit(verify_argv=[["pytest", ""]])
        self.assertEqual(self.queue.state_report(), {})
        unittest_run = [["python3", "-m", "unittest", "discover", "-s", "tests"]]
        job = self.submit(verify_argv=unittest_run)
        request = json.loads((self.root / "queue" / job["job_id"] / "request.json").read_text())
        self.assertEqual(request["verify_argv"], unittest_run)

    def test_a_failed_run_records_the_reason_not_only_the_class(self):
        job = self.submit()
        self.brief.write_text("changed", encoding="utf-8")
        self.queue.run_once("worker-1")
        result = self.queue.result(job["job_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "ExecutionAdmissionError")
        self.assertEqual(result["error_detail"], "brief_changed_after_admission")

    def test_an_unexpected_exception_records_a_fixed_marker_not_its_text(self):
        """Only admission and OS errors carry text vetted for a receipt.
        Anything else is recorded by class and a fixed marker, so a stray
        message never reaches the durable record."""
        job = self.submit()

        def explode(request, selected):
            raise RuntimeError("token=do-not-record")

        queue = ExecutionQueue(self.root / "queue", explode, clock=lambda: 100.0,
                               model_reserved=reserve_nothing)
        queue.run_once("worker-1")
        result = queue.result(job["job_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "RuntimeError")
        self.assertEqual(result["error_detail"], UNEXPECTED_FAILURE_DETAIL)
        self.assertNotIn("do-not-record", json.dumps(result))

    def test_job_is_durable_and_fake_executor_completes_with_nonlanding_receipt(self):
        queued = self.submit(idempotency_key="same-task")
        duplicate = self.submit(idempotency_key="same-task")
        self.assertEqual(queued["job_id"], duplicate["job_id"])
        with self.assertRaisesRegex(ExecutionAdmissionError, "idempotency_conflict"):
            self.submit(idempotency_key="same-task", base="other")
        restarted = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 101.0,
                                 model_reserved=reserve_nothing)
        self.assertEqual(restarted.status(queued["job_id"])["state"], "queued")
        self.assertEqual(restarted.run_once("worker-1")["state"], "complete")
        result = restarted.result(queued["job_id"])
        self.assertFalse(result["permission_to_apply"])
        self.assertFalse(result["permission_to_commit"])
        self.assertFalse(result["permission_to_push"])
        self.assertFalse(result["permission_to_merge"])
        self.assertNotIn("stdout", result["harness"])

    def test_changed_brief_fails_without_calling_provider(self):
        job = self.submit()
        self.brief.write_text("changed", encoding="utf-8")
        self.queue.run_once("worker-1")
        self.assertEqual(self.queue.result(job["job_id"])["state"], "failed")
        self.assertEqual(self.fake.requests, [])

    def test_restart_blocks_interrupted_job_instead_of_resending(self):
        job = self.submit()
        directory = self.root / "queue" / job["job_id"]
        receipt = json.loads((directory / "receipt.json").read_text())
        receipt["state"] = "running"
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        restarted = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 102.0,
                                 model_reserved=reserve_nothing)
        self.assertEqual(restarted.status(job["job_id"])["state"], "blocked")
        self.assertIsNone(restarted.run_once("worker-2"))
        self.assertEqual(self.fake.requests, [])

    def test_mcp_tools_bind_caller_and_only_queue_execution(self):
        # execution_dispatch now derives classification from the operator's
        # routing policy rather than a caller-supplied argument (an
        # adversarial review found the old handler trusted the caller's own
        # claim outright), so a policy admitting this repo for both routes
        # is required for the dispatch below to be eligible at all.
        state_root = self.root / "state"
        policy_path = Path(autoroute.policy_path(str(state_root)))
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(json.dumps({
            "version": 1,
            "repos": {str(self.repo): {"classification": "synthetic",
                                       "allowed_routes": ["claude", "codex"]}}}),
            encoding="utf-8")
        router = StageRouter(self.root / "capacity.sqlite3", clock=lambda: 100.0)
        router.observe_capacity(CapacityObservation(
            route="claude", observed_at=100.0, fresh_until=200.0, available=True,
            source="test"), trusted=True)
        registered = router.register("item-1", "implement", allowed_routes=["claude"])
        owned = router.assign("item-1", "implement", owner_id="owner-1", lease_seconds=60,
                              expected_revision=registered["revision"])
        service = LocalService(self.root / "local")
        server = Server("codex", service, router, execution=self.queue, interval=0.01,
                        state_root=str(state_root))
        names = {tool["name"] for tool in server.handle({"jsonrpc": "2.0", "id": 1,
                 "method": "tools/list"})["result"]["tools"]}
        self.assertTrue({"execution_dispatch", "execution_status", "execution_result"} <= names)
        response = server.tools["execution_dispatch"]["handler"]({
            "provider": "claude", "repo": str(self.repo), "brief": str(self.brief),
            "base": "HEAD", "classification": "synthetic", "model": "sonnet",
            "effort": "medium", "verify_argv": [["pytest", "-q"]], "item_id": "item-1",
            "stage": "implement", "owner_id": "owner-1", "stage_revision": owned["revision"]})
        self.assertTrue(response["ok"])
        self.assertEqual(response["state"], "queued")
        spoof = server.tools["execution_dispatch"]["handler"]({
            "caller": "claude", "provider": "codex", "repo": str(self.repo),
            "brief": str(self.brief), "base": "HEAD", "classification": "synthetic",
            "model": "gpt-5.6-terra", "effort": "medium", "item_id": "item-1",
            "stage": "implement", "owner_id": "owner-1", "stage_revision": owned["revision"]})
        self.assertFalse(spoof["ok"])

        server.start()
        import time
        time.sleep(0.03)
        server.stop()
        self.assertEqual(self.queue.status(response["job_id"])["state"], "queued")
        self.assertEqual(self.fake.requests, [])

    def test_status_only_queue_does_not_reconcile_or_execute(self):
        job = self.submit()
        directory = self.root / "queue" / job["job_id"]
        receipt = json.loads((directory / "receipt.json").read_text())
        receipt["state"] = "running"
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        status_only = ExecutionQueue(self.root / "queue", None,
                                     clock=lambda: 102.0,
                                     recover_interrupted=False,
                                     model_reserved=reserve_nothing)
        self.assertEqual(status_only.status(job["job_id"])["state"], "running")
        with self.assertRaisesRegex(ExecutionAdmissionError, "worker_not_configured"):
            status_only.run_once("mcp-process")

    def test_restart_blocks_claimed_but_not_yet_running_job(self):
        job = self.submit()
        directory = self.root / "queue" / job["job_id"]
        (directory / "claim.lock").write_text("", encoding="utf-8")
        restarted = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 103.0,
                                 model_reserved=reserve_nothing)
        result = restarted.result(job["job_id"])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["error"], "interrupted_requires_reconciliation")
        self.assertEqual(self.fake.requests, [])



class HarnessVerdictTests(unittest.TestCase):
    """A zero exit code is not a verified task."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _made(self, **extra):
        outcome = {"returncode": 0, "harness_ok": True,
                   "harness_status": "complete"}
        outcome.update(extra)
        return outcome

    def test_a_clean_run_succeeds(self):
        self.assertTrue(outcome_is_success(self._made()))

    def test_a_verification_failure_that_exited_zero_is_not_a_success(self):
        self.assertFalse(outcome_is_success(self._made(
            harness_ok=False, harness_status="verification_failed")))

    def test_a_harness_that_said_ok_but_did_not_complete_is_not_a_success(self):
        self.assertFalse(outcome_is_success(self._made(
            harness_status="verification_failed")))

    def test_a_nonzero_exit_is_not_a_success_whatever_the_receipt_said(self):
        self.assertFalse(outcome_is_success(self._made(returncode=1)))

    def test_an_outcome_with_no_verdict_at_all_is_not_a_success(self):
        self.assertFalse(outcome_is_success({"returncode": 0}))

    def test_an_unparseable_final_line_is_not_a_success(self):
        summary = _harness_summary(b"not json\n")
        self.assertEqual(summary["harness_verdict"], "receipt_unparseable")
        self.assertFalse(outcome_is_success({"returncode": 0, **summary}))

    def test_no_output_at_all_is_not_a_success(self):
        summary = _harness_summary(b"")
        self.assertEqual(summary["harness_verdict"], "receipt_absent")
        self.assertFalse(outcome_is_success({"returncode": 0, **summary}))

    def test_the_verdict_is_read_from_the_last_line_not_the_first(self):
        stdout = (b'{"ok": true, "status": "complete"}\n'
                  b'{"ok": false, "status": "verification_failed"}\n')
        summary = _harness_summary(stdout)
        self.assertEqual(summary["harness_status"], "verification_failed")
        self.assertFalse(summary["harness_ok"])

    def test_a_json_array_is_refused_rather_than_indexed(self):
        self.assertEqual(_harness_summary(b'["complete"]')["harness_verdict"],
                         "receipt_not_an_object")

    def test_a_harness_refusal_line_is_a_failure_with_its_reason(self):
        # Exactly what the live harness printed for job d8e5763d, plus the
        # detail the harness now adds.
        summary = _harness_summary(
            b'{"error": "TaskError", "error_detail": "Python verification is limited '
            b'to python -m pytest or python -m unittest", "ok": false}\n')
        self.assertEqual(summary["harness_status"], "failed")
        self.assertEqual(summary["harness_verdict"], "read_failure")
        self.assertEqual(summary["error"], "TaskError")
        self.assertTrue(summary["error_detail"].startswith("Python verification"))
        self.assertFalse(outcome_is_success({"returncode": 1, **summary}))
        self.assertFalse(outcome_is_success({"returncode": 0, **summary}))

    def test_a_failure_line_without_an_error_is_still_unknown(self):
        self.assertEqual(_harness_summary(b'{"ok": false}')["harness_verdict"],
                         "receipt_status_unknown")
        self.assertEqual(_harness_summary(b'{"ok": true}')["harness_verdict"],
                         "receipt_status_unknown")

    def test_diagnostics_are_bounded_and_printable(self):
        line = json.dumps({"ok": False, "error": "TaskError",
                           "error_detail": "x\x1b[31m" + "y" * 2000}).encode()
        summary = _harness_summary(line)
        self.assertEqual(len(summary["error_detail"]), 512)
        self.assertNotIn("\x1b", summary["error_detail"])
        self.assertEqual(_harness_summary(b'{"ok": false, "error": 7}')["harness_verdict"],
                         "receipt_status_unknown")

    def test_a_status_line_keeps_its_diagnostics(self):
        summary = _harness_summary(
            b'{"ok": false, "status": "failed", "error": "OSError", '
            b'"error_detail": "codex login status: not logged in"}')
        self.assertEqual(summary["harness_verdict"], "read")
        self.assertEqual(summary["error_detail"], "codex login status: not logged in")


class ExecutionDispatchClassificationTests(unittest.TestCase):
    """execution_dispatch (mcp.py) derives classification from the operator's
    routing-policy.json, never from the caller's own argument.

    A confirmed adversarial-review finding: the handler used to forward
    args["classification"] straight to execution.submit(), whose own check
    only validates that the string is a member of a fixed set, not that it
    is the actual classification of the repository being dispatched. A
    caller could declare "synthetic" for a repository the operator actually
    classified internal_nonclient (or one the operator never classified at
    all) and dispatch it to a peer's cloud CLI anyway. The same gap that
    made ``capacity_observe`` a removed tool (mcp.py's own module docstring:
    "an authorized source" meant whatever the model typed) applied here too.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.brief = self.root / "brief.md"
        self.brief.write_text("Synthetic implementation brief", encoding="utf-8")
        self.fake = FakeExecutor()
        self.queue = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 100.0,
                                    model_reserved=reserve_nothing)
        self.state_root = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def write_policy(self, **document):
        path = Path(autoroute.policy_path(str(self.state_root)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "repos": {}, **document}), encoding="utf-8")

    def dispatch(self, *, caller="claude", provider="codex", classification="synthetic",
                item_id="item-1", owner_id="owner-1", model="sonnet"):
        router = StageRouter(self.root / "capacity.sqlite3", clock=lambda: 100.0)
        router.observe_capacity(CapacityObservation(
            route=provider, observed_at=100.0, fresh_until=200.0, available=True,
            source="test"), trusted=True)
        registered = router.register(item_id, "implement", allowed_routes=[provider])
        owned = router.assign(item_id, "implement", owner_id=owner_id, lease_seconds=60,
                              expected_revision=registered["revision"])
        service = LocalService(self.root / "local")
        server = Server(caller, service, router, execution=self.queue, interval=0.01,
                        state_root=str(self.state_root))
        return server.tools["execution_dispatch"]["handler"]({
            "provider": provider, "repo": str(self.repo), "brief": str(self.brief),
            "base": "HEAD", "classification": classification, "model": model,
            "effort": "medium", "verify_argv": [["pytest", "-q"]], "item_id": item_id,
            "stage": "implement", "owner_id": owner_id, "stage_revision": owned["revision"]})

    def test_the_callers_classification_claim_is_ignored_not_forwarded(self):
        # The policy says internal_nonclient; the caller claims synthetic.
        # The dispatched job must carry the policy's truth, not the claim.
        self.write_policy(repos={str(self.repo): {
            "classification": "internal_nonclient", "allowed_routes": ["claude", "codex"]}})
        result = self.dispatch(classification="synthetic")
        self.assertTrue(result["ok"], result)
        self.queue.run_once("worker-1")
        receipt = self.queue.result(result["job_id"])
        self.assertEqual(receipt["classification"], "internal_nonclient")

    def test_an_unclassified_repository_is_refused_however_the_caller_labels_it(self):
        # No policy entry for this repo at all: the operator never admitted
        # it for any route. The caller's own claim of "synthetic" (a value
        # that would otherwise sail through execution.submit's fixed-set
        # check) must not be enough on its own.
        self.write_policy()
        result = self.dispatch(classification="synthetic")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "execution_dispatch_refused:classification_not_eligible_for_route")

    def test_a_route_the_policy_does_not_admit_is_refused(self):
        self.write_policy(repos={str(self.repo): {
            "classification": "public", "allowed_routes": ["claude"]}})
        result = self.dispatch(provider="codex", classification="public")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "execution_dispatch_refused:classification_not_eligible_for_route")

    def test_a_model_the_operator_reserved_is_refused_at_dispatch(self):
        """Reserved models are the operator's to spend, not an agent's to pick."""
        self.write_policy(
            repos={str(self.repo): {"classification": "synthetic",
                                    "allowed_routes": ["claude", "codex"]}},
            reserved_models=["astra", "fable"])
        result = self.dispatch(model="gpt-6-astra")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"],
                         "execution_dispatch_refused:model_reserved_to_operator:astra")

    def test_reserving_a_token_covers_every_spelling_of_that_model(self):
        """The point of a token, not an exact name: a new snapshot or a
        provider's long-form id must not walk straight through a list written
        against last month's shorthand."""
        self.write_policy(
            repos={str(self.repo): {"classification": "synthetic",
                                    "allowed_routes": ["claude", "codex"]}},
            reserved_models=["astra"])
        spellings = ("astra", "gpt-6-astra", "GPT-6-ASTRA", "gpt-6-astra-2026-09-01")
        for index, spelling in enumerate(spellings):
            with self.subTest(spelling=spelling):
                # A fresh stage each time: registering one twice is stage_exists,
                # which would fail the test for a reason unrelated to the model.
                result = self.dispatch(model=spelling, item_id=f"item-spelling-{index}")
                self.assertFalse(result["ok"], spelling)
                self.assertIn("model_reserved_to_operator", result["error"])

    def test_an_unreserved_model_still_dispatches(self):
        """The guard must refuse the reserved name and nothing else."""
        self.write_policy(
            repos={str(self.repo): {"classification": "synthetic",
                                    "allowed_routes": ["claude", "codex"]}},
            reserved_models=["astra", "fable"])
        result = self.dispatch(model="gpt-5.6-terra")
        self.assertTrue(result["ok"], result)

    def test_no_reservations_means_no_refusals(self):
        """An existing policy file with no reserved_models keeps behaving
        exactly as it did before the field existed."""
        self.write_policy(repos={str(self.repo): {
            "classification": "synthetic", "allowed_routes": ["claude", "codex"]}})
        self.assertTrue(self.dispatch(model="gpt-6-astra")["ok"])

    def test_route_classifications_is_enforced_live_at_dispatch_too(self):
        # Not just at the earlier routing decision: dispatch re-derives and
        # re-checks against the operator's policy for itself.
        self.write_policy(
            repos={str(self.repo): {"classification": "internal_nonclient",
                                    "allowed_routes": ["claude", "codex"]}},
            route_classifications={"codex": ["public"]})
        result = self.dispatch(provider="codex", classification="internal_nonclient")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "execution_dispatch_refused:classification_not_eligible_for_route")

    def test_an_unreadable_policy_is_refused_cleanly_not_left_to_crash(self):
        # A confirmed adversarial-review finding: autoroute.load_policy
        # raises a plain OSError, not autoroute.PolicyError, when the policy
        # path exists but cannot be opened as a file (chmod 000, or, as
        # here, a directory sitting where the policy file belongs) --
        # autoroute.py's own open() call only catches FileNotFoundError,
        # since an absent file is the retain-everything default, not an
        # error. The handler used to catch only autoroute.PolicyError around
        # this call, so the OSError escaped uncaught; called directly here
        # (bypassing server.py's own blanket except Exception -> internal
        # error), an uncaught OSError would fail this test with a real
        # exception instead of the clean refusal asserted below.
        policy_path = Path(autoroute.policy_path(str(self.state_root)))
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.mkdir()
        result = self.dispatch(classification="synthetic")
        self.assertFalse(result["ok"])
        self.assertTrue(
            result["error"].startswith("execution_dispatch_refused:policy_unreadable:"),
            result["error"])

    def test_no_state_root_refuses_rather_than_trusting_the_caller(self):
        router = StageRouter(self.root / "capacity.sqlite3", clock=lambda: 100.0)
        router.observe_capacity(CapacityObservation(
            route="codex", observed_at=100.0, fresh_until=200.0, available=True,
            source="test"), trusted=True)
        registered = router.register("item-1", "implement", allowed_routes=["codex"])
        owned = router.assign("item-1", "implement", owner_id="owner-1", lease_seconds=60,
                              expected_revision=registered["revision"])
        service = LocalService(self.root / "local")
        server = Server("claude", service, router, execution=self.queue, interval=0.01)
        result = server.tools["execution_dispatch"]["handler"]({
            "provider": "codex", "repo": str(self.repo), "brief": str(self.brief),
            "base": "HEAD", "classification": "synthetic", "model": "sonnet",
            "effort": "medium", "verify_argv": [["pytest", "-q"]], "item_id": "item-1",
            "stage": "implement", "owner_id": "owner-1", "stage_revision": owned["revision"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "execution_dispatch_unavailable:no_state_root")


if __name__ == "__main__":
    unittest.main()
