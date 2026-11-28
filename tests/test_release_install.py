"""Manifest-bound release installation into one task folder, and its gate form.

Everything here is synthetic and temporary: a throwaway HOME, a throwaway
ledger, a synthetic release of 17 Python files (16 launchers and a pin test),
a snapshot, a review and a queue, activated last. The task uses the real lock
layout (``task.json.claim.lock``, ``nightly/queue.lock``,
``nightly/state/owner.lock``). No model calls, no real task data, no user
configuration.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.capacity_router import CapacityObservation, StageRouter  # noqa: E402
from agent_bridge.orchestration import gate, release_install as ri  # noqa: E402

FIELD = ri.ACTIVATION_FIELD
PYTHON = os.path.realpath(sys.executable)
INSTALLER = os.path.realpath(ri.__file__)
HOLD_LOCK = ("import fcntl, os, sys\n"
             "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "sys.stdin.read()\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_files() -> dict[str, bytes]:
    """In manifest order: 17 Python files, then snapshot, review and queue."""
    files = {f"release/launcher_{n:02d}.py": f'"""synthetic launcher {n}"""\nNAME = {n}\n'.encode()
             for n in range(1, 17)}
    pins = {name: sha(data) for name, data in files.items()}
    files["release/test_release_pin.py"] = (
        '"""synthetic pin test"""\nPINS = ' + json.dumps(pins, sort_keys=True) + "\n").encode()
    files["nightly/state/snapshot.json"] = b'{"snapshot": "synthetic"}\n'
    files["nightly/review.json"] = b'{"review": "synthetic", "verdict": "accept"}\n'
    files["nightly/queue.jsonl"] = b'{"entry": 1}\n{"entry": 2}\n'
    return files


@unittest.skipUnless(os.name == "posix", "the release installer is POSIX-only")
class ReleaseInstallCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(os.path.realpath(temp.name)) / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.state = self.home / "Claude/team-state/orchestration/runtime"
        (self.state / "routing").mkdir(parents=True)
        self.db = self.state / "capacity.sqlite3"
        self.tasks = self.home / "Claude/team-state/tasks"
        self.item = "synthetic-task"
        self.task = self.tasks / self.item
        (self.task / "nightly/state").mkdir(parents=True)
        for lock in ri.LOCKS:
            (self.task / lock).write_bytes(b"")
        self.old_queue = b'{"entry": 1}\n'
        (self.task / "nightly/queue.jsonl").write_bytes(self.old_queue)
        os.chmod(self.task / "nightly/queue.jsonl", 0o644)
        now = time.time()
        router = StageRouter(str(self.db))
        router.observe_capacity(CapacityObservation(
            route="codex", observed_at=now, fresh_until=now + 3600, available=True,
            source="test-operator"), trusted=True)
        router.register(self.item, "release", allowed_routes=["codex"])
        router.assign(self.item, "release", owner_id="codex-session", lease_seconds=600,
                      expected_revision=0)
        self.write_record(now + 600)
        self.files = release_files()
        self.payload = self.fixture()
        self.digest = ""

    # ------------------------------------------------------------ fixture

    def write_record(self, lease: float, field=None):
        record = {"title": "synthetic", "workload": {"revision": 3, "claim": {
            "owner": "codex-session", "stage": "release", "lease_expires_at": lease}},
            FIELD: field if field is not None else {"release": "r0"}, "notes": ["keep"]}
        (self.task / "task.json").write_text(json.dumps(record), encoding="utf-8")

    def manifest(self, item: str | None = None) -> dict:
        entries = [{"path": name, "sha256": sha(data),
                    "previous_sha256": sha(self.old_queue) if name == "nightly/queue.jsonl" else None}
                   for name, data in self.files.items()]
        return {"schema": ri.SCHEMA, "task": item or self.item,
                "claim": {"owner": "codex-session", "stage": "release"},
                "locks": list(ri.LOCKS), "files": entries,
                "activation": {"field": FIELD, "previous": {"value": {"release": "r0"}},
                               "value": {"release": "r1", "channel": "stable"}}}

    def fixture(self, item: str | None = None) -> Path:
        payload = Path(tempfile.mkdtemp(dir=self.home))
        for name, data in self.files.items():
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (payload / "manifest.json").write_text(json.dumps(self.manifest(item), indent=2),
                                               encoding="utf-8")
        return payload

    def accept(self, payload: Path | None = None) -> str:
        result = ri.accept(str(self.state), str((payload or self.payload) / "manifest.json"),
                           str(payload or self.payload), str(self.tasks), str(self.db))
        self.assertEqual(result["status"], "accepted")
        if payload is None:
            self.digest = result["manifest_sha256"]
        return result["manifest_sha256"]

    def words(self, **change) -> list[str]:
        values = dict(python=PYTHON, installer=INSTALLER, state_root=str(self.state),
                      tasks_root=str(self.tasks), capacity_db=str(self.db), task=self.item,
                      manifest_sha256=self.digest)
        values.update(change)
        return ri.command_words(**values)

    def run_install(self, **change):
        return ri.install(str(self.state), str(self.tasks), str(self.db),
                          change.get("task", self.item), change.get("digest", self.digest),
                          now=change.get("now"))

    def run_process(self):
        completed = subprocess.run(self.words(), capture_output=True, timeout=60, check=False)
        return completed.returncode, json.loads(completed.stdout)

    def tree(self) -> dict:
        """Every entry under the tasks and the store, never opening a non-regular file."""
        out = {}
        for base in (self.tasks, self.state / ri.STORE_DIR):
            for path in sorted(base.rglob("*")):
                key = str(path)
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    out[key] = ("link", os.readlink(path))
                elif stat.S_ISREG(mode):
                    out[key] = path.read_bytes()
                elif stat.S_ISDIR(mode):
                    out[key] = "dir"
                else:
                    out[key] = ("special", stat.S_IFMT(mode))
        return out

    def record(self) -> dict:
        return json.loads((self.task / "task.json").read_text(encoding="utf-8"))

    def judge(self, command: str, client: str = "codex"):
        return gate.judge(client, "Bash", {"command": command}, str(self.home),
                          state_root=str(self.state), capacity_db=str(self.db),
                          protected=gate.protected_paths(str(self.state), None, str(self.home),
                                                         str(self.db)))

    def pinned(self):
        """Pin the gate to this interpreter by exact path (a venv test runner
        would otherwise fail the gate's pyvenv.cfg check)."""
        return mock.patch.object(gate.literal_records, "_interpreter_path_is_clean",
                                 lambda path: path == PYTHON)

    def assertRefused(self, code: str, **change):
        with self.assertRaises(ri.InstallError) as caught:
            self.run_install(**change)
        self.assertEqual(caught.exception.code, code, caught.exception.detail)
        return caught.exception

    def assertInstalled(self):
        for name, data in self.files.items():
            self.assertEqual((self.task / name).read_bytes(), data, name)
        self.assertEqual(self.record()[FIELD], {"release": "r1", "channel": "stable"})


def regular_only_fdopen():
    """``os.fdopen`` that fails the test if it is ever handed a non-regular file."""
    real = os.fdopen

    def checked(fd, *args, **kwargs):
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AssertionError("os.fdopen received a non-regular file descriptor")
        return real(fd, *args, **kwargs)

    return mock.patch.object(ri.os, "fdopen", checked)


def make_special(path: Path, kind: str) -> None:
    if kind == "folder":
        path.mkdir()
    else:
        os.mkfifo(path)


def remove_special(path: Path, kind: str) -> None:
    if kind == "folder":
        path.rmdir()
    else:
        path.unlink()


SPECIAL_KINDS = ("folder", "fifo") if hasattr(os, "mkfifo") else ("folder",)


class OperatorAcceptanceTests(ReleaseInstallCase):
    def test_clean_entry_points_accept_judge_install_and_activate_last(self):
        lock_inodes = {lock: os.stat(self.task / lock).st_ino for lock in ri.LOCKS}
        before = self.record()
        accepted = subprocess.run(
            [PYTHON, "-I", "-S", "-B", INSTALLER, "accept",
             "--state-root", str(self.state), "--manifest", str(self.payload / "manifest.json"),
             "--payload-dir", str(self.payload), "--tasks-root", str(self.tasks),
             "--capacity-db", str(self.db)],
            env={"LANG": "en_US.UTF-8"}, capture_output=True, timeout=60, check=False)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        result = json.loads(accepted.stdout)
        self.digest = result["manifest_sha256"]
        self.assertEqual(result["command"], shlex.join(self.words()))
        manifest = json.loads((self.state / ri.STORE_DIR / ri.APPROVED_DIR
                               / f"{self.digest}.json").read_bytes())
        python_files = [e["path"] for e in manifest["files"] if e["path"].endswith(".py")]
        self.assertEqual(len(python_files), 17)
        self.assertIn("release/test_release_pin.py", python_files)
        self.assertEqual([e["path"] for e in manifest["files"]][-3:],
                         ["nightly/state/snapshot.json", "nightly/review.json",
                          "nightly/queue.jsonl"])
        with self.pinned():
            decision = self.judge(result["command"])
            self.assertEqual((decision.permission, decision.code), ("allow", "release_install"))
            self.assertEqual(decision.extra["manifest_sha256"], self.digest)
            self.assertFalse(self.judge(result["command"], "claude").allowed)   # route binds
        self.assertEqual(self.record(), before)                  # judging writes nothing
        installed = subprocess.run(shlex.split(result["command"]), capture_output=True,
                                   timeout=60, check=False)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(json.loads(installed.stdout)["status"], "installed")
        self.assertInstalled()
        after = self.record()
        self.assertEqual({k: v for k, v in after.items() if k != FIELD},
                         {k: v for k, v in before.items() if k != FIELD})
        for name in self.files:
            if name != "nightly/queue.jsonl":
                self.assertEqual(stat.S_IMODE((self.task / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE((self.task / "release").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.task / "nightly/queue.jsonl").stat().st_mode), 0o644)
        self.assertEqual({lock: os.stat(self.task / lock).st_ino for lock in ri.LOCKS},
                         lock_inodes)                            # locks never replaced
        for lock in ri.LOCKS:
            self.assertEqual((self.task / lock).read_bytes(), b"")
        self.assertFalse([p for p in self.task.rglob("*") if p.name.endswith(ri.TEMP_SUFFIX)])
        snapshot = self.tree()
        again = subprocess.run(shlex.split(result["command"]), capture_output=True, timeout=60,
                               check=False)
        self.assertEqual(json.loads(again.stdout)["status"], "already_installed")
        self.assertEqual(self.tree(), snapshot)

    def test_non_regular_operator_payload_refuses_before_any_store_write(self):
        # A payload nobody accepted yet, so any store write would show up.
        payload = self.fixture(item="other-task")
        target = payload / "nightly/review.json"
        target.unlink()
        for kind in SPECIAL_KINDS:
            with self.subTest(kind=kind):
                make_special(target, kind)
                snapshot = self.tree()
                with regular_only_fdopen(), self.assertRaises(ri.InstallError) as caught:
                    ri.accept(str(self.state), str(payload / "manifest.json"), str(payload),
                              str(self.tasks), str(self.db))
                self.assertEqual(caught.exception.code, "payload_missing",
                                 caught.exception.detail)
                self.assertEqual(self.tree(), snapshot)
                self.assertFalse((self.state / ri.STORE_DIR).exists())
                remove_special(target, kind)
        target.write_bytes(self.files["nightly/review.json"])
        self.accept(payload)


class LockTests(ReleaseInstallCase):
    def setUp(self):
        super().setUp()
        self.accept()

    def hold(self, relative: str) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-I", "-c", HOLD_LOCK,
                                    str(self.task / relative)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.assertEqual(process.stdout.readline().strip(), b"held")
        return process

    def release(self, process: subprocess.Popen) -> None:
        process.stdin.close()
        process.wait(timeout=30)
        process.stdout.close()

    def test_an_independent_holder_of_any_canonical_lock_refuses_before_changes(self):
        snapshot = self.tree()
        for lock in ri.LOCKS:
            with self.subTest(lock=lock):
                process = self.hold(lock)
                try:
                    status, out = self.run_process()
                finally:
                    self.release(process)
                self.assertEqual((status, out["code"], out["detail"]), (2, "lock_busy", lock))
                self.assertEqual(self.tree(), snapshot)
        status, out = self.run_process()
        self.assertEqual((status, out["status"]), (0, "installed"))

    def test_missing_or_linked_locks_refuse_and_are_never_created(self):
        for lock in ri.LOCKS:
            with self.subTest(lock=lock):
                (self.task / lock).unlink()
                snapshot = self.tree()
                error = self.assertRefused("lock_missing")
                self.assertEqual(error.detail, lock)
                self.assertEqual(self.tree(), snapshot)
                (self.task / lock).write_bytes(b"")
        outside = self.home / "outside.lock"
        outside.write_bytes(b"")
        (self.task / ri.LOCKS[0]).unlink()
        (self.task / ri.LOCKS[0]).symlink_to(outside)
        snapshot = self.tree()
        self.assertRefused("link_refused")
        self.assertEqual(self.tree(), snapshot)

    def test_lock_schema_and_overlap_are_enforced(self):
        base = self.manifest()
        bad_locks = [list(reversed(ri.LOCKS)), list(ri.LOCKS[:2]), ["claim.lock", "queue.lock"],
                     [*ri.LOCKS, "extra.lock"]]
        bad_paths = ["task.json", "task.json.claim.lock", "nightly/queue.lock",
                     "nightly/state/owner.lock", "nightly/state", "nightly",
                     "task.json/x", "nightly/queue.lock/x", "Nightly/State/Owner.lock",
                     "../x", ".git/config", "a/b/c/d/e", ".hidden"]
        for locks in bad_locks:
            with self.subTest(locks=locks):
                value = json.loads(json.dumps(base))
                value["locks"] = locks
                with self.assertRaises(ri.InstallError) as caught:
                    ri.validate_manifest(value, self.item)
                self.assertEqual(caught.exception.code, "manifest_invalid")
        for path in bad_paths:
            with self.subTest(path=path):
                value = json.loads(json.dumps(base))
                value["files"][0]["path"] = path
                with self.assertRaises(ri.InstallError) as caught:
                    ri.validate_manifest(value, self.item)
                self.assertEqual(caught.exception.code, "manifest_invalid")


class WriteSetTests(ReleaseInstallCase):
    """The gate and the installer share one write set, and it is complete."""

    def setUp(self):
        super().setUp()
        self.accept()

    def temp(self, relative: str) -> Path:
        path = self.task / relative
        return path.parent / ri.temp_name(path.name, self.digest)

    def fd_path(self, fd: int) -> str:
        info = os.fstat(fd)
        for folder, _, _ in os.walk(self.task):
            found = os.stat(folder)
            if (found.st_dev, found.st_ino) == (info.st_dev, info.st_ino):
                return folder
        return "<outside the task>"

    def mutations(self, action) -> set[str]:
        """Every path ``action`` asks the OS to create, rename, unlink or remove."""
        touched: set[str] = set()
        real = {name: getattr(os, name) for name in ("open", "mkdir", "unlink", "rmdir",
                                                     "replace", "rename")}

        def at(name, dir_fd):
            return os.path.join(self.fd_path(dir_fd), name) if dir_fd is not None else str(name)

        def spy_open(path, flags, *args, dir_fd=None, **kwargs):
            if flags & os.O_CREAT:
                touched.add(at(path, dir_fd))
            return real["open"](path, flags, *args, dir_fd=dir_fd, **kwargs)

        def single(name):
            def spy(path, *args, dir_fd=None, **kwargs):
                touched.add(at(path, dir_fd))
                return real[name](path, *args, dir_fd=dir_fd, **kwargs)
            return spy

        def pair(name):
            def spy(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                touched.add(at(src, src_dir_fd))
                touched.add(at(dst, dst_dir_fd))
                return real[name](src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            return spy

        spies = {"open": spy_open, "mkdir": single("mkdir"), "unlink": single("unlink"),
                 "rmdir": single("rmdir"), "replace": pair("replace"), "rename": pair("rename")}
        # A spy stands in for its real function in ``os.supports_dir_fd``
        # exactly when the real function supports ``dir_fd``, so production
        # capability checks see this platform's true answer.
        supports = set(os.supports_dir_fd) | {spies[name] for name in spies
                                              if real[name] in os.supports_dir_fd}
        with mock.patch.object(ri.os, "supports_dir_fd", supports), \
                mock.patch.object(ri.os, "open", spies["open"]), \
                mock.patch.object(ri.os, "mkdir", spies["mkdir"]), \
                mock.patch.object(ri.os, "unlink", spies["unlink"]), \
                mock.patch.object(ri.os, "rmdir", spies["rmdir"]), \
                mock.patch.object(ri.os, "replace", spies["replace"]), \
                mock.patch.object(ri.os, "rename", spies["rename"]):
            action()
        return touched

    def test_the_gate_classifies_every_path_the_installer_touches(self):
        _, targets = ri.plan(str(self.state), str(self.tasks), self.item, self.digest)
        task = str(self.task)
        expected = {os.path.join(task, "task.json"), str(self.temp("task.json"))}
        for name in self.files:
            parts = name.split("/")
            expected.update(os.path.join(task, *parts[:i]) for i in range(1, len(parts)))
            expected.update({str(self.temp(name)), os.path.join(task, *parts)})
        self.assertEqual(len(targets), len(set(targets)))
        self.assertEqual(set(targets), expected)
        # 20 files and their temporaries, release/, nightly/, nightly/state/, the record pair.
        self.assertEqual(len(targets), 2 * len(self.files) + 3 + 2)
        self.assertEqual(targets, ri.plan(str(self.state), str(self.tasks), self.item,
                                          self.digest)[1])           # deterministic
        for path in targets:      # never the task folder, the task root or anything above
            self.assertTrue(path.startswith(task + os.sep), path)
        for lock in ri.LOCKS:
            self.assertNotIn(os.path.join(task, *lock.split("/")), targets)
        classified: list[list[str]] = []
        real = gate._judge_task_records

        def spy(client, paths, capacity_db, now):
            classified.append(list(paths))
            return real(client, paths, capacity_db, now)

        with self.pinned(), mock.patch.object(gate, "_judge_task_records", spy):
            decision = self.judge(shlex.join(self.words()))
        self.assertEqual((decision.permission, decision.code), ("allow", "release_install"))
        self.assertEqual(classified, [targets])
        touched = self.mutations(self.run_install)
        self.assertLessEqual(touched, set(targets))
        self.assertIn(os.path.join(task, "release"), touched)      # a created folder
        self.assertIn(str(self.temp("task.json")), touched)
        self.assertIn(str(self.temp("nightly/queue.jsonl")), touched)
        self.assertInstalled()

    def test_a_protected_temporary_denies_before_any_write(self):
        command = shlex.join(self.words())
        snapshot = self.tree()
        with self.pinned():
            for relative in ("nightly/review.json", "task.json"):
                with self.subTest(relative=relative):
                    temp = str(self.temp(relative))
                    decision = gate._judge_release_install(
                        "codex", command, state_root=str(self.state), protected=(temp,),
                        capacity_db=str(self.db))
                    self.assertEqual((decision.permission, decision.code),
                                     ("deny", "gate_state_protected"))
                    self.assertEqual(decision.repos, (temp,))
            # A name outside the write set is not this install's concern.
            generic = str(self.task / "nightly" / (".review.json" + ri.TEMP_SUFFIX))
            decision = gate._judge_release_install(
                "codex", command, state_root=str(self.state), protected=(generic,),
                capacity_db=str(self.db))
            self.assertEqual(decision.code, "release_install")
        self.assertEqual(self.tree(), snapshot)

    def test_a_preexisting_unowned_temporary_is_refused_and_kept(self):
        generic = self.task / "nightly" / (".review.json" + ri.TEMP_SUFFIX)
        generic.write_bytes(b"unrelated notes\n")
        for relative in ("nightly/review.json", "task.json"):
            with self.subTest(relative=relative):
                temp = self.temp(relative)
                temp.write_bytes(b"[someone else's notes]\n")
                snapshot = self.tree()
                error = self.assertRefused("temporary_conflict")
                self.assertEqual(error.detail, temp.name)
                self.assertEqual(self.tree(), snapshot)                # kept, nothing written
                temp.unlink()
        # An interrupted write of this install's own is replaced on retry.
        partial = self.temp("nightly/review.json")
        partial.write_bytes(self.files["nightly/review.json"][:9])
        self.assertEqual(self.run_install()["status"], "installed")
        self.assertFalse(partial.exists())
        self.assertEqual(generic.read_bytes(), b"unrelated notes\n")  # other names untouched
        self.assertInstalled()


class RefusalTests(ReleaseInstallCase):
    def setUp(self):
        super().setUp()
        self.accept()

    def test_rejections_write_nothing(self):
        other = self.accept(self.fixture(item="other-task"))
        cases = {
            "manifest_not_approved": dict(digest="0" * 64),
            "manifest_task_mismatch": dict(digest=other),
            "claim_not_live": dict(now=time.time() + 700),
        }
        snapshot = self.tree()
        for code, change in cases.items():
            with self.subTest(code=code):
                self.assertRefused(code, **change)
                self.assertEqual(self.tree(), snapshot)
        self.assertRefused("task_missing", task="other-task", digest=other)
        (self.task / "nightly/queue.jsonl").write_bytes(b"changed\n")
        snapshot = self.tree()
        self.assertRefused("stale_state")
        self.assertEqual(self.tree(), snapshot)
        (self.task / "nightly/queue.jsonl").write_bytes(self.old_queue)
        self.write_record(time.time() + 600, {"release": "someone-else"})
        snapshot = self.tree()
        self.assertRefused("stale_activation")
        self.assertEqual(self.tree(), snapshot)
        self.write_record(time.time() + 3600)        # ledger lease ends first
        snapshot = self.tree()
        self.assertRefused("ledger_not_owned", now=time.time() + 650)
        self.assertEqual(self.tree(), snapshot)

    def test_bad_hashes_write_nothing(self):
        blob = self.state / ri.STORE_DIR / ri.BLOB_DIR / sha(self.files["nightly/review.json"])
        good = blob.read_bytes()
        blob.write_bytes(b"tampered")
        snapshot = self.tree()
        self.assertRefused("blob_hash_mismatch")
        self.assertEqual(self.tree(), snapshot)
        blob.write_bytes(good)
        approved = self.state / ri.STORE_DIR / ri.APPROVED_DIR / f"{self.digest}.json"
        approved.write_bytes(approved.read_bytes() + b" ")
        snapshot = self.tree()
        self.assertRefused("manifest_digest_mismatch")
        with self.pinned():
            self.assertEqual(self.judge(shlex.join(self.words())).code,
                             "release_install_not_approved")
        self.assertEqual(self.tree(), snapshot)

    def test_every_destination_is_preflighted_before_the_first_write(self):
        # The last payload's slot is a folder: nothing earlier may be written.
        (self.task / "nightly/queue.jsonl").unlink()
        (self.task / "nightly/queue.jsonl").mkdir()
        snapshot = self.tree()
        self.assertRefused("destination_type_refused")
        self.assertEqual(self.tree(), snapshot)
        (self.task / "nightly/queue.jsonl").rmdir()
        (self.task / "nightly/queue.jsonl").write_bytes(self.old_queue)
        # A parent that is a file.
        (self.task / "release").write_bytes(b"")
        snapshot = self.tree()
        self.assertRefused("destination_type_refused")
        self.assertEqual(self.tree(), snapshot)
        (self.task / "release").unlink()
        # This install's temporary that is not a plain file.
        stray = self.task / "nightly" / ri.temp_name("review.json", self.digest)
        stray.mkdir()
        snapshot = self.tree()
        self.assertRefused("destination_type_refused")
        self.assertEqual(self.tree(), snapshot)
        stray.rmdir()
        # A complete leftover of this install's own is replaced and gone afterwards.
        stray.write_bytes(self.files["nightly/review.json"])
        self.assertEqual(self.run_install()["status"], "installed")
        self.assertFalse(stray.exists())

    def test_non_regular_destinations_refuse_without_writes_or_file_objects(self):
        # Present-and-replaced, absent-expected and the record itself, each as
        # a folder and a FIFO. The type is checked before any file object.
        saved = {name: (self.task / name).read_bytes()
                 for name in ("nightly/queue.jsonl", "task.json")}
        for name in ("nightly/queue.jsonl", "nightly/review.json", "task.json"):
            for kind in SPECIAL_KINDS:
                with self.subTest(name=name, kind=kind):
                    target = self.task / name
                    if name in saved:
                        target.unlink()
                    make_special(target, kind)
                    snapshot = self.tree()
                    with regular_only_fdopen():
                        error = self.assertRefused("destination_type_refused")
                    self.assertIn(target.name, error.detail)
                    self.assertEqual(self.tree(), snapshot)
                    remove_special(target, kind)
                    if name in saved:
                        target.write_bytes(saved[name])
        self.assertEqual(self.run_install()["status"], "installed")
        self.assertInstalled()

    def test_non_regular_store_blob_refuses_without_writes(self):
        blob = self.state / ri.STORE_DIR / ri.BLOB_DIR / sha(self.files["nightly/review.json"])
        good = blob.read_bytes()
        blob.unlink()
        for kind in SPECIAL_KINDS:
            with self.subTest(kind=kind):
                make_special(blob, kind)
                snapshot = self.tree()
                with regular_only_fdopen():
                    self.assertRefused("blob_missing")
                self.assertEqual(self.tree(), snapshot)
                remove_special(blob, kind)
        blob.write_bytes(good)

    def test_every_descriptor_is_closed_exactly_once(self):
        opened: list[int] = []
        closed: list[int] = []
        real_open, real_close = os.open, os.close

        def spy_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            real_close(fd)

        blob = str(self.state / ri.STORE_DIR / ri.BLOB_DIR / sha(self.files["nightly/review.json"]))
        task_fd = real_open(str(self.task), os.O_RDONLY)
        try:
            with mock.patch.object(ri.os, "open", spy_open), \
                    mock.patch.object(ri.os, "close", spy_close):
                # A folder is refused by fstat; no file object is ever made.
                with mock.patch.object(ri.os, "fdopen", side_effect=AssertionError):
                    with self.assertRaises(ri.InstallError) as caught:
                        ri._read_at(task_fd, "nightly", ri.MAX_FILE_BYTES)
                    self.assertEqual(caught.exception.code, "destination_type_refused")
                    with self.assertRaises(ri.InstallError) as caught:
                        ri._read_regular(str(self.task / "nightly"), ri.MAX_FILE_BYTES,
                                         "payload_missing")
                    self.assertEqual(caught.exception.code, "payload_missing")
                # Wrapping a regular file fails: the descriptor is still closed, once.
                failure = OSError(errno.EIO, "simulated fdopen failure")
                with mock.patch.object(ri.os, "fdopen", side_effect=failure):
                    with self.assertRaises(ri.InstallError) as caught:
                        ri._read_at(task_fd, "task.json", ri.MAX_RECORD_BYTES)
                    self.assertEqual(caught.exception.code, "io_error")
                    with self.assertRaises(ri.InstallError) as caught:
                        ri._read_regular(blob, ri.MAX_FILE_BYTES, "blob_missing")
                    self.assertEqual(caught.exception.code, "blob_missing")
                # The ordinary path closes through the file object, also once.
                data, _ = ri._read_at(task_fd, "task.json", ri.MAX_RECORD_BYTES)
                with self.assertRaises(OSError):
                    os.fstat(opened[-1])                           # already closed by its file
                self.assertEqual(data, (self.task / "task.json").read_bytes())
        finally:
            real_close(task_fd)
        self.assertEqual(len(opened), 5)
        self.assertEqual(sorted(closed), sorted(opened[:4]))      # the fifth closes via its file

    def test_links_protected_paths_and_nested_repositories_refuse(self):
        command = shlex.join(self.words())
        (self.task / "release").symlink_to(self.state, target_is_directory=True)
        snapshot = self.tree()
        with self.pinned():
            self.assertEqual(self.judge(command).code, "gate_state_protected")
        self.assertRefused("link_refused")
        self.assertEqual(self.tree(), snapshot)
        (self.task / "release").unlink()
        (self.task / ".git").mkdir()
        with self.pinned():
            self.assertEqual(self.judge(command).code, "release_install_repository")
        self.assertRefused("nested_repository")
        (self.task / ".git").rmdir()
        (self.task / "nightly/.git").mkdir()
        snapshot = self.tree()
        self.assertRefused("nested_repository")
        self.assertEqual(self.tree(), snapshot)

    def test_non_posix_refuses_and_import_needs_no_fcntl(self):
        self.assertNotIn("fcntl", vars(ri))
        snapshot = self.tree()
        with mock.patch.object(ri, "_supported", return_value=False):
            self.assertRefused("unsupported_platform")
            with self.assertRaises(ri.InstallError) as caught:
                ri.accept(str(self.state), str(self.payload / "manifest.json"),
                          str(self.payload), str(self.tasks), str(self.db))
            self.assertEqual(caught.exception.code, "unsupported_platform")
        self.assertEqual(self.tree(), snapshot)

    def test_only_the_canonical_form_is_recognised(self):
        good = self.words()
        variants = [
            good[:2] + [f"PYTHONPATH={self.home}"] + good[2:],             # dependency path
            good[:-4] + good[-2:] + good[-4:-2],                           # reordered flags
            good[:-1] + ["--extra"],
            self.words(tasks_root=str(self.home)),                         # other task root
            self.words(installer=str(self.home / "release_install.py")),   # unpinned file
            self.words(python="/nonexistent/python3"),                     # unpinned interpreter
            [sys.executable, INSTALLER, *good[8:]],
            [sys.executable, "-m", "agent_bridge.orchestration.release_install", *good[8:]],
            [sys.executable, INSTALLER, "accept", "--state-root", str(self.state)],
        ]
        with self.pinned():
            for words in variants:
                with self.subTest(words=words):
                    self.assertFalse(self.judge(shlex.join(words)).allowed)
            self.assertEqual(self.judge(shlex.join(variants[1])).code, "release_install_unresolved")
        self.assertIsNone(gate._judge_release_install(
            "codex", f"cat {shlex.quote(INSTALLER)}", state_root=str(self.state),
            protected=(), capacity_db=str(self.db)))
        self.assertEqual(ri.main(["install", "--task", self.item]), 2)


class RecoveryTests(ReleaseInstallCase):
    def setUp(self):
        super().setUp()
        self.accept()

    @staticmethod
    def fail_on(name: str):
        def hook(written: str):
            if written == name:
                raise OSError("simulated fsync failure after rename")
        return hook

    def test_post_rename_failure_on_a_payload_rolls_back_verified(self):
        snapshot = self.tree()
        with mock.patch.object(ri, "_FAULT_HOOK", self.fail_on("launcher_05.py")):
            error = self.assertRefused("write_failed")
        self.assertIn("rolled back", error.detail)
        self.assertEqual(self.tree(), snapshot)

    def test_post_rename_failure_on_activation_rolls_back_record_and_payloads(self):
        snapshot = self.tree()
        with mock.patch.object(ri, "_FAULT_HOOK", self.fail_on("task.json")):
            error = self.assertRefused("write_failed")
        self.assertIn("rolled back", error.detail)
        self.assertEqual(self.tree(), snapshot)
        self.assertEqual(self.record()[FIELD], {"release": "r0"})
        self.assertEqual(self.run_install()["status"], "installed")
        self.assertInstalled()

    def test_final_verification_failure_rolls_back(self):
        snapshot = self.tree()
        with mock.patch.object(ri, "_verify_final",
                               side_effect=ri.InstallError("verify_failed", "simulated")):
            error = self.assertRefused("verify_failed")
        self.assertIn("rolled back", error.detail)
        self.assertEqual(self.tree(), snapshot)

    def test_unrestorable_activation_never_reverts_payloads(self):
        restore = ri._restore

        def no_record(item_fd, parts, previous, digest):
            if parts == [ri.TASK_RECORD]:
                raise OSError("simulated")
            return restore(item_fd, parts, previous, digest)

        with mock.patch.object(ri, "_FAULT_HOOK", self.fail_on("task.json")), \
                mock.patch.object(ri, "_restore", no_record):
            error = self.assertRefused("rollback_incomplete")
        self.assertNotIn("rolled back", error.detail)
        self.assertInstalled()          # active record, so every payload stays new
        self.assertEqual(self.run_install()["status"], "already_installed")

    def test_interruption_completes_forward_on_retry(self):
        # A crash leaves no chance to roll back: simulate it, then retry.
        with mock.patch.object(ri, "_FAULT_HOOK", self.fail_on("launcher_05.py")), \
                mock.patch.object(ri, "_rollback", return_value=True):
            self.assertRefused("write_failed")
        self.assertEqual(self.record()[FIELD], {"release": "r0"})     # never activated early
        self.assertEqual(self.run_install()["status"], "installed")
        self.assertEqual(self.run_install()["status"], "already_installed")
        self.assertInstalled()


if __name__ == "__main__":
    unittest.main()
