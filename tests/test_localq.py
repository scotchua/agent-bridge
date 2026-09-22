"""Deterministic tests for the isolated local queue foundation."""

import inspect
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import (AdmissionError, FakeBackend, LocalQueue, QueueCaps,
                                 ResourceSnapshot)
from agent_bridge.localq import mcp
from agent_bridge.localq.spool import SubprocessBackend


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Sampler:
    def __init__(self, clock):
        self.clock = clock
        self.snapshot = ResourceSnapshot(clock(), "normal", "normal", True, 120.0)

    def sample(self):
        return self.snapshot


class LocalQueueTests(unittest.TestCase):
    def test_default_resource_policy_is_intentionally_aggressive(self):
        caps = QueueCaps()
        self.assertEqual(caps.bulk_min_idle_seconds, 0.0)
        self.assertEqual(caps.max_load_per_core, 1.25)
        self.assertEqual(caps.min_cpu_idle_ratio, 0.10)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.sampler = Sampler(self.clock)
        self.backend = FakeBackend(lambda data: {"draft": data["input"]})
        self.caps = QueueCaps(max_jobs=2, bulk_ttl_seconds=5, timeout_seconds=0.01)
        self.queue = LocalQueue(self.temp.name, sampler=self.sampler, backend=self.backend,
                                caps=self.caps, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, **more):
        args = {"task_type": "summarize", "input": "synthetic", "params": {"short": True},
                "priority": "interactive", "classification": "synthetic", "caller": "codex", "purpose": "work"}
        args.update(more)
        return self.queue.submit(**args)

    def test_normalized_idempotency_returns_same_job(self):
        first = self.submit(params={"b": 2, "a": 1})
        second = self.submit(params={"a": 1, "b": 2})
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["deduplicated"])

    def test_pressure_stale_and_bulk_battery_are_refused(self):
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "high", "normal", True, 120)
        pressure = self.submit()
        self.assertEqual(self.queue.run_once("runner")["status"], "queued")
        self.assertIn("resource_pressure", self.queue.status(pressure["job_id"])["error"])
        self.queue.cancel(pressure["job_id"])
        self.sampler.snapshot = ResourceSnapshot(self.clock() - 11, "normal", "normal", True, 120)
        self.clock.now += self.caps.lease_seconds + 1
        stale = self.submit(input="stale")
        self.assertEqual(self.queue.run_once("runner")["status"], "queued")
        self.assertIn("resource_sample_stale", self.queue.status(stale["job_id"])["error"])
        self.queue.cancel(stale["job_id"])
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", False, 120)
        bulk = self.submit(priority="bulk", input="bulk")
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", False, 120)
        self.assertEqual(self.queue.run_once("runner")["status"], "queued")
        self.assertIn("resource_bulk_policy", self.queue.status(bulk["job_id"])["error"])

    def test_confidential_input_is_hard_refused(self):
        for classification in ("confidential", "client", "unclassified", "internal"):
            with self.subTest(classification=classification), self.assertRaisesRegex(AdmissionError, "classification_refused"):
                self.submit(classification=classification)
        self.assertEqual(self.queue.state_report()["counts"], {})

    def test_single_executor_lease_blocks_second_runner(self):
        job = self.submit()
        with self.queue._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.queue._acquire(db, "first", self.clock()))
            db.execute("COMMIT")
        other = LocalQueue(self.temp.name, sampler=self.sampler, backend=self.backend,
                           caps=self.caps, clock=self.clock)
        self.assertIsNone(other.run_once("second"))
        self.assertEqual(other.status(job["job_id"])["status"], "queued")

    def test_timeout_is_unknown_and_cancelled_job_is_not_executed(self):
        self.backend.fn = lambda _: (_ for _ in ()).throw(TimeoutError())
        timeout = self.submit()
        terminal = self.queue.run_once("runner")
        self.assertEqual(terminal["status"], "unknown")
        self.assertEqual(terminal["disposition"]["outcome"], "unknown")
        self.clock.now += self.caps.lease_seconds + 1
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", True, 120)
        waiting = self.submit(input="cancel me")
        cancelled = self.queue.cancel(waiting["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(self.queue.run_once("runner"))

    def test_a_child_failure_records_the_backend_reason_not_only_the_class(self):
        import dataclasses
        caps = dataclasses.replace(self.caps, timeout_seconds=30)
        for command, reason in (
                ("import sys; sys.exit(3)", "child exited unsuccessfully"),
                ("import sys; print('{\"ok\": false, \"error\": \"ValueError\", "
                 "\"error_detail\": \"queue provenance unavailable\"}'); sys.exit(1)",
                 "child exited unsuccessfully: ValueError: queue provenance unavailable"),
                ("import sys; print('{\"ok\": false, \"error\": \"x\\\\u0007y\"}'); sys.exit(1)",
                 "child exited unsuccessfully: xy"),
                ("print('not json')", "child did not return a JSON object")):
            with self.subTest(reason):
                queue = LocalQueue(self.temp.name, sampler=self.sampler, caps=caps,
                                   backend=SubprocessBackend([sys.executable, "-c", command]),
                                   clock=self.clock)
                job = queue.submit(task_type="summarize", input=reason, params={},
                                   priority="interactive", classification="synthetic",
                                   caller="codex", purpose="work")
                terminal = queue.run_once("runner")
                self.assertEqual(terminal["job_id"], job["job_id"])
                self.assertEqual(terminal["status"], "failed")
                self.assertEqual(terminal["error"], f"RuntimeError: {reason}")

    def test_a_non_backend_exception_records_only_its_class(self):
        def boom(payload):
            raise ValueError("payload said: " + payload["input"])
        self.queue.backend = FakeBackend(boom)
        self.submit(input="private text")
        terminal = self.queue.run_once("runner")
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["error"], "ValueError")

    def test_child_process_timeout_is_hard_and_indeterminate(self):
        self.queue.backend = SubprocessBackend([sys.executable, "-c", "import time; time.sleep(1)"])
        job = self.submit()
        terminal = self.queue.run_once("runner")
        self.assertEqual(terminal["job_id"], job["job_id"])
        self.assertEqual(terminal["status"], "unknown")
        self.assertEqual(terminal["error"], "execution_timeout")

    def test_startup_recovery_marks_expired_lease_unknown(self):
        job = self.submit()
        with self.queue._db() as db:
            db.execute("UPDATE jobs SET status='running',lease_until=? WHERE job_id=?",
                       (self.clock() - 1, job["job_id"]))
        recovered = LocalQueue(self.temp.name, sampler=self.sampler, backend=self.backend,
                               caps=self.caps, clock=self.clock)
        self.assertIsNone(recovered.run_once("recovery"))
        self.assertEqual(recovered.status(job["job_id"])["status"], "unknown")
        self.assertEqual(recovered.result(job["job_id"])["disposition"]["reason"], "executor_lease_lost")

    def test_bulk_expiry_and_queue_cap(self):
        bulk = self.submit(priority="bulk")
        self.clock.now += 6
        self.queue.run_once("runner")
        self.assertEqual(self.queue.status(bulk["job_id"])["status"], "expired")
        self.clock.now += self.caps.lease_seconds + 1
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", True, 120)
        one = self.submit(input="one")
        two = self.submit(input="two")
        with self.assertRaisesRegex(AdmissionError, "queue_full"):
            self.submit(input="three")
        self.assertEqual(self.queue.status(one["job_id"])["status"], "queued")
        self.assertEqual(self.queue.status(two["job_id"])["status"], "queued")

    def test_result_disposition_and_mcp_surface(self):
        job = self.submit()
        terminal = self.queue.run_once("runner")
        self.assertEqual(terminal["result"]["draft"], "synthetic")
        self.assertEqual(terminal["disposition"]["outcome"], "complete")
        self.assertEqual(terminal["caller"], "codex")
        self.assertEqual(terminal["purpose"], "work")
        tools = mcp.build_tools(self.queue)
        self.assertEqual(set(tools), {"local_submit", "local_status", "local_result", "local_cancel", "local_feedback"})
        schema = tools["local_submit"]["inputSchema"]
        self.assertEqual(schema["properties"]["input"]["type"], "string")
        self.assertIn("caller", schema["required"])
        self.assertIn("purpose", schema["required"])
        self.assertNotIn("fallback", inspect.signature(self.queue.submit).parameters)
        self.assertNotIn("fallback", inspect.getsource(SubprocessBackend))

    def test_feedback_is_immutable_and_work_only(self):
        job = self.submit()
        self.queue.run_once("runner")
        self.assertEqual(self.queue.feedback(job["job_id"], "used")["feedback"]["outcome"], "used")
        with self.assertRaisesRegex(AdmissionError, "feedback_immutable"):
            self.queue.feedback(job["job_id"], "discarded")
        test_job = self.submit(input="test", purpose="test")
        self.queue.run_once("runner")
        with self.assertRaisesRegex(AdmissionError, "feedback_not_eligible"):
            self.queue.feedback(test_job["job_id"], "used")

    def test_execution_rechecks_resources_and_reports_metadata_only(self):
        job = self.submit(priority="bulk")
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", False, 120)
        deferred = self.queue.run_once("runner")
        self.assertEqual(deferred["job_id"], job["job_id"])
        self.assertEqual(deferred["status"], "queued")
        self.assertEqual(deferred["admission"], "deferred")
        report = self.queue.state_report()
        self.assertEqual(report["queue_depth_by_class"], {"bulk": 1})
        self.assertFalse(report["resource"]["ac_power"])
        self.assertEqual(report["resource"]["verdict"]["bulk"], "deferred")

    def test_high_per_core_load_defers_execution(self):
        job = self.submit(input="wait for lower load")
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", True, 120, 1.50)
        deferred = self.queue.run_once("runner")
        self.assertEqual(deferred["status"], "queued")
        self.assertIn("resource_cpu_load", self.queue.status(job["job_id"])["error"])

    def test_high_load_ratio_with_healthy_measured_idle_is_admitted(self):
        job = self.submit(input="load average lags a genuinely idle cpu")
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", True, 120, 1.50, 0.40)
        terminal = self.queue.run_once("runner")
        self.assertEqual(terminal["job_id"], job["job_id"])
        self.assertEqual(terminal["disposition"]["outcome"], "complete")
        report = self.queue.state_report()
        self.assertEqual(report["resource"]["verdict"]["interactive"], "admissible")

    def test_high_load_ratio_with_unmeasured_idle_still_defers(self):
        job = self.submit(input="load average high, no idle probe available")
        self.sampler.snapshot = ResourceSnapshot(self.clock(), "normal", "normal", True, 120, 1.50, None)
        deferred = self.queue.run_once("runner")
        self.assertEqual(deferred["status"], "queued")
        self.assertIn("resource_cpu_load", self.queue.status(job["job_id"])["error"])

    def test_child_receives_minimal_environment_and_running_cancel_terminates_it(self):
        os.environ["EXAMPLE_API_KEY"] = "must-not-pass"
        try:
            command = [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"]
            backend = SubprocessBackend(command)
            env = backend.run({"job_id": "env-check"}, 1)
        finally:
            os.environ.pop("EXAMPLE_API_KEY", None)
        self.assertNotIn("EXAMPLE_API_KEY", env)
        self.assertTrue({"PATH", "LANG", "LC_ALL"}.issubset(env))
        self.assertFalse(any(name.endswith("API_KEY") for name in env))
        self.queue.caps = QueueCaps(max_jobs=2, bulk_ttl_seconds=5, timeout_seconds=2)
        self.queue.backend = SubprocessBackend([sys.executable, "-c", "import time; time.sleep(5)"])
        job = self.submit(input="cancel a running child")
        import threading
        thread = threading.Thread(target=lambda: self.queue.run_once("runner"))
        thread.start()
        for _ in range(100):
            if self.queue.status(job["job_id"])["status"] == "running":
                break
            time.sleep(0.01)
        self.assertEqual(self.queue.status(job["job_id"])["status"], "running")
        self.queue.cancel(job["job_id"])
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.queue.status(job["job_id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
