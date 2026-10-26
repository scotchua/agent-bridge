"""Deterministic tests for the isolated local queue foundation."""

import inspect
import dataclasses
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import (AdmissionError, FakeBackend, LocalQueue, QueueCaps,
                                 ResourceSnapshot)
from agent_bridge.localq import mcp
from agent_bridge.localq.spool import SubprocessBackend
from agent_bridge.bounded_capture import capture_process


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
    def test_duration_caps_require_finite_positive_values(self):
        for field in ("bulk_ttl_seconds", "sample_max_age_seconds", "lease_seconds", "timeout_seconds"):
            for value in (-1.0, 0.0, math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        dataclasses.replace(QueueCaps(), **{field: value})

    def test_default_and_custom_duration_caps_allow_jobs(self):
        for caps in (QueueCaps(), QueueCaps(bulk_ttl_seconds=5.0,
                                           sample_max_age_seconds=2.0,
                                           lease_seconds=3.0, timeout_seconds=4.0)):
            with self.subTest(caps=caps), tempfile.TemporaryDirectory() as root:
                queue = LocalQueue(root, sampler=self.sampler, backend=self.backend,
                                   caps=caps, clock=self.clock)
                job = queue.submit(task_type="summarize", input="invented bulk input",
                                   params={}, priority="bulk", classification="synthetic",
                                   caller="codex", purpose="test")
                self.assertEqual(queue.status(job["job_id"])["expires_at"],
                                 self.clock() + caps.bulk_ttl_seconds)
                self.assertEqual(queue.run_once("runner")["status"], "complete")

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
        for index, (command, reason) in enumerate((
                ("import sys; sys.exit(3)", "child exited unsuccessfully"),
                ("import sys; print('{\"ok\": false, \"error\": \"ValueError\", "
                 "\"error_detail\": \"queue provenance unavailable\"}'); sys.exit(1)",
                 "child exited unsuccessfully: ValueError: queue provenance unavailable"),
                ("import sys; print('{\"ok\": false, \"error\": \"x\\\\u0007y\"}'); sys.exit(1)",
                 "child exited unsuccessfully"),
                ("print('not json')", "child did not return a JSON object"))):
            with self.subTest(reason):
                queue = LocalQueue(self.temp.name, sampler=self.sampler, caps=caps,
                                   backend=SubprocessBackend([sys.executable, "-c", command]),
                                   clock=self.clock)
                job = queue.submit(task_type="summarize", input=f"{index}:{reason}", params={},
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

    def test_runtime_error_does_not_store_client_text(self):
        sentinel = "INVENTED_CLIENT_SENTINEL_731"

        def boom(payload):
            raise RuntimeError("backend failed near " + payload["input"])

        self.queue.backend = FakeBackend(boom)
        job = self.submit(input=sentinel)
        terminal = self.queue.run_once("runner")
        with self.queue._db() as db:
            stored = db.execute("SELECT error FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]
        self.assertEqual(stored, "RuntimeError")
        self.assertEqual(terminal["status"], "failed")
        self.assertNotIn(sentinel, json.dumps(terminal))

    def test_corrupt_selected_input_blob_fails_and_later_job_runs(self):
        missing = self.submit(input="INVENTED_MISSING_BLOB_319")
        later = self.submit(input="later valid input")
        with self.queue._db() as db:
            digest = db.execute("SELECT input_blob FROM jobs WHERE job_id=?",
                                (missing["job_id"],)).fetchone()[0]
        (self.queue.blobs / digest).write_text('"corrupt but valid JSON"', encoding="utf-8")

        failed = self.queue.run_once("runner")
        self.assertEqual(failed["job_id"], missing["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "payload_unreadable")
        self.assertEqual(failed["disposition"],
                         {"outcome": "failed", "reason": "payload_unreadable"})
        self.assertNotIn("INVENTED_MISSING_BLOB_319", json.dumps(failed))
        self.assertEqual(self.backend.calls, [])

        completed = self.queue.run_once("runner")
        self.assertEqual(completed["job_id"], later["job_id"])
        self.assertEqual(completed["status"], "complete")

    def test_child_failure_does_not_store_client_text(self):
        import dataclasses
        sentinel = "INVENTED_CHILD_SENTINEL_842"
        self.queue.caps = dataclasses.replace(self.caps, timeout_seconds=30)
        for index, response in enumerate((
                {"ok": False, "error": "ValueError", "error_detail": "queue provenance unavailable " + sentinel},
                {"ok": False, "error": "ValueError " + sentinel, "error_detail": "queue provenance unavailable"})):
            with self.subTest(index=index):
                command = "import sys; print(" + repr(json.dumps(response)) + "); sys.exit(1)"
                self.queue.backend = SubprocessBackend([sys.executable, "-c", command])
                job = self.submit(input=f"invented input {index}")
                terminal = self.queue.run_once("runner")
                with self.queue._db() as db:
                    stored = db.execute("SELECT error FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]
                expected = ("RuntimeError: child exited unsuccessfully: ValueError" if index == 0
                            else "RuntimeError: child exited unsuccessfully")
                self.assertEqual(stored, expected)
                self.assertEqual(terminal["status"], "failed")
                self.assertNotIn(sentinel, json.dumps(terminal))

    def test_child_keeps_fixed_worker_and_delegate_reasons(self):
        import dataclasses
        self.queue.caps = dataclasses.replace(self.caps, timeout_seconds=30)
        for error, detail in (("RuntimeError", "private worker rejected request"),
                              ("GemmaReceiptError", "receipt_model_digest_mismatch"),
                              ("GemmaRefusal", "extract_document_1_failed:receipt_invalid"),
                              ("GemmaRefusal", "classify_line_1_invalid")):
            with self.subTest(detail=detail):
                response = json.dumps({"ok": False, "error": error, "error_detail": detail})
                command = "import sys; print(" + repr(response) + "); sys.exit(1)"
                self.queue.backend = SubprocessBackend([sys.executable, "-c", command])
                self.submit(input=detail)
                terminal = self.queue.run_once("runner")
                self.assertEqual(terminal["status"], "failed")
                self.assertEqual(terminal["error"],
                                 f"RuntimeError: child exited unsuccessfully: {error}: {detail}")

    def test_child_process_timeout_is_hard_and_indeterminate(self):
        self.queue.backend = SubprocessBackend([sys.executable, "-c", "import time; time.sleep(1)"])
        job = self.submit()
        terminal = self.queue.run_once("runner")
        self.assertEqual(terminal["job_id"], job["job_id"])
        self.assertEqual(terminal["status"], "unknown")
        self.assertEqual(terminal["error"], "execution_timeout")

    def test_chatty_child_is_capped_and_records_only_a_fixed_failure(self):
        self.queue.caps = dataclasses.replace(self.caps, timeout_seconds=5)
        command = ("import sys; sys.stdout.write('private-output-' * 10000); "
                   "sys.stdout.flush()")
        self.queue.backend = SubprocessBackend([sys.executable, "-c", command],
                                               capture_limit_bytes=1024)
        self.submit(input="synthetic input")
        terminal = self.queue.run_once("runner")
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["error"],
                         "RuntimeError: child output exceeded bounded capture")
        self.assertNotIn("private-output", json.dumps(terminal))

    def test_slow_named_capture_fsync_after_eof_is_not_stream_unclosed(self):
        with tempfile.TemporaryDirectory() as temporary:
            process = subprocess.Popen(
                [sys.executable, "-c", "print('ok')"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)

            def slow_fsync(_fd):
                time.sleep(1)

            with mock.patch("agent_bridge.bounded_capture.os.fsync", side_effect=slow_fsync):
                capture = capture_process(
                    process, stdout_path=Path(temporary) / "stdout",
                    stderr_path=Path(temporary) / "stderr", limit=1024, timeout=2)
        self.assertIsNone(capture.failure)

    def test_anonymous_capture_failure_does_not_start_a_local_child(self):
        backend = SubprocessBackend([sys.executable, "-c", "raise SystemExit(0)"])
        with mock.patch("agent_bridge.localq.spool.tempfile.TemporaryFile",
                        side_effect=OSError("capture unavailable")), \
             mock.patch("agent_bridge.localq.spool.subprocess.Popen") as popen:
            with self.assertRaisesRegex(OSError, "capture unavailable"):
                backend.run({"job_id": "capture-setup"}, 1)
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "descendant pipe inheritance is POSIX-only")
    def test_blocked_stdin_writer_does_not_extend_the_capture_deadline(self):
        child = ("import os,time\n"
                 "if os.fork(): os._exit(0)\n"
                 "time.sleep(5)\n")
        backend = SubprocessBackend([sys.executable, "-c", child])
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "child output capture failed"):
            backend.run({"job_id": "stdin-hang", "input": "x" * (2 * 1024 * 1024)}, .5)
        self.assertLess(time.monotonic() - started, 2.5)

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

    def test_lease_renewal_keeps_a_live_long_job_from_losing_its_lease(self):
        """A job outliving the lease is not recovered while its executor runs."""
        caps = dataclasses.replace(self.caps, lease_seconds=0.3, timeout_seconds=5)
        sampler = Sampler(time.time)
        release = threading.Event()
        running = threading.Event()

        def long_job(data):
            running.set()
            release.wait(5)
            return {"draft": "done"}

        queue = LocalQueue(self.temp.name, sampler=sampler, backend=FakeBackend(long_job),
                           caps=caps, clock=time.time)
        job = self.submit()
        results = []
        runner = threading.Thread(target=lambda: results.append(queue.run_once("alive")))
        runner.start()
        try:
            self.assertTrue(running.wait(2))
            time.sleep(0.9)  # three lease lengths without renewal
            other = LocalQueue(self.temp.name, sampler=sampler, backend=FakeBackend(),
                               caps=caps, clock=time.time)
            self.assertIsNone(other._run_once_under_lock("recovery"))
            self.assertEqual(queue.status(job["job_id"])["status"], "running")
        finally:
            release.set()
            runner.join(5)
        self.assertEqual(results[0]["status"], "complete")

    def _running_under(self, job, lock_owner, lock_until, job_until):
        with self.queue._db() as db:
            db.execute("UPDATE jobs SET status='running',lease_owner='sleeper',lease_until=? WHERE job_id=?",
                       (job_until, job["job_id"]))
            db.execute("INSERT INTO executor_lock(name,owner,lease_until) VALUES('executor',?,?) "
                       "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until",
                       (lock_owner, lock_until))

    def _renew_once(self, job):
        stop = mock.MagicMock()
        stop.wait.side_effect = [False, True]
        self.queue._renew_lease_while_running(job["job_id"], "sleeper", stop)
        with self.queue._db() as db:
            leased = db.execute("SELECT lease_until FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]
            lock = db.execute("SELECT owner,lease_until FROM executor_lock").fetchone()
        return leased, tuple(lock), stop.wait.call_count

    def test_renewal_after_sleep_does_not_resurrect_an_expired_lease(self):
        job = self.submit()
        now = self.clock()
        self._running_under(job, "sleeper", now - 1, now - 1)
        leased, lock, waits = self._renew_once(job)
        self.assertEqual(leased, now - 1)
        self.assertEqual(lock, ("sleeper", now - 1))
        self.assertEqual(waits, 1)  # renewal stopped for good

    def test_renewal_stops_when_another_executor_holds_the_lock(self):
        job = self.submit()
        now = self.clock()
        self._running_under(job, "successor", now + 5, now + 1)
        leased, lock, waits = self._renew_once(job)
        self.assertEqual(leased, now + 1)
        self.assertEqual(lock, ("successor", now + 5))
        self.assertEqual(waits, 1)

    def test_renewal_reads_the_clock_after_taking_the_write_lock(self):
        """Leases that lapse while the renewer waits for the lock stay lapsed."""
        job = self.submit()
        now = self.clock()
        self._running_under(job, "sleeper", now + 1, now + 1)
        blocker = self.queue._connect()
        blocker.execute("BEGIN IMMEDIATE")
        stop = mock.MagicMock()
        stop.wait.side_effect = [False, True]
        waiting = threading.Event()
        connect = self.queue._connect

        def traced_connect():
            # Signals once the renewer's own BEGIN IMMEDIATE has started.
            conn = connect()
            conn.set_trace_callback(lambda sql: sql == "BEGIN IMMEDIATE" and waiting.set())
            return conn

        renewer = threading.Thread(target=self.queue._renew_lease_while_running,
                                   args=(job["job_id"], "sleeper", stop))
        with mock.patch.object(self.queue, "_connect", traced_connect):
            try:
                renewer.start()
                self.assertTrue(waiting.wait(5))  # renewer is blocked on the write lock
                self.clock.now += 2  # both leases lapse during the wait
            finally:
                blocker.execute("COMMIT")
                blocker.close()
                renewer.join(5)
        self.assertFalse(renewer.is_alive())
        with self.queue._db() as db:
            leased = db.execute("SELECT lease_until FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]
            lock = tuple(db.execute("SELECT owner,lease_until FROM executor_lock").fetchone())
        self.assertEqual(leased, now + 1)
        self.assertEqual(lock, ("sleeper", now + 1))
        self.assertEqual(stop.wait.call_count, 1)

    def test_renewal_extends_both_leases_while_still_held(self):
        job = self.submit()
        now = self.clock()
        self._running_under(job, "sleeper", now + 1, now + 1)
        leased, lock, waits = self._renew_once(job)
        until = now + self.caps.lease_seconds
        self.assertEqual(leased, until)
        self.assertEqual(lock, ("sleeper", until))
        self.assertEqual(waits, 2)

    def test_dead_executor_still_goes_unknown_after_its_lease_lapses(self):
        job = self.submit()
        with self.queue._db() as db:
            db.execute("UPDATE jobs SET status='running',lease_owner='dead',lease_until=? WHERE job_id=?",
                       (self.clock() + 1, job["job_id"]))
            db.execute("INSERT INTO executor_lock(name,owner,lease_until) VALUES('executor','dead',?) "
                       "ON CONFLICT(name) DO UPDATE SET owner='dead',lease_until=?",
                       (self.clock() + 1, self.clock() + 1))
        self.assertIsNone(self.queue.run_once("successor"))
        self.assertEqual(self.queue.status(job["job_id"])["status"], "running")
        self.clock.now += 2  # nothing renews a dead executor's lease
        self.assertIsNone(self.queue.run_once("successor"))
        self.assertEqual(self.queue.status(job["job_id"])["status"], "unknown")
        self.assertEqual(self.queue.result(job["job_id"])["disposition"]["reason"],
                         "executor_lease_lost")

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
