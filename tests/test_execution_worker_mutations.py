"""Focused offline checks for the execution worker's safety and control flow."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge import drift
from agent_bridge.orchestration import execution_worker as worker
from agent_bridge.orchestration.execution_queue import ExecutionAdmissionError


class WorkerLockMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_new_lock_has_no_descriptor_to_close(self):
        lock = worker.WorkerLock(self.root / "queue")
        with mock.patch.object(worker.os, "close") as close:
            lock.__exit__(None, None, None)
        close.assert_not_called()

    def test_enter_uses_private_mode_and_commits_pid_record(self):
        real_open = os.open
        real_seek = os.lseek
        real_truncate = os.ftruncate
        real_fsync = os.fsync
        with mock.patch.object(worker.os, "open", wraps=real_open) as opened, \
             mock.patch.object(worker.os, "lseek", wraps=real_seek) as seek, \
             mock.patch.object(worker.os, "ftruncate", wraps=real_truncate) as truncate, \
             mock.patch.object(worker.os, "fsync", wraps=real_fsync) as sync:
            with worker.WorkerLock(self.root / "queue") as lock:
                fd = lock._fd
                self.assertIsNotNone(fd)
                self.assertTrue(any(call.args[0] == lock.path and
                                    call.args[2] == 0o600 for call in opened.call_args_list))
                seek.assert_any_call(fd, 0, os.SEEK_SET)
                truncate.assert_any_call(fd, len(f"pid={os.getpid()}\n"))
                sync.assert_any_call(fd)
        self.assertEqual(lock.path.read_text(encoding="ascii"),
                         f"pid={os.getpid()}\n")

    def test_enter_returns_the_lock_and_exit_releases_and_closes(self):
        events = []

        class Platform:
            def enforce_owner_only_file(self, fd):
                pass

            def verify_owner_only_path(self, directory, probe_file):
                return True, {}

            def lock_exclusive(self, fd, path, timeout):
                class Held:
                    def __enter__(self):
                        events.append("acquired")

                    def __exit__(self, *_):
                        events.append("released")

                return Held()

        real_close = os.close
        lock = worker.WorkerLock(self.root / "queue", platform=Platform())
        with mock.patch.object(worker.os, "close", wraps=real_close) as close:
            with lock as entered:
                self.assertIs(entered, lock)
                fd = lock._fd
                self.assertIsInstance(fd, int)
                self.assertEqual(events, ["acquired"])
            self.assertEqual(events, ["acquired", "released"])
            close.assert_any_call(fd)
            self.assertIsNone(lock._fd)
            self.assertIsNone(lock._release)


class SelectExecutorMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("codex.py", "claude.py", "python"):
            path = self.root / name
            path.write_text("# invented executable\n", encoding="utf-8")
            path.chmod(0o700)

    def _config(self):
        return SimpleNamespace(
            codex_task_executable=self.root / "codex.py",
            claude_task_executable=self.root / "claude.py",
            python_executable=self.root / "python",
            claude_config_dir=None,
            client_derived_routes=frozenset())

    def test_one_missing_posix_executable_refuses(self):
        cfg = self._config()
        cfg.python_executable = None
        with self.assertRaisesRegex(ExecutionAdmissionError,
                                    "execution_configuration_missing"):
            worker.select_executor(cfg, os_name="posix")

    def test_posix_review_script_comes_from_the_package(self):
        executor = worker.select_executor(self._config(), os_name="posix")
        expected = Path(worker.__file__).resolve().parents[1] / "execution" / "codex_review.py"
        self.assertEqual(executor.harnesses.codex_review, expected)
        self.assertTrue(expected.is_file())


class ConfiguredQueueMutationTests(unittest.TestCase):
    def test_configured_queue_recovers_after_lock_acquisition(self):
        cfg = SimpleNamespace(execution_queue_root=Path("invented-queue"),
                              state_root=Path("invented-state"),
                              client_derived_routes=frozenset())
        with mock.patch.object(worker, "load", return_value=cfg), \
             mock.patch.object(worker, "select_executor", return_value=object()), \
             mock.patch.object(worker.autoroute, "model_reserved_for", return_value=lambda _: False), \
             mock.patch.object(worker, "ExecutionQueue") as queue_type:
            self.assertIs(worker._configured_queue("invented-config.json"),
                          queue_type.return_value)
        self.assertTrue(queue_type.call_args.kwargs["recover_interrupted"])


class RunMutationTests(unittest.TestCase):
    def _context(self, queue, changed=(), **config_values):
        cfg = SimpleNamespace(execution_queue_root=Path("invented-queue"),
                              **config_values)
        queue.queued_providers.return_value = set()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(worker, "load", return_value=cfg))
        stack.enter_context(mock.patch.object(worker, "WorkerLock", return_value=contextlib.nullcontext()))
        stack.enter_context(mock.patch.object(worker, "_configured_queue", return_value=queue))
        quick = stack.enter_context(mock.patch.object(
            drift, "quick_check", return_value={"changed": list(changed)}))
        attention = stack.enter_context(mock.patch.object(drift, "report_attention"))
        signals = stack.enter_context(mock.patch.object(worker.signal, "signal",
                                                        return_value="previous"))
        return quick, attention, signals

    def test_zero_interval_is_refused_before_loading_configuration(self):
        with mock.patch.object(worker, "load") as load:
            with self.assertRaisesRegex(ExecutionAdmissionError, "interval_invalid"):
                worker.run("invented-config.json", once=True, interval=0)
        load.assert_not_called()

    def test_once_reports_drift_holds_and_dispatches_under_worker_id(self):
        queue = mock.Mock()
        queue.run_once.side_effect = [
            {"job_id": "job-invented", "state": "done"},
            AssertionError("once mode dispatched twice")]
        quick, attention, signals = self._context(queue, ("codex", "claude"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = worker.run("invented-config.json", once=True, interval=1,
                                worker_id="worker-invented")
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "job-invented done\n")
        quick.assert_called_once_with("invented-config.json")
        attention.assert_called_once()
        queue.hold_dependencies.assert_called_once_with(
            {"codex": "codex", "claude": "claude"})
        queue.run_once.assert_called_once_with("worker-invented",
                                               allowed_providers=set())
        self.assertEqual(signals.call_count, 4)

    def test_stop_signal_ends_loop_and_restores_handlers(self):
        queue = mock.Mock()
        _, _, signals = self._context(queue)

        def run_once(_identity, **_kwargs):
            if queue.run_once.call_count > 1:
                self.fail("worker ran after stop signal")
            handler = signals.call_args_list[0].args[1]
            handler(worker.signal.SIGTERM, None)
            return None

        queue.run_once.side_effect = run_once
        with mock.patch.object(worker.time, "sleep",
                               side_effect=[None, AssertionError("worker did not stop")]) as sleep:
            self.assertEqual(worker.run("invented-config.json", once=False,
                                        interval=1, worker_id="worker-invented"), 0)
        self.assertEqual(queue.run_once.call_count, 1)
        sleep.assert_called_once_with(1)
        self.assertEqual(signals.call_args_list[-2].args,
                         (worker.signal.SIGTERM, "previous"))
        self.assertEqual(signals.call_args_list[-1].args,
                         (worker.signal.SIGINT, "previous"))

    def test_failed_auth_probe_makes_zero_generation_calls_and_restored_auth_runs_job(self):
        """The same queued route is held before claim, then released on login."""

        queue = mock.Mock()
        queue.run_once.return_value = {"job_id": "same-job", "state": "complete"}
        self._context(queue, claude_config_dir=Path("/invented/claude-home"))
        queue.queued_providers.return_value = {"claude"}
        signed_in = {"state": "signed_in", "authMethod": "claude.ai",
                     "subscriptionType": "max"}
        with (mock.patch.object(worker.claude_config, "is_ready", return_value=True),
              mock.patch.object(worker, "_provider_executable", return_value="/fake/claude"),
              mock.patch.object(worker.health, "auth_status",
                                side_effect=[{"state": "not_signed_in"}, signed_in])):
            with contextlib.redirect_stdout(io.StringIO()):
                first = worker.run("invented-config.json", once=True, interval=1,
                                   worker_id="worker-invented")
            self.assertEqual(first, 0)
            # ``run_once`` is the only worker path that can invoke the
            # provider harness. It was never reached while auth was absent.
            queue.run_once.assert_not_called()
            queue.hold_dependencies.assert_called_once_with(
                {"claude": worker.READINESS_UNAVAILABLE})

            with contextlib.redirect_stdout(io.StringIO()):
                second = worker.run("invented-config.json", once=True, interval=1,
                                    worker_id="worker-invented")
        self.assertEqual(second, 0)
        queue.run_once.assert_called_once_with("worker-invented",
                                               allowed_providers={"claude"})
        self.assertEqual(queue.hold_dependencies.call_args_list[-1].args[0], {})

    def test_late_unprobed_provider_is_held_and_cannot_be_claimed(self):
        queue = mock.Mock()
        queue.queued_providers.side_effect = [{"claude"}, {"claude", "codex"}]
        queue.run_once.return_value = {"job_id": "claude-job", "state": "complete"}
        self._context(queue)
        with mock.patch.object(worker, "readiness_holds", return_value={}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(worker.run("invented-config.json", once=True,
                                            interval=1, worker_id="worker-invented"), 0)
        queue.hold_dependencies.assert_called_once_with(
            {"codex": worker.READINESS_UNAVAILABLE})
        queue.run_once.assert_called_once_with("worker-invented",
                                               allowed_providers={"claude"})

    def test_refused_codex_admission_holds_the_queued_job(self):
        queue = mock.Mock()
        self._context(queue)
        queue.queued_providers.return_value = {"codex"}
        with tempfile.TemporaryDirectory() as home:
            with (mock.patch.object(worker.codex_task, "DEFAULT_CODEX_HOME", Path(home)),
                  mock.patch.object(worker, "_provider_executable", return_value="/path/codex"),
                  mock.patch.object(worker.preflight, "assert_peer_home_has_no_config"),
                  mock.patch.object(worker.codex_promotion, "admit", side_effect=
                                    worker.codex_promotion.AdmissionRefused("maintenance")),
                  mock.patch.object(worker.health, "auth_status") as auth):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(worker.run("invented-config.json", once=True,
                                                interval=1, worker_id="worker-invented"), 0)
        auth.assert_not_called()
        queue.hold_dependencies.assert_called_once_with(
            {"codex": worker.READINESS_UNAVAILABLE})
        queue.run_once.assert_not_called()


class ReadinessMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex-home"
        self.home.mkdir()

    def test_claude_business_subscription_is_ready(self):
        cfg = SimpleNamespace(claude_config_dir=Path("/invented/claude-home"))
        report = {"state": "signed_in", "authMethod": "claude.ai",
                  "subscriptionType": "business"}
        with (mock.patch.object(worker.claude_config, "is_ready", return_value=True),
              mock.patch.object(worker, "_provider_executable", return_value="/fake/claude"),
              mock.patch.object(worker.health, "auth_status", return_value=report)):
            self.assertTrue(worker._claude_ready(cfg))

    def test_codex_refused_admission_does_not_probe_cli(self):
        with (mock.patch.object(worker.codex_task, "DEFAULT_CODEX_HOME", self.home),
              mock.patch.object(worker, "_provider_executable", return_value="/path/codex"),
              mock.patch.object(worker.preflight, "assert_peer_home_has_no_config"),
              mock.patch.object(worker.codex_promotion, "admit",
                                side_effect=worker.codex_promotion.AdmissionRefused("gate")),
              mock.patch.object(worker.health, "auth_status") as auth):
            self.assertFalse(worker._codex_ready())
        auth.assert_not_called()

    def test_codex_probe_uses_admitted_executable(self):
        admission = worker.codex_promotion.Admission("no_record", Path("/admitted/codex"))
        report = {"state": "signed_in", "authMethod": "chatgpt"}
        with (mock.patch.object(worker.codex_task, "DEFAULT_CODEX_HOME", self.home),
              mock.patch.object(worker, "_provider_executable", return_value="/path/codex"),
              mock.patch.object(worker.preflight, "assert_peer_home_has_no_config"),
              mock.patch.object(worker.codex_promotion, "admit",
                                return_value=contextlib.nullcontext(admission)) as admit,
              mock.patch.object(worker.health, "auth_status", return_value=report) as auth):
            self.assertTrue(worker._codex_ready())
        admit.assert_called_once_with(Path("/path/codex"), node_bin=mock.ANY)
        auth.assert_called_once_with(
            "codex", "/admitted/codex",
            {**worker.codex_task._env(), "CODEX_HOME": str(self.home)})


class MainMutationTests(unittest.TestCase):
    def test_main_returns_run_status(self):
        with mock.patch.object(worker, "run", return_value=7) as run:
            self.assertEqual(worker.main(["--config", "invented-config.json", "--once"]), 7)
        run.assert_called_once_with("invented-config.json", once=True,
                                    interval=5.0, worker_id=None)

    def test_main_returns_error_status(self):
        with mock.patch.object(worker, "run", side_effect=ExecutionAdmissionError("invalid")), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(worker.main(["--config", "invented-config.json"]), 2)
        self.assertEqual(stderr.getvalue(), "invalid\n")


if __name__ == "__main__":
    unittest.main()
