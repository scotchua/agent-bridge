"""Offline pin refresh contracts using only isolated operator config files."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import operator_isolation  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge.localq.backend_select import BackendSelectionError
from agent_bridge.localq.service import Service
from agent_bridge.localq.spool import ResourceSnapshot, SubprocessBackend, failure_code, _failure_text
from agent_bridge.orchestration.config import load


class Sampler:
    def sample(self):
        return ResourceSnapshot(time.time(), "normal", "normal", True, 120.0)


@unittest.skipUnless(os.name == "posix", "Gemma requires POSIX")
class GemmaPinRefreshTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.doc = {"config_version": "1", "local_backend": "gemma_certified"}
        for field in ("state_root", "local_queue_root", "capacity_db",
                      "worker_executable", "worker_state", "gemma_receipt_root"):
            self.doc[field] = str(self.root / field)
        Path(self.doc["gemma_receipt_root"]).mkdir()
        for field in ("gemma_delegate_executable", "gemma_python_executable",
                      "gemma_receipt_validator_executable"):
            path = self.root / field
            path.write_text("synthetic", encoding="utf-8")
            self.doc[field] = str(path)
        for index, field in enumerate(("gemma_delegate_sha256",
                                       "gemma_receipt_validator_sha256", "gemma_model_digest")):
            self.doc[field] = str(index + 1) * 64
        self.path = self.root / "operator.json"
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.doc), encoding="utf-8")

    def service(self, *, refresh=True):
        return Service.for_config(load(self.path), sampler=Sampler(),
                                  config_path=self.path if refresh else None)

    def job(self, service):
        service.queue.submit(task_type="summarize", input="synthetic text " + str(time.time_ns()), params={},
                             priority="interactive", classification="internal_nonclient",
                             caller="codex", purpose="test", idempotency_key=str(time.time_ns()))
        return service.once()["last_result"]

    def test_next_job_refreshes_all_pins_without_rebuilding_service(self):
        service = self.service()
        script = self.root / "echo-argv.py"
        script.write_text("import json, sys\njson.load(sys.stdin)\n"
                          "print(json.dumps({'argv': sys.argv[1:]}))\n", encoding="utf-8")
        backend = service.queue.backend
        backend.command = (sys.executable, str(script), *backend.command[2:])
        first = self.job(service)
        self.assertEqual(first["status"], "complete")
        for field in ("gemma_delegate_sha256", "gemma_receipt_validator_sha256", "gemma_model_digest"):
            self.doc[field] = "a" * 64
        self.write()
        second = self.job(service)
        self.assertEqual(second["status"], "complete")
        commands = [first["result"]["argv"], second["result"]["argv"]]
        self.assertEqual(len(commands), 2)
        for flag, old in (("--delegate-sha256", "1"), ("--validator-sha256", "2"),
                          ("--model-digest", "3")):
            self.assertEqual(commands[0][commands[0].index(flag) + 1], old * 64)
            self.assertEqual(commands[1][commands[1].index(flag) + 1], "a" * 64)

    def test_all_reload_failures_refuse_without_running_old_argv(self):
        original = dict(self.doc)
        cases = ["missing", "unreadable", "invalid", "incomplete", "private_worker",
                 "gemma_delegate_executable", "gemma_python_executable",
                 "gemma_receipt_root", "gemma_receipt_validator_executable"]
        for case in cases:
            with self.subTest(case=case):
                self.doc = dict(original)
                self.write()
                service = self.service()
                if case == "missing":
                    self.path.unlink()
                elif case == "invalid":
                    self.path.write_text("invalid secret config", encoding="utf-8")
                elif case == "incomplete":
                    del self.doc["gemma_model_digest"]
                    self.write()
                elif case == "private_worker":
                    self.doc = {k: v for k, v in self.doc.items() if not k.startswith("gemma_")}
                    self.doc["local_backend"] = "private_worker"
                    self.write()
                elif case.startswith("gemma_"):
                    replacement = self.root / (case + "-changed")
                    if case == "gemma_receipt_root":
                        replacement.mkdir()
                    else:
                        replacement.write_text("synthetic", encoding="utf-8")
                    self.doc[case] = str(replacement)
                    self.write()
                read_text = Path.read_text
                def read(path, *args, **kwargs):
                    if path == self.path:
                        raise PermissionError("secret")
                    return read_text(path, *args, **kwargs)
                expected = ("gemma_config_restart_required" if case == "private_worker"
                            or case.startswith("gemma_") else "gemma_config_reload_failed")
                read_context = mock.patch.object(Path, "read_text", read) if case == "unreadable" else nullcontext()
                with read_context, mock.patch.object(SubprocessBackend, "run", autospec=True) as run:
                    with self.assertRaisesRegex(BackendSelectionError, "^" + expected + "$"):
                        service.queue.backend.run({"job_id": "test"}, 1)
                    result = self.job(service)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["error"], expected)
                    self.assertEqual(failure_code(result["error"]), expected)
                    self.assertEqual(service.queue.backend._job_snapshots, {})
                    run.assert_not_called()
                # A failed read is never cached; a corrected file is usable.
                self.doc = dict(original)
                self.write()
                with mock.patch.object(SubprocessBackend, "run", return_value={"output": "ok"}) as run:
                    self.assertEqual(self.job(service)["status"], "complete")
                    run.assert_called_once()

    def test_without_config_path_pins_stay_fixed_even_if_file_disappears(self):
        service = self.service(refresh=False)
        self.path.unlink()
        with mock.patch.object(SubprocessBackend, "run", autospec=True,
                               return_value={"output": "ok"}) as run:
            self.assertEqual(self.job(service)["status"], "complete")
            backend = run.call_args.args[0]
            self.assertEqual(backend.command[backend.command.index("--delegate-sha256") + 1], "1" * 64)

    def test_health_gate_binds_to_current_delegate_pin(self):
        for name in ("delegate.py", "local_endpoint.py", "config.json"):
            (self.root / name).write_text("synthetic wrapper " + name, encoding="utf-8")
        self.doc["gemma_delegate_executable"] = str(self.root / "delegate.py")
        record = self.root / "health-record.json"
        self.doc.update(client_data_health_command=[sys.executable, "status", "--json"],
                        client_data_health_path=os.defpath, client_data_health_timeout_seconds=1,
                        client_data_health_record=str(record))
        self.write()
        service = self.service()
        gate = service.queue.client_data_health_gate
        fixed_gate = self.service(refresh=False).queue.client_data_health_gate
        delegate = self.root / "delegate.py"
        delegate.write_text("updated synthetic wrapper", encoding="utf-8")
        hashes = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                  for name in ("delegate.py", "local_endpoint.py", "config.json")}
        raw = json.dumps({"state": "verified", "full_passed": True, "wrapper_hashes": hashes}).encode()
        record.write_bytes(raw)
        response = json.dumps({"schema": 1, "result": "verified",
                               "record_sha256": hashlib.sha256(raw).hexdigest()}).encode()
        with mock.patch.object(gate, "_status", return_value=(0, response)):
            self.assertEqual(gate.check(), "client_data_health_binding_mismatch")
            self.doc["gemma_delegate_sha256"] = hashes["delegate.py"]
            self.write()
            self.assertIsNone(gate.check())
            with mock.patch.object(fixed_gate, "_status", return_value=(0, response)):
                self.assertEqual(fixed_gate.check(), "client_data_health_binding_mismatch")
            self.path.unlink()
            self.assertEqual(gate.check(), "client_data_health_unverified")

    def configure_client_fixture(self):
        fakes = Path(__file__).resolve().parent / "fakes"
        delegate = self.root / "delegate.py"
        validator = self.root / "delegation_receipt.py"
        shutil.copy(fakes / "fake_gemma_delegate.py", delegate)
        shutil.copy(fakes / "fake_receipt_validator.py", validator)
        for name in ("local_endpoint.py", "config.json"):
            (self.root / name).write_text("synthetic wrapper", encoding="utf-8")
        self.doc.update(gemma_delegate_executable=str(delegate),
                        gemma_python_executable=os.path.realpath(sys.executable),
                        gemma_receipt_validator_executable=str(validator),
                        gemma_delegate_sha256=hashlib.sha256(delegate.read_bytes()).hexdigest(),
                        gemma_receipt_validator_sha256=hashlib.sha256(validator.read_bytes()).hexdigest(),
                        gemma_model_digest="ab" * 32)
        status = self.root / "status.py"
        self.response = self.root / "status-response.json"
        status.write_text("from pathlib import Path\n"
                          f"print(Path({str(self.response)!r}).read_text())\n", encoding="utf-8")
        self.doc.update(client_data_health_command=[sys.executable, str(status), "status", "--json"],
                        client_data_health_path=os.defpath, client_data_health_timeout_seconds=2,
                        client_data_health_record=str(self.root / "health-record.json"))
        hashes = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                  for name in ("delegate.py", "local_endpoint.py", "config.json")}
        raw = json.dumps({"state": "verified", "full_passed": True, "wrapper_hashes": hashes}).encode()
        Path(self.doc["client_data_health_record"]).write_bytes(raw)
        self.response.write_text(json.dumps({"schema": 1, "result": "verified",
                                            "record_sha256": hashlib.sha256(raw).hexdigest()}))
        self.write()

    def submit_client(self, service):
        return service.queue.submit(task_type="summarize", input="synthetic client fixture " + str(time.time_ns()), params={},
                                    priority="interactive", classification="client_derived",
                                    caller="codex", purpose="test", idempotency_key=str(time.time_ns()))

    def test_other_startup_settings_require_restart(self):
        self.configure_client_fixture()
        original = dict(self.doc)
        changes = {"gemma_timeout_seconds": 720, "gemma_pilot_tasks": ["extract"],
                   "client_data_health_command": [sys.executable, "changed.py", "status", "--json"],
                   "client_data_health_path": os.defpath + os.pathsep + str(self.root),
                   "client_data_health_timeout_seconds": 3,
                   "client_data_health_record": str(self.root / "other-record.json")}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.doc = dict(original)
                self.write()
                service = self.service()
                self.doc[field] = value
                self.write()
                with mock.patch.object(SubprocessBackend, "run", autospec=True) as run:
                    with self.assertRaisesRegex(BackendSelectionError, "^gemma_config_restart_required$"):
                        service.queue.backend.run({"job_id": "direct"}, 1)
                    result = self.job(service)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["error"], "gemma_config_restart_required")
                    run.assert_not_called()
                self.assertEqual(service.queue.backend._job_snapshots, {})

    def test_only_exact_content_free_reload_diagnostics_are_recorded(self):
        for reason in ("gemma_config_reload_failed", "gemma_config_restart_required"):
            self.assertEqual(_failure_text(BackendSelectionError(reason)), reason)
            self.assertEqual(_failure_text(BackendSelectionError(reason + ": secret")), "backend_failure")
            self.assertEqual(_failure_text(BackendSelectionError(reason, "secret")), "backend_failure")
            self.assertEqual(failure_code(reason + ": secret"), "unknown_failure")

    def test_client_job_through_service_checks_gate_and_launches_child(self):
        self.configure_client_fixture()
        service = self.service()
        gate = service.queue.client_data_health_gate
        with mock.patch.object(gate, "check", wraps=gate.check) as check:
            job = self.submit_client(service)
            with mock.patch.object(service.queue.backend.pins, "current",
                                   wraps=service.queue.backend.pins.current) as current:
                result = service.once()["last_result"]
            current.assert_called_once_with()
        self.assertEqual(result["job_id"], job["job_id"])
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["result"]["provider"], "gemma_certified")
        self.assertEqual(check.call_count, 2)  # admission and execution
        self.assertEqual(check.call_args.kwargs["config_snapshot"].gemma_delegate_sha256,
                         self.doc["gemma_delegate_sha256"])
        self.assertEqual(service.queue.backend._job_snapshots, {})

    def test_delegate_and_pin_swap_after_gate_keeps_verified_pin_in_child(self):
        self.configure_client_fixture()
        service = self.service()
        self.submit_client(service)
        backend = service.queue.backend
        gate = service.queue.client_data_health_gate
        check = gate.check
        old_pin = self.doc["gemma_delegate_sha256"]
        def check_then_swap(**kwargs):
            self.assertIsNone(check(**kwargs))
            self.assertEqual(kwargs["config_snapshot"].gemma_delegate_sha256, old_pin)
            delegate = Path(self.doc["gemma_delegate_executable"])
            delegate.write_bytes(delegate.read_bytes() + b"\n# replaced after health check\n")
            self.doc["gemma_delegate_sha256"] = hashlib.sha256(delegate.read_bytes()).hexdigest()
            self.write()
            return None
        run_child = SubprocessBackend.run
        commands = []
        def launch(child, payload, timeout_seconds):
            commands.append(child.command)
            return run_child(child, payload, timeout_seconds)
        with mock.patch.object(gate, "check", side_effect=check_then_swap), \
             mock.patch.object(SubprocessBackend, "run", autospec=True, side_effect=launch), \
             mock.patch.object(backend.pins, "current", wraps=backend.pins.current) as current:
            result = service.once()["last_result"]
        current.assert_called_once_with()
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command[command.index("--delegate-sha256") + 1], old_pin)
        self.assertNotEqual(old_pin, self.doc["gemma_delegate_sha256"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(failure_code(result["error"]), "delegate_source_changed")
        self.assertFalse((self.root / "calls.txt").exists())
        self.assertEqual(backend._job_snapshots, {})

    def run_other_thread(self, action):
        errors = []
        def invoke():
            try:
                action()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=invoke)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "direct caller did not finish")
        if errors:
            raise errors[0]

    def test_same_job_direct_run_on_other_thread_cannot_consume_queue_snapshot(self):
        self.configure_client_fixture()
        service = self.service()
        job = self.submit_client(service)
        backend = service.queue.backend
        gate = service.queue.client_data_health_gate
        check = gate.check
        old_pin = self.doc["gemma_delegate_sha256"]
        queue_thread = threading.get_ident()
        commands = []
        run_child = SubprocessBackend.run
        def launch(child, payload, timeout_seconds):
            commands.append((threading.get_ident(), child.command))
            self.assertEqual(set(payload), {"job_id", "task_type", "input", "params"})
            return run_child(child, payload, timeout_seconds)
        def check_then_interleave(**kwargs):
            self.assertIsNone(check(**kwargs))
            self.assertEqual(kwargs["config_snapshot"].gemma_delegate_sha256, old_pin)
            def direct():
                result = backend.run({"job_id": job["job_id"], "task_type": "summarize",
                                      "input": "synthetic direct call", "params": {}}, 5)
                self.assertEqual(result["provider"], "gemma_certified")
            self.run_other_thread(direct)
            delegate = Path(self.doc["gemma_delegate_executable"])
            delegate.write_bytes(delegate.read_bytes() + b"\n# changed to B\n")
            self.doc["gemma_delegate_sha256"] = hashlib.sha256(delegate.read_bytes()).hexdigest()
            self.write()
            return None
        with mock.patch.object(gate, "check", side_effect=check_then_interleave), \
             mock.patch.object(SubprocessBackend, "run", autospec=True, side_effect=launch), \
             mock.patch.object(backend.pins, "current", wraps=backend.pins.current) as current:
            result = service.once()["last_result"]
        self.assertEqual(current.call_count, 2)  # queue preparation and fresh direct call
        self.assertEqual(len(commands), 2)
        self.assertNotEqual(commands[0][0], queue_thread)
        self.assertEqual(commands[1][0], queue_thread)
        for _, command in commands:
            self.assertEqual(command[command.index("--delegate-sha256") + 1], old_pin)
        self.assertNotEqual(old_pin, self.doc["gemma_delegate_sha256"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(failure_code(result["error"]), "delegate_source_changed")
        self.assertEqual(backend._job_snapshots, {})

    def test_same_job_direct_discard_on_other_thread_preserves_queue_snapshot(self):
        self.configure_client_fixture()
        service = self.service()
        job = self.submit_client(service)
        backend = service.queue.backend
        gate = service.queue.client_data_health_gate
        check = gate.check
        def check_then_discard(**kwargs):
            self.assertIsNone(check(**kwargs))
            snapshot = backend._job_snapshots[job["job_id"]]
            self.run_other_thread(lambda: backend.discard_job(job["job_id"]))
            self.assertIs(backend._job_snapshots[job["job_id"]], snapshot)
            return None
        with mock.patch.object(gate, "check", side_effect=check_then_discard), \
             mock.patch.object(backend.pins, "current", wraps=backend.pins.current) as current:
            result = service.once()["last_result"]
        current.assert_called_once_with()
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(backend._job_snapshots, {})

    def test_prepared_run_raises_when_snapshot_is_removed_or_already_consumed(self):
        backend = self.service().queue.backend
        for removed in (True, False):
            with self.subTest(removed=removed):
                backend.prepare_job("same-job")
                with mock.patch.object(SubprocessBackend, "run", return_value={}) as run:
                    if removed:
                        backend._job_snapshots.pop("same-job")
                    else:
                        backend.run({"job_id": "same-job"}, 1)
                        run.assert_called_once()
                        run.reset_mock()
                    with mock.patch.object(backend.pins, "current") as current:
                        with self.assertRaisesRegex(BackendSelectionError, "^gemma_config_reload_failed$"):
                            backend.run({"job_id": "same-job"}, 1)
                    current.assert_not_called()
                    run.assert_not_called()
                backend.discard_job("same-job")
                self.assertEqual(backend._preparation.prepared, set())

    def test_missing_prepared_snapshot_refuses_without_reload_or_launch(self):
        self.configure_client_fixture()
        service = self.service()
        job = self.submit_client(service)
        backend = service.queue.backend
        gate = service.queue.client_data_health_gate
        check = gate.check
        def check_then_remove(**kwargs):
            self.assertIsNone(check(**kwargs))
            backend._job_snapshots.pop(job["job_id"])
            return None
        with mock.patch.object(gate, "check", side_effect=check_then_remove), \
             mock.patch.object(SubprocessBackend, "run", autospec=True) as run, \
             mock.patch.object(backend.pins, "current", wraps=backend.pins.current) as current:
            result = service.once()["last_result"]
        current.assert_called_once_with()
        run.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "gemma_config_reload_failed")
        self.assertEqual(backend._job_snapshots, {})
        self.assertEqual(backend._preparation.prepared, set())
        # Cleanup restores the direct-call behavior on the same thread.
        with mock.patch.object(SubprocessBackend, "run", return_value={}) as run, \
             mock.patch.object(backend.pins, "current", wraps=backend.pins.current) as current:
            backend.run({"job_id": job["job_id"]}, 1)
        current.assert_called_once_with()
        run.assert_called_once()

    def test_snapshot_discarded_on_prelaunch_refusal_payload_failure_and_interrupt(self):
        self.configure_client_fixture()
        for case in ("health", "payload", "interrupt"):
            with self.subTest(case=case):
                service = self.service()
                self.submit_client(service)
                backend = service.queue.backend
                gate = service.queue.client_data_health_gate
                if case == "health":
                    context = mock.patch.object(gate, "check", return_value="client_data_health_failed")
                elif case == "payload":
                    context = mock.patch.object(service.queue, "_read_blob", side_effect=OSError("secret"))
                else:
                    context = mock.patch.object(gate, "check", side_effect=SystemExit())
                with context, mock.patch.object(SubprocessBackend, "run", autospec=True) as run:
                    if case == "interrupt":
                        with self.assertRaises(SystemExit):
                            service.once()
                    else:
                        result = service.once()["last_result"]
                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(result["error"], "client_data_health_failed" if case == "health"
                                         else "payload_unreadable")
                    run.assert_not_called()
                self.assertEqual(backend._job_snapshots, {})

    def test_timeout_kills_process_group_and_clears_shared_registry(self):
        service = self.service()
        backend = service.queue.backend
        script = self.root / "timeout.py"
        pidfile = self.root / "grandchild.pid"
        script.write_text(
            "import pathlib, signal, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n"
            "time.sleep(30)\n", encoding="utf-8")
        backend.command = (sys.executable, str(script), *backend.command[2:])
        service.queue.caps = replace(service.queue.caps, timeout_seconds=.5)
        killpg = os.killpg
        killed = []
        def kill_registered_group(pid, sig):
            registered = pid in [proc.pid for proc in backend._processes.values()]
            killed.append((pid, sig, registered))
            killpg(pid, sig)
        with mock.patch("agent_bridge.bounded_capture.os.killpg", side_effect=kill_registered_group):
            result = self.job(service)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "execution_timeout")
        self.assertTrue(killed)
        self.assertTrue(all(sig == signal.SIGKILL and registered for _, sig, registered in killed))
        self.assertEqual(backend._processes, {})
        self.assertEqual(backend._job_snapshots, {})
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                self.fail("grandchild survived timeout")
            time.sleep(.02)

    def test_inflight_argv_and_cancel_registry_survive_pin_change(self):
        service = self.service()
        backend = service.queue.backend
        script = self.root / "sleep.py"
        started = self.root / "started"
        released = self.root / "released"
        script.write_text(f"from pathlib import Path\nimport time\nPath({str(started)!r}).touch()\n"
                          f"while not Path({str(released)!r}).exists(): time.sleep(.01)\n"
                          "print('{}')\n")
        backend.command = (sys.executable, str(script), *backend.command[2:])
        errors = []
        def run():
            try:
                backend.run({"job_id": "inflight"}, 5)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(started.exists())
            proc = backend._processes["inflight"]
            old_argv = proc.args
            self.doc["gemma_delegate_sha256"] = "a" * 64
            self.write()
            self.assertEqual(proc.args, old_argv)
            self.assertEqual(proc.args[proc.args.index("--delegate-sha256") + 1], "1" * 64)
            with mock.patch("agent_bridge.localq.spool.os.killpg") as killpg:
                self.assertTrue(backend.cancel("inflight"))
                killpg.assert_called_once_with(proc.pid, signal.SIGTERM)
        finally:
            released.touch()
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(backend._processes, {})
        self.assertFalse(backend.cancel("inflight"))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
