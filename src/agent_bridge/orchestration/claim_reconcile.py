"""Report where task-record claims and routing-ledger stages disagree, and nothing else.

Two records say who owns a stage.  The task record's ``workload.claim``
(written under its claim lock) is the ownership record; the routing ledger's
stage row is the orchestrator's assignment.  CAPACITY.md settles a
disagreement: the task-record claim governs, and reconciling keeps the current
task-record owner unless ownership moves through the task-record claim process.

**Report only.**  This module opens the ledger read-only and reads each task
record once.  It never renews, releases, completes, archives or reassigns
anything on either side, so running it cannot move ownership.  Each finding
carries both sides' fencing values (the task record's ``workload.revision``,
the ledger's ``revision`` and ``assignment``) so the operator who acts on it
can pass the current expected revision rather than guess one.

**Expiry is a signal, never a transfer.**  An expired lease on either side is
reported as a notice.  Neither side's lease expiry makes the other side's
owner the owner.

**Fail closed.**  A task record that is missing, unreadable, outside the task
root or the wrong shape is reported as such; no claim is inferred from it.
The caller gets owner and stage identifiers from a task record only when they
match the opaque-identifier pattern the claim tool enforces, and no other text.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any

from .config import OrchestrationConfigError, load
from .task_closure import _safe_item_id

#: The claim tool's opaque-identifier rule.  Anything else is not reported.
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
#: Release reasons the claim tool writes.
_RELEASE_REASONS = frozenset({"completed", "handoff", "live-owner-checked-inactive"})
#: Task records are small; refuse anything absurd.
MAX_RECORD_BYTES = 4 * 1024 * 1024

#: A finding that needs an operator decision before work continues.
CONFLICT = "conflict"
#: A finding worth seeing that does not by itself contradict ownership.
NOTICE = "notice"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # An integer too large for a float is not a timestamp.
        return None
    return number if math.isfinite(number) else None


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _read_record(tasks_root: str | Path, item_id: str) -> bytes | None:
    """Read ``<root>/<item_id>/task.json`` without following a link at either step.

    The item directory and the record are opened relative to the already-open
    parent, with ``O_NOFOLLOW``, so swapping either for a link after a check
    cannot redirect the read outside the root.  Returns None for anything that
    is not a regular file of at most ``MAX_RECORD_BYTES``.
    """
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("no-follow relative open unsupported")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    root_fd = os.open(Path(tasks_root).resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY)
    try:
        item_fd = os.open(item_id, flags | os.O_DIRECTORY, dir_fd=root_fd)
        try:
            # O_NONBLOCK keeps a FIFO planted as task.json from hanging the open.
            fd = os.open("task.json", flags | os.O_NONBLOCK, dir_fd=item_fd)
        finally:
            os.close(item_fd)
    finally:
        os.close(root_fd)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        data = handle.read(MAX_RECORD_BYTES + 1)
    return None if len(data) > MAX_RECORD_BYTES else data


def read_task_claim(tasks_root: str | Path, item_id: object) -> dict:
    """Return ``{"status": ..., ...}`` for one task record's claim.

    ``status`` is ``no_record``, ``unreadable``, ``malformed``, ``no_claim``
    or ``claim``.  Only the last carries ``claim`` (owner, stage, lease and
    release fields, already validated) and ``revision``.
    """
    if not _safe_item_id(item_id):
        return {"status": "no_record"}
    try:
        data = _read_record(tasks_root, item_id)
        if data is None:
            return {"status": "unreadable"}
        value = json.loads(data.decode("utf-8"))
    except FileNotFoundError:
        return {"status": "no_record"}
    except (OSError, ValueError, RecursionError):
        # RecursionError: deeply nested JSON is rejected, not a crash.
        return {"status": "unreadable"}
    if not isinstance(value, dict):
        return {"status": "malformed"}
    workload = value.get("workload", {"revision": 0, "claim": None})
    if not isinstance(workload, dict):
        return {"status": "malformed"}
    revision = workload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return {"status": "malformed"}
    claim = workload.get("claim")
    if claim is None:
        return {"status": "no_claim", "revision": revision}
    if not isinstance(claim, dict):
        return {"status": "malformed", "revision": revision}
    owner, stage = _token(claim.get("owner")), _token(claim.get("stage"))
    lease = _number(claim.get("lease_expires_at"))
    released_at = claim.get("released_at")
    reason = claim.get("release_reason")
    if (owner is None or stage is None or lease is None
            or (released_at is not None and _number(released_at) is None)
            or (released_at is None and reason is not None)
            or (released_at is not None
                and (not isinstance(reason, str) or reason not in _RELEASE_REASONS))):
        return {"status": "malformed", "revision": revision}
    return {"status": "claim", "revision": revision,
            "claim": {"owner": owner, "stage": stage, "lease_expires_at": lease,
                      "active": released_at is None,
                      "released_at": _number(released_at), "release_reason": reason}}


def _ledger_rows(ledger: str | Path) -> list[dict]:
    """Every live stage row, through a read-only connection."""
    uri = Path(ledger).resolve(strict=True).as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    db.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(stages)")}
        if not columns:
            raise sqlite3.DatabaseError("stages table missing")
        where = " WHERE archived_at IS NULL" if "archived_at" in columns else ""
        rows = [dict(row) for row in db.execute(
            f"SELECT * FROM stages{where} ORDER BY item_id, stage")]
    finally:
        db.close()
    for row in rows:
        # Ledgers older than the assignment column have assignment zero.
        row.setdefault("assignment", 0)
    return rows


def _finding(kind: str, severity: str, item_id: str, stage: str | None,
             task: dict, row: dict | None) -> dict:
    claim = task.get("claim")
    finding: dict[str, Any] = {
        "item_id": item_id, "stage": stage, "kind": kind, "severity": severity,
        "task_record": {"status": task["status"], "revision": task.get("revision"),
                        "owner": claim and claim["owner"],
                        "stage": claim and claim["stage"],
                        "active": claim and claim["active"],
                        "lease_expires_at": claim and claim["lease_expires_at"],
                        "release_reason": claim and claim["release_reason"]},
        "ledger": None if row is None else {
            "state": row["state"], "owner_id": row["owner_id"],
            "owner_route": row["owner_route"], "lease_until": row["lease_until"],
            "revision": row["revision"], "assignment": row["assignment"]},
    }
    if claim and claim["active"]:
        # CAPACITY.md: the task-record claim governs a disagreement.
        finding["governing_owner"] = claim["owner"]
    return finding


def _compare(item_id: str, rows: list[dict], task: dict, now: float) -> list[dict]:
    findings = []
    claim = task.get("claim")
    by_stage = {row["stage"]: row for row in rows}
    record_kind = {"unreadable": ("task_record_unreadable", CONFLICT),
                   "malformed": ("task_record_malformed", CONFLICT),
                   "no_record": ("task_record_missing", NOTICE)}.get(task["status"])
    if record_kind:
        # One finding per ledger row, so each carries that row's fencing values.
        for row in rows or [None]:
            findings.append(_finding(*record_kind, item_id,
                                     None if row is None else row["stage"], task, row))
    if claim and claim["active"] and claim["lease_expires_at"] < now:
        findings.append(_finding("task_lease_expired", NOTICE, item_id, claim["stage"],
                                 task, by_stage.get(claim["stage"])))
    if claim and claim["active"] and claim["stage"] not in by_stage:
        findings.append(_finding("task_claim_stage_not_in_ledger", NOTICE, item_id,
                                 claim["stage"], task, None))
    for row in rows:
        stage = row["stage"]
        matches = claim is not None and claim["stage"] == stage
        active = matches and claim["active"]
        state = row["state"]
        if state == "reconcile_required":
            findings.append(_finding("ledger_reconcile_required", CONFLICT, item_id,
                                     stage, task, row))
        if state == "owned":
            lease = _number(row["lease_until"])
            if lease is None or lease < now:
                findings.append(_finding("ledger_lease_expired", NOTICE, item_id,
                                         stage, task, row))
        if active:
            if state == "owned" and row["owner_id"] != claim["owner"]:
                findings.append(_finding("owner_mismatch", CONFLICT, item_id, stage,
                                         task, row))
            elif state == "complete":
                findings.append(_finding("complete_in_ledger_only", CONFLICT, item_id,
                                         stage, task, row))
            elif state in ("pending", "blocked"):
                findings.append(_finding("unowned_in_ledger_only", NOTICE, item_id,
                                         stage, task, row))
        elif matches and state == "owned":
            findings.append(_finding("released_in_task_record_only", CONFLICT, item_id,
                                     stage, task, row))
        elif state == "owned" and not (claim and claim["active"]):
            findings.append(_finding("ledger_owner_without_task_claim", NOTICE, item_id,
                                     stage, task, row))
        elif state == "owned":
            # The task record's one active claim names a different stage.
            findings.append(_finding("ledger_owner_for_other_stage", NOTICE, item_id,
                                     stage, task, row))
    return findings


def reconcile(ledger: str | Path, tasks_root: str | Path, *, now: float,
              items: list[str] | None = None) -> list[dict]:
    """Compare every live ledger item (or only ``items``) with its task record.

    Returns findings in a stable order.  Writes nothing on either side.
    """
    by_item: dict[str, list[dict]] = {}
    for row in _ledger_rows(ledger):
        by_item.setdefault(row["item_id"], []).append(row)
    wanted = sorted(by_item) if items is None else sorted(set(items))
    findings = []
    for item_id in wanted:
        findings.extend(_compare(item_id, by_item.get(item_id, []),
                                 read_task_claim(tasks_root, item_id), now))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report disagreements between task-record claims and the routing "
                    "ledger. Read only: never moves ownership on either side.")
    parser.add_argument("--config", required=True, help="Private orchestration config path.")
    parser.add_argument("--tasks-root", required=True, type=Path,
                        help="Directory holding one <item_id>/task.json per task.")
    parser.add_argument("--item", action="append", dest="items",
                        help="Only this item id. Repeatable. Default: every live ledger item.")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
    except (OSError, OrchestrationConfigError, ValueError) as exc:
        print(f"claim reconcile: config unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        findings = reconcile(config.capacity_db, args.tasks_root, now=time.time(),
                             items=args.items)
    except Exception as exc:  # noqa: BLE001 - report the type only, never a traceback
        print(f"claim reconcile failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    conflicts = sum(1 for finding in findings if finding["severity"] == CONFLICT)
    print(json.dumps({"conflicts": conflicts, "findings": findings}, indent=2, sort_keys=True))
    # Exit 3 lets a scheduled caller notice conflicts without parsing output.
    return 3 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
