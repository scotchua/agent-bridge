"""Offline regression tests for mutation gaps in the WSL runtime."""

import contextlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_wsl_runtime as wr  # noqa: E402
from test_windows_wsl_runtime import (  # noqa: E402
    DISTRO, FakeOps, GUEST_RUNNER_SHA256, MANIFEST, ROOTFS_BYTES,
    ROOTFS_SHA256, RUNTIME_ROOT, SOURCE_PATH, _run,
)


class RuntimeValueMutationTests(unittest.TestCase):
    def test_runtime_error_preserves_safe_message(self):
        error = wr.WindowsWslRuntimeError("copy_failed", "synthetic detail")
        self.assertEqual(str(error), "copy_failed: synthetic_detail")

    def test_runtime_result_ok_tracks_status(self):
        self.assertTrue(wr.RuntimeResult(status=wr.STATUS_COMPLETED, reason=wr.REASON_OK).ok)
        self.assertFalse(wr.RuntimeResult(status=wr.STATUS_ABORTED, reason="synthetic").ok)

    def test_lock_path_names_lock_file(self):
        self.assertEqual(wr.lock_path(RUNTIME_ROOT), RUNTIME_ROOT + "\\wsl-runtime.lock")

    def test_copy_rootfs_accepts_exact_size_cap(self):
        ops = FakeOps()
        destination = RUNTIME_ROOT + "\\exact.tar"
        copied = wr._copy_rootfs(ops, SOURCE_PATH, destination,
                                 max_bytes=len(ROOTFS_BYTES), expected_sha256=ROOTFS_SHA256)
        self.assertEqual(copied, len(ROOTFS_BYTES))
        self.assertEqual(ops.nodes[ops._key(destination)].data, ROOTFS_BYTES)

    def test_run_canaries_records_each_timing(self):
        timings = {}
        wr._run_canaries(FakeOps(), DISTRO, MANIFEST, GUEST_RUNNER_SHA256,
                         wr.DEFAULT_LIMITS, timings)
        self.assertEqual(set(timings), {wr.canary_timing_key(name) for name in wr.CANARY_ORDER})
        self.assertTrue(all(value >= 0 for value in timings.values()))

    def test_build_receipt_rounds_timings_to_six_places(self):
        receipt = wr.build_receipt(
            distro_name=DISTRO, status=wr.STATUS_COMPLETED, reason=wr.REASON_OK,
            detail="", exit_code=0, canaries_passed=True, spawned=True,
            timings={"synthetic_seconds": 1.12345678}, cleanup=wr.CleanupReport(),
            finished_at="2026-02-01T00:00:00+00:00",
        )
        self.assertEqual(receipt["timings"], {"synthetic_seconds": 1.123457})

    def test_run_job_accepts_input_at_exact_byte_cap(self):
        limits = wr.RuntimeLimits(**{**wr.DEFAULT_LIMITS.__dict__, "input_max_bytes": 5})
        result = _run(FakeOps(), stdin_data="hello", limits=limits)
        self.assertEqual(result.status, wr.STATUS_COMPLETED)

    def test_reap_rejects_zero_max_age(self):
        report = wr.reap_stale_instances(
            runtime_root=RUNTIME_ROOT, max_age=timedelta(0),
            ops=FakeOps(), platform_name="win32",
            now=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(report.status, wr.STATUS_ABORTED)
        self.assertIn("max_age_invalid", report.detail)


class HostOpsMutationTests(unittest.TestCase):
    def setUp(self):
        self.ops = wr.HostOps()

    def test_monotonic(self):
        with mock.patch("time.monotonic", return_value=12.5):
            self.assertEqual(self.ops.monotonic(), 12.5)

    def test_now_utc(self):
        moment = datetime(2026, 2, 1, tzinfo=timezone.utc)
        with mock.patch.object(wr, "datetime") as clock:
            clock.now.return_value = moment
            clock.now.assert_not_called()
            self.assertEqual(self.ops.now_utc(), moment)
            clock.now.assert_called_once_with(timezone.utc)

    def test_lock(self):
        events = []

        @contextlib.contextmanager
        def locked(path, timeout):
            events.append((path, timeout, "enter"))
            yield
            events.append((path, timeout, "exit"))

        with mock.patch.object(wr.store, "file_lock", locked):
            with self.ops.lock("synthetic.lock", 4):
                events.append("body")
        self.assertEqual(events, [("synthetic.lock", 4, "enter"), "body",
                                  ("synthetic.lock", 4, "exit")])

    def test_lstat(self):
        sentinel = object()
        with mock.patch.object(wr.os, "lstat", return_value=sentinel) as call:
            self.assertIs(self.ops.lstat("synthetic"), sentinel)
            call.assert_called_once_with("synthetic")

    def test_listdir(self):
        with mock.patch.object(wr.os, "listdir", return_value=["synthetic"]) as call:
            self.assertEqual(self.ops.listdir("root"), ["synthetic"])
            call.assert_called_once_with("root")

    def test_secure_mkdir(self):
        with mock.patch.object(wr.store, "secure_mkdir") as call:
            self.ops.secure_mkdir("synthetic")
            call.assert_called_once_with("synthetic")

    def test_verify_owner_only(self):
        verdict = (True, {"synthetic": True})
        with mock.patch.object(wr.host_platform, "verify_owner_only_path",
                               return_value=verdict) as call:
            self.assertEqual(self.ops.verify_owner_only("root", "probe"), verdict)
            call.assert_called_once_with("root", "probe")

    def test_open_read(self):
        with mock.patch.object(wr.os, "open", return_value=23) as call:
            self.assertEqual(self.ops.open_read("synthetic"), 23)
            call.assert_called_once_with("synthetic", os.O_RDONLY | getattr(os, "O_BINARY", 0))

    def test_open_exclusive_write(self):
        with mock.patch.object(wr.os, "open", return_value=24) as call:
            self.assertEqual(self.ops.open_exclusive_write("synthetic"), 24)
            call.assert_called_once_with(
                "synthetic", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                wr.store.FILE_MODE,
            )

    def test_fstat(self):
        sentinel = object()
        with mock.patch.object(wr.os, "fstat", return_value=sentinel) as call:
            self.assertIs(self.ops.fstat(23), sentinel)
            call.assert_called_once_with(23)

    def test_read(self):
        with mock.patch.object(wr.os, "read", return_value=b"sample") as call:
            self.assertEqual(self.ops.read(23, 6), b"sample")
            call.assert_called_once_with(23, 6)

    def test_write(self):
        with mock.patch.object(wr.os, "write", return_value=6) as call:
            self.assertEqual(self.ops.write(23, b"sample"), 6)
            call.assert_called_once_with(23, b"sample")

    def test_fsync(self):
        with mock.patch.object(wr.os, "fsync") as call:
            self.ops.fsync(23)
            call.assert_called_once_with(23)

    def test_close(self):
        with mock.patch.object(wr.os, "close") as call:
            self.ops.close(23)
            call.assert_called_once_with(23)

    def test_unlink(self):
        with mock.patch.object(wr.os, "unlink") as call:
            self.ops.unlink("synthetic")
            call.assert_called_once_with("synthetic")

    def test_remove_tree(self):
        with mock.patch("shutil.rmtree") as call:
            self.ops.remove_tree("synthetic")
            call.assert_called_once_with("synthetic")

    def test_exists(self):
        with mock.patch.object(wr.os.path, "lexists", return_value=True) as call:
            self.assertTrue(self.ops.exists("synthetic"))
            call.assert_called_once_with("synthetic")

    def test_write_json(self):
        with mock.patch.object(wr.store, "atomic_write_json") as call:
            self.ops.write_json("synthetic.json", {"value": 1})
            call.assert_called_once_with("synthetic.json", {"value": 1})

    def test_read_json(self):
        with mock.patch.object(wr.store, "read_json_atomic", return_value={"value": 1}) as call:
            self.assertEqual(self.ops.read_json("synthetic.json"), {"value": 1})
            call.assert_called_once_with("synthetic.json")

    def test_run_host(self):
        sentinel = object()
        with mock.patch.object(wr.runner, "scrubbed_env", return_value={"A": "B"}), \
             mock.patch.object(wr.runner, "run", return_value=sentinel) as call:
            result = self.ops.run_host(["synthetic"], cwd="C:\\Windows", timeout=3,
                                       grace=1, stdout_cap=8, stderr_cap=8)
        self.assertIs(result, sentinel)
        call.assert_called_once_with(
            ["synthetic"], cwd="C:\\Windows", env={"A": "B"}, stdin_data="",
            timeout=3, grace=1, stdout_cap=8, stderr_cap=8,
        )

    def test_host_cwd(self):
        with mock.patch.dict(os.environ, {"SystemRoot": "C:\\Synthetic"}, clear=True):
            self.assertEqual(self.ops.host_cwd(), "C:\\Synthetic")

    def test_process_liveness(self):
        evidence = {"alive": False, "wait_result": 0}
        with mock.patch.object(wr.host_platform, "process_liveness", return_value=evidence) as call:
            self.assertIs(self.ops.process_liveness(123), evidence)
            call.assert_called_once_with(123)

    def test_process_identity(self):
        with mock.patch.object(wr.host_platform, "process_identity", return_value="synthetic") as call:
            self.assertEqual(self.ops.process_identity(123), "synthetic")
            call.assert_called_once_with(123)

    def test_current_pid(self):
        with mock.patch.object(wr.os, "getpid", return_value=123) as call:
            self.assertEqual(self.ops.current_pid(), 123)
            call.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
