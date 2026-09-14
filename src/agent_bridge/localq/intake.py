"""Conservative automatic intake for the local mechanical-work queue.

The router is intentionally one-way: a request is either admitted to the local
queue or refused.  It never selects, invokes, or recommends a cloud fallback.
Classification and the routing decision are written once to a durable receipt.
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

from .spool import ALLOWED_CLASSIFICATIONS, MECHANICAL_TASKS, AdmissionError, LocalQueue


PROHIBITED_FLAGS = frozenset({
    "client_derived",
    "confidential",
    "exact_sensitive_identifiers",
    "external_side_effects",
    "licensed_review",
    "professional_judgment",
})


@dataclass(frozen=True)
class IntakePolicy:
    """Deterministic suitability thresholds, versioned in every receipt."""

    version: str = "localq-intake/v1"
    min_input_chars: int = 800
    min_nonblank_lines: int = 12


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
            db.execute("""CREATE TRIGGER IF NOT EXISTS routing_receipts_no_update
                BEFORE UPDATE ON routing_receipts BEGIN
                SELECT RAISE(ABORT, 'routing receipts are immutable'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS routing_receipts_no_delete
                BEFORE DELETE ON routing_receipts BEGIN
                SELECT RAISE(ABORT, 'routing receipts are immutable'); END""")

    @staticmethod
    def _normalized(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _classify(self, *, task_type: str, input: str, classification: str,
                  flags: list[str]) -> tuple[str, str, int]:
        if classification not in ALLOWED_CLASSIFICATIONS:
            return "refused", "classification_refused", 0
        unknown = set(flags) - PROHIBITED_FLAGS
        if unknown:
            return "refused", "risk_flags_invalid", 0
        if flags:
            return "refused", "prohibited:" + ",".join(sorted(flags)), 0
        if task_type not in MECHANICAL_TASKS:
            return "refused", "task_requires_cloud_or_human_judgment", 0
        nonblank = sum(1 for line in input.splitlines() if line.strip())
        if len(input) < self.policy.min_input_chars and nonblank < self.policy.min_nonblank_lines:
            return "refused", "below_local_delegation_threshold", nonblank
        return "local", "eligible_mechanical_work", nonblank

    def route(self, *, task_type: str, input: str, params: dict[str, Any] | None,
              priority: str, classification: str, caller: str, purpose: str,
              risk_flags: list[str] | None = None,
              idempotency_key: str | None = None) -> dict[str, Any]:
        if not isinstance(input, str):
            raise AdmissionError("input_invalid")
        if caller not in {"codex", "claude"} or purpose not in {"work", "test"}:
            raise AdmissionError("provenance_invalid")
        if priority not in {"interactive", "bulk"}:
            raise AdmissionError("priority_invalid")
        if not isinstance(params or {}, dict):
            raise AdmissionError("params_invalid")
        if not isinstance(risk_flags or [], list) or not all(isinstance(flag, str) for flag in (risk_flags or [])):
            raise AdmissionError("risk_flags_invalid")
        flags = sorted(set(risk_flags or []))
        decision, reason, nonblank = self._classify(
            task_type=task_type, input=input, classification=classification, flags=flags)
        input_raw = input.encode("utf-8")
        request = {
            "policy": asdict(self.policy), "task_type": task_type,
            "classification": classification, "caller": caller, "purpose": purpose,
            "priority": priority, "params": params or {}, "risk_flags": flags,
            "input_sha256": hashlib.sha256(input_raw).hexdigest(),
        }
        request_key = idempotency_key or hashlib.sha256(self._normalized(request).encode("utf-8")).hexdigest()
        if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
            raise AdmissionError("idempotency_key_invalid")

        with self._db() as db:
            existing = db.execute("SELECT * FROM routing_receipts WHERE request_key=?", (request_key,)).fetchone()
        if existing is not None:
            if (existing["input_sha256"] != request["input_sha256"] or
                    existing["task_type"] != task_type or
                    existing["classification"] != classification or
                    existing["caller"] != caller or existing["purpose"] != purpose or
                    existing["priority"] != priority or
                    existing["flags_json"] != self._normalized(flags)):
                raise AdmissionError("idempotency_key_conflict")
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
            self._normalized(flags), request["input_sha256"], len(input_raw), nonblank, job_id,
        )
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO routing_receipts(
                    receipt_id,request_key,created_at,policy_version,classification,
                    decision,reason,task_type,caller,purpose,priority,flags_json,
                    input_sha256,input_bytes,nonblank_lines,job_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                db.execute("COMMIT")
                row = db.execute("SELECT * FROM routing_receipts WHERE request_key=?", (request_key,)).fetchone()
        except sqlite3.IntegrityError:
            with self._db() as db:
                row = db.execute("SELECT * FROM routing_receipts WHERE request_key=?", (request_key,)).fetchone()
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
            "job_id": row["job_id"], "deduplicated": deduplicated,
            "fallback": "none",
        }

    def receipt(self, receipt_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM routing_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
        if row is None:
            raise AdmissionError("routing_receipt_not_found")
        return self._public(row)
