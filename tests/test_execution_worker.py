"""Tests for the out-of-sandbox execution queue consumer."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.orchestration import config as orchestration_config
from agent_bridge.orchestration.execution_queue import (
    ExecutionAdmissionError, SubprocessHarnessExecutor)
from agent_bridge.orchestration.execution_worker import (
    QueueNotPrivate, WorkerAlreadyRunning, WorkerLock, run, select_executor)
import platform_support


class ExecutionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _assert_owner_only(self, path: Path, mode: int) -> None:
        """Mode bits on POSIX, the platform ACL check on Windows.

        The guarantee is the same on both systems; only the way to observe it
        differs. Skipping the assertion on Windows would leave the queue root's
        permissions untested on the one platform this worker is being built
        for.
        """

        platform_support.assert_owner_only(self, path, mode)

    def test_queue_lock_is_exclusive_and_secure(self):
        queue = self.root / "queue"
        with WorkerLock(queue):
            self._assert_owner_only(queue, 0o700)
            self._assert_owner_only(queue / ".execution-worker.lock", 0o600)
            with self.assertRaisesRegex(WorkerAlreadyRunning, "already_running"):
                with WorkerLock(queue):
                    pass
        with WorkerLock(queue):
            pass

    def test_once_mode_with_empty_queue_exits_cleanly(self):
        executable = self.root / "python"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        codex = self.root / "codex_task.py"
        claude = self.root / "claude_task.py"
        codex.write_text("# fake\n", encoding="utf-8")
        claude.write_text("# fake\n", encoding="utf-8")
        state = self.root / "state"
        config = self.root / "orchestration.json"
        config.write_text(json.dumps({
            "config_version": "1", "state_root": str(state),
            "local_queue_root": str(state / "local"),
            "capacity_db": str(state / "capacity.sqlite3"),
            "worker_executable": str(executable),
            "worker_state": str(state / "worker-state"),
            "execution_queue_root": str(state / "execution"),
            "codex_task_executable": str(codex),
            "claude_task_executable": str(claude),
            "python_executable": str(executable),
            # A Windows host selects the WSL executor, which needs its paths
            # configured even to refuse by name; the paths need not exist
            # for an empty queue to be drained.
            **(WINDOWS_PATHS if os.name == "nt" else {}),
        }), encoding="utf-8")
        self.assertEqual(run(str(config), once=True, interval=0.01,
                             worker_id="test-worker"), 0)
        self._assert_owner_only(state / "execution", 0o700)


WINDOWS_PATHS = {
    "windows_wsl_runtime_root": "C:\\Users\\sam\\AgentBridge\\wsl",
    "windows_wsl_rootfs_path": "C:\\Users\\sam\\AgentBridge\\rootfs.tar",
    "windows_wsl_manifest_path": "C:\\Users\\sam\\AgentBridge\\manifest.json",
}


class ExecutorSelectionTests(unittest.TestCase):
    """The dispatch seam must pick the executor the platform actually has."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _config(self, **extra):
        executable = self.root / "python"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        for name in ("codex_task.py", "claude_task.py"):
            (self.root / name).write_text("# fake\n", encoding="utf-8")
        state = self.root / "state"
        path = self.root / "orchestration.json"
        payload = {
            "config_version": "1", "state_root": str(state),
            "local_queue_root": str(state / "local"),
            "capacity_db": str(state / "capacity.sqlite3"),
            "worker_executable": str(executable),
            "worker_state": str(state / "worker-state"),
            "execution_queue_root": str(state / "execution"),
            "codex_task_executable": str(self.root / "codex_task.py"),
            "claude_task_executable": str(self.root / "claude_task.py"),
            "python_executable": str(executable),
        }
        payload.update(extra)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return orchestration_config.load(path)

    def test_posix_gets_the_harness_executor(self):
        executor = select_executor(self._config(), os_name="posix")
        self.assertIsInstance(executor, SubprocessHarnessExecutor)

    def test_windows_gets_the_wsl_executor_not_the_posix_harness(self):
        from agent_bridge.orchestration.windows_delegation import WindowsWslExecutor
        executor = select_executor(self._config(**WINDOWS_PATHS), os_name="nt")
        self.assertIsInstance(executor, WindowsWslExecutor)

    def test_windows_without_delegation_paths_refuses_at_selection(self):
        with self.assertRaisesRegex(ExecutionAdmissionError,
                                    "windows_wsl_configuration_missing"):
            select_executor(self._config(), os_name="nt")

    def test_the_windows_executor_refuses_before_it_spawns(self):
        executor = select_executor(self._config(**WINDOWS_PATHS), os_name="nt")
        outcome = executor({"provider": "codex", "classification": "public"},
                           self.root)
        self.assertFalse(outcome["spawned"])
        self.assertEqual(outcome["reason"], "refused")
        self.assertNotEqual(outcome["returncode"], 0)

    def test_an_unverified_windows_host_says_which_check_refused(self):
        """No verification record, so the manifest read is what fails first."""
        executor = select_executor(self._config(**WINDOWS_PATHS), os_name="nt")
        executor.platform_name = "win32"
        outcome = executor({"provider": "codex", "classification": "public"},
                           self.root)
        self.assertEqual(outcome["detail"],
                         "provisioning_unverified:manifest_unreadable")

    def test_the_worker_never_assumes_a_windows_host_is_provisioned(self):
        executor = select_executor(self._config(**WINDOWS_PATHS), os_name="nt")
        self.assertIsNone(executor.provision_state)
        self.assertFalse(executor.provider_lane.enabled_for("claude"))

    def test_the_windows_executor_carries_the_configured_pinned_paths(self):
        executor = select_executor(self._config(**WINDOWS_PATHS), os_name="nt")
        self.assertEqual(executor.config.rootfs_path,
                         WINDOWS_PATHS["windows_wsl_rootfs_path"])
        self.assertIsNone(executor.config.sidecar_path)


