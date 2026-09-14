"""Tests for the bounded ephemeral WSL2 runtime.

Everything here is mocked and portable: an in-memory Windows-shaped
filesystem, a scripted host-command responder, and a deterministic clock.
Nothing spawns a process, touches a real path, or requires WSL, so these
run identically on macOS, Linux, and Windows.

Passing these tests is evidence about this module's control flow only. It
is not evidence that WSL delegation works on a live Windows host; no such
validation has been performed.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import stat as stat_module
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import runner  # noqa: E402
from agent_bridge.orchestration import windows_wsl as ww  # noqa: E402
from agent_bridge.orchestration import windows_wsl_runtime as wr  # noqa: E402


RUNTIME_ROOT = "C:\\Users\\tester\\.agent-bridge\\wsl"
SOURCE_PATH = "C:\\Users\\tester\\images\\pinned.tar"
TOKEN = "abc123"
DISTRO = ww.DISTRO_NAME_PREFIX + TOKEN
JOB_DIR = wr.job_directory(RUNTIME_ROOT, DISTRO)
INSTALL_DIR = JOB_DIR + "\\distro"
ROOTFS_COPY = JOB_DIR + "\\rootfs.tar"
RECORD_PATH = wr.owner_record_path(RUNTIME_ROOT, DISTRO)
RECEIPT_PATH = wr.receipt_path(RUNTIME_ROOT, DISTRO)

ROOTFS_BYTES = b"pinned-rootfs-bytes" * 16
ROOTFS_SHA256 = hashlib.sha256(ROOTFS_BYTES).hexdigest()
GUEST_RUNNER_SHA256 = "b" * 64

MANIFEST = ww.parse_manifest({
    "schema_version": 1,
    "distro_release": "22.04.3",
    "rootfs_sha256": ROOTFS_SHA256,
    "node_version": "20.11.1",
    "claude_version": "1.2.3",
    "codex_version": "0.9.0",
})

JOB_SPEC = {
    "command": [ww.GUEST_RUNNER_PATH, "--run"],
    "workdir": "/workspace/job",
    "env": {"HOME": "/root"},
}


def _ancestor_chain(path):
    return wr._ancestors(path)


class FakeStat:
    """Just the fields the runtime reads, including the Windows-only one."""

    def __init__(self, mode, dev=7, ino=1, size=0, mtime_ns=1000, attributes=0):
        self.st_mode = mode
        self.st_dev = dev
        self.st_ino = ino
        self.st_size = size
        self.st_mtime_ns = mtime_ns
        self.st_file_attributes = attributes


class _Node:
    def __init__(self, kind, data=b"", ino=1, dev=7, mtime_ns=1000, reparse=False):
        self.kind = kind
        self.data = data
        self.ino = ino
        self.dev = dev
        self.mtime_ns = mtime_ns
        self.reparse = reparse

    def stat(self):
        mode = stat_module.S_IFDIR if self.kind == "dir" else stat_module.S_IFREG
        return FakeStat(
            mode | 0o600, dev=self.dev, ino=self.ino, size=len(self.data),
            mtime_ns=self.mtime_ns,
            attributes=wr.FILE_ATTRIBUTE_REPARSE_POINT if self.reparse else 0,
        )


# Windows process_liveness evidence shapes, as platform/windows.py emits them.
EXITED = {"probe": "fake", "alive": False, "wait_result": 0}
RUNNING = {"probe": "fake", "alive": True, "wait_result": 258}
PID_GONE = {"probe": "fake", "alive": False, "open_process_error": 87}
ACCESS_DENIED = {"probe": "fake", "alive": False, "open_process_error": 5}
WAIT_FAILED = {"probe": "fake", "alive": False, "liveness_indeterminate": True,
               "wait_error": 6}
UNEXPECTED_WAIT = {"probe": "fake", "alive": False, "unexpected_wait_result": True,
                   "wait_result": 12345}


def _ok(argv, stdout=b"", returncode=0, **overrides):
    fields = dict(
        argv=list(argv), returncode=returncode, stdout=stdout, stderr=b"",
        timed_out=False, duration_seconds=0.0, pgid=None,
    )
    fields.update(overrides)
    return runner.RunResult(**fields)


class FakeOps(wr.HostOps):
    """In-memory Windows-shaped host, with injectable faults."""

    def __init__(self):
        self.nodes: dict[str, _Node] = {}
        self.json: dict[str, dict] = {}
        self.events: list[tuple] = []
        self.fds: dict[int, dict] = {}
        self._next_fd = 100
        self._next_ino = 10
        self._clock = 0.0
        self.lock_calls = 0
        self.lock_error: BaseException | None = None

        self.acl_verified = True
        self.acl_failures: set[str] = set()
        self.import_result_override = None
        # A real WSL registry: --import adds the name, --unregister removes
        # it, --list reports it. The runtime's pre/post ownership proof is
        # only meaningful against a host that actually tracks the name.
        self.registered: set[str] = set()
        self.list_result_override = None
        self.list_results: list = []
        self.import_registers = True
        self.unregister_removes = True
        self.lock_depth = 0
        self.exclusive_lock = False
        self.job_result_override = None
        self.canary_overrides: dict[str, bytes] = {}
        self.command_errors: dict[str, BaseException] = {}
        self.command_returncodes: dict[str, int] = {}
        self.fs_errors: dict[str, BaseException] = {}
        self.stat_errors: dict[str, BaseException] = {}
        self.json_write_errors: dict[str, BaseException] = {}
        self.read_json_queue: dict[str, list] = {}
        self.drift_after_open = False
        self.drift_during_copy = False
        self.grow_during_copy = False
        self.liveness: dict[int, dict] = {}
        self.liveness_errors: dict[int, BaseException] = {}
        self.identities: dict[int, str] = {}
        self.identity_errors: dict[int, BaseException] = {}
        self.pid = 4242

        self._mkdir("C:\\")
        self._mkdir("C:\\Users")
        self._mkdir("C:\\Users\\tester")
        self._mkdir("C:\\Users\\tester\\images")
        self._mkdir(RUNTIME_ROOT)
        self.write_file(SOURCE_PATH, ROOTFS_BYTES)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _key(path):
        return path.replace("/", "\\").casefold()

    def _ensure_parents(self, path):
        # Mirror a real volume: a component cannot exist unless its parent
        # does. A fake that allows gaps would hide the ancestor walk entirely.
        for component in _ancestor_chain(path)[:-1]:
            key = self._key(component)
            if key not in self.nodes:
                self._next_ino += 1
                self.nodes[key] = _Node("dir", ino=self._next_ino)

    def _mkdir(self, path):
        self._ensure_parents(path)
        self._next_ino += 1
        self.nodes.setdefault(self._key(path), _Node("dir", ino=self._next_ino))

    def write_file(self, path, data):
        self._ensure_parents(path)
        self._next_ino += 1
        self.nodes[self._key(path)] = _Node("file", data=data, ino=self._next_ino)

    def make_reparse(self, path):
        self._ensure_parents(path)
        self._next_ino += 1
        self.nodes[self._key(path)] = _Node("dir", ino=self._next_ino, reparse=True)

    def _maybe_raise(self, name):
        error = self.fs_errors.get(name)
        if error is not None:
            raise error

    # -- clocks -------------------------------------------------------------

    def monotonic(self):
        self._clock += 0.25
        return self._clock

    def now_utc(self):
        return datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    # -- locking ------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, path, timeout):
        if self.lock_error is not None:
            raise self.lock_error
        if self.exclusive_lock and self.lock_depth:
            # What a real exclusive per-user lock does to a second holder.
            raise TimeoutError("lock is already held")
        self.lock_calls += 1
        self.lock_depth += 1
        self.events.append(("lock", path))
        try:
            yield
        finally:
            self.lock_depth -= 1
        self.events.append(("unlock", path))

    # -- filesystem ---------------------------------------------------------

    def lstat(self, path):
        error = self.stat_errors.get(path)
        if error is not None:
            raise error
        node = self.nodes.get(self._key(path))
        if node is None:
            raise FileNotFoundError(path)
        return node.stat()

    def listdir(self, path):
        prefix = self._key(path) + "\\"
        names = set()
        for key in self.nodes:
            if key.startswith(prefix):
                names.add(key[len(prefix):].split("\\")[0])
        for key in self.json:
            if self._key(key).startswith(prefix):
                names.add(self._key(key)[len(prefix):].split("\\")[0])
        if self._key(path) not in self.nodes and not names:
            raise FileNotFoundError(path)
        return sorted(names)

    def secure_mkdir(self, path):
        self._maybe_raise("secure_mkdir")
        self.events.append(("mkdir", path))
        self._mkdir(path)

    def verify_owner_only(self, directory, probe_file):
        self.events.append(("verify_acl", directory))
        if not self.acl_verified:
            return False, {"mechanism": "fake"}
        failed = {self._key(path) for path in self.acl_failures}
        return self._key(directory) not in failed, {"mechanism": "fake"}

    def verified_dirs(self):
        return [self._key(e[1]) for e in self.events if e[0] == "verify_acl"]

    def open_read(self, path):
        self._maybe_raise("open_read")
        node = self.nodes.get(self._key(path))
        if node is None:
            raise FileNotFoundError(path)
        fd = self._next_fd
        self._next_fd += 1
        self.fds[fd] = {"path": path, "pos": 0, "read": True}
        return fd

    def open_exclusive_write(self, path):
        self._maybe_raise("open_exclusive_write")
        if self._key(path) in self.nodes:
            raise FileExistsError(path)
        self.write_file(path, b"")
        fd = self._next_fd
        self._next_fd += 1
        self.fds[fd] = {"path": path, "pos": 0, "read": False}
        self.events.append(("create", path))
        return fd

    def fstat(self, fd):
        entry = self.fds[fd]
        node = self.nodes[self._key(entry["path"])]
        if entry["read"] and self.drift_after_open:
            self.drift_after_open = False
            node.ino += 1000
        return node.stat()

    def read(self, fd, size):
        entry = self.fds[fd]
        node = self.nodes[self._key(entry["path"])]
        chunk = node.data[entry["pos"]:entry["pos"] + size]
        entry["pos"] += len(chunk)
        if chunk and self.drift_during_copy:
            self.drift_during_copy = False
            node.mtime_ns += 5
        if chunk and self.grow_during_copy:
            node.data = node.data + b"x" * size
        return chunk

    def write(self, fd, data):
        entry = self.fds[fd]
        node = self.nodes[self._key(entry["path"])]
        node.data += data
        return len(data)

    def fsync(self, fd):
        return None

    def close(self, fd):
        self.fds.pop(fd, None)

    def unlink(self, path):
        self._maybe_raise("unlink")
        key = self._key(path)
        if key in self.nodes:
            del self.nodes[key]
            self.events.append(("unlink", path))
            return
        for stored in list(self.json):
            if self._key(stored) == key:
                del self.json[stored]
                self.events.append(("unlink", path))
                return
        raise FileNotFoundError(path)

    def remove_tree(self, path):
        self._maybe_raise("remove_tree")
        prefix = self._key(path)
        keys = [k for k in self.nodes if k == prefix or k.startswith(prefix + "\\")]
        if not keys:
            raise FileNotFoundError(path)
        for key in keys:
            del self.nodes[key]
        self.events.append(("remove_tree", path))

    def exists(self, path):
        key = self._key(path)
        return key in self.nodes or any(self._key(p) == key for p in self.json)

    def write_json(self, path, payload):
        error = self.json_write_errors.get(path)
        if error is not None:
            raise error
        self.json[path] = dict(payload)
        self.events.append(("write_json", path))

    def read_json(self, path):
        queued = self.read_json_queue.get(path)
        if queued:
            return queued.pop(0)
        if path not in self.json:
            raise FileNotFoundError(path)
        return self.json[path]

    # -- processes ----------------------------------------------------------

    def host_cwd(self):
        return "C:\\Windows"

    def current_pid(self):
        return self.pid

    def process_liveness(self, pid):
        error = self.liveness_errors.get(pid)
        if error is not None:
            raise error
        return self.liveness.get(pid, EXITED)

    def process_identity(self, pid):
        error = self.identity_errors.get(pid)
        if error is not None:
            raise error
        return self.identities.get(pid, "creation-time-1")

    def run_host(self, argv, *, cwd, timeout, grace, stdout_cap, stderr_cap, stdin_data=""):
        argv = list(argv)
        verb = argv[1]
        self.events.append(("run", verb, tuple(argv)))
        error = self.command_errors.get(verb)
        if error is not None:
            raise error
        forced = self.command_returncodes.get(verb)
        if verb == "--import" and self.import_registers:
            # A nonzero exit does not mean nothing was registered. Register
            # first so the forced-failure tests exercise the ambiguous case.
            self.registered.add(argv[2])
        if forced is not None:
            return _ok(argv, returncode=forced)
        if verb == "--list":
            if self.list_results:
                return self.list_results.pop(0)
            if self.list_result_override is not None:
                return self.list_result_override
            listing = "".join(name + "\r\n" for name in sorted(self.registered))
            return _ok(argv, stdout=listing.encode("utf-16-le"))
        if verb == "--import":
            return self.import_result_override or _ok(argv)
        if verb == "--terminate":
            return _ok(argv)
        if verb == "--unregister":
            if self.unregister_removes:
                self.registered.discard(argv[2])
            return _ok(argv)
        if verb == "-d":
            if "--canary" in argv:
                name = argv[argv.index("--canary") + 1]
                expected = wr.canary_expectations(MANIFEST, GUEST_RUNNER_SHA256)[name]
                stdout = self.canary_overrides.get(name, (expected + "\n").encode("utf-8"))
                return _ok(argv, stdout=stdout)
            self.events.append(("job_stdin", stdin_data))
            return self.job_result_override or _ok(argv, stdout=b"job output")
        raise AssertionError(f"unexpected argv {argv}")

    # -- assertions helpers -------------------------------------------------

    def verbs(self):
        return [event[1] for event in self.events if event[0] == "run"]

    def event_names(self):
        return [event[0] for event in self.events]


def _request(**overrides):
    fields = dict(
        runtime_root=RUNTIME_ROOT,
        rootfs_source_path=SOURCE_PATH,
        manifest=MANIFEST,
        guest_runner_sha256=GUEST_RUNNER_SHA256,
        distro_token=TOKEN,
        job_spec=JOB_SPEC,
        stdin_data="hello",
        limits=wr.DEFAULT_LIMITS,
    )
    fields.update(overrides)
    return wr.JobRequest(**fields)


def _run(ops, **overrides):
    return wr.run_job(_request(**overrides), ops=ops, platform_name="win32")


# ---------------------------------------------------------------------------


class PlatformGateTests(unittest.TestCase):
    def test_non_windows_aborts_without_spawning_or_touching_anything(self):
        ops = FakeOps()
        result = wr.run_job(_request(), ops=ops, platform_name="darwin")
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, wr.REASON_UNSUPPORTED_PLATFORM)
        self.assertFalse(result.spawned)
        self.assertEqual(ops.events, [])
        self.assertEqual(ops.lock_calls, 0)
        self.assertFalse(ops.exists(JOB_DIR))
        self.assertEqual(ops.json, {})

    def test_non_windows_reap_does_nothing(self):
        ops = FakeOps()
        report = wr.reap_stale_instances(
            runtime_root=RUNTIME_ROOT, max_age=timedelta(hours=1),
            ops=ops, platform_name="linux")
        self.assertEqual(report.status, wr.REASON_UNSUPPORTED_PLATFORM)
        self.assertEqual(ops.events, [])


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.ops = FakeOps()
        self.result = _run(self.ops)

    def test_completes_with_canaries_and_complete_cleanup(self):
        self.assertEqual(self.result.status, wr.STATUS_COMPLETED, self.result.detail)
        self.assertEqual(self.result.reason, wr.REASON_OK)
        self.assertTrue(self.result.canaries_passed)
        self.assertTrue(self.result.cleanup.complete)
        self.assertEqual(self.result.exit_code, 0)
        self.assertEqual(self.result.stdout, b"job output")

    def test_serialised_under_the_per_user_lock(self):
        self.assertEqual(self.ops.lock_calls, 1)
        names = self.ops.events
        lock_at = names.index(("lock", wr.lock_path(RUNTIME_ROOT)))
        job_mkdir_at = names.index(("mkdir", JOB_DIR))
        self.assertLess(lock_at, job_mkdir_at)

    def test_owner_record_is_written_before_the_import(self):
        names = [
            event for event in self.ops.events
            if event[0] == "write_json" or (event[0] == "run" and event[1] == "--import")
        ]
        self.assertEqual(names[0][0], "write_json")
        self.assertEqual(names[0][1], RECORD_PATH)
        self.assertEqual(names[1][1], "--import")

    def test_imports_only_the_private_verified_copy_as_wsl2(self):
        imports = [e for e in self.ops.events if e[0] == "run" and e[1] == "--import"]
        argv = list(imports[0][2])
        self.assertEqual(argv, ["wsl.exe", "--import", DISTRO, INSTALL_DIR,
                                ROOTFS_COPY, "--version", "2"])
        self.assertNotIn(SOURCE_PATH, argv)

    def test_all_fixed_canaries_run_in_order_before_the_job(self):
        canary_names = [
            event[2][event[2].index("--canary") + 1]
            for event in self.ops.events
            if event[0] == "run" and event[1] == "-d" and "--canary" in event[2]
        ]
        self.assertEqual(canary_names, list(wr.CANARY_ORDER))
        verbs = [e for e in self.ops.events if e[0] == "run"]
        last_canary = max(i for i, e in enumerate(verbs) if "--canary" in e[2])
        job_index = min(i for i, e in enumerate(verbs)
                        if e[1] == "-d" and "--canary" not in e[2])
        self.assertLess(last_canary, job_index)

    def test_input_moves_only_through_the_pipe(self):
        stdin_events = [e for e in self.ops.events if e[0] == "job_stdin"]
        self.assertEqual(stdin_events, [("job_stdin", "hello")])
        written_bytes = b"".join(
            node.data for key, node in self.ops.nodes.items() if node.kind == "file")
        self.assertNotIn(b"hello", written_bytes)

    def test_guest_argv_carries_no_host_path(self):
        job_argv = [e[2] for e in self.ops.events
                    if e[0] == "run" and e[1] == "-d" and "--canary" not in e[2]][0]
        for argument in job_argv[1:]:
            self.assertNotIn("C:", argument)
            self.assertNotIn("\\", argument)

    def test_job_directory_and_owner_record_are_gone(self):
        self.assertFalse(self.ops.exists(JOB_DIR))
        self.assertNotIn(RECORD_PATH, self.ops.json)

    def test_receipt_is_written_outside_the_deleted_job_directory(self):
        self.assertIn(RECEIPT_PATH, self.ops.json)
        self.assertFalse(RECEIPT_PATH.casefold().startswith(JOB_DIR.casefold()))

    def test_receipt_holds_no_input_output_command_or_source(self):
        blob = repr(self.ops.json[RECEIPT_PATH])
        for forbidden in ("hello", "job output", "wsl.exe", SOURCE_PATH,
                          ROOTFS_COPY, ww.GUEST_RUNNER_PATH):
            self.assertNotIn(forbidden, blob)

    def test_receipt_records_status_and_cleanup(self):
        receipt = self.ops.json[RECEIPT_PATH]
        self.assertEqual(receipt["status"], wr.STATUS_COMPLETED)
        self.assertEqual(receipt["schema_version"], wr.SCHEMA_VERSION)
        self.assertTrue(receipt["cleanup"]["complete"])
        self.assertTrue(receipt["canaries_passed"])


class TimingTests(unittest.TestCase):
    def test_every_phase_is_timed_and_reported(self):
        ops = FakeOps()
        result = _run(ops)
        for key in ("copy_seconds", "import_seconds", "canary_seconds",
                    "job_seconds", "cleanup_seconds", "total_seconds"):
            self.assertIn(key, result.timings)
            self.assertGreaterEqual(result.timings[key], 0.0)
        self.assertEqual(
            sorted(ops.json[RECEIPT_PATH]["timings"]), sorted(result.timings))

    def test_aborted_run_still_reports_cleanup_and_total_timings(self):
        ops = FakeOps()
        ops.command_returncodes["--import"] = 1
        result = _run(ops)
        self.assertEqual(result.reason, "import_failed")
        self.assertIn("cleanup_seconds", result.timings)
        self.assertIn("total_seconds", result.timings)
        self.assertNotIn("job_seconds", result.timings)


class CleanupTests(unittest.TestCase):
    def test_filesystem_cleanup_exception_forces_abort(self):
        ops = FakeOps()
        ops.fs_errors["remove_tree"] = OSError("device busy")
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)
        self.assertFalse(result.cleanup.complete)
        self.assertTrue(result.cleanup.filesystem.startswith("failed"))
        self.assertTrue(result.cleanup.owner_record.startswith("retained"))
        self.assertIn(RECORD_PATH, ops.json)
        self.assertEqual(result.stdout, b"")

    def test_terminate_exception_does_not_skip_unregister_or_filesystem(self):
        ops = FakeOps()
        ops.command_errors["--terminate"] = OSError("wsl unavailable")
        result = _run(ops)
        self.assertTrue(result.cleanup.terminate.startswith("failed"))
        self.assertEqual(result.cleanup.unregister, "ok")
        self.assertEqual(result.cleanup.filesystem, "ok")
        self.assertTrue(result.cleanup.owner_record.startswith("retained"))
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertIn("--unregister", ops.verbs())

    def test_unregister_failure_is_reported_not_swallowed(self):
        ops = FakeOps()
        ops.command_returncodes["--unregister"] = 1
        result = _run(ops)
        self.assertEqual(result.cleanup.unregister, "failed+exit_1")
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)
        self.assertTrue(result.cleanup.owner_record.startswith("retained"))

    def test_cleanup_runs_after_a_canary_failure(self):
        ops = FakeOps()
        ops.canary_overrides[wr.CANARY_INTEROP] = b"wsl-interop-absent:tampered\n"
        result = _run(ops)
        self.assertEqual(result.reason, "canary_failed")
        self.assertFalse(result.canaries_passed)
        self.assertTrue(result.cleanup.complete)
        self.assertIn("--terminate", ops.verbs())
        self.assertIn("--unregister", ops.verbs())
        self.assertFalse(ops.exists(JOB_DIR))

    def test_no_terminate_or_unregister_when_import_was_never_attempted(self):
        ops = FakeOps()
        ops.acl_failures.add(JOB_DIR)
        result = _run(ops)
        self.assertEqual(result.reason, "acl_not_verified")
        self.assertEqual(result.cleanup.terminate, "not_attempted")
        self.assertEqual(result.cleanup.unregister, "not_attempted")
        self.assertEqual(result.cleanup.filesystem, "ok")
        self.assertNotIn("--import", ops.verbs())

    def test_removal_that_leaves_the_directory_behind_is_a_failure(self):
        ops = FakeOps()
        original = ops.remove_tree

        def fake_remove(path):
            original(path)
            ops._mkdir(path)

        ops.remove_tree = fake_remove
        result = _run(ops)
        self.assertEqual(result.cleanup.filesystem, "failed+still_present")
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)


class CopyTests(unittest.TestCase):
    def test_identity_drift_between_stat_and_open_aborts(self):
        ops = FakeOps()
        ops.drift_after_open = True
        result = _run(ops)
        self.assertEqual(result.reason, "source_identity_drift")
        self.assertNotIn("--import", ops.verbs())

    def test_identity_drift_during_copy_aborts_and_removes_partial_copy(self):
        ops = FakeOps()
        ops.drift_during_copy = True
        result = _run(ops)
        self.assertEqual(result.reason, "source_identity_drift")
        self.assertIn(("unlink", ROOTFS_COPY), ops.events)
        self.assertNotIn("--import", ops.verbs())

    def test_oversized_source_is_rejected_before_any_copy(self):
        ops = FakeOps()
        limits = wr.RuntimeLimits(**{**wr.DEFAULT_LIMITS.__dict__, "rootfs_max_bytes": 8})
        result = _run(ops, limits=limits)
        self.assertEqual(result.reason, "rootfs_too_large")
        self.assertNotIn(("create", ROOTFS_COPY), ops.events)

    def test_source_that_grows_past_the_cap_mid_copy_aborts(self):
        ops = FakeOps()
        ops.grow_during_copy = True
        limits = wr.RuntimeLimits(
            **{**wr.DEFAULT_LIMITS.__dict__, "rootfs_max_bytes": len(ROOTFS_BYTES) + 1})
        result = _run(ops, limits=limits)
        self.assertEqual(result.reason, "rootfs_too_large")
        self.assertIn(("unlink", ROOTFS_COPY), ops.events)

    def test_hash_mismatch_aborts_before_import(self):
        ops = FakeOps()
        ops.write_file(SOURCE_PATH, b"different bytes entirely")
        result = _run(ops)
        self.assertEqual(result.reason, "rootfs_hash_mismatch")
        self.assertNotIn("--import", ops.verbs())
        self.assertIn(("unlink", ROOTFS_COPY), ops.events)

    def test_destination_must_be_created_exclusively(self):
        ops = FakeOps()
        ops._mkdir(wr.jobs_root(RUNTIME_ROOT))
        original = ops.secure_mkdir

        def mkdir_then_squat(path):
            original(path)
            if path == JOB_DIR:
                ops.write_file(ROOTFS_COPY, b"squatted")

        ops.secure_mkdir = mkdir_then_squat
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, "destination_exists")
        self.assertNotIn("--import", ops.verbs())
        self.assertFalse(ops.exists(JOB_DIR))


class PathPolicyTests(unittest.TestCase):
    def test_reparse_ancestor_of_the_job_directory_is_rejected(self):
        ops = FakeOps()
        ops.make_reparse(wr.jobs_root(RUNTIME_ROOT))
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")
        self.assertEqual(ops.verbs(), [])
        self.assertEqual(result.cleanup.filesystem, "not_attempted")

    def test_reparse_ancestor_of_the_source_is_rejected(self):
        ops = FakeOps()
        ops.make_reparse("C:\\Users\\tester\\images")
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")
        self.assertEqual(ops.verbs(), [])

    def test_reparse_source_file_is_rejected(self):
        ops = FakeOps()
        ops.nodes[FakeOps._key(SOURCE_PATH)].reparse = True
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")

    def test_existing_job_directory_is_rejected(self):
        ops = FakeOps()
        ops._mkdir(JOB_DIR)
        result = _run(ops)
        self.assertEqual(result.reason, "path_exists")
        self.assertEqual(ops.verbs(), [])

    def test_relative_unc_and_device_paths_are_rejected(self):
        for bad in ("relative\\path", "\\\\server\\share\\x", "\\\\?\\C:\\x",
                    "C:\\a\\..\\b"):
            with self.subTest(bad=bad):
                result = _run(FakeOps(), runtime_root=bad)
                self.assertEqual(result.reason, "path_invalid")

    def test_missing_source_is_rejected(self):
        ops = FakeOps()
        del ops.nodes[FakeOps._key(SOURCE_PATH)]
        result = _run(ops)
        self.assertEqual(result.reason, "source_missing")


class InputOutputBoundTests(unittest.TestCase):
    def test_oversized_input_aborts_before_anything_is_created(self):
        ops = FakeOps()
        limits = wr.RuntimeLimits(**{**wr.DEFAULT_LIMITS.__dict__, "input_max_bytes": 4})
        result = _run(ops, limits=limits, stdin_data="much too long")
        self.assertEqual(result.reason, "input_too_large")
        self.assertEqual(ops.events, [])
        self.assertFalse(result.spawned)

    def test_output_cap_breach_aborts_and_discards_output(self):
        ops = FakeOps()
        ops.job_result_override = _ok(
            ["wsl.exe", "-d"], stdout=b"x" * 32, cap_exceeded=True)
        result = _run(ops)
        self.assertEqual(result.reason, "output_too_large")
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.cleanup.complete)

    def test_job_timeout_aborts(self):
        ops = FakeOps()
        ops.job_result_override = _ok(["wsl.exe", "-d"], timed_out=True)
        result = _run(ops)
        self.assertEqual(result.reason, "job_timed_out")


class LimitValidationTests(unittest.TestCase):
    def _limits(self, **overrides):
        return wr.RuntimeLimits(**{**wr.DEFAULT_LIMITS.__dict__, **overrides})

    def test_rejects_zero_negative_boolean_and_over_ceiling(self):
        cases = [
            {"rootfs_max_bytes": 0},
            {"input_max_bytes": -1},
            {"output_max_bytes": True},
            {"job_timeout_seconds": 0},
            {"grace_seconds": float("inf")},
            {"job_timeout_seconds": wr.MAX_TIMEOUT_SECONDS + 1},
            {"rootfs_max_bytes": wr.MAX_ROOTFS_BYTES + 1},
            {"lock_timeout_seconds": wr.MAX_LOCK_TIMEOUT_SECONDS + 1},
            {"canary_timeout_seconds": "60"},
        ]
        for override in cases:
            with self.subTest(override=override):
                result = _run(FakeOps(), limits=self._limits(**override))
                self.assertEqual(result.reason, "limits_invalid")

    def test_rejects_non_limits_object(self):
        result = _run(FakeOps(), limits={"rootfs_max_bytes": 1})
        self.assertEqual(result.reason, "limits_invalid")

    def test_accepts_the_defaults(self):
        self.assertEqual(wr.validate_limits(wr.DEFAULT_LIMITS), wr.DEFAULT_LIMITS)


class RequestValidationTests(unittest.TestCase):
    def test_rejects_unpinned_guest_runner_hash(self):
        result = _run(FakeOps(), guest_runner_sha256="not-a-hash")
        self.assertEqual(result.reason, "guest_runner_hash_invalid")

    def test_rejects_unsafe_distro_token(self):
        result = _run(FakeOps(), distro_token="../escape")
        self.assertEqual(result.reason, "distro_name_invalid")

    def test_rejects_job_spec_naming_a_host_path(self):
        spec = dict(JOB_SPEC, workdir="/mnt/c/Users/tester")
        result = _run(FakeOps(), job_spec=spec)
        self.assertEqual(result.reason, "job_spec_invalid")

    def test_rejects_job_spec_that_does_not_use_the_fixed_guest_runner(self):
        spec = dict(JOB_SPEC, command=["/bin/sh", "-c", "id"])
        result = _run(FakeOps(), job_spec=spec)
        self.assertEqual(result.reason, "job_spec_invalid")

    def test_rejects_unparsed_manifest(self):
        result = _run(FakeOps(), manifest={"rootfs_sha256": ROOTFS_SHA256})
        self.assertEqual(result.reason, "manifest_invalid")

    def test_rejects_non_string_input(self):
        result = _run(FakeOps(), stdin_data=b"bytes")
        self.assertEqual(result.reason, "input_invalid")


class CanaryTests(unittest.TestCase):
    def test_expectations_are_pinned_to_the_manifest_and_wsl_conf(self):
        expectations = wr.canary_expectations(MANIFEST, GUEST_RUNNER_SHA256)
        self.assertEqual(set(expectations), set(wr.CANARY_ORDER))
        self.assertEqual(
            expectations[wr.CANARY_WSL_CONF],
            f"{wr.CANARY_WSL_CONF}:"
            + hashlib.sha256(ww.WSL_CONF_CONTENTS.encode("utf-8")).hexdigest())
        self.assertIn("node=20.11.1", expectations[wr.CANARY_VERSIONS])
        self.assertIn(GUEST_RUNNER_SHA256, expectations[wr.CANARY_GUEST_RUNNER])

    def test_each_canary_failure_aborts_before_the_job_runs(self):
        for name in wr.CANARY_ORDER:
            with self.subTest(canary=name):
                ops = FakeOps()
                ops.canary_overrides[name] = b"wrong\n"
                result = _run(ops)
                self.assertEqual(result.reason, "canary_failed")
                job_runs = [e for e in ops.events
                            if e[0] == "run" and e[1] == "-d" and "--canary" not in e[2]]
                self.assertEqual(job_runs, [])

    def test_extra_output_appended_to_a_canary_is_rejected(self):
        ops = FakeOps()
        expected = wr.canary_expectations(MANIFEST, GUEST_RUNNER_SHA256)[wr.CANARY_HOST_MOUNT]
        ops.canary_overrides[wr.CANARY_HOST_MOUNT] = (expected + " trailing\n").encode()
        self.assertEqual(_run(ops).reason, "canary_failed")


class ReceiptTests(unittest.TestCase):
    def test_receipt_write_failure_cannot_report_success(self):
        ops = FakeOps()
        ops.json_write_errors[RECEIPT_PATH] = OSError("disk full")
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, wr.REASON_RECEIPT_WRITE_FAILED)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.cleanup.complete)

    def test_owner_record_write_failure_aborts_before_import(self):
        ops = FakeOps()
        ops.json_write_errors[RECORD_PATH] = OSError("disk full")
        result = _run(ops)
        self.assertEqual(result.reason, "owner_record_write_failed")
        self.assertNotIn("--import", ops.verbs())


# ---------------------------------------------------------------------------
# Stale reaping
# ---------------------------------------------------------------------------


NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed_record(ops, name=DISTRO, *, age_hours=5.0, owner_pid=9999, identity="old",
                 install_dir=None, job_dir=None, dev=7, ino=55):
    job_dir = job_dir or wr.job_directory(RUNTIME_ROOT, name)
    install_dir = install_dir or (job_dir + "\\distro")
    ops._mkdir(job_dir)
    ops.nodes[FakeOps._key(job_dir)].dev = dev
    ops.nodes[FakeOps._key(job_dir)].ino = ino
    ops._mkdir(install_dir)
    ops.nodes[FakeOps._key(install_dir)].dev = dev
    ops.nodes[FakeOps._key(install_dir)].ino = ino + 1
    ops._mkdir(wr.owner_records_root(RUNTIME_ROOT))
    path = wr.owner_record_path(RUNTIME_ROOT, name)
    ops.json[path] = {
        "schema_version": wr.SCHEMA_VERSION,
        "distro_name": name,
        "job_dir": job_dir,
        "install_dir": install_dir,
        "job_dir_identity": f"{dev}:{ino}",
        "install_dir_identity": f"{dev}:{ino + 1}",
        "created_at": (NOW - timedelta(hours=age_hours)).isoformat(),
        "owner_pid": owner_pid,
        "owner_identity": identity,
    }
    return path


def _reap(ops, **overrides):
    fields = dict(runtime_root=RUNTIME_ROOT, max_age=timedelta(hours=1),
                  ops=ops, platform_name="win32", now=NOW)
    fields.update(overrides)
    return wr.reap_stale_instances(**fields)


class ReapTests(unittest.TestCase):
    def test_reaps_a_stale_owned_idle_instance_under_the_lock(self):
        ops = FakeOps()
        path = _seed_record(ops)
        report = _reap(ops)
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.reaped, [DISTRO])
        self.assertEqual(ops.lock_calls, 1)
        self.assertIn("--terminate", ops.verbs())
        self.assertIn("--unregister", ops.verbs())
        self.assertFalse(ops.exists(wr.job_directory(RUNTIME_ROOT, DISTRO)))
        self.assertNotIn(path, ops.json)

    def test_never_touches_an_instance_active_in_this_process(self):
        ops = FakeOps()
        _seed_record(ops)
        report = _reap(ops, active_distros=[DISTRO])
        self.assertEqual(report.outcomes[DISTRO], "skipped+active_in_this_process")
        self.assertEqual(ops.verbs(), [])
        self.assertTrue(ops.exists(wr.job_directory(RUNTIME_ROOT, DISTRO)))

    def test_never_touches_an_instance_whose_owner_is_still_alive(self):
        ops = FakeOps()
        _seed_record(ops, owner_pid=1234, identity="live")
        ops.liveness[1234] = RUNNING
        ops.identities[1234] = "live"
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_alive")
        self.assertEqual(ops.verbs(), [])

    def test_unreadable_owner_identity_is_indeterminate_not_dead(self):
        ops = FakeOps()
        _seed_record(ops, owner_pid=1234, identity="live")
        ops.liveness[1234] = RUNNING
        ops.identities[1234] = ""
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_indeterminate")
        self.assertEqual(ops.verbs(), [])

    def test_leaves_a_fresh_instance_alone(self):
        ops = FakeOps()
        _seed_record(ops, age_hours=0.1)
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+not_stale")
        self.assertEqual(ops.verbs(), [])

    def test_record_rewritten_between_validation_and_action_is_skipped(self):
        ops = FakeOps()
        path = _seed_record(ops)
        drifted = dict(ops.json[path])
        drifted["owner_pid"] = 5555
        ops.read_json_queue[path] = [dict(ops.json[path]), drifted]
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_record_changed")
        self.assertEqual(ops.verbs(), [])
        self.assertTrue(ops.exists(wr.job_directory(RUNTIME_ROOT, DISTRO)))

    def test_owner_becoming_alive_between_validation_and_action_is_skipped(self):
        ops = FakeOps()
        path = _seed_record(ops, owner_pid=777, identity="live")
        record = dict(ops.json[path])

        calls = {"n": 0}
        original = ops.read_json

        def racing_read(target):
            calls["n"] += 1
            if calls["n"] == 2:
                ops.liveness[777] = RUNNING
                ops.identities[777] = "live"
            return original(target)

        ops.read_json = racing_read
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_alive")
        self.assertEqual(ops.verbs(), [])
        self.assertEqual(record["owner_pid"], 777)

    def test_job_directory_identity_drift_is_skipped(self):
        ops = FakeOps()
        _seed_record(ops)
        ops.nodes[FakeOps._key(wr.job_directory(RUNTIME_ROOT, DISTRO))].ino = 999
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+job_dir_identity_drift")
        self.assertEqual(ops.verbs(), [])

    def test_job_directory_replaced_by_a_reparse_point_is_skipped(self):
        ops = FakeOps()
        _seed_record(ops)
        ops.nodes[FakeOps._key(wr.job_directory(RUNTIME_ROOT, DISTRO))].reparse = True
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+job_dir_reparse_point_rejected")
        self.assertEqual(ops.verbs(), [])

    def test_record_pointing_outside_the_runtime_root_is_never_acted_on(self):
        ops = FakeOps()
        path = _seed_record(ops)
        ops.json[path]["job_dir"] = "C:\\Windows\\System32"
        ops.json[path]["install_dir"] = "C:\\Windows\\System32\\distro"
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_record_unowned")
        self.assertEqual(ops.verbs(), [])

    def test_install_dir_outside_the_job_directory_is_never_acted_on(self):
        ops = FakeOps()
        path = _seed_record(ops)
        ops.json[path]["install_dir"] = "C:\\Users\\tester\\elsewhere"
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_record_unowned")
        self.assertEqual(ops.verbs(), [])

    def test_foreign_and_malformed_record_names_are_ignored(self):
        ops = FakeOps()
        ops._mkdir(wr.owner_records_root(RUNTIME_ROOT))
        ops.json[wr.owner_records_root(RUNTIME_ROOT) + "\\notes.txt"] = {}
        ops.json[wr.owner_records_root(RUNTIME_ROOT) + "\\someone-else.json"] = {}
        report = _reap(ops)
        self.assertEqual(ops.verbs(), [])
        self.assertEqual(report.status, "ok")
        self.assertIn("skipped", report.outcomes["notes.txt"])
        self.assertIn("skipped", report.outcomes["someone-else.json"])

    def test_malformed_record_contents_are_skipped(self):
        ops = FakeOps()
        path = _seed_record(ops)
        del ops.json[path]["owner_identity"]
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_record_invalid")
        self.assertEqual(ops.verbs(), [])

    def test_naive_created_at_is_rejected(self):
        ops = FakeOps()
        path = _seed_record(ops)
        ops.json[path]["created_at"] = "2026-03-01T06:00:00"
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_record_invalid")

    def test_missing_job_directory_refuses_to_unregister_by_name(self):
        """A distro name is not a registration identity: the replacement that
        now owns the name could belong to anyone."""
        ops = FakeOps()
        path = _seed_record(ops)
        ops.remove_tree(wr.job_directory(RUNTIME_ROOT, DISTRO))
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+registration_unverifiable")
        self.assertEqual(ops.verbs(), [])
        self.assertIn(path, ops.json)

    def test_missing_install_directory_refuses_to_unregister_by_name(self):
        ops = FakeOps()
        path = _seed_record(ops)
        ops.remove_tree(wr.job_directory(RUNTIME_ROOT, DISTRO) + "\\distro")
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+registration_unverifiable")
        self.assertEqual(ops.verbs(), [])
        self.assertIn(path, ops.json)

    def test_partial_reap_failure_is_reported_as_incomplete(self):
        ops = FakeOps()
        _seed_record(ops)
        ops.fs_errors["remove_tree"] = OSError("locked")
        report = _reap(ops)
        self.assertEqual(report.status, "incomplete")
        self.assertTrue(report.outcomes[DISTRO].startswith("failed"))
        self.assertEqual(report.reaped, [])
        self.assertIn(wr.owner_record_path(RUNTIME_ROOT, DISTRO), ops.json)

    def test_missing_records_directory_is_not_an_error(self):
        ops = FakeOps()
        report = _reap(ops)
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.outcomes, {})

    def test_invalid_max_age_aborts_without_touching_anything(self):
        ops = FakeOps()
        _seed_record(ops)
        report = _reap(ops, max_age=timedelta(0))
        self.assertEqual(report.status, wr.STATUS_ABORTED)
        self.assertEqual(ops.verbs(), [])


class OwnerRecordTests(unittest.TestCase):
    def test_round_trips_a_record_this_module_built(self):
        ops = FakeOps()
        ops._mkdir(JOB_DIR)
        record = wr.build_owner_record(ops, DISTRO, JOB_DIR, INSTALL_DIR, "7:55", "7:56")
        parsed = wr.parse_owner_record(record, runtime_root=RUNTIME_ROOT)
        self.assertEqual(parsed["distro_name"], DISTRO)
        self.assertEqual(parsed["install_dir"], INSTALL_DIR)
        self.assertEqual(parsed["install_dir_identity"], "7:56")
        self.assertEqual(parsed["owner_pid"], ops.pid)

    def test_holds_no_source_path_input_or_command(self):
        ops = FakeOps()
        record = wr.build_owner_record(ops, DISTRO, JOB_DIR, INSTALL_DIR, "7:55", "7:56")
        blob = repr(record)
        for forbidden in (SOURCE_PATH, "hello", ww.GUEST_RUNNER_PATH, "wsl.exe"):
            self.assertNotIn(forbidden, blob)

    def test_rejects_extra_keys(self):
        ops = FakeOps()
        record = wr.build_owner_record(ops, DISTRO, JOB_DIR, INSTALL_DIR, "7:55", "7:56")
        record["extra"] = 1
        with self.assertRaises(wr.WindowsWslRuntimeError):
            wr.parse_owner_record(record, runtime_root=RUNTIME_ROOT)


class LockFailureTests(unittest.TestCase):
    def test_lock_timeout_becomes_a_safe_aborted_result(self):
        ops = FakeOps()
        ops.lock_error = TimeoutError("busy")
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, "lock_unavailable")
        self.assertEqual(ops.verbs(), [])


# ---------------------------------------------------------------------------
# Regressions for the review findings
# ---------------------------------------------------------------------------


class PrivateTreeAclTests(unittest.TestCase):
    """Finding 1: every directory the runtime relies on is verified, not just
    the per-job one. The owner-record directory is a deletion authority."""

    TREE = (
        RUNTIME_ROOT,
        wr.jobs_root(RUNTIME_ROOT),
        wr.owner_records_root(RUNTIME_ROOT),
        wr.receipts_root(RUNTIME_ROOT),
    )

    def test_every_shared_directory_and_the_job_directory_are_verified(self):
        ops = FakeOps()
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_COMPLETED, result.detail)
        verified = ops.verified_dirs()
        for path in self.TREE + (JOB_DIR, INSTALL_DIR):
            self.assertIn(FakeOps._key(path), verified)

    def test_lock_directory_is_verified_before_the_lock_is_taken(self):
        ops = FakeOps()
        _run(ops)
        first_verify = min(
            i for i, e in enumerate(ops.events)
            if e[0] == "verify_acl" and ops._key(e[1]) == ops._key(RUNTIME_ROOT))
        lock_at = ops.events.index(("lock", wr.lock_path(RUNTIME_ROOT)))
        self.assertLess(first_verify, lock_at)

    def test_each_shared_directory_failing_its_acl_aborts_with_no_spawn(self):
        for path in self.TREE:
            with self.subTest(directory=path):
                ops = FakeOps()
                ops.acl_failures.add(path)
                result = _run(ops)
                self.assertEqual(result.reason, "acl_not_verified")
                self.assertEqual(ops.verbs(), [])
                self.assertEqual(ops.lock_calls, 0)
                self.assertFalse(ops.exists(JOB_DIR))

    def test_owner_record_directory_acl_failure_stops_a_reap_untouched(self):
        ops = FakeOps()
        _seed_record(ops)
        ops.acl_failures.add(wr.owner_records_root(RUNTIME_ROOT))
        report = _reap(ops)
        self.assertEqual(report.status, wr.STATUS_ABORTED)
        self.assertIn("acl_not_verified", report.detail)
        self.assertEqual(ops.verbs(), [])
        self.assertIn(wr.owner_record_path(RUNTIME_ROOT, DISTRO), ops.json)

    def test_runtime_root_acl_failure_stops_a_reap_before_the_lock(self):
        ops = FakeOps()
        _seed_record(ops)
        ops.acl_failures.add(RUNTIME_ROOT)
        report = _reap(ops)
        self.assertEqual(report.status, wr.STATUS_ABORTED)
        self.assertEqual(ops.lock_calls, 0)
        self.assertEqual(ops.verbs(), [])

    def test_reparse_ancestor_of_a_shared_directory_is_rejected(self):
        ops = FakeOps()
        ops.make_reparse(wr.receipts_root(RUNTIME_ROOT))
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")
        self.assertEqual(ops.lock_calls, 0)


class CleanupVerdictTests(unittest.TestCase):
    """Finding 2: a teardown command is judged on the whole runner verdict."""

    SIGNALS = (
        ({"spawn_failed": True}, "spawn_failed"),
        ({"timed_out": True}, "timed_out"),
        ({"cap_exceeded": True}, "cap_exceeded"),
        ({"descendant_held_pipes": True}, "descendant_held_pipes"),
        ({"returncode": None}, "unknown_exit"),
        ({"returncode": 3}, "exit_3"),
    )

    def test_command_failure_names_each_signal(self):
        for overrides, expected in self.SIGNALS:
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    wr.command_failure(_ok(["wsl.exe"], **overrides)), expected)
        self.assertEqual(wr.command_failure(_ok(["wsl.exe"])), "")

    def _cleanup_with(self, verb, overrides):
        ops = FakeOps()
        original = ops.run_host

        def responder(argv, **kwargs):
            if argv[1] == verb:
                ops.events.append(("run", argv[1], tuple(argv)))
                return _ok(list(argv), **overrides)
            return original(argv, **kwargs)

        ops.run_host = responder
        return ops, _run(ops)

    def test_every_signal_fails_terminate(self):
        for overrides, expected in self.SIGNALS:
            with self.subTest(overrides=overrides):
                ops, result = self._cleanup_with("--terminate", overrides)
                self.assertEqual(result.cleanup.terminate, f"failed+{expected}")
                self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)
                self.assertIn("--unregister", ops.verbs())

    def test_every_signal_fails_unregister(self):
        for overrides, expected in self.SIGNALS:
            with self.subTest(overrides=overrides):
                _ops, result = self._cleanup_with("--unregister", overrides)
                self.assertEqual(result.cleanup.unregister, f"failed+{expected}")
                self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)

    def test_descendant_holding_pipes_during_import_aborts(self):
        ops = FakeOps()
        ops.import_result_override = _ok(
            ["wsl.exe", "--import"], descendant_held_pipes=True)
        result = _run(ops)
        self.assertEqual(result.reason, "import_failed")

    def test_incomplete_cleanup_outranks_an_earlier_failure(self):
        ops = FakeOps()
        ops.canary_overrides[wr.CANARY_HOST_MOUNT] = b"tampered\n"
        ops.fs_errors["remove_tree"] = OSError("locked")
        result = _run(ops)
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)
        self.assertIn("canary_failed", result.detail)
        self.assertEqual(ops.json[RECEIPT_PATH]["reason"], wr.REASON_CLEANUP_FAILED)
        self.assertFalse(ops.json[RECEIPT_PATH]["cleanup"]["complete"])

    def test_incomplete_cleanup_outranks_a_receipt_write_failure(self):
        ops = FakeOps()
        ops.fs_errors["remove_tree"] = OSError("locked")
        ops.json_write_errors[RECEIPT_PATH] = OSError("disk full")
        result = _run(ops)
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)
        self.assertIn("receipt write failed", result.detail)


class JobExitTests(unittest.TestCase):
    """Finding 3: a nonzero delegated exit is never a completed delegation."""

    def test_nonzero_exit_is_aborted_but_still_reported(self):
        ops = FakeOps()
        ops.job_result_override = _ok(
            ["wsl.exe", "-d"], stdout=b"partial", returncode=7)
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, "job_failed")
        self.assertEqual(result.detail, "exit_7")
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.cleanup.complete)
        self.assertEqual(ops.json[RECEIPT_PATH]["exit_code"], 7)

    def test_unknown_exit_is_aborted(self):
        ops = FakeOps()
        ops.job_result_override = _ok(["wsl.exe", "-d"], returncode=None)
        result = _run(ops)
        self.assertEqual(result.reason, "job_failed")
        self.assertEqual(result.detail, "unknown_exit")

    def test_descendant_holding_pipes_is_aborted(self):
        ops = FakeOps()
        ops.job_result_override = _ok(
            ["wsl.exe", "-d"], descendant_held_pipes=True)
        result = _run(ops)
        self.assertEqual(result.reason, "job_failed")
        self.assertEqual(result.detail, "descendant_held_pipes")


class PreImportRevalidationTests(unittest.TestCase):
    """Finding 5: reparse and identity checks are repeated before the import."""

    def _before_import(self, ops, hook):
        """Fire the swap in the real window: after the copy has been hashed
        and its identity recorded, before ``wsl.exe --import`` is invoked."""
        original = ops.write_json

        def write_json(path, payload):
            original(path, payload)
            if path == RECORD_PATH:
                hook()

        ops.write_json = write_json

    def test_job_directory_swapped_after_the_copy_aborts_before_import(self):
        ops = FakeOps()
        self._before_import(
            ops, lambda: setattr(ops.nodes[FakeOps._key(JOB_DIR)], "ino", 4242))
        result = _run(ops)
        self.assertEqual(result.reason, "job_dir_identity_drift")
        self.assertNotIn("--import", ops.verbs())

    def test_reparse_point_appearing_on_the_job_directory_aborts(self):
        ops = FakeOps()
        self._before_import(
            ops, lambda: setattr(ops.nodes[FakeOps._key(JOB_DIR)], "reparse", True))
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")
        self.assertNotIn("--import", ops.verbs())

    def test_reparse_point_appearing_on_an_ancestor_aborts(self):
        ops = FakeOps()
        self._before_import(
            ops,
            lambda: setattr(
                ops.nodes[FakeOps._key(wr.jobs_root(RUNTIME_ROOT))], "reparse", True))
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")
        self.assertNotIn("--import", ops.verbs())

    def test_private_copy_replaced_after_verification_aborts(self):
        ops = FakeOps()

        def swap():
            ops.nodes[FakeOps._key(ROOTFS_COPY)].ino = 9191

        self._before_import(ops, swap)
        result = _run(ops)
        self.assertEqual(result.reason, "rootfs_copy_identity_drift")
        self.assertNotIn("--import", ops.verbs())

    def test_private_copy_turned_into_a_reparse_point_aborts(self):
        ops = FakeOps()
        self._before_import(
            ops, lambda: setattr(ops.nodes[FakeOps._key(ROOTFS_COPY)], "reparse", True))
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")
        self.assertNotIn("--import", ops.verbs())

    def test_install_dir_replaced_by_a_reparse_point_aborts(self):
        ops = FakeOps()
        self._before_import(
            ops, lambda: setattr(ops.nodes[FakeOps._key(INSTALL_DIR)], "reparse", True))
        result = _run(ops)
        self.assertEqual(result.reason, "reparse_point_rejected")
        self.assertNotIn("--import", ops.verbs())

    def test_still_imports_only_the_private_copy_after_revalidation(self):
        ops = FakeOps()
        _run(ops)
        argv = [e[2] for e in ops.events if e[0] == "run" and e[1] == "--import"][0]
        self.assertIn(ROOTFS_COPY, argv)
        self.assertNotIn(SOURCE_PATH, argv)


class RecordFailureCleanupTests(unittest.TestCase):
    """Finding 6: a record failure after the directories exist still cleans up."""

    def test_owner_record_failure_removes_the_job_directory(self):
        ops = FakeOps()
        ops.json_write_errors[RECORD_PATH] = OSError("disk full")
        result = _run(ops)
        self.assertEqual(result.reason, "owner_record_write_failed")
        self.assertFalse(ops.exists(JOB_DIR))
        self.assertEqual(result.cleanup.filesystem, "ok")
        self.assertEqual(result.cleanup.owner_record, "not_attempted")
        self.assertNotIn("--import", ops.verbs())

    def test_non_oserror_record_failure_also_cleans_up(self):
        ops = FakeOps()
        ops.json_write_errors[RECORD_PATH] = ValueError("not serialisable")
        result = _run(ops)
        self.assertEqual(result.reason, "owner_record_write_failed")
        self.assertFalse(ops.exists(JOB_DIR))

    def test_receipt_failure_still_leaves_nothing_behind(self):
        ops = FakeOps()
        ops.json_write_errors[RECEIPT_PATH] = OSError("disk full")
        result = _run(ops)
        self.assertEqual(result.reason, wr.REASON_RECEIPT_WRITE_FAILED)
        self.assertFalse(ops.exists(JOB_DIR))
        self.assertNotIn(RECORD_PATH, ops.json)
        self.assertTrue(result.cleanup.complete)


class TriStateLivenessTests(unittest.TestCase):
    """Finding 1: only a positive proof of death permits a destructive step."""

    def test_classifier_maps_every_evidence_shape(self):
        cases = [
            (EXITED, wr.LIVENESS_DEAD),
            (PID_GONE, wr.LIVENESS_DEAD),
            (RUNNING, wr.LIVENESS_ALIVE),
            (ACCESS_DENIED, wr.LIVENESS_INDETERMINATE),
            (WAIT_FAILED, wr.LIVENESS_INDETERMINATE),
            (UNEXPECTED_WAIT, wr.LIVENESS_INDETERMINATE),
            ({"alive": False, "open_process_error": 1314}, wr.LIVENESS_INDETERMINATE),
            ({"probe": "x"}, wr.LIVENESS_INDETERMINATE),
            ({"alive": False, "reason": "errno 13"}, wr.LIVENESS_INDETERMINATE),
            ({"alive": False, "reason": "no such process"}, wr.LIVENESS_DEAD),
            (None, wr.LIVENESS_INDETERMINATE),
            ("alive", wr.LIVENESS_INDETERMINATE),
        ]
        for evidence, expected in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(wr.classify_liveness(evidence), expected)

    def test_access_denied_never_reaps(self):
        ops = FakeOps()
        path = _seed_record(ops, owner_pid=555)
        ops.liveness[555] = ACCESS_DENIED
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_indeterminate")
        self.assertEqual(ops.verbs(), [])
        self.assertTrue(ops.exists(wr.job_directory(RUNTIME_ROOT, DISTRO)))
        self.assertIn(path, ops.json)

    def test_wait_failed_never_reaps(self):
        ops = FakeOps()
        path = _seed_record(ops, owner_pid=556)
        ops.liveness[556] = WAIT_FAILED
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_indeterminate")
        self.assertEqual(ops.verbs(), [])
        self.assertTrue(ops.exists(wr.job_directory(RUNTIME_ROOT, DISTRO)))
        self.assertIn(path, ops.json)

    def test_a_raising_liveness_probe_never_reaps(self):
        ops = FakeOps()
        _seed_record(ops, owner_pid=557)
        ops.liveness_errors[557] = OSError("probe exploded")
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_indeterminate")
        self.assertEqual(ops.verbs(), [])

    def test_a_raising_identity_probe_never_reaps(self):
        ops = FakeOps()
        _seed_record(ops, owner_pid=558)
        ops.liveness[558] = EXITED
        ops.identity_errors[558] = OSError("ps failed")
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_indeterminate")
        self.assertEqual(ops.verbs(), [])

    def test_contradictory_identity_on_a_dead_pid_never_reaps(self):
        ops = FakeOps()
        _seed_record(ops, owner_pid=559, identity="ours")
        ops.liveness[559] = EXITED
        ops.identities[559] = "ours"
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_indeterminate")
        self.assertEqual(ops.verbs(), [])

    def test_a_recycled_pid_running_someone_else_is_reapable(self):
        ops = FakeOps()
        _seed_record(ops, owner_pid=560, identity="ours")
        ops.liveness[560] = RUNNING
        ops.identities[560] = "somebody-elses-process"
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "reaped")

    def test_the_boolean_probe_is_not_used_at_all(self):
        self.assertFalse(hasattr(wr.HostOps, "process_alive"))
        source = (ROOT / "src/agent_bridge/orchestration/windows_wsl_runtime.py").read_text()
        for line in source.splitlines():
            code = line.split("#")[0]
            self.assertNotIn(".process_alive(", code)


class ReceiptConfidentialityTests(unittest.TestCase):
    """Finding 2: a persistent receipt never carries a filesystem path."""

    def test_sanitizer_suppresses_anything_path_shaped(self):
        leaky = [
            "could not read " + SOURCE_PATH,
            "[Errno 13] Permission denied: '" + SOURCE_PATH + "'",
            r"rootfs_source_path must be an absolute path: 'C:\x\y'",
            "workdir rejected: /mnt/c/Users/tester",
            'value "secret.tar" refused',
        ]
        for detail in leaky:
            with self.subTest(detail=detail):
                self.assertEqual(wr.sanitize_detail(detail), wr.DETAIL_SUPPRESSED)

    def test_sanitizer_keeps_fixed_codes_readable(self):
        self.assertEqual(
            wr.sanitize_detail("rootfs_max_bytes must be positive"),
            "rootfs_max_bytes_must_be_positive")
        self.assertEqual(wr.sanitize_detail("exit_7"), "exit_7")
        self.assertEqual(wr.sanitize_detail(""), "")

    def test_the_error_type_sanitizes_at_the_raise_site(self):
        exc = wr.WindowsWslRuntimeError(
            "path_unreadable", "denied: '" + SOURCE_PATH + "'")
        self.assertEqual(exc.detail, wr.DETAIL_SUPPRESSED)
        self.assertNotIn(SOURCE_PATH, str(exc))

    def _leaky_oserror(self):
        return OSError(13, "Permission denied", SOURCE_PATH)

    def test_an_oserror_naming_the_source_never_reaches_the_receipt(self):
        ops = FakeOps()
        ops.fs_errors["open_read"] = self._leaky_oserror()
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        blob = repr(ops.json[RECEIPT_PATH])
        self.assertNotIn(SOURCE_PATH, blob)
        self.assertNotIn("pinned.tar", blob)
        self.assertNotIn("Permission denied", blob)
        self.assertNotIn(SOURCE_PATH, result.detail)

    def test_an_unreadable_ancestor_does_not_leak_its_path(self):
        ops = FakeOps()
        ops.stat_errors[SOURCE_PATH] = self._leaky_oserror()
        result = _run(ops)
        self.assertEqual(result.reason, "path_unreadable")
        # The exception type survives, the filename it names does not.
        self.assertEqual(result.detail, "rootfs_source_path_ancestor_permissionerror")
        self.assertNotIn(SOURCE_PATH, repr(ops.json[RECEIPT_PATH]))

    def test_a_rejected_path_value_is_not_echoed(self):
        result = _run(FakeOps(), runtime_root="C:\\bad\\..\\escape")
        self.assertEqual(result.reason, "path_invalid")
        self.assertEqual(result.detail, "runtime_root_contract_rejected")
        self.assertNotIn("escape", result.detail)

    def test_a_rejected_job_spec_value_is_not_echoed(self):
        spec = dict(JOB_SPEC, workdir="/mnt/c/Users/tester/secret-client")
        result = _run(FakeOps(), job_spec=spec)
        self.assertEqual(result.detail, "contract_rejected")
        self.assertNotIn("secret-client", result.detail)

    def test_every_receipt_detail_in_the_suite_is_a_safe_token(self):
        pattern = re.compile(r"^[a-z0-9_]*$")
        for setup in (
            lambda ops: ops.fs_errors.__setitem__("remove_tree", OSError("busy")),
            lambda ops: ops.canary_overrides.__setitem__(wr.CANARY_INTEROP, b"x\n"),
            lambda ops: ops.command_returncodes.__setitem__("--import", 9),
            lambda ops: setattr(ops, "job_result_override",
                                _ok(["wsl.exe", "-d"], returncode=4)),
            lambda ops: ops.fs_errors.__setitem__("open_read", OSError(13, "denied", SOURCE_PATH)),
        ):
            ops = FakeOps()
            setup(ops)
            _run(ops)
            receipt = ops.json.get(RECEIPT_PATH)
            if receipt is None:
                continue
            with self.subTest(detail=receipt["detail"]):
                self.assertTrue(pattern.match(receipt["detail"]), receipt["detail"])
                self.assertNotIn(SOURCE_PATH, repr(receipt))


class RegistrationBindingTests(unittest.TestCase):
    """Finding 4: a reused distro name never authorizes an unregister."""

    def test_binding_holds_for_an_intact_instance(self):
        ops = FakeOps()
        _seed_record(ops)
        parsed = wr.parse_owner_record(
            ops.json[wr.owner_record_path(RUNTIME_ROOT, DISTRO)],
            runtime_root=RUNTIME_ROOT)
        self.assertEqual(wr.verify_registration_binding(ops, parsed), "")

    def test_job_directory_identity_drift_refuses(self):
        ops = FakeOps()
        _seed_record(ops)
        ops.nodes[FakeOps._key(wr.job_directory(RUNTIME_ROOT, DISTRO))].ino = 31337
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+job_dir_identity_drift")
        self.assertEqual(ops.verbs(), [])

    def test_install_directory_identity_drift_refuses(self):
        ops = FakeOps()
        _seed_record(ops)
        ops.nodes[FakeOps._key(
            wr.job_directory(RUNTIME_ROOT, DISTRO) + "\\distro")].ino = 31337
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+install_dir_identity_drift")
        self.assertEqual(ops.verbs(), [])

    def test_binding_breaking_at_the_last_moment_refuses_every_step(self):
        """The precheck runs immediately before the first destructive command."""
        ops = FakeOps()
        path = _seed_record(ops)
        job_dir = wr.job_directory(RUNTIME_ROOT, DISTRO)
        original = ops.lstat
        seen = {"n": 0}

        def drifting_lstat(target):
            result = original(target)
            if ops._key(target) == ops._key(job_dir):
                seen["n"] += 1
                if seen["n"] >= 3:
                    ops.nodes[ops._key(job_dir)].ino = 90909
            return result

        ops.lstat = drifting_lstat
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+registration_unverifiable")
        self.assertEqual(ops.verbs(), [])
        self.assertTrue(ops.exists(job_dir))
        self.assertIn(path, ops.json)

    def test_a_refusing_precheck_blocks_deletion_as_well_as_unregister(self):
        ops = FakeOps()
        report = wr._cleanup(
            ops, distro_name=DISTRO, job_dir=JOB_DIR, record_path=RECORD_PATH,
            limits=wr.DEFAULT_LIMITS, registration=wr.REGISTRATION_CREATED,
            job_dir_created=True, record_written=True,
            precheck=lambda: "registration_unverifiable")
        self.assertFalse(report.complete)
        for value in (report.terminate, report.unregister,
                      report.filesystem, report.owner_record):
            self.assertTrue(value.startswith("refused+"), value)
        self.assertEqual(ops.verbs(), [])

    def test_a_raising_precheck_is_a_refusal(self):
        ops = FakeOps()
        def boom():
            raise OSError("cannot stat")
        report = wr._cleanup(
            ops, distro_name=DISTRO, job_dir=JOB_DIR, record_path=RECORD_PATH,
            limits=wr.DEFAULT_LIMITS, registration=wr.REGISTRATION_CREATED,
            job_dir_created=True, record_written=True, precheck=boom)
        self.assertEqual(report.terminate, "refused+precheck_oserror")
        self.assertEqual(ops.verbs(), [])

    def test_the_live_path_has_no_precheck_and_still_tears_down(self):
        """The live path created the registration itself under the lock, so it
        does not need the record to prove ownership, and must never refuse."""
        ops = FakeOps()
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_COMPLETED, result.detail)
        self.assertEqual(result.cleanup.terminate, "ok")
        self.assertEqual(result.cleanup.unregister, "ok")

    def test_record_without_the_install_binding_is_rejected(self):
        ops = FakeOps()
        path = _seed_record(ops)
        del ops.json[path]["install_dir_identity"]
        report = _reap(ops)
        self.assertEqual(report.outcomes[DISTRO], "skipped+owner_record_invalid")
        self.assertEqual(ops.verbs(), [])


class DocumentedLimitationTests(unittest.TestCase):
    """Finding 5: the residual race is encoded, not claimed closed."""

    def test_the_private_copy_race_is_documented_as_open(self):
        text = wr.PRIVATE_COPY_RACE_LIMITATION
        self.assertIn("narrowed, not closed", text)
        self.assertIn("same-user", text)
        self.assertIn("Known open windows", wr.__doc__)
        self.assertIn("PRIVATE_COPY_RACE_LIMITATION", wr.__doc__)

    def test_the_wsl_registration_identity_gap_is_documented(self):
        text = wr.WSL_REGISTRATION_IDENTITY_LIMITATION
        self.assertIn("no stable per-registration identity", text)
        self.assertIn("WSL_REGISTRATION_IDENTITY_LIMITATION", wr.__doc__)

    def test_the_registration_proof_race_is_documented_as_narrowed(self):
        text = wr.REGISTRATION_PROOF_LIMITATION
        self.assertIn("not an atomic create-if-absent", text)
        self.assertIn("same-user", text)
        self.assertIn("REGISTRATION_PROOF_LIMITATION", wr.__doc__)

    def test_the_module_makes_no_closure_claim(self):
        doc = wr.__doc__
        for overclaim in ("race is closed", "cannot be raced",
                          "validated on Windows", "verified on a live"):
            self.assertNotIn(overclaim, doc)
        self.assertIn("has been validated on a live Windows host", doc)

    def test_revalidation_is_described_as_narrowing_not_closing(self):
        self.assertIn("narrowed to the interval", wr.__doc__)
        self.assertIn(
            "narrows the window; it does not\n  close it", wr.__doc__)


if __name__ == "__main__":
    unittest.main()


class RegistrationProofTests(unittest.TestCase):
    """A distro name is not a capability.

    ``wsl.exe --unregister <name>`` destroys whatever currently answers to
    that name. Issuing it after a failed or ambiguous import would let this
    runtime delete an instance it never created, so every teardown of a
    registration has to rest on proof that this run is what put the name
    there: absent immediately before, present immediately after.
    """

    def test_an_already_registered_name_aborts_before_the_import(self):
        ops = FakeOps()
        ops.registered.add(DISTRO)
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, "registration_precondition_failed")
        self.assertEqual(result.detail, "name_already_registered")
        self.assertNotIn("--import", ops.verbs())

    def test_a_stranger_owning_the_name_is_never_terminated_or_unregistered(self):
        ops = FakeOps()
        ops.registered.add(DISTRO)
        result = _run(ops)
        self.assertNotIn("--terminate", ops.verbs())
        self.assertNotIn("--unregister", ops.verbs())
        self.assertEqual(ops.registered, {DISTRO})
        self.assertEqual(result.cleanup.terminate, "not_attempted")
        self.assertEqual(result.cleanup.unregister, "not_attempted")

    def test_nothing_this_run_created_is_left_behind_after_the_refusal(self):
        """Refusing to touch a stranger's registration is not a reason to
        leak this run's own directory: it created one, so it removes it."""
        ops = FakeOps()
        ops.registered.add(DISTRO)
        result = _run(ops)
        self.assertEqual(result.cleanup.filesystem, "ok")
        self.assertEqual(result.cleanup.owner_record, "ok")
        self.assertFalse(ops.exists(JOB_DIR))
        self.assertNotIn(RECORD_PATH, ops.json)

    def test_an_unusable_listing_aborts_before_the_import(self):
        ops = FakeOps()
        ops.list_result_override = _ok(list(wr.WSL_LIST_ARGV), returncode=1)
        result = _run(ops)
        self.assertEqual(result.reason, "registration_precondition_failed")
        self.assertEqual(result.detail, "registration_listing_unusable")
        self.assertNotIn("--import", ops.verbs())
        self.assertNotIn("--unregister", ops.verbs())

    def test_a_listing_that_will_not_decode_is_not_read_as_absent(self):
        ops = FakeOps()
        ops.list_result_override = _ok(
            list(wr.WSL_LIST_ARGV), stdout=b"\x00\xd8\x00\x00\x41")
        result = _run(ops)
        self.assertEqual(result.detail, "registration_listing_unusable")
        self.assertNotIn("--import", ops.verbs())

    def test_a_probe_that_raises_is_not_read_as_absent(self):
        ops = FakeOps()
        ops.command_errors["--list"] = OSError("wsl.exe unavailable")
        result = _run(ops)
        self.assertEqual(result.detail, "registration_listing_unusable")
        self.assertNotIn("--import", ops.verbs())

    def test_a_failed_import_that_registered_nothing_is_not_unregistered(self):
        ops = FakeOps()
        ops.import_registers = False
        ops.command_returncodes["--import"] = 1
        result = _run(ops)
        self.assertEqual(result.reason, "import_failed")
        self.assertEqual(result.detail, "exit_1")
        self.assertEqual(result.cleanup.terminate, "not_attempted")
        self.assertEqual(result.cleanup.unregister, "not_attempted")
        self.assertNotIn("--terminate", ops.verbs())
        self.assertNotIn("--unregister", ops.verbs())

    def test_a_failed_import_that_did_register_is_torn_down(self):
        """The other side of the proof: absent before, present after, so it
        is ours and must not be left behind."""
        ops = FakeOps()
        ops.command_returncodes["--import"] = 1
        result = _run(ops)
        self.assertEqual(result.reason, "import_failed")
        self.assertIn("--terminate", ops.verbs())
        self.assertIn("--unregister", ops.verbs())
        self.assertEqual(ops.registered, set())
        self.assertEqual(result.cleanup.unregister, "ok")

    def test_an_import_spawn_failure_never_unregisters_by_name(self):
        ops = FakeOps()
        ops.import_result_override = _ok(
            list(wr.WSL_LIST_ARGV), returncode=None, spawn_failed=True)
        ops.import_registers = False
        result = _run(ops)
        self.assertEqual(result.reason, "import_failed")
        self.assertEqual(result.detail, "spawn_failed")
        self.assertNotIn("--unregister", ops.verbs())

    def test_an_import_call_that_raises_never_unregisters_by_name(self):
        ops = FakeOps()
        ops.command_errors["--import"] = OSError("CreateProcess failed")
        result = _run(ops)
        self.assertEqual(result.reason, "import_failed")
        self.assertEqual(result.detail, "spawn_oserror")
        self.assertNotIn("--unregister", ops.verbs())
        self.assertTrue(result.spawned)

    def test_an_import_that_raises_after_registering_is_still_torn_down(self):
        ops = FakeOps()
        original = ops.run_host

        def raising(argv, **kwargs):
            if list(argv)[1] == "--import":
                ops.registered.add(list(argv)[2])
                raise OSError("pipe broke after the child started")
            return original(argv, **kwargs)

        ops.run_host = raising
        result = _run(ops)
        self.assertEqual(result.reason, "import_failed")
        self.assertIn("--unregister", ops.verbs())
        self.assertEqual(ops.registered, set())

    def test_an_ambiguous_import_with_an_unusable_listing_touches_nothing(self):
        ops = FakeOps()
        ops.import_result_override = _ok(
            list(wr.WSL_LIST_ARGV), returncode=None, timed_out=True)
        # Absent before the import, then no usable answer afterwards.
        ops.list_results = [
            _ok(list(wr.WSL_LIST_ARGV), stdout=b""),
            _ok(list(wr.WSL_LIST_ARGV), returncode=1),
        ]
        result = _run(ops)
        self.assertEqual(result.cleanup.terminate, "skipped+registration_unproven")
        self.assertEqual(result.cleanup.unregister, "skipped+registration_unproven")
        self.assertNotIn("--terminate", ops.verbs())
        self.assertNotIn("--unregister", ops.verbs())

    def test_an_unproven_registration_keeps_every_piece_of_evidence(self):
        ops = FakeOps()
        ops.import_result_override = _ok(
            list(wr.WSL_LIST_ARGV), returncode=None, timed_out=True)
        ops.list_results = [
            _ok(list(wr.WSL_LIST_ARGV), stdout=b""),
            _ok(list(wr.WSL_LIST_ARGV), returncode=1),
        ]
        result = _run(ops)
        self.assertEqual(result.cleanup.filesystem, "retained+registration_not_proven")
        self.assertEqual(result.cleanup.owner_record, "retained+containment_not_proven")
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)
        self.assertTrue(ops.exists(JOB_DIR))
        self.assertTrue(ops.exists(INSTALL_DIR))
        self.assertIn(RECORD_PATH, ops.json)

    def test_the_receipt_records_what_was_believed_about_the_registration(self):
        ops = FakeOps()
        self.assertEqual(_run(ops).status, wr.STATUS_COMPLETED)
        self.assertEqual(ops.json[RECEIPT_PATH]["registration"], "created")

        refused = FakeOps()
        refused.registered.add(DISTRO)
        _run(refused)
        self.assertEqual(refused.json[RECEIPT_PATH]["registration"], "none")

    def test_the_cleanup_helper_rejects_an_unknown_registration_state(self):
        with self.assertRaises(ValueError):
            wr._cleanup(
                FakeOps(), distro_name=DISTRO, job_dir=JOB_DIR,
                record_path=RECORD_PATH, limits=wr.DEFAULT_LIMITS,
                registration="probably-fine", job_dir_created=False,
                record_written=False)


