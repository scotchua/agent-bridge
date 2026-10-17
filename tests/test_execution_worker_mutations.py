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
                              capacity_db=Path("invented-capacity.sqlite3"),
                              client_derived_routes=frozenset())
        with mock.patch.object(worker, "load", return_value=cfg), \
             mock.patch.object(worker, "select_executor", return_value=object()), \
             mock.patch.object(worker.autoroute, "model_reserved_for", return_value=lambda _: False), \
             mock.patch.object(worker.revision, "record"), \
             mock.patch.object(worker, "StageRouter") as router_type, \
             mock.patch.object(worker, "ExecutionQueue") as queue_type:
            self.assertIs(worker._configured_queue("invented-config.json"),
                          queue_type.return_value)
        self.assertTrue(queue_type.call_args.kwargs["recover_interrupted"])
        # The configured worker always rechecks the live stage binding.
        router_type.assert_called_once_with(cfg.capacity_db)
        self.assertIs(queue_type.call_args.kwargs["stage_router"], router_type.return_value)
        self.assertTrue(queue_type.call_args.kwargs["require_stage_binding"])


class RunMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _context(self, queue, changed=(), status=None, queue_root=None):
        cfg = SimpleNamespace(execution_queue_root=queue_root or Path("invented-queue"))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(worker, "load", return_value=cfg))
        stack.enter_context(mock.patch.object(worker, "WorkerLock", return_value=contextlib.nullcontext()))
        stack.enter_context(mock.patch.object(worker.store, "file_lock", return_value=contextlib.nullcontext()))
        stack.enter_context(mock.patch.object(worker, "_configured_queue", return_value=queue))
        quick = stack.enter_context(mock.patch.object(
            drift, "quick_check", return_value=status or {
                "changed": list(changed), "checked_at": 100.0,
                "cache_age_seconds": 0.0}))
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
        quick.assert_called_once_with("invented-config.json", force_refresh=False)
        attention.assert_called_once()
        queue.hold_dependencies.assert_called_once_with(
            {"codex": "codex", "claude": "claude"}, drift_status={
                "changed": ["codex", "claude"], "checked_at": 100.0,
                "cache_age_seconds": 0.0})
        queue.run_once.assert_called_once_with("worker-invented")
        self.assertEqual(signals.call_count, 4)

    def test_refresh_rechecks_and_clears_a_hold_without_dispatching(self):
        queue = mock.Mock()
        status = {"changed": [], "checked_at": 200.0, "cache_age_seconds": 0.0}
        quick, attention, _ = self._context(queue, status=status)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = worker.run("invented-config.json", once=False, interval=1,
                                worker_id="worker-invented", refresh=True)
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "drift_refresh_complete\n")
        quick.assert_called_once_with("invented-config.json", force_refresh=True)
        attention.assert_called_once_with(None, status)
        queue.hold_dependencies.assert_called_once_with({}, drift_status=status)
        queue.run_once.assert_not_called()

    def test_refresh_requests_running_worker_without_dispatching(self):
        queue = mock.Mock()
        queue_root = self.root / "queue"
        queue_root.mkdir(mode=0o700)
        real_lock = worker.WorkerLock
        self._context(queue, queue_root=queue_root)
        output = io.StringIO()
        with real_lock(queue_root), \
             mock.patch.object(worker, "WorkerLock", real_lock), \
             contextlib.redirect_stdout(output):
            result = worker.run("invented-config.json", once=False, interval=1,
                                refresh=True)
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "drift_refresh_requested\n")
        marker = queue_root / worker.REFRESH_REQUEST_NAME
        self.assertTrue(marker.is_file())
        self.assertEqual(os.stat(marker).st_mode & 0o777, 0o600)
        queue.run_once.assert_not_called()

    def test_marker_forces_refresh_and_clears_hold_in_one_worker_pass(self):
        queue = mock.Mock()
        queue.run_once.return_value = None
        queue_root = self.root / "queue"
        queue_root.mkdir(mode=0o700)
        marker = queue_root / worker.REFRESH_REQUEST_NAME
        marker.write_bytes(b"")
        marker.chmod(0o600)
        status = {"changed": [], "checked_at": 200.0, "cache_age_seconds": 0.0}
        quick, _, _ = self._context(queue, status=status, queue_root=queue_root)
        self.assertEqual(worker.run("invented-config.json", once=True,
                                    interval=1, worker_id="worker-invented"), 0)
        quick.assert_called_once_with("invented-config.json", force_refresh=True)
        queue.hold_dependencies.assert_called_once_with({}, drift_status=status)
        self.assertFalse(marker.exists())

    def test_symlinked_refresh_marker_is_not_followed(self):
        queue_root = self.root / "queue"
        queue_root.mkdir(mode=0o700)
        target = self.root / "target"
        target.write_text("do not read", encoding="utf-8")
        marker = queue_root / worker.REFRESH_REQUEST_NAME
        marker.symlink_to(target)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertFalse(worker._consume_drift_refresh_request(queue_root))
        self.assertEqual(stderr.getvalue(), worker.REFRESH_REQUEST_INVALID + "\n")
        self.assertFalse(marker.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "do not read")

    def test_foreign_owned_refresh_marker_is_removed_without_use(self):
        queue_root = self.root / "queue"
        queue_root.mkdir(mode=0o700)
        marker = queue_root / worker.REFRESH_REQUEST_NAME
        marker.write_bytes(b"")
        with mock.patch.object(worker, "require_private_file",
                               side_effect=worker.PrivacyError(
                                   "file_owned_by_another_account")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(worker._consume_drift_refresh_request(queue_root))
        self.assertFalse(marker.exists())

    def test_stop_signal_ends_loop_and_restores_handlers(self):
        queue = mock.Mock()
        _, _, signals = self._context(queue)

        def run_once(_identity):
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


class MainMutationTests(unittest.TestCase):
    def test_main_returns_run_status(self):
        with mock.patch.object(worker, "run", return_value=7) as run:
            self.assertEqual(worker.main(["--config", "invented-config.json", "--once"]), 7)
        run.assert_called_once_with("invented-config.json", once=True,
                                    interval=5.0, worker_id=None, refresh=False)

    def test_main_returns_error_status(self):
        with mock.patch.object(worker, "run", side_effect=ExecutionAdmissionError("invalid")), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(worker.main(["--config", "invented-config.json"]), 2)
        self.assertEqual(stderr.getvalue(), "invalid\n")


if __name__ == "__main__":
    unittest.main()
