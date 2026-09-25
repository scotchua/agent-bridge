"""codex_promotion: execution-lane admission under codex-bridge's CLI promotion.

Offline only. Every test builds its own promotion directory and slot under a
temporary directory; none reads the operator's real ~/.codex-bridge.
"""
import contextlib
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


def build_tree(root: Path, tree):
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
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for n in filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                os.chmod(p, 0o644)


def independent_digest(tree) -> str:
    """The digest rule restated from the spec, without walking a filesystem."""
    lines = []
    for e in tree:
        if e["type"] == "f":
            mode = format(int(e["mode"], 8) & 0o7555, "04o")
            lines.append(("f", e["path"], mode, hashlib.sha256(e["content"].encode()).hexdigest()))
        elif e["type"] == "d":
            lines.append(("d", e["path"], format(int(e["mode"], 8) & 0o7555, "04o")))
        else:
            lines.append(("l", e["path"], e["target"]))
    lines.sort(key=lambda x: x[1].encode("utf-8"))
    h = hashlib.sha256()
    for x in lines:
        h.update("\0".join(x).encode("utf-8") + b"\n")
    return h.hexdigest()


class DigestVector(unittest.TestCase):
    def test_vector_matches_an_independent_restatement_of_the_rule(self):
        self.assertEqual(independent_digest(VECTOR["tree"]), VECTOR["expected_tree_digest"])

    def test_filesystem_walk_reproduces_the_vector(self):
        with tempfile.TemporaryDirectory() as t:
            slot = Path(t) / "slot"; slot.mkdir(); build_tree(slot, VECTOR["tree"])
            try:
                self.assertEqual(cp.tree_digest(slot), VECTOR["expected_tree_digest"])
            finally:
                make_writable(slot)

    def test_read_only_slot_has_the_same_digest(self):
        with tempfile.TemporaryDirectory() as t:
            slot = Path(t) / "slot"; slot.mkdir(); build_tree(slot, VECTOR["tree"])
            try:
                subprocess.run(["chmod", "-R", "a-w", str(slot)], check=True)
                self.assertEqual(cp.tree_digest(slot), VECTOR["expected_tree_digest"])
            finally:
                make_writable(slot)

    def test_record_id_vector(self):
        self.assertEqual(cp.record_id_of(VECTOR["record"]), VECTOR["expected_record_id"])
        with_id = {**VECTOR["record"], "record_id": "f" * 64}
        self.assertEqual(cp.record_id_of(with_id), VECTOR["expected_record_id"])