class RegistrationRetentionTests(unittest.TestCase):
    """Incomplete registration cleanup must leave the evidence in place.

    The reaper re-proves ownership from the job and install directory
    identities recorded in the owner record. Deleting those while a
    registration may still exist would turn a recoverable leak into a
    permanent one: nothing could ever prove the leftover distro was ours.
    """

    def _run_with_failing_unregister(self):
        ops = FakeOps()
        ops.command_returncodes["--unregister"] = 1
        return ops, _run(ops)

    def test_a_failed_unregister_retains_the_directories_and_the_record(self):
        ops, result = self._run_with_failing_unregister()
        self.assertEqual(result.cleanup.unregister, "failed+exit_1")
        self.assertEqual(result.cleanup.filesystem, "retained+registration_not_proven")
        self.assertEqual(result.cleanup.owner_record, "retained+containment_not_proven")
        self.assertTrue(ops.exists(JOB_DIR))
        self.assertTrue(ops.exists(INSTALL_DIR))
        self.assertIn(RECORD_PATH, ops.json)
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)

    def test_the_retained_instance_is_reapable_on_a_later_pass(self):
        """The point of retaining it: a later reap can finish the job."""
        ops, _ = self._run_with_failing_unregister()
        self.assertIn(DISTRO, ops.registered)
        ops.command_returncodes.pop("--unregister")
        # The owning process is gone: exited, and no longer answering with
        # the creation time the record pinned it to.
        ops.liveness[ops.pid] = EXITED
        ops.identities[ops.pid] = "a-different-process"
        report = wr.reap_stale_instances(
            runtime_root=RUNTIME_ROOT, max_age=timedelta(seconds=0.0001),
            ops=ops, platform_name="win32",
            now=datetime(2026, 3, 1, 18, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(report.reaped, [DISTRO])
        self.assertEqual(ops.registered, set())
        self.assertFalse(ops.exists(JOB_DIR))
        self.assertNotIn(RECORD_PATH, ops.json)

    def test_a_zero_exit_unregister_is_not_trusted_without_a_read_back(self):
        ops = FakeOps()
        ops.unregister_removes = False
        result = _run(ops)
        self.assertEqual(result.cleanup.unregister, "failed+still_registered")
        self.assertEqual(result.cleanup.filesystem, "retained+registration_not_proven")
        self.assertTrue(ops.exists(JOB_DIR))

    def test_an_unverifiable_read_back_is_not_trusted_either(self):
        ops = FakeOps()
        ops.list_results = [
            _ok(list(wr.WSL_LIST_ARGV), stdout=b""),          # pre-import
            _ok(list(wr.WSL_LIST_ARGV), returncode=1),        # post-unregister
        ]
        result = _run(ops)
        self.assertEqual(result.cleanup.unregister, "failed+unverified")
        self.assertEqual(result.cleanup.filesystem, "retained+registration_not_proven")
        self.assertTrue(ops.exists(JOB_DIR))

    def test_a_terminate_failure_alone_does_not_strand_the_directory(self):
        """A successful, verified unregister means there is nothing left to
        reap, so holding the tree forever would leak disk for no benefit.
        The run is still aborted: containment was not clean."""
        ops = FakeOps()
        ops.command_errors["--terminate"] = OSError("wsl unavailable")
        result = _run(ops)
        self.assertTrue(result.cleanup.terminate.startswith("failed"))
        self.assertEqual(result.cleanup.unregister, "ok")
        self.assertEqual(result.cleanup.filesystem, "ok")
        self.assertFalse(ops.exists(JOB_DIR))
        self.assertEqual(result.reason, wr.REASON_CLEANUP_FAILED)

    def test_a_filesystem_failure_still_retains_the_owner_record(self):
        ops = FakeOps()
        ops.fs_errors["remove_tree"] = OSError("directory busy")
        result = _run(ops)
        self.assertTrue(result.cleanup.filesystem.startswith("failed"))
        self.assertEqual(result.cleanup.owner_record, "retained+containment_not_proven")
        self.assertIn(RECORD_PATH, ops.json)


class ReceiptOrderingTests(unittest.TestCase):
    """The receipt is keyed by distro name, so it belongs under the same
    lock that serialises the instance. Written after release, a second job
    reusing the token could overwrite or interleave with it."""

    def _events(self, ops):
        return [(event[0], event[1]) for event in ops.events
                if event[0] in ("lock", "unlock", "write_json")]

    def test_the_receipt_is_written_before_the_lock_is_released(self):
        ops = FakeOps()
        _run(ops)
        events = self._events(ops)
        self.assertIn(("write_json", RECEIPT_PATH), events)
        self.assertLess(events.index(("write_json", RECEIPT_PATH)),
                        events.index(("unlock", wr.lock_path(RUNTIME_ROOT))))

    def test_the_lock_is_still_held_at_the_moment_the_receipt_is_written(self):
        ops = FakeOps()
        depths = {}
        original = ops.write_json

        def recording(path, payload):
            depths[path] = ops.lock_depth
            return original(path, payload)

        ops.write_json = recording
        _run(ops)
        self.assertEqual(depths[RECEIPT_PATH], 1)

    def test_a_second_job_cannot_reach_the_receipt_while_the_first_holds_it(self):
        """A real exclusive lock, and a competing job with the same token
        launched at the instant the first is writing its receipt."""
        ops = FakeOps()
        ops.exclusive_lock = True
        original = ops.write_json
        competitor = {}

        def recording(path, payload):
            if path == RECEIPT_PATH and "result" not in competitor:
                competitor["result"] = _run(ops)
            return original(path, payload)

        ops.write_json = recording
        first = _run(ops)
        self.assertEqual(first.status, wr.STATUS_COMPLETED)
        self.assertEqual(competitor["result"].reason, "lock_unavailable")
        self.assertEqual(ops.json[RECEIPT_PATH]["status"], wr.STATUS_COMPLETED)
        self.assertEqual(ops.json[RECEIPT_PATH]["reason"], wr.REASON_OK)

    def test_a_receipt_write_failure_is_still_reported_to_the_caller(self):
        ops = FakeOps()
        ops.json_write_errors[RECEIPT_PATH] = OSError("disk full")
        result = _run(ops)
        self.assertEqual(result.status, wr.STATUS_ABORTED)
        self.assertEqual(result.reason, wr.REASON_RECEIPT_WRITE_FAILED)
        self.assertEqual(result.detail, "OSError")
