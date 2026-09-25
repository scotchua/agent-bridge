"""codex_promotion: execution-lane admission under codex-bridge's CLI promotion.

Offline only. Every test builds its own promotion directory and slot under a
temporary directory; none reads the operator's real ~/.codex-bridge.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import codex_promotion as cp
from agent_bridge.execution import codex_task

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "codex_promotion_vector.json").read_text())

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
try:
    import pwd
except ImportError:  # pragma: no cover
    pwd = None


def build_tree(root: Path, tree):
    """Create the vector's tree under ``root`` with its (read-only) modes."""
    for e in tree:
        p = root / e["path"]
        if e["type"] == "d":
            p.mkdir()
        elif e["type"] == "f":
            p.write_text(e["content"])
    for e in tree:
        if e["type"] == "l":
            os.symlink(e["target"], root / e["path"])
    for e in reversed(tree):
        if e["type"] != "l":
            os.chmod(root / e["path"], int(e["mode"], 8))


def make_writable(root: Path):
    if not root.exists():
        return
    os.chmod(root, 0o755)
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                os.chmod(p, 0o755)
        for n in filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                os.chmod(p, 0o644)


def independent_digest(tree) -> str:
    """The digest rule restated from the spec, without walking a filesystem."""
    lines = []
    for e in tree:
        if e["type"] == "f":
            lines.append(("f", e["path"], e["mode"], hashlib.sha256(e["content"].encode()).hexdigest()))
        elif e["type"] == "d":
            lines.append(("d", e["path"], e["mode"]))
        else:
            lines.append(("l", e["path"], e["target"]))
    lines.sort(key=lambda x: x[1].encode("utf-8"))
    h = hashlib.sha256()
    for x in lines:
        h.update("\0".join(x).encode("utf-8") + b"\n")
    return h.hexdigest()


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def new_slot(self, name="slot", read_only_root=True):
        slot = self.root / name
        slot.mkdir(parents=True)
        build_tree(slot, VECTOR["tree"])
        if read_only_root:
            os.chmod(slot, 0o555)
        self.addCleanup(make_writable, slot)
        return slot


class DigestVector(TmpCase):
    def test_vector_matches_an_independent_restatement_of_the_rule(self):
        self.assertEqual(independent_digest(VECTOR["tree"]), VECTOR["expected_tree_digest"])

    def test_filesystem_walk_reproduces_the_vector(self):
        self.assertEqual(cp.tree_digest(self.new_slot()), VECTOR["expected_tree_digest"])

    def test_adding_a_write_bit_changes_the_digest(self):
        slot = self.new_slot()
        os.chmod(slot / "node_modules/@openai/codex/package.json", 0o644)
        self.assertNotEqual(cp.tree_digest(slot), VECTOR["expected_tree_digest"])

    def test_record_id_vector(self):
        self.assertEqual(cp.record_id_of(VECTOR["record"]), VECTOR["expected_record_id"])
        with_id = {**VECTOR["record"], "record_id": "f" * 64}
        self.assertEqual(cp.record_id_of(with_id), VECTOR["expected_record_id"])