class DigestDetects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.slot = Path(self.tmp.name) / "slot"; self.slot.mkdir(); build_tree(self.slot, VECTOR["tree"])
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
    """A promotion directory, a slot, a launcher and a node stand-in, all under tmp."""

    def __init__(self, root: Path):
        self.root = root
        self.pdir = root / "promotion"; self.pdir.mkdir(mode=0o700)
        self.lock = self.pdir / "admission.lock"
        fd = os.open(self.lock, os.O_CREAT | os.O_WRONLY, 0o600); os.close(fd)
        self.slot = root / "slots" / "0.156.1-aaaaaaaaaaaa"; self.slot.mkdir(parents=True)
        build_tree(self.slot, VECTOR["tree"]); make_writable(self.slot)
        self.target = self.slot / "node_modules/@openai/codex/bin/codex.js"
        self.bin = root / "bin"; self.bin.mkdir()
        self.launcher = self.bin / "codex"; os.symlink(str(self.target), self.launcher)
        self.node = root / "node"; self.node.write_text("node stand-in\n")
        self.record = self.make_record()

    def make_record(self, **overrides):
        record = {
            "schema_version": 1, "predecessor_id": None, "version": "0.156.1",
            "slot_path": str(self.slot), "slot_tree_digest": cp.tree_digest(self.slot),
            "launchers": [{"name": "local", "path": str(self.launcher), "target": str(self.target),
                           "node": {"path": os.path.realpath(self.node),
                                    "sha256": hashlib.sha256(self.node.read_bytes()).hexdigest(),
                                    "version": "v24.16.0"}}],
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

    def admit(self, launcher=None):
        return cp.admit(launcher or self.launcher, promotion_dir=self.pdir, node_bin=str(self.node))


@unittest.skipIf(fcntl is None, "POSIX file locks required")
class Admission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name))

    def refused(self, pattern, launcher=None):
        with self.assertRaisesRegex(cp.AdmissionRefused, pattern):
            with self.f.admit(launcher):
                self.fail("admitted")

    def test_not_installed_admits_unchanged(self):
        with cp.admit(self.f.launcher, promotion_dir=self.f.root / "absent", node_bin=None) as a:
            self.assertEqual(a.state, "not_installed")
            self.assertEqual(a.exec_path, self.f.launcher)

    def test_no_record_admits_but_still_honours_the_gate(self):
        with self.f.admit() as a:
            self.assertEqual(a.state, "no_record")
        self.f.write_gate()
        self.refused(r"maintenance in progress \(transaction tx-9\)")

    def test_promoted_record_admits_and_returns_the_resolved_target(self):
        self.f.write_record()
        with self.f.admit() as a:
            self.assertEqual(a.state, "promoted")
            self.assertEqual(a.exec_path, Path(os.path.realpath(self.f.target)))
            self.assertEqual(a.record_id, self.f.record["record_id"])

    def test_gate_refuses_even_with_a_valid_record(self):
        self.f.write_record(); self.f.write_gate()
        self.refused("maintenance in progress")

    def test_malformed_gate_fails_closed(self):
        self.f.write_gate(raw=b"{not json")
        self.refused("gate is unreadable")
        self.f.write_gate(raw=b'{"transaction_id": "x"}')
        self.refused("gate is malformed")

    def test_a_symlinked_gate_fails_closed(self):
        other = self.f.root / "elsewhere.json"; other.write_text("{}")
        os.symlink(other, self.f.pdir / "gate.json")
        self.refused("gate is unreadable")

    def test_missing_lock_fails_closed(self):
        os.unlink(self.f.lock)
        self.refused("admission lock is missing")

    def test_symlinked_lock_fails_closed(self):
        os.unlink(self.f.lock)
        real = self.f.root / "real.lock"; real.write_text("")
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
        probe = os.open(self.f.lock, os.O_RDONLY)
        try:
            with self.f.admit():
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
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

    def test_launcher_not_in_record_is_refused(self):
        self.f.write_record()
        other = self.f.bin / "codex2"; os.symlink(str(self.f.target), other)
        self.refused("not a launcher named", launcher=other)

    def test_launcher_pointing_elsewhere_is_refused(self):
        self.f.write_record()
        os.unlink(self.f.launcher); os.symlink(str(self.f.node), self.f.launcher)
        self.refused("does not point at the promoted slot")

    def test_changed_slot_is_refused(self):
        self.f.write_record()
        (self.f.slot / "node_modules/@openai/codex-darwin-arm64/codex").write_text("swapped\n")
        self.refused("no longer matches its digest")

    def test_changed_node_is_refused(self):
        self.f.write_record()
        self.f.node.write_text("a different node\n")
        self.refused("node runtime changed")

    def test_other_node_path_is_refused(self):
        self.f.write_record()
        other = self.f.root / "node2"; other.write_bytes(self.f.node.read_bytes())
        with self.assertRaisesRegex(cp.AdmissionRefused, "differs from the one the canaries ran"):
            with cp.admit(self.f.launcher, promotion_dir=self.f.pdir, node_bin=str(other)):
                pass


@unittest.skipIf(fcntl is None, "POSIX file locks required")
class TaskLaneAdmission(unittest.TestCase):
    """run_task refuses before running anything and records admission when it proceeds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name))

    def test_gate_refusal_happens_before_any_codex_process(self):
        self.f.write_gate()
        with mock.patch.object(codex_task, "_run", side_effect=AssertionError("ran a process")), \
             mock.patch.object(codex_task, "_run_admitted", side_effect=AssertionError("admitted")):
            with self.assertRaisesRegex(codex_task.TaskError, "Codex CLI admission refused: maintenance in progress"):
                codex_task.run_task(codex_bin=self.f.launcher, promotion_dir=self.f.pdir,
                                    brief=Path("/b"), repo=Path("/r"), task_root=Path("/t"),
                                    classification="synthetic")

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
        self.assertEqual(seen["admission"]["state"], "promoted")
        self.assertEqual(seen["admission"]["record_id"], self.f.record["record_id"])


if __name__ == "__main__":
    unittest.main()
