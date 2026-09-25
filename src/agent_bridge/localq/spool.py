"""A bounded SQLite spool for mechanically-scoped local work.

It has no network client and no alternate execution route.  A caller supplies
an executor; a timed-out child leaves an explicit ``unknown`` disposition
instead of being retried automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


TERMINAL = frozenset({"complete", "failed", "cancelled", "expired", "unknown"})
MECHANICAL_TASKS = frozenset({"summarize", "extract", "checklist", "log_triage", "test_draft"})
#: Includes client_derived: this queue's only worker is the on-device model.
ALLOWED_CLASSIFICATIONS = frozenset({"synthetic", "public", "internal_nonclient", "client_derived"})
#: Fixed refusal reason for a mechanical task type that is real (it is in
#: ``MECHANICAL_TASKS``) but not carried by the queue's *currently configured*
#: backend -- e.g. every kind but ``summarize`` when the backend is
#: ``gemma_certified``. Kept separate from ``task_type_refused`` (a task type
#: that is not mechanical at all): one is "this queue never does that kind of
#: work"; this one is "this backend does not, today". Both refuse before
#: anything is queued.
UNSUPPORTED_KIND_REASON = "unsupported_kind"


class AdmissionError(ValueError):
    """The job was not admitted to the durable queue."""


class JobNotFound(KeyError):
    pass


@dataclass(frozen=True)
class ResourceSnapshot:
    observed_at: float
    memory_pressure: str
    thermal_state: str
    ac_power: bool
    idle_seconds: float
    cpu_load_ratio: float = 0.0
    #: Fraction of CPU idle from a direct measurement (e.g. `top`), or None
    #: when that probe was unavailable. Load-average-per-core conflates
    #: waiting-on-I/O with genuine CPU contention; this is a second,
    #: independent signal that can rescue admission when load looks high
    #: but the CPU is demonstrably not busy. It never revokes admission
    #: that cpu_load_ratio alone already grants.
    cpu_idle_ratio: float | None = None


class Sampler(Protocol):
    def sample(self) -> ResourceSnapshot: ...


class Backend(Protocol):
    def run(self, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class QueueCaps:
    max_jobs: int = 100
    max_input_bytes: int = 24_000
    bulk_ttl_seconds: float = 3600.0
    sample_max_age_seconds: float = 10.0
    # Aggressive local-first defaults. Bulk work may start while the operator
    # is active; actual pressure and heat remain hard stops below.
    bulk_min_idle_seconds: float = 0.0
    lease_seconds: float = 120.0
    timeout_seconds: float = 60.0
    max_load_per_core: float = 1.25
    #: Alternate admission threshold: a directly measured CPU idle fraction
    #: at or above this rescues admission when cpu_load_ratio alone would
    #: refuse it. See ResourceSnapshot.cpu_idle_ratio.
    min_cpu_idle_ratio: float = 0.10


#: Longest failure text the ``error`` column records.
FAILURE_TEXT_LIMIT = 200


def _failure_text(exc: BaseException) -> str:
    """``ClassName: reason`` for a backend failure, or the class name alone.

    The subprocess backend raises RuntimeError with fixed text ("child
    exited unsuccessfully", "child did not return a JSON object"); the
    class name alone told an operator nothing (the same defect the
    execution queue had). Only RuntimeError text is copied, because that
    is the backend's own fixed diagnostics; any other exception's text may
    quote the payload and stays out of the record.
    """
    name = type(exc).__name__
    if not isinstance(exc, RuntimeError):
        return name
    detail = "".join(char for char in str(exc) if char.isprintable())[:FAILURE_TEXT_LIMIT]
    return f"{name}: {detail}" if detail else name


class FakeBackend:
    """Deterministic in-process executor for unit tests."""

    def __init__(self, fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None):
        self.fn = fn or (lambda payload: {"output": payload})
        self.calls: list[dict[str, Any]] = []

    def run(self, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        self.calls.append(payload)
        return self.fn(payload)


def _child_failure_suffix(output: str) -> str:
    """``: <error>: <detail>`` when a failed child left its own fixed
    diagnostics on stdout (``{"ok": false, "error": ..., "error_detail":
    ...}``, the shape worker_child prints); empty otherwise. Printable
    characters only, bounded, so a child cannot put a payload in the record."""
    try:
        value = json.loads(output or "")
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict) or value.get("ok") is not False:
        return ""
    parts = []
    for key in ("error", "error_detail"):
        text = value.get(key)
        if isinstance(text, str):
            text = "".join(char for char in text if char.isprintable())[:FAILURE_TEXT_LIMIT]
            if text:
                parts.append(text)
    return ": " + ": ".join(parts) if parts else ""


class SubprocessBackend:
    """Run one side-effect-free child command with a hard timeout.

    The child reads one JSON payload from stdin and writes one JSON object to
    stdout.  It is intentionally not given environment overrides or any route
    selection option.
    """

    def __init__(self, command: list[str]):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must be a non-empty argv list")
        self.command = tuple(command)
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def run(self, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        job_id = payload.get("job_id")
        if not isinstance(job_id, str):
            raise RuntimeError("child payload lacks job id")
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True,
                                env=env, shell=False)
        with self._lock:
            self._processes[job_id] = proc
        try:
            output, _ = proc.communicate(json.dumps(payload), timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate_group(proc)
            proc.communicate()
            raise TimeoutError("child exceeded local execution timeout") from exc
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
        if proc.returncode != 0:
            raise RuntimeError("child exited unsuccessfully" + _child_failure_suffix(output))
        try:
            value = json.loads(output)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("child did not return a JSON object") from exc
        if not isinstance(value, dict):
            raise RuntimeError("child did not return a JSON object")
        return value

    @staticmethod
    def _terminate_group(proc: "subprocess.Popen[str]", sig: int = getattr(signal, "SIGKILL", signal.SIGTERM)) -> None:
        """Kill the whole isolated process group, not only the immediate child.

        ``start_new_session=True`` above makes this child its own
        process-group leader, so anything it spawns (a further certified
        delegate process, in particular) shares that group unless it starts
        its own new session. A plain ``proc.kill()``/``proc.terminate()``
        signals only the immediate process by pid; a timed-out or cancelled
        job could leave a grandchild running past the queue's own deadline.
        Found by review: the timeout branch here used ``proc.kill()`` alone
        while ``cancel()`` a few lines down already used ``killpg`` -- the
        same bug fixed in one place and left live in the other.
        """
        if os.name != "posix":  # pragma: no cover - Windows is covered by Popen termination semantics
            proc.kill()
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def cancel(self, job_id: str) -> bool:
        """Terminate the isolated child process group for an active job."""
        with self._lock:
            proc = self._processes.get(job_id)
        if proc is None or proc.poll() is not None:
            return False
        self._terminate_group(proc, signal.SIGTERM)
        return True


class LocalQueue:
    """SQLite/WAL queue with content-addressed JSON blobs and one lease owner."""

    def __init__(self, root: str | Path, *, sampler: Sampler,
                 backend: Backend | None = None, caps: QueueCaps = QueueCaps(),
                 clock: Callable[[], float] = time.time,
                 allowed_task_types: "frozenset[str] | None" = None,
                 backend_id: str = "private_worker"):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        self.db_path = self.root / "localq.sqlite3"
        self.lock_path = self.root / "executor.lock"
        self.sampler, self.backend, self.caps, self.clock = sampler, backend, caps, clock
        if not isinstance(backend_id, str) or not backend_id:
            raise ValueError("backend_id_invalid")
        self.backend_id = backend_id
        # The kinds *this* backend actually carries. Defaults to every
        # mechanical task, unchanged from before this existed. A backend
        # that supports fewer kinds (``gemma_certified`` supports only
        # ``summarize`` today) passes a narrower set here so an unsupported
        # kind is refused at submission, before anything is queued, rather
        # than discovered only when the executor finally runs it.
        self.allowed_task_types = (MECHANICAL_TASKS if allowed_task_types is None
                                  else frozenset(allowed_task_types))
        if not self.allowed_task_types <= MECHANICAL_TASKS:
            raise ValueError("allowed_task_types must be a subset of MECHANICAL_TASKS")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, idem_key TEXT UNIQUE NOT NULL, content_key TEXT UNIQUE NOT NULL,
                task_type TEXT NOT NULL, priority TEXT NOT NULL, classification TEXT NOT NULL,
                caller TEXT NOT NULL, purpose TEXT NOT NULL,
                params_json TEXT NOT NULL, input_blob TEXT NOT NULL, status TEXT NOT NULL,
                submitted_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
                lease_owner TEXT, lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                result_blob TEXT, disposition_json TEXT, feedback_json TEXT, error TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, submitted_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS executor_lock (
                name TEXT PRIMARY KEY, owner TEXT NOT NULL, lease_until REAL NOT NULL)""")

    @staticmethod
    def _normalized(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _blob(self, value: Any) -> str:
        raw = self._normalized(value).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        target = self.blobs / digest
        if not target.exists():
            temporary = self.blobs / ("." + digest + "." + uuid.uuid4().hex)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temporary, target)
            except PermissionError:
                # Windows refuses to replace a file another writer is
                # replacing or reading. The name is the content's digest, so
                # an existing target already holds these exact bytes.
                os.unlink(temporary)
                if not target.exists():
                    raise
        return digest

    def _read_blob(self, digest: str) -> Any:
        return json.loads((self.blobs / digest).read_text(encoding="utf-8"))

    def _admit(self, priority: str) -> ResourceSnapshot:
        try:
            snapshot = self.sampler.sample()
        except Exception as exc:  # sampling failure is conservative refusal
            raise AdmissionError("resource_sample_unavailable") from exc
        if not isinstance(snapshot, ResourceSnapshot):
            raise AdmissionError("resource_sample_invalid")
        age = self.clock() - snapshot.observed_at
        if age < -1 or age > self.caps.sample_max_age_seconds:
            raise AdmissionError("resource_sample_stale")
        if snapshot.memory_pressure != "normal" or snapshot.thermal_state != "normal":
            raise AdmissionError("resource_pressure")
        load_ok = 0 <= snapshot.cpu_load_ratio <= self.caps.max_load_per_core
        idle_ok = snapshot.cpu_idle_ratio is not None and snapshot.cpu_idle_ratio >= self.caps.min_cpu_idle_ratio
        if not (load_ok or idle_ok):
            raise AdmissionError("resource_cpu_load")
        if priority == "bulk" and (not snapshot.ac_power or snapshot.idle_seconds < self.caps.bulk_min_idle_seconds):
            raise AdmissionError("resource_bulk_policy")
        return snapshot

    def submit(self, *, task_type: str, input: str, params: dict[str, Any] | None,
               priority: str, classification: str, caller: str, purpose: str,
               idempotency_key: str | None = None) -> dict[str, Any]:
        if task_type not in MECHANICAL_TASKS:
            raise AdmissionError("task_type_refused")
        if task_type not in self.allowed_task_types:
            raise AdmissionError(UNSUPPORTED_KIND_REASON)
        if classification not in ALLOWED_CLASSIFICATIONS:
            raise AdmissionError("classification_refused")
        if caller not in {"codex", "claude"} or purpose not in {"work", "test"}:
            raise AdmissionError("provenance_invalid")
        if priority not in {"interactive", "bulk"}:
            raise AdmissionError("priority_invalid")
        if not isinstance(params or {}, dict):
            raise AdmissionError("params_invalid")
        if not isinstance(input, str):
            raise AdmissionError("input_invalid")
        payload = {"task_type": task_type, "input": input, "params": params or {},
                   "priority": priority, "classification": classification,
                   "caller": caller, "purpose": purpose}
        normalized = self._normalized(payload)
        if len(normalized.encode("utf-8")) > self.caps.max_input_bytes:
            raise AdmissionError("input_too_large")
        content_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        idem = idempotency_key or content_key
        if not isinstance(idem, str) or not idem or len(idem) > 256:
            raise AdmissionError("idempotency_key_invalid")
        with self._db() as db:
            existing = db.execute("SELECT * FROM jobs WHERE idem_key=? OR content_key=?", (idem, content_key)).fetchone()
        if existing is not None:
            if existing["idem_key"] == idem and existing["content_key"] != content_key:
                raise AdmissionError("idempotency_key_conflict")
            return self._public(existing, deduplicated=True)
        now = self.clock()
        expires = now + self.caps.bulk_ttl_seconds if priority == "bulk" else None
        job_id = uuid.uuid4().hex
        blob = self._blob(input)
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                count = db.execute("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('complete','failed','cancelled','expired','unknown')").fetchone()[0]
                if count >= self.caps.max_jobs:
                    raise AdmissionError("queue_full")
                db.execute("""INSERT INTO jobs(job_id,idem_key,content_key,task_type,priority,classification,caller,purpose,params_json,input_blob,status,submitted_at,updated_at,expires_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, idem, content_key, task_type, priority, classification, caller, purpose, self._normalized(params or {}), blob,
                     "queued", now, now, expires))
                db.execute("COMMIT")
        except sqlite3.IntegrityError:
            with self._db() as db:
                existing = db.execute("SELECT * FROM jobs WHERE idem_key=? OR content_key=?", (idem, content_key)).fetchone()
            if existing is not None:
                if existing["idem_key"] == idem and existing["content_key"] != content_key:
                    raise AdmissionError("idempotency_key_conflict")
                return self._public(existing, deduplicated=True)
            raise
        return {"job_id": job_id, "status": "queued", "deduplicated": False}

    def _get(self, db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return row

    def _public(self, row: sqlite3.Row, *, deduplicated: bool = False) -> dict[str, Any]:
        return {"job_id": row["job_id"], "status": row["status"], "task_type": row["task_type"],
                "priority": row["priority"], "classification": row["classification"],
                "caller": row["caller"], "purpose": row["purpose"],
                "submitted_at": row["submitted_at"], "updated_at": row["updated_at"],
                "expires_at": row["expires_at"], "deduplicated": deduplicated,
                "cancel_requested": bool(row["cancel_requested"]), "error": row["error"],
                "admission": "deferred" if row["status"] == "queued" and str(row["error"] or "").startswith("deferred:") else "admitted"}

    def status(self, job_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._public(self._get(db, job_id))

    def result(self, job_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = self._get(db, job_id)
        public = self._public(row)
        if row["status"] not in TERMINAL:
            return {**public, "ready": False}
        return {**public, "ready": True,
                "result": self._read_blob(row["result_blob"]) if row["result_blob"] else None,
                "disposition": json.loads(row["disposition_json"]) if row["disposition_json"] else None,
                "feedback": json.loads(row["feedback_json"]) if row["feedback_json"] else None}

    def cancel(self, job_id: str) -> dict[str, Any]:
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, job_id)
            if row["status"] == "queued":
                db.execute("UPDATE jobs SET status='cancelled',updated_at=?,disposition_json=? WHERE job_id=?",
                           (now, self._normalized({"outcome": "cancelled_before_execution"}), job_id))
            elif row["status"] == "running":
                db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE job_id=?", (now, job_id))
            db.execute("COMMIT")
            public = self._public(self._get(db, job_id))
        if row["status"] == "running":
            cancel = getattr(self.backend, "cancel", None)
            if callable(cancel):
                cancel(job_id)
        return public

    def feedback(self, job_id: str, outcome: str) -> dict[str, Any]:
        """Record one immutable usefulness observation for completed work only."""
        if outcome not in {"used", "reworked", "discarded"}:
            raise AdmissionError("feedback_invalid")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, job_id)
            if row["status"] != "complete" or row["purpose"] != "work":
                raise AdmissionError("feedback_not_eligible")
            if row["feedback_json"] is not None:
                raise AdmissionError("feedback_immutable")
            value = {"outcome": outcome, "recorded_at": self.clock()}
            db.execute("UPDATE jobs SET feedback_json=? WHERE job_id=?", (self._normalized(value), job_id))
            db.execute("COMMIT")
            return {"job_id": job_id, "feedback": value}

    def _recover_locked(self, db: sqlite3.Connection, now: float) -> int:
        """Recovery used only by a caller that already owns the executor lease."""
        cur = db.execute("""UPDATE jobs SET status='unknown',updated_at=?,error='executor_lease_lost',
            disposition_json=?,lease_owner=NULL,lease_until=NULL
            WHERE status='running' AND lease_until < ?""",
            (now, self._normalized({"outcome": "unknown", "reason": "executor_lease_lost"}), now))
        return cur.rowcount

    def _acquire(self, db: sqlite3.Connection, owner: str, now: float) -> bool:
        until = now + self.caps.lease_seconds
        row = db.execute("SELECT owner,lease_until FROM executor_lock WHERE name='executor'").fetchone()
        if row is None:
            db.execute("INSERT INTO executor_lock(name,owner,lease_until) VALUES('executor',?,?)", (owner, until))
            return True
        if row["owner"] == owner or row["lease_until"] < now:
            db.execute("UPDATE executor_lock SET owner=?,lease_until=? WHERE name='executor'", (owner, until))
            return True
        return False

    def run_once(self, owner: str | None = None) -> dict[str, Any] | None:
        """Run at most one job while holding an OS file lock and SQLite lease."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - supported POSIX deployments use flock
            return self._run_once_under_lock(owner)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            return self._run_once_under_lock(owner)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _run_once_under_lock(self, owner: str | None = None) -> dict[str, Any] | None:
        if self.backend is None:
            raise RuntimeError("no local executor configured")
        owner = owner or uuid.uuid4().hex
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._acquire(db, owner, now):
                db.execute("COMMIT")
                return None
            self._recover_locked(db, now)
            db.execute("UPDATE jobs SET status='expired',updated_at=?,disposition_json=? WHERE status='queued' AND expires_at IS NOT NULL AND expires_at < ?",
                       (now, self._normalized({"outcome": "expired"}), now))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY CASE priority WHEN 'interactive' THEN 0 ELSE 1 END,submitted_at LIMIT 1").fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            try:
                self._admit(row["priority"])
            except AdmissionError as exc:
                db.execute("UPDATE jobs SET updated_at=?,error=? WHERE job_id=?",
                           (now, "deferred:" + str(exc), row["job_id"]))
                db.execute("COMMIT")
                return self.status(row["job_id"])
            db.execute("UPDATE jobs SET status='running',lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?",
                       (owner, now + self.caps.lease_seconds, now, row["job_id"]))
            db.execute("COMMIT")
        payload = {"task_type": row["task_type"], "input": self._read_blob(row["input_blob"]),
                   "params": json.loads(row["params_json"]), "job_id": row["job_id"]}
        outcome, result, error = "complete", None, None
        try:
            result = self.backend.run(payload, self.caps.timeout_seconds)
            if not isinstance(result, dict):
                raise RuntimeError("executor result must be an object")
        except TimeoutError:
            outcome, error = "unknown", "execution_timeout"
        except Exception as exc:  # backend error was observed, so it is not indeterminate
            outcome, error = "failed", _failure_text(exc)
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, row["job_id"])
            if current["cancel_requested"]:
                outcome, result, error = "cancelled", None, "cancel_requested"
            result_blob = self._blob(result) if result is not None else None
            disposition = {"outcome": outcome, "executor": "child" if isinstance(self.backend, SubprocessBackend) else "local"}
            db.execute("UPDATE jobs SET status=?,updated_at=?,lease_owner=NULL,lease_until=NULL,result_blob=?,error=?,disposition_json=? WHERE job_id=?",
                       (outcome, now, result_blob, error, self._normalized(disposition), row["job_id"]))
            db.execute("COMMIT")
        return self.result(row["job_id"])

    def state_report(self) -> dict[str, Any]:
        with self._db() as db:
            rows = db.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
            classes = db.execute("SELECT priority,COUNT(*) AS count FROM jobs WHERE status NOT IN ('complete','failed','cancelled','expired','unknown') GROUP BY priority").fetchall()
        try:
            snapshot = self.sampler.sample()
            age = self.clock() - snapshot.observed_at
            fresh = isinstance(snapshot, ResourceSnapshot) and -1 <= age <= self.caps.sample_max_age_seconds
            base_ok = fresh and snapshot.memory_pressure == "normal" and snapshot.thermal_state == "normal"
            load_ok = 0 <= snapshot.cpu_load_ratio <= self.caps.max_load_per_core
            idle_ok = snapshot.cpu_idle_ratio is not None and snapshot.cpu_idle_ratio >= self.caps.min_cpu_idle_ratio
            base_ok = base_ok and (load_ok or idle_ok)
            interactive_verdict = "admissible" if base_ok else "deferred"
            bulk_verdict = "admissible" if base_ok and snapshot.ac_power and snapshot.idle_seconds >= self.caps.bulk_min_idle_seconds else "deferred"
            sample = {"age_seconds": age, "fresh": fresh, "memory_pressure": snapshot.memory_pressure,
                      "thermal_state": snapshot.thermal_state, "ac_power": snapshot.ac_power,
                      "idle_seconds": snapshot.idle_seconds, "cpu_load_ratio": snapshot.cpu_load_ratio,
                      "cpu_idle_ratio": snapshot.cpu_idle_ratio,
                      "verdict": {"interactive": interactive_verdict, "bulk": bulk_verdict}}
        except Exception:
            sample = {"fresh": False, "verdict": "deferred", "reason": "resource_sample_unavailable"}
        return {"schema": 1, "state": "local_queue", "counts": {row["status"]: row["count"] for row in rows},
                "queue_depth_by_class": {row["priority"]: row["count"] for row in classes},
                "resource": sample, "caps": asdict(self.caps), "executor": "single_flock_lease"}