class DigestDetects(TmpCase):
    def setUp(self):
        super().setUp()
        self.slot = self.new_slot(read_only_root=False)
        make_writable(self.slot)
        self.base = cp.tree_digest(self.slot)

    def test_a_changed_nested_native_binary(self):
        p = self.slot / "node_modules/@openai/codex-darwin-arm64/codex"
        st = os.stat(self.slot)
        p.write_text("different bytes\n")
        os.utime(self.slot, ns=(st.st_atime_ns, st.st_mtime_ns))  # root metadata unchanged
        self.assertNotEqual(cp.tree_digest(self.slot), self.base)

    def test_a_changed_execute_bit(self):
        os.chmod(self.slot / "node_modules/@openai/codex/package.json", 0o755)
        self.assertNotEqual(cp.tree_digest(self.slot), self.base)

    def test_an_added_file(self):
        (self.slot / "node_modules/extra").write_text("x")
        self.assertNotEqual(cp.tree_digest(self.slot), self.base)

    def test_an_escaping_symlink_is_refused(self):
        os.symlink("../../outside", self.slot / "node_modules/escape")
        with self.assertRaisesRegex(cp.AdmissionRefused, "escapes"):
            cp.tree_digest(self.slot)

    def test_an_absolute_symlink_is_refused(self):
        os.symlink(str(self.slot / "node_modules/@openai/codex/bin/codex.js"), self.slot / "node_modules/abs")
        with self.assertRaisesRegex(cp.AdmissionRefused, "absolute"):
            cp.tree_digest(self.slot)

    def test_a_dangling_symlink_is_refused(self):
        os.symlink("missing", self.slot / "node_modules/dangling")
        with self.assertRaisesRegex(cp.AdmissionRefused, "escapes"):
            cp.tree_digest(self.slot)

    def test_a_fifo_is_refused(self):
        os.mkfifo(self.slot / "node_modules/fifo")
        with self.assertRaisesRegex(cp.AdmissionRefused, "unsupported file type"):
            cp.tree_digest(self.slot)


class Fixture:
    """An account home with a promotion directory, a read-only slot, the three
    managed launchers of spec v5 section 1, and a node stand-in."""

    def __init__(self, case: TmpCase):
        root = case.root
        self.root = root
        self.home = root / "home"; self.home.mkdir()
        self.pdir = self.home / ".codex-bridge" / "promotion"; self.pdir.mkdir(mode=0o700, parents=True)
        self.lock = self.pdir / "admission.lock"
        fd = os.open(self.lock, os.O_CREAT | os.O_WRONLY, 0o600); os.close(fd)
        self.slot = case.new_slot("home/.codex-cli/slots/0.156.1-aaaaaaaaaaaa")
        self.target = self.slot / "node_modules/@openai/codex/bin/codex.js"
        self.launcher = self._link(self.home / ".local/bin/codex")
        self.nvm_launcher = self._link(self.home / ".nvm/versions/node/v24.16.0/bin/codex")
        self.peer_launcher = self._link(self.home / ".codex-cli/peer/codex")
        self.node = root / "node"; self.node.write_text("node stand-in\n")
        self.record = self.make_record()
        patcher = mock.patch.object(cp, "managed_home", return_value=self.home)
        patcher.start(); case.addCleanup(patcher.stop)

    def _link(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(self.target), path)
        return path

    def node_entry(self):
        return {"path": os.path.realpath(self.node),
                "sha256": hashlib.sha256(self.node.read_bytes()).hexdigest(), "version": "v24.16.0"}

    def make_record(self, **overrides):
        record = {
            "schema_version": 1, "predecessor_id": None, "version": "0.156.1",
            "slot_path": str(self.slot), "slot_tree_digest": cp.tree_digest(self.slot),
            "launchers": [
                {"name": name, "path": str(path), "target": str(self.target), "node": self.node_entry()}
                for name, path in (("local", self.launcher), ("nvm", self.nvm_launcher),
                                   ("peer", self.peer_launcher))],
            "canary": {"harness_version": "1"}, "promoted_at": "2026-09-24T00:00:00Z",
            "promoted_by": "bootstrap", "transaction_id": "tx-1"}
        record.update(overrides)
        record["record_id"] = cp.record_id_of(record)
        return record

    def write_record(self, record=None, raw=None):
        p = self.pdir / "promoted-cli.json"
        p.write_bytes(raw if raw is not None else json.dumps(record or self.record).encode())
        os.chmod(p, 0o600)

    def write_gate(self, raw=None):
        p = self.pdir / "gate.json"
        p.write_bytes(raw if raw is not None else json.dumps(
            {"transaction_id": "tx-9", "state": "opened", "opened_at": "2026-09-24T00:00:00Z",
             "controller_pid": 1}).encode())
        os.chmod(p, 0o600)

    def admit(self, launcher=None, node=None):
        return cp.admit(launcher or self.launcher, promotion_dir=self.pdir, node_bin=str(node or self.node))