class WindowsConfigurationTests(unittest.TestCase):
    """Windows delegation paths are all-or-none and validated off Windows."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _load(self, **extra):
        state = self.root / "state"
        path = self.root / "orchestration.json"
        payload = {
            "config_version": "1", "state_root": str(state),
            "local_queue_root": str(state / "local"),
            "capacity_db": str(state / "capacity.sqlite3"),
            "worker_executable": str(self.root / "python"),
            "worker_state": str(state / "worker-state"),
        }
        payload.update(extra)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return orchestration_config.load(path)

    def test_omitting_every_windows_path_is_allowed(self):
        self.assertIsNone(self._load().windows_wsl_runtime_root)

    def test_a_partial_windows_configuration_is_refused_at_load_time(self):
        with self.assertRaisesRegex(orchestration_config.OrchestrationConfigError,
                                    "windows_wsl_configuration_incomplete"):
            self._load(windows_wsl_runtime_root=WINDOWS_PATHS["windows_wsl_runtime_root"])

    def test_a_sidecar_without_the_rest_is_refused(self):
        with self.assertRaisesRegex(orchestration_config.OrchestrationConfigError,
                                    "windows_wsl_configuration_incomplete"):
            self._load(windows_wsl_sidecar_path="C:\\a\\sidecar.json")

    def test_a_complete_windows_configuration_loads_on_this_platform(self):
        cfg = self._load(**WINDOWS_PATHS)
        self.assertEqual(cfg.windows_wsl_manifest_path,
                         WINDOWS_PATHS["windows_wsl_manifest_path"])

    def test_the_paths_stay_strings_so_they_are_not_mangled_off_windows(self):
        cfg = self._load(**WINDOWS_PATHS)
        self.assertIsInstance(cfg.windows_wsl_rootfs_path, str)

    def test_a_unc_path_is_refused(self):
        with self.assertRaises(orchestration_config.OrchestrationConfigError):
            self._load(**{**WINDOWS_PATHS,
                          "windows_wsl_rootfs_path": "\\\\server\\share\\rootfs.tar"})

    def test_a_relative_path_is_refused(self):
        with self.assertRaises(orchestration_config.OrchestrationConfigError):
            self._load(**{**WINDOWS_PATHS, "windows_wsl_rootfs_path": "rootfs.tar"})

    def test_a_traversal_path_is_refused(self):
        with self.assertRaises(orchestration_config.OrchestrationConfigError):
            self._load(**{**WINDOWS_PATHS,
                          "windows_wsl_rootfs_path": "C:\\a\\..\\rootfs.tar"})

    def test_an_unknown_key_is_still_refused(self):
        with self.assertRaises(orchestration_config.OrchestrationConfigError):
            self._load(windows_wsl_extra_path="C:\\a")



class QueuePrivacyTests(unittest.TestCase):
    """A queue whose privacy cannot be established must stop the worker."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    class _Platform:
        """Only what WorkerLock uses, so the seam is the whole surface."""

        def __init__(self, *, enforce=None, verify=None):
            self._enforce = enforce
            self._verify = verify

        def enforce_owner_only_file(self, fd):
            if self._enforce is not None:
                raise self._enforce

        def verify_owner_only_path(self, directory, probe_file):
            if self._verify is not None:
                return self._verify
            return True, {}

        def lock_exclusive(self, fd, lock_path, timeout):
            import contextlib

            @contextlib.contextmanager
            def held():
                yield

            return held()

    def _lock(self, platform):
        return WorkerLock(self.root / "queue", platform=platform)

    def test_a_platform_that_cannot_enforce_acls_stops_the_worker(self):
        platform = self._Platform(enforce=NotImplementedError("no acls"))
        with self.assertRaisesRegex(QueueNotPrivate,
                                    "queue_root_acl_unenforceable"):
            with self._lock(platform):
                pass

    def test_a_platform_with_no_verification_call_stops_the_worker(self):
        class Bare:
            def enforce_owner_only_file(self, fd):
                return None

            def lock_exclusive(self, fd, lock_path, timeout):
                raise AssertionError("must refuse before taking the lock")

        with self.assertRaisesRegex(QueueNotPrivate,
                                    "queue_root_acl_unenforceable"):
            with self._lock(Bare()):
                pass

    def test_a_queue_root_that_reads_back_wrong_stops_the_worker(self):
        platform = self._Platform(verify=(False, {"directory": "evidence"}))
        with self.assertRaisesRegex(QueueNotPrivate, "queue_root_not_owner_only"):
            with self._lock(platform):
                pass

    def test_the_refusal_carries_no_pathname(self):
        platform = self._Platform(verify=(False, {"directory": str(self.root)}))
        try:
            with self._lock(platform):
                pass
        except QueueNotPrivate as exc:
            self.assertNotIn(str(self.root), str(exc))
        else:
            self.fail("expected QueueNotPrivate")

    def test_a_verified_queue_root_proceeds(self):
        with self._lock(self._Platform()):
            pass



if __name__ == "__main__":
    unittest.main()
