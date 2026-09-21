"""Conservative automatic intake for the local mechanical-work queue.

The router is intentionally one-way: a request is either admitted to the local
queue or refused.  It never selects, invokes, or recommends a cloud fallback.
Classification and the routing decision are written once to a durable receipt.

Checkpoints (this module's other durable table) exist so that a caller who
must be able to prove "I checked before I acted" can do so without ever
handing a second copy of the content to this ledger.  A checkpoint records
only bounded metadata -- a caller-chosen task id, task type, classification,
UTF-8 byte count, nonblank-line count and risk flags -- and the deterministic
eligibility decision those metrics produce.  ``route`` then requires a
checkpoint for every ``purpose="work"`` call and validates, at consumption
time, that the exact same bounded metrics were declared; the routing
receipt's own ``input_sha256`` (unchanged, computed from the real payload)
is what proves exact payload identity, not the checkpoint.  A checkpoint's
route request key is derived solely from the checkpoint id, so a caller
cannot retry the same checkpoint with a different payload and get a job for
it: the second attempt either dedupes (identical payload) or is refused
(different payload), never silently reuses someone else's receipt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .spool import (ALLOWED_CLASSIFICATIONS, MECHANICAL_TASKS, UNSUPPORTED_KIND_REASON,
                    AdmissionError, LocalQueue)


PROHIBITED_FLAGS = frozenset({
    "client_derived",
    "confidential",
    "exact_sensitive_identifiers",
    "external_side_effects",
    "licensed_review",
    "professional_judgment",
})

#: Fixed, closed vocabulary of checkpoint outcomes. "eligible_mechanical_work"
#: and "no_eligible_unit" are not refusals; every other member is a refusal
#: reason. This set is what makes the audit tool's per-reason counts a fixed
#: closed vocabulary rather than free text: nothing dynamic (a flag name, a
#: task id, a caller-chosen string) ever becomes a reason.
CHECKPOINT_REASONS = frozenset({
    "eligible_mechanical_work",
    "classification_refused",
    "risk_flags_invalid",
    "prohibited_risk_flags",
    "task_requires_cloud_or_human_judgment",
    UNSUPPORTED_KIND_REASON,
    "below_local_delegation_threshold",
    "no_eligible_unit",
})

#: The subset of CHECKPOINT_REASONS that are refusals, used to key the
#: audit tool's "counts by fixed closed reason" so a reason that has never
#: fired still appears with a zero count rather than being absent.
CLOSED_REFUSAL_REASONS = frozenset(CHECKPOINT_REASONS - {"eligible_mechanical_work", "no_eligible_unit"})

#: Sanity bounds on the metrics a checkpoint declares. These are not queue
#: capacity limits (a checkpoint never submits anything); they exist only so
#: a malformed or hostile declaration cannot wedge the ledger with a
#: nonsensical value.
MAX_DECLARED_BYTES = 50_000_000
MAX_DECLARED_LINES = 5_000_000
MAX_TASK_ID_LEN = 256


@dataclass(frozen=True)
class IntakePolicy:
    """Deterministic suitability thresholds, versioned in every receipt.

    ``min_input_chars`` is kept under its original name for every existing
    caller and test that constructs ``IntakePolicy(min_input_chars=...)``;
    despite the name, the threshold it feeds (``byte_threshold``) has always
    been compared against UTF-8 bytes since checkpoints were added, because a
    checkpoint never has the original text to count characters from -- only
    a declared byte count. ``min_input_bytes``, when set, overrides it
    explicitly for a caller that wants the byte semantics spelled out rather
    than inherited from the legacy field name.
    """

    version: str = "localq-intake/v2"
    min_input_chars: int = 800
    min_nonblank_lines: int = 12
    min_input_bytes: int | None = None

    @property
    def byte_threshold(self) -> int:
        return self.min_input_chars if self.min_input_bytes is None else self.min_input_bytes


class AutomaticIntake:
    """Classify a bounded request, persist the decision, then submit if eligible."""

    def __init__(self, queue: LocalQueue, *, policy: IntakePolicy = IntakePolicy(),
                 clock: Callable[[], float] = time.time):
        self.queue, self.policy, self.clock = queue, policy, clock
        self.db_path = Path(queue.root) / "routing.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _migrate_checkpoint_column(db: sqlite3.Connection) -> None:
        """Fail-closed migration for a pre-checkpoint ``routing_receipts`` table.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op against a database that
        already has the table under an older column set, so it never adds a
        column an existing install is missing. This inspects
        ``PRAGMA table_info`` and issues ``ALTER TABLE ... ADD COLUMN``
        itself, exactly once (skipped once the column is present), and lets
        any failure raise rather than swallowing it and proceeding against a
        table this process never actually verified -- an untested migration
        left as a bare ``ALTER TABLE`` with no prior inspection would either
        raise ``duplicate column`` forever on a database that already has the
        column, or silently no-op forever with a mistyped column name; the
        inspection is what makes this idempotent and provably correct rather
        than merely optimistic.
        """
        columns = {row[1] for row in db.execute("PRAGMA table_info(routing_receipts)").fetchall()}
        if "checkpoint_id" not in columns:
            db.execute("ALTER TABLE routing_receipts ADD COLUMN checkpoint_id TEXT")

    def _init_db(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS routing_receipts (
                receipt_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL, policy_version TEXT NOT NULL,
                classification TEXT NOT NULL, decision TEXT NOT NULL,
                reason TEXT NOT NULL, task_type TEXT NOT NULL, caller TEXT NOT NULL,
                purpose TEXT NOT NULL, priority TEXT NOT NULL, flags_json TEXT NOT NULL,
                input_sha256 TEXT NOT NULL, input_bytes INTEGER NOT NULL,
                nonblank_lines INTEGER NOT NULL, job_id TEXT)""")
            # Mandatory migration for a database created before checkpoints
            # existed: adds the nullable column the partial unique index
            # below depends on. Must run before that index is created, and
            # before the immutability triggers (harmless either order, but
            # keeping schema evolution together is easier to audit).
            self._migrate_checkpoint_column(db)
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS routing_receipts_checkpoint_id_uq
                ON routing_receipts(checkpoint_id) WHERE checkpoint_id IS NOT NULL""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS routing_receipts_no_update
                BEFORE UPDATE ON routing_receipts BEGIN
                SELECT RAISE(ABORT, 'routing receipts are immutable'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS routing_receipts_no_delete
                BEFORE DELETE ON routing_receipts BEGIN
                SELECT RAISE(ABORT, 'routing receipts are immutable'); END""")
            db.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL, policy_version TEXT NOT NULL,
                task_id TEXT NOT NULL, task_type TEXT NOT NULL, classification TEXT NOT NULL,
                caller TEXT NOT NULL, input_bytes INTEGER NOT NULL, nonblank_lines INTEGER NOT NULL,
                flags_json TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL)""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS checkpoints_no_update
                BEFORE UPDATE ON checkpoints BEGIN
                SELECT RAISE(ABORT, 'checkpoints are append-only'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS checkpoints_no_delete
                BEFORE DELETE ON checkpoints BEGIN
                SELECT RAISE(ABORT, 'checkpoints are append-only'); END""")

    @staticmethod
    def _normalized(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _validated_flags(risk_flags: list[str] | None) -> list[str]:
        flags = [] if risk_flags is None else risk_flags
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            raise AdmissionError("risk_flags_invalid")
        return sorted(set(flags))

    def _classify_metrics(self, *, task_type: str, input_bytes: int, nonblank_lines: int,
                          classification: str, flags: list[str]) -> tuple[str, str]:
        """The one deterministic eligibility rule, on metrics alone.

        Both ``checkpoint`` (which never sees the text) and ``route``'s
        legacy ``purpose="test"`` path (which does) go through this, so the
        same UTF-8-byte threshold applies everywhere: a checkpoint minted
        from a caller's declared byte count and a route call classifying raw
        text can never disagree about whether the same-sized unit is
        eligible.
        """
        if classification not in ALLOWED_CLASSIFICATIONS:
            return "refused", "classification_refused"
        unknown = set(flags) - PROHIBITED_FLAGS
        if unknown:
            return "refused", "risk_flags_invalid"
        if flags:
            return "refused", "prohibited_risk_flags"
        if task_type not in MECHANICAL_TASKS:
            return "refused", "task_requires_cloud_or_human_judgment"
        # A configured backend may intentionally support a strict subset of
        # mechanical work. The certified Gemma lane currently supports only
        # summarize; refuse other kinds before a job can enter the queue.
        if task_type not in self.queue.allowed_task_types:
            return "refused", UNSUPPORTED_KIND_REASON
        if input_bytes < self.policy.byte_threshold and nonblank_lines < self.policy.min_nonblank_lines:
            return "refused", "below_local_delegation_threshold"
        return "eligible", "eligible_mechanical_work"

    # ---- checkpoints --------------------------------------------------

    def checkpoint(self, *, task_id: str, task_type: str, classification: str, caller: str,
                   input_bytes: int, nonblank_lines: int, risk_flags: list[str] | None = None,
                   idempotency_key: str | None = None, no_eligible_unit: bool = False) -> dict[str, Any]:
        """Record one content-free checkpoint and its deterministic decision.

        Accepts only bounded metadata: a caller-chosen task id, task type,
        classification, a UTF-8 byte count, a nonblank-line count, risk
        flags and an optional idempotency key. Never accepts document text
        or a bare content hash as the checkpoint's identity -- callers that
        already hold the content (``work_digest_file``, ``route`` itself)
        may mint one internally using a hash derived from that content as
        their own ``task_id``/idempotency key, but that is this method's
        caller choosing its own opaque correlation string, not a second
        content channel into the ledger.

        ``no_eligible_unit`` records the distinct, honest closure "there is
        no unit of work here" (design requirement: no invented work) rather
        than forcing a search that found nothing into a refusal reason that
        implies content existed and fell short.
        """
        if not isinstance(task_id, str) or not task_id or len(task_id) > MAX_TASK_ID_LEN:
            raise AdmissionError("task_id_invalid")
        if not isinstance(task_type, str) or not task_type:
            raise AdmissionError("task_type_invalid")
        if caller not in {"codex", "claude"}:
            raise AdmissionError("provenance_invalid")
        if (not isinstance(input_bytes, int) or isinstance(input_bytes, bool)
                or input_bytes < 0 or input_bytes > MAX_DECLARED_BYTES):
            raise AdmissionError("input_bytes_invalid")
        if (not isinstance(nonblank_lines, int) or isinstance(nonblank_lines, bool)
                or nonblank_lines < 0 or nonblank_lines > MAX_DECLARED_LINES):
            raise AdmissionError("nonblank_lines_invalid")
        flags = self._validated_flags(risk_flags)

        if no_eligible_unit:
            if input_bytes != 0 or nonblank_lines != 0 or flags:
                raise AdmissionError("no_eligible_unit_invalid")
            if classification not in ALLOWED_CLASSIFICATIONS:
                raise AdmissionError("classification_refused")
            status, reason = "no_eligible_unit", "no_eligible_unit"
        else:
            status, reason = self._classify_metrics(
                task_type=task_type, input_bytes=input_bytes, nonblank_lines=nonblank_lines,
                classification=classification, flags=flags)

        request = {
            "kind": "checkpoint", "task_id": task_id, "task_type": task_type,
            "classification": classification, "caller": caller,
            "input_bytes": input_bytes, "nonblank_lines": nonblank_lines, "risk_flags": flags,
        }
        request_key = idempotency_key or hashlib.sha256(self._normalized(request).encode("utf-8")).hexdigest()
        if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
            raise AdmissionError("idempotency_key_invalid")

        with self._db() as db:
            existing = db.execute("SELECT * FROM checkpoints WHERE request_key=?", (request_key,)).fetchone()
        if existing is not None:
            self._validate_existing_checkpoint(
                existing, task_id=task_id, task_type=task_type, classification=classification,
                caller=caller, input_bytes=input_bytes, nonblank_lines=nonblank_lines, flags=flags)
            return self._public_checkpoint(existing, deduplicated=True)

        values = (uuid.uuid4().hex, request_key, self.clock(), self.policy.version, task_id, task_type,
                  classification, caller, input_bytes, nonblank_lines, self._normalized(flags), status, reason)
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO checkpoints(
                    checkpoint_id,request_key,created_at,policy_version,task_id,task_type,
                    classification,caller,input_bytes,nonblank_lines,flags_json,status,reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                db.execute("COMMIT")
                row = db.execute("SELECT * FROM checkpoints WHERE request_key=?", (request_key,)).fetchone()
        except sqlite3.IntegrityError:
            with self._db() as db:
                row = db.execute("SELECT * FROM checkpoints WHERE request_key=?", (request_key,)).fetchone()
            if row is not None:
                self._validate_existing_checkpoint(
                    row, task_id=task_id, task_type=task_type, classification=classification,
                    caller=caller, input_bytes=input_bytes, nonblank_lines=nonblank_lines, flags=flags)
        if row is None:  # pragma: no cover - defensive storage failure
            raise RuntimeError("checkpoint unavailable")
        return self._public_checkpoint(row, deduplicated=False)

    def _validate_existing_checkpoint(self, row: sqlite3.Row, *, task_id: str, task_type: str,
                                      classification: str, caller: str, input_bytes: int,
                                      nonblank_lines: int, flags: list[str]) -> None:
        mismatch = (
            row["task_id"] != task_id or row["task_type"] != task_type or
            row["classification"] != classification or row["caller"] != caller or
            row["input_bytes"] != input_bytes or row["nonblank_lines"] != nonblank_lines or
            row["flags_json"] != self._normalized(flags)
        )
        if mismatch:
            raise AdmissionError("idempotency_key_conflict")

    @staticmethod
    def _public_checkpoint(row: sqlite3.Row, *, deduplicated: bool = False) -> dict[str, Any]:
        return {
            "checkpoint_id": row["checkpoint_id"], "created_at": row["created_at"],
            "policy_version": row["policy_version"], "task_id": row["task_id"],
            "task_type": row["task_type"], "classification": row["classification"],
            "caller": row["caller"], "input_bytes": row["input_bytes"],
            "nonblank_lines": row["nonblank_lines"], "risk_flags": json.loads(row["flags_json"]),
            "status": row["status"], "reason": row["reason"], "deduplicated": deduplicated,
        }

    def get_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
        if row is None:
            raise AdmissionError("checkpoint_not_found")
        return self._public_checkpoint(row)

    def audit(self) -> dict[str, Any]:
        """Content-free aggregate counts. No task id or content hash ever appears here."""
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            eligible = db.execute("SELECT COUNT(*) FROM checkpoints WHERE status='eligible'").fetchone()[0]
            no_eligible_unit = db.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE status='no_eligible_unit'").fetchone()[0]
            # "Dispatched" means a job was actually queued, not merely that a
            # receipt exists: a checkpoint consumed by a refused route call
            # gets a receipt (checkpoint_id is set on it) but no job_id, and
            # that is a refusal, not a dispatch.
            dispatched = db.execute(
                "SELECT COUNT(*) FROM checkpoints c WHERE EXISTS ("
                "SELECT 1 FROM routing_receipts r "
                "WHERE r.checkpoint_id = c.checkpoint_id AND r.job_id IS NOT NULL)").fetchone()[0]
            rows = db.execute(
                "SELECT reason, COUNT(*) AS n FROM checkpoints WHERE status='refused' GROUP BY reason").fetchall()
        refused_by_reason = {reason: 0 for reason in sorted(CLOSED_REFUSAL_REASONS)}
        for row in rows:
            if row["reason"] in CLOSED_REFUSAL_REASONS:
                refused_by_reason[row["reason"]] = row["n"]
        return {"total": total, "eligible": eligible, "dispatched": dispatched,
                "no_eligible_unit": no_eligible_unit, "refused_by_reason": refused_by_reason}

    # ---- routing --------------------------------------------------------

    def _validate_existing_receipt(self, existing: sqlite3.Row, *, task_type: str, classification: str,
                                   caller: str, purpose: str, priority: str, flags: list[str],
                                   input_sha256: str, checkpoint_id: str | None) -> None:
        """Re-validate a receipt found by request key -- on the ordinary
        lookup path and equally on an insert-race re-read -- before it is
        ever returned as deduplicated. A receipt with the same request key
        but different content, provenance or checkpoint binding is a
        conflict, never a match, even when byte/line metrics happen to
        agree.
        """
        mismatch = (
            existing["input_sha256"] != input_sha256 or
            existing["task_type"] != task_type or
            existing["classification"] != classification or
            existing["caller"] != caller or existing["purpose"] != purpose or
            existing["priority"] != priority or
            existing["flags_json"] != self._normalized(flags) or
            (existing["checkpoint_id"] or None) != (checkpoint_id or None)
        )
        if mismatch:
            raise AdmissionError("idempotency_key_conflict")

    def _bind_checkpoint(self, checkpoint_id: str, *, caller: str, task_type: str,
                         classification: str, input_bytes: int, nonblank: int,
                         flags: list[str]) -> tuple[str, str]:
        """Fetch and exactly validate the checkpoint a ``route`` call names.

        Validates caller, task type, classification, UTF-8 byte count,
        nonblank-line count and risk flags against what the checkpoint
        recorded; a mismatch on any of them is refused rather than routed.
        Returns the checkpoint's own ``(decision, reason)`` -- "local" for
        an eligible checkpoint, "refused" otherwise -- so ``route`` never
        re-derives eligibility from a checkpoint it has already bound.
        """
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise AdmissionError("checkpoint_id_invalid")
        with self._db() as db:
            row = db.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
        if row is None:
            raise AdmissionError("checkpoint_not_found")
        if row["status"] == "no_eligible_unit":
            raise AdmissionError("checkpoint_not_routable")
        if (row["caller"] != caller or row["task_type"] != task_type or
                row["classification"] != classification or row["input_bytes"] != input_bytes or
                row["nonblank_lines"] != nonblank or row["flags_json"] != self._normalized(flags)):
            raise AdmissionError("checkpoint_binding_mismatch")
        decision = "local" if row["status"] == "eligible" else "refused"
        return decision, row["reason"]

    def route(self, *, task_type: str, input: str, params: dict[str, Any] | None,
              priority: str, classification: str, caller: str, purpose: str,
              risk_flags: list[str] | None = None,
              idempotency_key: str | None = None,
              checkpoint_id: str | None = None) -> dict[str, Any]:
        if not isinstance(input, str):
            raise AdmissionError("input_invalid")
        if caller not in {"codex", "claude"} or purpose not in {"work", "test"}:
            raise AdmissionError("provenance_invalid")
        if priority not in {"interactive", "bulk"}:
            raise AdmissionError("priority_invalid")
        if not isinstance(params or {}, dict):
            raise AdmissionError("params_invalid")
        flags = self._validated_flags(risk_flags)
        input_raw = input.encode("utf-8")
        input_bytes = len(input_raw)
        nonblank = sum(1 for line in input.splitlines() if line.strip())
        input_sha256 = hashlib.sha256(input_raw).hexdigest()

        # Every purpose="work" call requires a checkpoint. purpose="test" may
        # omit one -- that exemption is for internal direct test/calibration
        # code, never for an assistant-facing tool, which never lets a
        # caller choose "test" at all (see orchestration/mcp.py).
        if checkpoint_id is not None:
            decision, reason = self._bind_checkpoint(
                checkpoint_id, caller=caller, task_type=task_type, classification=classification,
                input_bytes=input_bytes, nonblank=nonblank, flags=flags)
            # A consumed checkpoint's route request key is derived solely
            # from the checkpoint id. A caller-supplied idempotency key that
            # disagrees is refused rather than silently ignored or, worse,
            # silently honoured -- either of those would let a second,
            # differently-keyed call detach from the checkpoint it claims to
            # be consuming.
            request_key = "checkpoint:" + checkpoint_id
            if idempotency_key is not None and idempotency_key != request_key:
                raise AdmissionError("checkpoint_idempotency_conflict")
        else:
            if purpose == "work":
                raise AdmissionError("checkpoint_required")
            outcome, reason = self._classify_metrics(
                task_type=task_type, input_bytes=input_bytes, nonblank_lines=nonblank,
                classification=classification, flags=flags)
            decision = "local" if outcome == "eligible" else "refused"
            request = {
                "policy": asdict(self.policy), "task_type": task_type,
                "classification": classification, "caller": caller, "purpose": purpose,
                "priority": priority, "params": params or {}, "risk_flags": flags,
                "input_sha256": input_sha256,
            }
            request_key = idempotency_key or hashlib.sha256(self._normalized(request).encode("utf-8")).hexdigest()
        if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
            raise AdmissionError("idempotency_key_invalid")

        with self._db() as db:
            existing = db.execute("SELECT * FROM routing_receipts WHERE request_key=?", (request_key,)).fetchone()
        if existing is not None:
            self._validate_existing_receipt(
                existing, task_type=task_type, classification=classification, caller=caller,
                purpose=purpose, priority=priority, flags=flags, input_sha256=input_sha256,
                checkpoint_id=checkpoint_id)
            return self._public(existing, deduplicated=True)

        job_id = None
        if decision == "local":
            submitted = self.queue.submit(
                task_type=task_type, input=input, params=params, priority=priority,
                classification=classification, caller=caller, purpose=purpose,
                idempotency_key="route:" + request_key)
            job_id = submitted["job_id"]

        values = (
            uuid.uuid4().hex, request_key, self.clock(), self.policy.version,
            classification, decision, reason, task_type, caller, purpose, priority,
            self._normalized(flags), input_sha256, input_bytes, nonblank, job_id, checkpoint_id,
        )
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO routing_receipts(
                    receipt_id,request_key,created_at,policy_version,classification,
                    decision,reason,task_type,caller,purpose,priority,flags_json,
                    input_sha256,input_bytes,nonblank_lines,job_id,checkpoint_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                db.execute("COMMIT")
                row = db.execute("SELECT * FROM routing_receipts WHERE request_key=?", (request_key,)).fetchone()
        except sqlite3.IntegrityError:
            # An insert race: another call (a genuine concurrent retry, or a
            # hostile reuse of the same request key with different content)
            # won. The existing row is re-read and fully validated -- never
            # trusted on the strength of the key alone -- before it is
            # returned as deduplicated.
            with self._db() as db:
                row = db.execute("SELECT * FROM routing_receipts WHERE request_key=?", (request_key,)).fetchone()
            if row is not None:
                self._validate_existing_receipt(
                    row, task_type=task_type, classification=classification, caller=caller,
                    purpose=purpose, priority=priority, flags=flags, input_sha256=input_sha256,
                    checkpoint_id=checkpoint_id)
            if row is None:  # pragma: no cover - defensive storage failure
                raise RuntimeError("routing receipt unavailable")
            return self._public(row, deduplicated=True)
        if row is None:  # pragma: no cover - defensive storage failure
            raise RuntimeError("routing receipt unavailable")
        return self._public(row, deduplicated=False)

    @staticmethod
    def _public(row: sqlite3.Row, *, deduplicated: bool = False) -> dict[str, Any]:
        return {
            "receipt_id": row["receipt_id"], "created_at": row["created_at"],
            "policy_version": row["policy_version"], "classification": row["classification"],
            "decision": row["decision"], "reason": row["reason"],
            "task_type": row["task_type"], "caller": row["caller"],
            "purpose": row["purpose"], "priority": row["priority"],
            "risk_flags": json.loads(row["flags_json"]), "input_sha256": row["input_sha256"],
            "input_bytes": row["input_bytes"], "nonblank_lines": row["nonblank_lines"],
            "job_id": row["job_id"], "checkpoint_id": row["checkpoint_id"],
            "deduplicated": deduplicated, "fallback": "none",
        }

    def receipt(self, receipt_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM routing_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
        if row is None:
            raise AdmissionError("routing_receipt_not_found")
        return self._public(row)