@unittest.skipIf(fcntl is None, "POSIX file locks required")
class Admission(TmpCase):
    def setUp(self):
        super().setUp()
        self.f = Fixture(self)

    def refused(self, pattern, launcher=None, node=None):
        with self.assertRaisesRegex(cp.AdmissionRefused, pattern):
            with self.f.admit(launcher, node):
                self.fail("admitted")

    def assert_exclusive_blocked(self):
        probe = os.open(self.f.lock, os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    # -- transition

    def test_absent_directory_is_created_and_the_lock_is_taken(self):
        pdir = self.root / "fresh-home" / ".codex-bridge" / "promotion"
        with cp.admit(self.f.launcher, promotion_dir=pdir, node_bin=None) as a:
            self.assertEqual(a.state, "no_record")
            self.assertEqual(a.exec_path, self.f.launcher)
            self.assertEqual(oct(os.stat(pdir).st_mode & 0o777), oct(0o700))
            probe = os.open(pdir / "admission.lock", os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)

    def test_task_started_before_bootstrap_blocks_its_drain(self):
        """Finding 1: admission before the directory exists still holds the lock bootstrap drains."""
        pdir = self.root / "fresh" / "promotion"
        entered, release = threading.Event(), threading.Event()
        def job():
            with cp.admit(self.f.launcher, promotion_dir=pdir, node_bin=None):
                entered.set(); release.wait(10)
        t = threading.Thread(target=job); t.start(); entered.wait(10)
        # Bootstrap: opens the same lock (never replacing it), writes the gate, drains.
        fd = os.open(pdir / "admission.lock", os.O_RDONLY | os.O_CREAT, 0o600)
        try:
            (pdir / "gate.json").write_text(json.dumps(
                {"transaction_id": "boot", "state": "opened", "opened_at": "x", "controller_pid": 1}))
            os.chmod(pdir / "gate.json", 0o600)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.set(); t.join(10)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_missing_lock_is_created_not_refused(self):
        os.unlink(self.f.lock)
        with self.f.admit() as a:
            self.assertEqual(a.state, "no_record")
        self.assertTrue(self.f.lock.is_file())

    def test_no_environment_override_exists(self):
        """Finding 2: a worker environment cannot move admission away from the real gate."""
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_CODEX_PROMOTION_DIR": str(self.root / "absent")}):
            self.assertEqual(cp.default_promotion_dir(), self.f.home / ".codex-bridge" / "promotion")
        self.assertFalse(hasattr(cp, "PROMOTION_DIR_ENV"))

    # -- gate and lock

    def test_no_record_admits_but_still_honours_the_gate(self):
        with self.f.admit() as a:
            self.assertEqual(a.state, "no_record")
        self.f.write_gate()
        self.refused(r"maintenance in progress \(transaction tx-9\)")

    def test_gate_refuses_even_with_a_valid_record(self):
        self.f.write_record(); self.f.write_gate()
        self.refused("maintenance in progress")

    def test_malformed_gate_fails_closed(self):
        self.f.write_gate(raw=b"{not json")
        self.refused("gate is unreadable")
        self.f.write_gate(raw=b'{"transaction_id": "x"}')
        self.refused("gate is malformed")

    def test_a_symlinked_gate_fails_closed(self):
        other = self.root / "elsewhere.json"; other.write_text("{}")
        os.symlink(other, self.f.pdir / "gate.json")
        self.refused("gate is unreadable")

    def test_symlinked_lock_fails_closed(self):
        os.unlink(self.f.lock)
        real = self.root / "real.lock"; real.write_text("")
        os.symlink(real, self.f.lock)
        self.refused("not a regular file")

    def test_directory_open_to_others_is_refused(self):
        os.chmod(self.f.pdir, 0o755)
        self.refused("readable or writable by others")

    def test_exclusive_holder_refuses_without_waiting(self):
        fd = os.open(self.f.lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            start = time.monotonic()
            self.refused(r"admission lock held")
            self.assertLess(time.monotonic() - start, 2)
        finally:
            os.close(fd)

    def test_shared_lock_is_held_for_the_body_and_released_after(self):
        with self.f.admit():
            self.assert_exclusive_blocked()
        probe = os.open(self.f.lock, os.O_RDONLY)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released: succeeds
        finally:
            os.close(probe)

    def test_lock_is_released_when_the_body_raises(self):
        with self.assertRaises(ZeroDivisionError):
            with self.f.admit():
                1 / 0
        probe = os.open(self.f.lock, os.O_RDONLY)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    def test_controller_drain_waits_for_a_running_job(self):
        """The controller's ordering: write the gate, then LOCK_EX to drain."""
        entered, release = threading.Event(), threading.Event()
        def job():
            with self.f.admit():
                entered.set(); release.wait(10)
        t = threading.Thread(target=job); t.start(); entered.wait(10)
        self.f.write_gate()
        fd = os.open(self.f.lock, os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # job still holds it
            self.refused("maintenance in progress")  # a new job sees the gate
            release.set(); t.join(10)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # drained
        finally:
            os.close(fd)

    # -- record

    def test_promoted_record_admits_and_returns_the_resolved_target(self):
        self.f.write_record()
        for launcher in (self.f.launcher, self.f.nvm_launcher, self.f.peer_launcher):
            with self.f.admit(launcher) as a:
                self.assertEqual(a.state, "promoted")
                self.assertEqual(a.exec_path, Path(os.path.realpath(self.f.target)))
                self.assertEqual(a.record_id, self.f.record["record_id"])

    def test_record_with_a_bad_id_is_refused(self):
        self.f.write_record({**self.f.record, "version": "0.157.0"})
        self.refused("record id does not match")

    def test_record_with_an_unknown_field_is_refused(self):
        r = {**self.f.record, "extra": 1}; r["record_id"] = cp.record_id_of(r)
        self.f.write_record(r)
        self.refused("wrong fields")

    def test_record_with_a_float_is_refused(self):
        self.f.write_record(raw=json.dumps({**self.f.record, "schema_version": 1.0}).encode())
        self.refused("does not parse")

    def test_prerelease_version_is_refused(self):
        self.f.write_record(self.f.make_record(version="0.157.0-alpha.1"))
        self.refused("stable x.y.z")

    def test_record_version_must_match_the_slot_package(self):
        self.f.write_record(self.f.make_record(version="0.156.2"))
        self.refused("package version differs")

    def test_writable_slot_root_is_refused(self):
        self.f.write_record()
        os.chmod(self.f.slot, 0o755)
        self.refused("slot is writable")

    def test_changed_slot_is_refused(self):
        self.f.write_record()
        p = self.f.slot / "node_modules/@openai/codex-darwin-arm64/codex"
        os.chmod(p, 0o755); p.write_text("swapped\n"); os.chmod(p, 0o555)
        self.refused("no longer matches its digest")

    def test_mismatched_other_launcher_refuses_this_task(self):
        """Finding 3: a half-switched pair refuses even a task using the good launcher."""
        self.f.write_record()
        os.unlink(self.f.nvm_launcher); os.symlink(str(self.f.node), self.f.nvm_launcher)
        self.refused("managed launcher nvm does not point at the promoted slot")

    def test_missing_other_launcher_refuses(self):
        self.f.write_record()
        os.unlink(self.f.nvm_launcher)
        self.refused("managed launcher nvm is not a symlink")

    def test_launcher_pointing_elsewhere_is_refused(self):
        self.f.write_record()
        os.unlink(self.f.launcher); os.symlink(str(self.f.node), self.f.launcher)
        self.refused("managed launcher local does not point at the promoted slot")

    def test_changed_node_is_refused(self):
        self.f.write_record()
        self.f.node.write_text("a different node\n")
        self.refused("node runtime for launcher local changed")

    def test_task_path_node_must_be_the_recorded_one(self):
        self.f.write_record()
        other = self.root / "node2"; other.write_bytes(self.f.node.read_bytes())
        self.refused("differs from the one the canaries ran", node=other)

    def test_unrecorded_binary_is_refused_once_a_record_exists(self):
        """Re-review finding 1: nothing the record does not name runs on a real task."""
        self.f.write_record()
        fake = self.root / "fake-codex"; fake.write_text("#!/bin/sh\n"); os.chmod(fake, 0o755)
        self.refused("not a launcher named in the promotion record", launcher=fake)
        other = self.new_slot("home/.codex-cli/slots/0.157.0-bbbbbbbbbbbb")
        self.refused("not a launcher named in the promotion record",
                     launcher=other / "node_modules/@openai/codex/bin/codex.js")
        self.refused("not a launcher named in the promotion record", launcher=self.f.target)

    def test_unrecorded_binary_still_runs_before_any_record(self):
        fake = self.root / "fake-codex"; fake.write_text("#!/bin/sh\n"); os.chmod(fake, 0o755)
        with self.f.admit(fake) as a:
            self.assertEqual((a.state, a.exec_path), ("no_record", fake))
            self.assert_exclusive_blocked()

    def test_record_omitting_a_launcher_is_refused(self):
        """Re-review finding 3: a record naming only good launchers cannot hide a bad one."""
        os.unlink(self.f.nvm_launcher); os.symlink(str(self.f.node), self.f.nvm_launcher)
        good = [e for e in self.f.record["launchers"] if e["name"] != "nvm"]
        self.f.write_record(self.f.make_record(launchers=good))
        self.refused("exactly the local, nvm and peer launchers")

    def test_record_with_a_duplicate_or_unknown_launcher_is_refused(self):
        ls = self.f.record["launchers"]
        for launchers in (ls + [ls[0]], ls[:2] + [{**ls[2], "name": "extra"}]):
            with self.subTest(names=[e["name"] for e in launchers]):
                self.f.write_record(self.f.make_record(launchers=launchers))
                self.refused("exactly the local, nvm and peer launchers")

    def test_launchers_must_be_at_their_spec_paths(self):
        elsewhere = self.root / "elsewhere" / "bin" / "codex"; elsewhere.parent.mkdir(parents=True)
        os.symlink(str(self.f.target), elsewhere)
        for name in ("local", "nvm", "peer"):
            with self.subTest(name=name):
                launchers = [{**e, "path": str(elsewhere)} if e["name"] == name else e
                             for e in self.f.record["launchers"]]
                self.f.write_record(self.f.make_record(launchers=launchers))
                self.refused(f"{name} launcher path is wrong")

    def test_reported_version_must_match_the_record(self):
        self.f.write_record()
        with self.f.admit() as a:
            a.check_reported_version("codex-cli 0.156.1\n")
            with self.assertRaisesRegex(cp.AdmissionRefused, "reports a version other than the promoted one"):
                a.check_reported_version("codex-cli 0.157.0\n")
        cp.Admission(state="no_record", exec_path=self.f.launcher).check_reported_version("anything")

    @unittest.skipIf(pwd is None, "password database required")
    def test_account_home_comes_from_the_password_database(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root / "other-home")}):
            self.assertEqual(cp.account_home(), Path(pwd.getpwuid(os.getuid()).pw_dir))

    def test_other_HOME_cannot_run_an_account_home_launcher(self):
        """Re-review finding 2: a worker with a stray HOME is refused before it takes a private lock."""
        account = self.root / "account"
        shared = account / ".local/bin/codex"; shared.parent.mkdir(parents=True)
        os.symlink(str(self.f.target), shared)
        pdir = self.root / "stray" / "promotion"
        with mock.patch.object(cp, "account_home", return_value=account):
            with self.assertRaisesRegex(cp.AdmissionRefused, "HOME is not the account home"):
                with cp.admit(shared, promotion_dir=pdir, node_bin=None):
                    self.fail("admitted")
            # A link outside the account home that resolves into it is refused too.
            via = self.root / "via-codex"; os.symlink(str(shared), via)
            os.unlink(shared); shared.write_text("#!/bin/sh\n"); os.chmod(shared, 0o755)
            with self.assertRaisesRegex(cp.AdmissionRefused, "HOME is not the account home"):
                with cp.admit(via, promotion_dir=pdir, node_bin=None):
                    self.fail("admitted")
        self.assertFalse(pdir.exists())
        # Same HOME and account home: no refusal on that ground.
        with mock.patch.object(cp, "account_home", return_value=self.f.home):
            with self.f.admit() as a:
                self.assertEqual(a.state, "no_record")


@unittest.skipIf(fcntl is None, "POSIX file locks required")
class TaskLaneAdmission(TmpCase):
    """run_task refuses before running anything and records admission when it proceeds."""

    def setUp(self):
        super().setUp()
        self.f = Fixture(self)

    def test_gate_refusal_happens_before_any_codex_process(self):
        self.f.write_gate()
        with mock.patch.object(codex_task, "_run", side_effect=AssertionError("ran a process")), \
             mock.patch.object(codex_task, "_run_admitted", side_effect=AssertionError("admitted")):
            with self.assertRaisesRegex(codex_task.TaskError, "Codex CLI admission refused: maintenance in progress"):
                codex_task.run_task(codex_bin=self.f.launcher, promotion_dir=self.f.pdir,
                                    brief=Path("/b"), repo=Path("/r"), task_root=Path("/t"),
                                    classification="synthetic")

    def test_default_directory_is_used_when_none_is_given(self):
        self.f.write_gate()
        with mock.patch.object(cp, "default_promotion_dir", return_value=self.f.pdir), \
             mock.patch.object(codex_task, "_run_admitted", side_effect=AssertionError("admitted")):
            with self.assertRaisesRegex(codex_task.TaskError, "maintenance in progress"):
                codex_task.run_task(codex_bin=self.f.launcher, classification="synthetic")

    def test_admitted_task_runs_the_verified_target_with_the_lock_held(self):
        self.f.write_record()
        seen = {}
        def fake(**kw):
            seen.update(kw)
            probe = os.open(self.f.lock, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            return {"status": "complete"}
        with mock.patch.object(codex_task.shutil, "which", return_value=str(self.f.node)), \
             mock.patch.object(codex_task, "_run_admitted", side_effect=fake):
            codex_task.run_task(codex_bin=self.f.launcher, promotion_dir=self.f.pdir, classification="synthetic")
        self.assertEqual(seen["codex_bin"], Path(os.path.realpath(self.f.target)))
        self.assertEqual(seen["admission"].state, "promoted")
        self.assertEqual(seen["admission"].receipt()["record_id"], self.f.record["record_id"])

    def test_wrong_reported_version_is_an_admission_refusal(self):
        self.f.write_record()
        def fake(**kw):
            kw["admission"].check_reported_version("codex-cli 0.157.0")
        with mock.patch.object(codex_task.shutil, "which", return_value=str(self.f.node)), \
             mock.patch.object(codex_task, "_run_admitted", side_effect=fake):
            with self.assertRaisesRegex(codex_task.TaskError, "admission refused: .*reports a version"):
                codex_task.run_task(codex_bin=self.f.launcher, promotion_dir=self.f.pdir, classification="synthetic")


if __name__ == "__main__":
    unittest.main()
