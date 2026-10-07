"""Offline contract tests for the client-derived local-queue health gate."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import client_data_health
from agent_bridge.localq.client_data_health import ClientDataHealthGate
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import (AdmissionError, FakeBackend, LocalQueue,
                                       QueueCaps, ResourceSnapshot)
from agent_bridge.capacity_router import StageRouter
from agent_bridge.orchestration import autoroute, mcp
from agent_bridge.orchestration.config import OrchestrationConfigError, load as load_config


class Sampler:
    def sample(self):
        return ResourceSnapshot(time.time(), "normal", "normal", True, 120.0)


class ToggleGate:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def check(self):
        self.calls += 1
        return self.answers.pop(0) if self.answers else None


class ClientDataHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.delegate_dir = self.root / "delegate"
        self.delegate_dir.mkdir()
        for name in ("delegate.py", "local_endpoint.py", "config.json"):
            (self.delegate_dir / name).write_text(name + " contents", encoding="utf-8")
        self.record = self.root / "ollama-check.json"
        self.response = self.root / "status.json"
        self.code = self.root / "status-code"
        self.code.write_text("0", encoding="ascii")
        self.script = self.root / "status.py"
        self.script.write_text(
            "import pathlib, sys\n"
            f"data = pathlib.Path({str(self.response)!r}).read_text(encoding='utf-8')\n"
            f"code = int(pathlib.Path({str(self.code)!r}).read_text(encoding='ascii'))\n"
            "sys.stdout.write(data)\n"
            "raise SystemExit(code)\n", encoding="utf-8")
        self.write_healthy_record()

    def hashes(self):
        return {name: hashlib.sha256((self.delegate_dir / name).read_bytes()).hexdigest()
                for name in ("delegate.py", "local_endpoint.py", "config.json")}

    def write_healthy_record(self, **changes):
        value = {"state": "verified", "full_passed": True, "wrapper_hashes": self.hashes()}
        value.update(changes)
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.record.write_bytes(raw)
        self.response.write_text(json.dumps({"schema": 1, "result": "verified",
                                             "record_sha256": hashlib.sha256(raw).hexdigest()}), encoding="utf-8")

    def gate(self, **changes):
        values = dict(client_data_health_command=(sys.executable, str(self.script), "status", "--json"),
                      client_data_health_path=os.defpath, client_data_health_timeout_seconds=1.0,
                      client_data_health_record=self.record,
                      gemma_delegate_executable=self.delegate_dir / "delegate.py",
                      gemma_delegate_sha256=self.hashes()["delegate.py"])
        aliases = {"command": "client_data_health_command", "path": "client_data_health_path",
                   "timeout_seconds": "client_data_health_timeout_seconds", "record": "client_data_health_record"}
        for old, new in aliases.items():
            if old in changes:
                changes[new] = changes.pop(old)
        values.update(changes)
        return ClientDataHealthGate.from_config(SimpleNamespace(**values))

    def queue(self, gate=None, *, root=None, backend_id="gemma_certified", clock=time.time):
        return LocalQueue(root or self.root / "queue", sampler=Sampler(), backend=FakeBackend(),
                          caps=QueueCaps(lease_seconds=2), clock=clock, backend_id=backend_id,
                          allowed_task_types=frozenset({"summarize"}),
                          client_data_health_gate=gate)

    def submit_client(self, queue, key="client"):
        return queue.submit(task_type="summarize", input="client derived text", params={},
                            priority="interactive", classification="client_derived", caller="codex",
                            purpose="test", idempotency_key=key)

    # 1. Every command/config failure is a precise client-derived refusal.
    def test_01_status_failures_and_configuration_refuse(self):
        cases = [(1, {"schema": 1, "result": "not verified", "record_sha256": None},
                  "client_data_health_unverified"),
                 (2, {"schema": 1, "result": "failed", "record_sha256": hashlib.sha256(self.record.read_bytes()).hexdigest()},
                  "client_data_health_failed"),
                 (9, {"schema": 1, "result": "verified", "record_sha256": hashlib.sha256(self.record.read_bytes()).hexdigest()},
                  "client_data_health_unverified")]
        for code, response, expected in cases:
            with self.subTest(code=code):
                self.code.write_text(str(code), encoding="ascii")
                self.response.write_text(json.dumps(response), encoding="utf-8")
                with self.assertRaisesRegex(AdmissionError, expected):
                    self.submit_client(self.queue(self.gate()), str(code))
        slow = self.root / "slow.py"
        slow.write_text("import time; time.sleep(5)\n", encoding="utf-8")
        timeout_gate = self.gate(command=(sys.executable, str(slow), "status", "--json"), timeout_seconds=.05)
        with self.assertRaisesRegex(AdmissionError, "client_data_health_unverified"):
            self.submit_client(self.queue(timeout_gate), "timeout")
        spawn_gate = self.gate(command=(str(self.root / "missing-command"), "status", "--json"))
        with self.assertRaisesRegex(AdmissionError, "client_data_health_unverified"):
            self.submit_client(self.queue(spawn_gate), "spawn")
        with self.assertRaisesRegex(AdmissionError, "client_data_health_unverified"):
            self.submit_client(self.queue(None), "unconfigured")
        with self.assertRaises(ValueError):
            ClientDataHealthGate.from_config(SimpleNamespace(client_data_health_command=(sys.executable,),
                client_data_health_path=os.defpath, client_data_health_timeout_seconds=1,
                client_data_health_record=self.record))

    # 2. The check must bind to all of the actual pinned delegate files.
    def test_02_binding_requires_every_record_and_delegate_hash(self):
        for name in ("delegate.py", "local_endpoint.py", "config.json"):
            with self.subTest(name=name):
                hashes = self.hashes()
                hashes[name] = "0" * 64
                self.write_healthy_record(wrapper_hashes=hashes)
                self.assertEqual(self.gate().check(), "client_data_health_binding_mismatch")
        self.write_healthy_record()
        self.assertEqual(self.gate(gemma_delegate_sha256="0" * 64).check(),
                         "client_data_health_binding_mismatch")
        for record in (None, b"not json", json.dumps({"state": "failed", "full_passed": True}).encode(),
                       json.dumps({"state": "verified", "full_passed": False}).encode()):
            with self.subTest(record=record):
                if record is None:
                    self.record.unlink(missing_ok=True)
                else:
                    self.record.write_bytes(record)
                self.response.write_text(json.dumps({"schema": 1, "result": "verified",
                    "record_sha256": hashlib.sha256(record or b"anything").hexdigest()}), encoding="utf-8")
                self.assertEqual(self.gate().check(), "client_data_health_binding_mismatch")
                self.write_healthy_record()
        self.assertIsNone(self.gate().check())  # positive, real 64-hex passing path
        queue = self.queue(self.gate(), root=self.root / "positive")
        self.submit_client(queue, "positive")
        queue.run_once("positive-owner")
        self.assertEqual(len(queue.backend.calls), 1)

    # 2a. The status hash identifies exactly the bytes subsequently parsed.
    def test_02a_status_identity_and_shape_are_strict(self):
        record_a = self.record.read_bytes()
        record_b = record_a + b" "
        self.record.write_bytes(record_b)
        self.response.write_text(json.dumps({"schema": 1, "result": "verified",
            "record_sha256": hashlib.sha256(record_a).hexdigest()}), encoding="utf-8")
        self.assertEqual(self.gate().check(), "client_data_health_binding_mismatch")
        self.write_healthy_record()
        self.code.write_text("2", encoding="ascii")
        self.response.write_text(json.dumps({"schema": 1, "result": "verified",
            "record_sha256": hashlib.sha256(self.record.read_bytes()).hexdigest()}), encoding="utf-8")
        self.assertEqual(self.gate().check(), "client_data_health_unverified")
        self.code.write_text("0", encoding="ascii")
        for output in ("not-json", "x" * 4097):
            self.response.write_text(output, encoding="utf-8")
            self.assertEqual(self.gate().check(), "client_data_health_unverified")
        # The record can change after status has hashed it but before the
        # gate reads it; the command reports A while replacing the path B.
        self.write_healthy_record()
        old = self.record.read_bytes()
        replacement = old + b"\n"
        replacing = self.root / "replace.py"
        replacing.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(self.record)!r}).write_bytes({replacement!r})\n"
            f"print({json.dumps(json.dumps({'schema': 1, 'result': 'verified', 'record_sha256': hashlib.sha256(old).hexdigest()}))})\n",
            encoding="utf-8")
        self.assertEqual(self.gate(command=(sys.executable, str(replacing), "status", "--json")).check(),
                         "client_data_health_binding_mismatch")

    # 3. A different active backend cannot use the Gemma proof.
    def test_03_private_worker_refuses_and_rebuilt_queue_fails_queued_job(self):
        private = self.queue(self.gate(), backend_id="private_worker")
        with self.assertRaisesRegex(AdmissionError, "client_data_backend_unsupported"):
            self.submit_client(private)
        gemma = self.queue(self.gate(), root=self.root / "rebuild")
        job = self.submit_client(gemma)
        backend = FakeBackend()
        rebuilt = LocalQueue(self.root / "rebuild", sampler=Sampler(), backend=backend, caps=QueueCaps(),
                             backend_id="private_worker", allowed_task_types=frozenset({"summarize"}))
        result = rebuilt.run_once()
        self.assertEqual(result["error"], "client_data_backend_unsupported")
        self.assertEqual(backend.calls, [])

    # 4. Other classifications bypass this gate entirely.
    def test_04_non_client_never_calls_gate(self):
        gate = ToggleGate("client_data_health_failed")
        queue = self.queue(gate)
        queue.submit(task_type="summarize", input="ordinary text", params={}, priority="interactive",
                     classification="internal_nonclient", caller="codex", purpose="test")
        self.assertEqual(gate.calls, 0)

    # 5. Refusal is content-free in intake and does not consume a checkpoint/key.
    def test_05_intake_refusal_is_audited_and_retryable(self):
        gate = ToggleGate("client_data_health_unverified", None)
        queue = self.queue(gate)
        intake = AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1))
        checkpoint = intake.checkpoint(task_id="c", task_type="summarize", classification="client_derived",
                                      caller="codex", input_bytes=len("client derived text".encode()), nonblank_lines=1)
        kwargs = dict(task_type="summarize", input="client derived text", params={}, priority="interactive",
                      classification="client_derived", caller="codex", purpose="work",
                      checkpoint_id=checkpoint["checkpoint_id"])
        with self.assertRaisesRegex(AdmissionError, "client_data_health_unverified"):
            intake.route(**kwargs)
        self.assertEqual(queue.state_report()["counts"], {})
        with intake._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM routing_receipts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM health_refusals").fetchone()[0], 1)
        self.assertEqual(intake.audit()["health_refusals_by_reason"]["client_data_health_unverified"], 1)
        self.assertEqual(intake.route(**kwargs)["decision"], "local")

    # 6. A run-time failure fails just that job and does not block the next.
    def test_06_run_time_check_fails_before_backend_then_next_runs(self):
        gate = ToggleGate(None, "client_data_health_failed")
        backend = FakeBackend()
        queue = LocalQueue(self.root / "runtime", sampler=Sampler(), backend=backend, caps=QueueCaps(),
                           backend_id="gemma_certified", allowed_task_types=frozenset({"summarize"}),
                           client_data_health_gate=gate)
        client = self.submit_client(queue)
        ordinary = queue.submit(task_type="summarize", input="ordinary", params={}, priority="interactive",
                                classification="internal_nonclient", caller="codex", purpose="test")
        failed = queue.run_once("runtime-owner")
        self.assertEqual(failed["error"], "client_data_health_failed")
        self.assertEqual(backend.calls, [])
        self.assertEqual(queue.run_once("runtime-owner")["job_id"], ordinary["job_id"])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(queue.status(client["job_id"])["status"], "failed")

    # 7. A timeout kills and reaps the entire health-check process group.
    @unittest.skipUnless(os.name == "posix", "process-group assertion is POSIX-specific")
    def test_07_timeout_kills_sigterm_ignoring_grandchild(self):
        pidfile = self.root / "grandchild.pid"
        script = self.root / "grandchild.py"
        script.write_text(
            "import pathlib, signal, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', \"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"])\n"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n"
            "time.sleep(30)\n", encoding="utf-8")
        gate = self.gate(command=(sys.executable, str(script), "status", "--json"), timeout_seconds=.5)
        backend = FakeBackend()
        queue = LocalQueue(self.root / "timeout", sampler=Sampler(), backend=backend, caps=QueueCaps(),
                           backend_id="gemma_certified", allowed_task_types=frozenset({"summarize"}),
                           client_data_health_gate=gate)
        with self.assertRaisesRegex(AdmissionError, "client_data_health_unverified"):
            self.submit_client(queue)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                self.fail("health-check grandchild survived timeout")
            time.sleep(.02)
        self.assertEqual(backend.calls, [])

    # 8. An interruption in the post-commit gap is recovered as unknown.
    def test_08_crash_after_running_commit_recovers_unknown_without_backend(self):
        now = [100.0]
        gate = ToggleGate(None)
        backend = FakeBackend()
        class ClockSampler:
            def sample(self):
                return ResourceSnapshot(now[0], "normal", "normal", True, 120.0)
        queue = LocalQueue(self.root / "crash", sampler=ClockSampler(), backend=backend,
                           caps=QueueCaps(lease_seconds=2), clock=lambda: now[0],
                           backend_id="gemma_certified", allowed_task_types=frozenset({"summarize"}),
                           client_data_health_gate=gate)
        job = self.submit_client(queue)
        queue._client_data_health_refusal = lambda: (_ for _ in ()).throw(SystemExit())
        with self.assertRaises(SystemExit):
            queue._run_once_under_lock("crash-owner")
        now[0] += 3
        recovered = LocalQueue(self.root / "crash", sampler=ClockSampler(), backend=FakeBackend(),
                               caps=QueueCaps(lease_seconds=2), clock=lambda: now[0],
                               backend_id="gemma_certified", allowed_task_types=frozenset({"summarize"}),
                               client_data_health_gate=ToggleGate())
        self.assertIsNone(recovered.run_once("next-owner"))
        self.assertEqual(recovered.status(job["job_id"])["status"], "unknown")
        self.assertEqual(backend.calls, [])

    # 9. Both production MCP paths report the same gate refusal; digest's
    # classification comes from protected repo policy, never the request.
    def test_09_both_mcp_entry_points_refuse_and_digest_uses_policy(self):
        queue = self.queue(ToggleGate("client_data_health_unverified",
                                      "client_data_health_unverified"))
        intake = AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1))
        state = self.root / "state"
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        target = repo / "client.log"
        target.write_text("client-derived log line\n" * 100, encoding="utf-8")
        policy = Path(autoroute.policy_path(str(state)))
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({"version": 1, "declared_available": ["local"],
            "local_first": {"enabled": True}, "repos": {str(repo): {
                "classification": "client_derived", "allowed_routes": ["local"],
                "mechanical_ok": True, "mechanical_globs": ["**/*.log"]}}}), encoding="utf-8")
        tools = mcp.build_tools("codex", StageRouter(str(self.root / "router.sqlite3")), queue, intake,
                                state_root=str(state))
        routed = tools["work_route_local"]["handler"]({"task_type": "summarize",
            "input": "client derived text", "priority": "interactive", "classification": "client_derived"})
        self.assertEqual(routed, {"ok": False, "error": "client_data_health_unverified"})
        digested = tools["work_digest_file"]["handler"]({"path": str(target), "task_type": "summarize",
            "classification": "internal_nonclient"})
        self.assertFalse(digested["ok"])
        self.assertEqual(digested["error"], "client_data_health_unverified")
        self.assertEqual(digested.get("classification"), None)
        for tool in ("work_route_local", "work_digest_file"):
            self.assertFalse(any("health" in key for key in tools[tool]["inputSchema"]["properties"]))

    # 10. The subprocess sees only the configured argv/env and no stdin.
    def test_10_status_subprocess_is_isolated_from_request(self):
        audit = self.root / "child-audit.json"
        script = self.root / "inspect.py"
        raw = self.record.read_bytes()
        script.write_text(
            "import json, os, pathlib, sys\n"
            f"pathlib.Path({str(audit)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'env': dict(os.environ)}}))\n"
            f"print(json.dumps({{'schema': 1, 'result': 'verified', 'record_sha256': {hashlib.sha256(raw).hexdigest()!r}}}))\n",
            encoding="utf-8")
        gate = self.gate(command=(sys.executable, str(script), "status", "--json"))
        queue = self.queue(gate)
        actual_popen = client_data_health.subprocess.Popen
        launches = []
        def capture(*args, **kwargs):
            launches.append((args, kwargs))
            return actual_popen(*args, **kwargs)
        with mock.patch.object(client_data_health.subprocess, "Popen", side_effect=capture):
            self.submit_client(queue)
        seen = json.loads(audit.read_text())
        self.assertEqual(seen["argv"], ["status", "--json"])
        self.assertEqual(seen["stdin"], "")
        self.assertEqual(launches[0][0][0], gate.command)
        self.assertEqual(launches[0][1]["env"], {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        self.assertNotIn("REQUEST_SECRET", seen["env"])

    # 11. An isolated status command reporting exit 1 is unverified.
    def test_11_status_exit_one_integration_fake(self):
        self.code.write_text("1", encoding="ascii")
        self.response.write_text(json.dumps({"schema": 1, "result": "not verified", "record_sha256": None}),
                                 encoding="utf-8")
        self.assertEqual(self.gate().check(), "client_data_health_unverified")

    def test_config_loader_requires_the_complete_health_group(self):
        doc = {"config_version": "1", "state_root": str(self.root / "state"),
               "local_queue_root": str(self.root / "queue-config"),
               "capacity_db": str(self.root / "capacity.sqlite3"),
               "worker_executable": str(self.root / "worker"), "worker_state": str(self.root / "worker-state"),
               "client_data_health_command": [sys.executable, str(self.script), "status", "--json"],
               "client_data_health_path": os.defpath, "client_data_health_timeout_seconds": 30,
               "client_data_health_record": str(self.record)}
        config = self.root / "orchestration.json"
        config.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(load_config(config).client_data_health_record, self.record)
        doc.pop("client_data_health_path")
        config.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationConfigError, "configuration_incomplete"):
            load_config(config)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
