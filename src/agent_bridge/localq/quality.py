"""Content-free quality evidence and sticky, backend-scoped routing stops.

Readers never create files. Absence before the first successful write is unmeasured;
a missing initialized state, or unreadable/malformed state, stops every task.
Only the orchestration executor evaluates/writes automatic stops. The
operator CLI is the sole path that clears one, under the same lock.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any, Iterable, Mapping

from .. import store

WINDOW_SECONDS = 14 * 86400
FIELDS = {"disabled_at", "n", "used", "reworked", "discarded", "lower_bound", "reenabled_at"}


def _number(value: Any) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def state_path(state_root: str | Path) -> Path:
    return Path(state_root) / "routing" / "quality-state.json"


def _read_value(path: Path) -> Any:
    # Avoid the Windows replacement retry's one-second delay on first run.
    # lstat distinguishes absence from access errors and refuses dangling links.
    if stat.S_ISLNK(path.lstat().st_mode):
        raise ValueError("quality_state_invalid")
    return store.read_json_atomic(str(path)) if store.WINDOWS else store.read_json(str(path))


def read_state(state_root: str | Path) -> dict[str, dict[str, dict[str, Any]]]:
    path = state_path(state_root)
    try:
        value = _read_value(path)
    except FileNotFoundError:
        try:
            path.with_suffix(".initialized").lstat()
        except FileNotFoundError:
            return {}
        raise ValueError("quality_state_invalid")
    except RecursionError as exc:
        raise ValueError("quality_state_invalid") from exc
    return _validate_state(value)


def _validate_state(value: Any) -> dict[str, dict[str, dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ValueError("quality_state_invalid")
    for backend, tasks in value.items():
        if not isinstance(backend, str) or not backend or not isinstance(tasks, dict):
            raise ValueError("quality_state_invalid")
        for task, row in tasks.items():
            if not isinstance(task, str) or not task or not isinstance(row, dict) or set(row) != FIELDS:
                raise ValueError("quality_state_invalid")
            for key in ("disabled_at", "reenabled_at", "lower_bound"):
                if row[key] is not None and not _number(row[key]):
                    raise ValueError("quality_state_invalid")
            for key in ("n", "used", "reworked", "discarded"):
                if type(row[key]) is not int or row[key] < 0:
                    raise ValueError("quality_state_invalid")
            if (row["n"] != row["used"] + row["reworked"] + row["discarded"]
                    or row["lower_bound"] is not None and row["lower_bound"] > 1
                    or row["disabled_at"] is not None and row["reenabled_at"] is not None
                    and row["disabled_at"] <= row["reenabled_at"]):
                raise ValueError("quality_state_invalid")
    return value


def disabled_types(state_root: str | Path, backend_id: str) -> frozenset[str]:
    from .spool import MECHANICAL_TASKS
    try:
        tasks = read_state(state_root).get(backend_id, {})
    except (OSError, ValueError):
        return MECHANICAL_TASKS
    return frozenset(task for task, row in tasks.items() if row["disabled_at"] is not None)


def evaluate(rows: Iterable[Mapping[str, Any]], *, backend_id: str, now: float,
             previous: Mapping[str, Mapping[str, Any]] | None = None,
             task_types: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """Pure Wilson 95% test of not-used reviews, dated by recorded_at.

    A stop remains sticky even after its evidence ages out. Re-enabling
    starts a fresh evidence window, strictly after the operator's action.
    """
    previous = previous or {}
    counts: dict[str, Counter] = {task: Counter() for task in set(task_types) | set(previous)}
    for row in rows:
        if row["backend_id"] != backend_id or row["purpose"] != "work" or row["status"] != "complete":
            continue
        task = row["task_type"]
        counts.setdefault(task, Counter())
        raw = row["feedback_json"]
        try:
            feedback = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, RecursionError):
            continue
        if not isinstance(feedback, dict):
            continue
        at = feedback.get("recorded_at")
        after = previous.get(task, {}).get("reenabled_at")
        if (not _number(at) or not now - WINDOW_SECONDS <= at <= now
                or after is not None and at <= after):
            continue
        outcome = feedback.get("outcome")
        if isinstance(outcome, str) and outcome in {"used", "reworked", "discarded"}:
            counts[task][outcome] += 1
    result = {}
    for task, count in sorted(counts.items()):
        n = sum(count.values())
        lower = None
        if n:
            p = (count["reworked"] + count["discarded"]) / n
            z = 1.959963984540054
            lower = (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)
            lower = max(0.0, min(1.0, lower))
        old = previous.get(task, {})
        disabled_at = old.get("disabled_at")
        below = n >= 20 and lower is not None and lower > 0.5
        result[task] = {"n": n, "not_used": count["reworked"] + count["discarded"],
                        "used": count["used"], "reworked": count["reworked"],
                        "discarded": count["discarded"], "lower_bound": lower,
                        "used_rate": count["used"] / n if n else None,
                        "disabled_at": disabled_at if disabled_at is not None else now if below else None,
                        "reenabled_at": old.get("reenabled_at"),
                        "verdict": "below the quality bar" if disabled_at is not None or below
                                   else "insufficient data" if n < 20 else "meets the quality bar"}
    return result


def job_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    # Older queues did not record a backend. Never attribute that evidence
    # to whichever backend happens to be configured today.
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    backend = "backend_id" if "backend_id" in columns else "NULL AS backend_id"
    cursor = db.execute(f"SELECT {backend}, task_type, purpose, status, feedback_json FROM jobs")
    return [dict(zip(("backend_id", "task_type", "purpose", "status", "feedback_json"), row)) for row in cursor]


def update(state_root: str | Path, queue_path: str | Path, backend_id: str,
           task_types: Iterable[str], *, now: float | None = None) -> dict[str, Any]:
    """Called only by the orchestration executor; serializes with enable."""
    path = state_path(state_root)
    with store.file_lock(str(path.with_suffix(".lock")), timeout=2.0):
        state = read_state(state_root)
        with closing(sqlite3.connect(Path(queue_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = job_rows(db)
        measured = evaluate(rows, backend_id=backend_id, now=time.time() if now is None else now,
                            previous=state.get(backend_id), task_types=task_types)
        tasks = state.setdefault(backend_id, {})
        for task, row in measured.items():
            # Preserve the evidence that actually caused a sticky stop.
            if tasks.get(task, {}).get("disabled_at") is None:
                tasks[task] = {key: row[key] for key in FIELDS}
        if state != read_state(state_root) or not path.with_suffix(".initialized").exists():
            _write_state(path, state)
        return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    store.atomic_write_json(str(path), state)
    # An abandoned lock is harmless. Only a completed state write can mark
    # initialization; a later loss of that state must still fail closed.
    marker = path.with_suffix(".initialized")
    if not marker.exists():
        store.atomic_write_json(str(marker), {"version": 1})


def enable(state_root: str | Path, backend_id: str, task_type: str, *, now: float | None = None) -> None:
    path = state_path(state_root)
    with store.file_lock(str(path.with_suffix(".lock")), timeout=2.0):
        state = read_state(state_root)
        state.setdefault(backend_id, {})[task_type] = {
            "disabled_at": None, "reenabled_at": time.time() if now is None else now,
            "n": 0, "used": 0, "reworked": 0, "discarded": 0, "lower_bound": None}
        _write_state(path, state)


def _recover_state(path: Path, *, now: float) -> dict[str, dict[str, dict[str, Any]]]:
    """Salvage independent rows, even when another row invalidates the state."""
    try:
        value = _read_value(path)
    except (FileNotFoundError, ValueError, RecursionError):
        return {}
    recovered = {}
    if not isinstance(value, dict):
        return recovered
    for backend, tasks in value.items():
        if not isinstance(backend, str) or not backend or not isinstance(tasks, dict):
            continue
        recovered[backend] = {}
        for task, row in tasks.items():
            if not isinstance(task, str) or not task or not isinstance(row, dict):
                continue
            try:
                _validate_state({backend: {task: row}})
            except ValueError:
                # Damaged fields must not erase a sticky stop or a valid
                # evidence reset. An unreadable stop timestamp stays stopped.
                disabled = row.get("disabled_at")
                reenabled = row.get("reenabled_at")
                reenabled = reenabled if _number(reenabled) else None
                if disabled is not None and not _number(disabled):
                    disabled, reenabled = now, None
                if disabled is not None and reenabled is not None and disabled <= reenabled:
                    reenabled = None
                row = {"disabled_at": disabled, "reenabled_at": reenabled,
                       "n": 0, "used": 0, "reworked": 0, "discarded": 0, "lower_bound": None}
            recovered[backend][task] = row
    return recovered


def repair(state_root: str | Path, queue_path: str | Path, backend_id: str,
           task_types: Iterable[str], *, now: float | None = None) -> dict[str, Any]:
    """Operator-only rebuild from current evidence, retaining readable stops."""
    path = state_path(state_root)
    with store.file_lock(str(path.with_suffix(".lock")), timeout=2.0):
        at = time.time() if now is None else now
        state = _recover_state(path, now=at)
        with closing(sqlite3.connect(Path(queue_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = job_rows(db)
        backends = set(state) | {backend_id} | {row["backend_id"] for row in rows if row["backend_id"]}
        for backend in sorted(backends):
            tasks = state.setdefault(backend, {})
            measured = evaluate(rows, backend_id=backend, now=at, previous=tasks,
                                task_types=task_types if backend == backend_id else ())
            for task, row in measured.items():
                if tasks.get(task, {}).get("disabled_at") is None:
                    tasks[task] = {key: row[key] for key in FIELDS}
        _write_state(path, _validate_state(state))
        return state


def attention(state_root: str | Path, backend_id: str) -> list[str]:
    try:
        tasks = read_state(state_root).get(backend_id, {})
    except (OSError, ValueError):
        return ["Local jobs are below the quality bar because quality state is unavailable; stopped routing them. Repair with setup_bridge.py local-quality --repair"]
    return [f"Local {task} jobs are below the quality bar ({row['used']}/{row['n']} used); "
            f"stopped routing them. Re-enable with setup_bridge.py local-quality --enable {task}"
            for task, row in sorted(tasks.items()) if row["disabled_at"] is not None]
