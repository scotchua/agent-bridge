"""Content-free weekly reliability accounting for the bridge.

This reader deliberately opens existing state read-only.  It neither imports
queue classes (some constructors create state) nor exposes any request, brief,
provider, or harness text.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from . import config as broker_config
from . import drift, store
from .errors import ErrorCategory
from .orchestration import config as orchestration_config

_SAFE_EXECUTION_CODES = frozenset({
    "interrupted_requires_reconciliation", "execution_harness_timeout",
    "execution_receipt_unreadable", "execution_receipt_invalid",
    "source_snapshot_unavailable", "source_repository_changed",
    "brief_changed_after_admission", "model_reserved", "model_invalid",
    "reservation_policy_check_failed", "claude_config_dir_unavailable",
    "execution_worker_platform_unsupported", "verify_argv_invalid",
    "verify_argv_rejected", "verify_not_runnable", "unreadable",
})
_SAFE_LOCAL_CODES = frozenset({
    "executor_lease_lost", "execution_timeout", "cancel_requested",
    "waiting_on_dependency", "unreadable",
})
_SAFE_SOURCE_REASONS = frozenset({
    "source_snapshot_unavailable", "git_config_changed", "head_changed",
    "status_changed", "dirty_content_changed", "source_repository_changed",
})
_SAFE_STAGE_REASONS = frozenset({
    "interrupted_requires_reconciliation", "liveness_reconciliation_required",
    "lease_expired", "no_fresh_eligible_route", "unreadable",
})
_TEST_ID = re.compile(r"^[A-Za-z0-9_./:-]{1,240}$")
_GH_OUTPUT_LIMIT = 1_000_000


def _count(items: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(items).items()))


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _fixed(value: object, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unrecognized"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = store.read_json(str(path))
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _execution(root: Path | None, since: float) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {key: [] for key in (
        "provider", "state", "harness_status", "harness_verdict", "error_detail", "source_integrity_reasons")}
    waits: list[float] = []
    runs: list[float] = []
    if root is None or not root.is_dir():
        return {"present": False, "by_provider": {}, "by_state": {}, "by_harness_status": {},
                "by_harness_verdict": {}, "by_error_detail": {}, "by_source_integrity_reasons": {},
                "queue_wait_seconds": {"median": None, "p90": None}, "run_seconds": {"median": None, "p90": None}}
    try:
        entries = list(root.iterdir())
    except OSError:
        entries = []
        buckets["state"].append("unreadable")
    for directory in entries:
        if not directory.is_dir():
            continue
        receipt = _read_json(directory / "receipt.json")
        if receipt is None:
            buckets["state"].append("unreadable")
            continue
        submitted = _epoch(receipt.get("submitted_at"))
        if submitted is None:
            buckets["state"].append("unreadable")
            continue
        if submitted < since:
            continue
        buckets["provider"].append(_fixed(receipt.get("provider"), frozenset({"claude", "codex", "local"})))
        buckets["state"].append(_fixed(receipt.get("state"), frozenset({"queued", "running", "complete", "failed", "blocked"})))
        harness = receipt.get("harness") if isinstance(receipt.get("harness"), dict) else {}
        buckets["harness_status"].append(_fixed(harness.get("harness_status"), frozenset({"complete", "failed", "aborted", "verification_failed"})))
        buckets["harness_verdict"].append(_fixed(harness.get("harness_verdict"), frozenset({"guest_receipt", "runtime_aborted", "receipt_absent", "receipt_not_an_object", "receipt_status_unknown", "receipt_unparseable", "read", "read_failure", "refused", "translation_refused"})))
        detail = receipt.get("error_detail", harness.get("error_detail"))
        if detail is not None:
            buckets["error_detail"].append(_fixed(detail, _SAFE_EXECUTION_CODES))
        reasons = receipt.get("source_integrity_reasons")
        if isinstance(reasons, list):
            buckets["source_integrity_reasons"].extend(_fixed(reason, _SAFE_SOURCE_REASONS) for reason in reasons)
        started, finished = _epoch(receipt.get("started_at")), _epoch(receipt.get("finished_at"))
        if submitted is not None and started is not None and started >= submitted:
            waits.append(started - submitted)
        if started is not None and finished is not None and finished >= started:
            runs.append(finished - started)
    return {"present": True, **{f"by_{key}": _count(value) for key, value in buckets.items()},
            "queue_wait_seconds": {"median": _percentile(waits, .5), "p90": _percentile(waits, .9)},
            "run_seconds": {"median": _percentile(runs, .5), "p90": _percentile(runs, .9)}}


def _consultations(root: Path, since: float) -> dict[str, Any]:
    directions: list[str] = []; outcomes: list[str] = []; categories: list[str] = []
    jobs = root / "jobs"
    if not jobs.is_dir():
        return {"present": False, "by_direction": {}, "by_outcome": {}, "by_error_category": {}}
    try:
        entries = list(jobs.iterdir())
    except OSError:
        return {"present": True, "by_direction": {}, "by_outcome": {"unreadable": 1}, "by_error_category": {}}
    known_categories = frozenset(item.value for item in ErrorCategory)
    for directory in entries:
        if not directory.is_dir():
            continue
        status = _read_json(directory / "status.json")
        if status is None:
            outcomes.append("unreadable")
            continue
        observed = _epoch(status.get("updated_at"))
        if observed is None:
            outcomes.append("unreadable")
            continue
        if observed < since:
            continue
        caller, peer = status.get("caller"), status.get("peer")
        directions.append(f"{caller}->{peer}" if caller in {"claude", "codex"} and peer in {"claude", "codex"} else "unrecognized")
        outcomes.append(_fixed(status.get("status"), frozenset({"queued", "running", "complete", "failed", "timed_out", "cancelled"})))
        category = status.get("error_category")
        if category is not None:
            categories.append(_fixed(category, known_categories))
    return {"present": True, "by_direction": _count(directions), "by_outcome": _count(outcomes), "by_error_category": _count(categories)}


def _readonly(path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect("file:" + path.resolve().as_posix().replace("?", "%3F").replace("#", "%23") + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None


def _local_queue(root: Path | None, since: float) -> dict[str, Any]:
    if root is None or not root.is_dir():
        return {"present": False, "by_state": {}, "by_failure_code": {}}
    if not (root / "localq.sqlite3").is_file():
        return {"present": True, "by_state": {}, "by_failure_code": {}}
    db = _readonly(root / "localq.sqlite3")
    if db is None:
        return {"present": True, "by_state": {"unreadable": 1}, "by_failure_code": {}}
    try:
        rows = db.execute("SELECT status,error FROM jobs WHERE submitted_at>=?", (since,)).fetchall()
    except sqlite3.Error:
        return {"present": True, "by_state": {"unreadable": 1}, "by_failure_code": {}}
    finally:
        db.close()
    return {"present": True, "by_state": _count(_fixed(row[0], frozenset({"queued", "running", "complete", "failed", "cancelled", "expired", "unknown"})) for row in rows),
            "by_failure_code": _count(_fixed(row[1].split(":", 1)[0], _SAFE_LOCAL_CODES) for row in rows if row[1])}


def _stages(path: Path | None, since: float, now: float) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"present": False, "by_blocked_reason": {}, "blocked_over_24_hours": 0}
    db = _readonly(path)
    if db is None:
        return {"present": True, "by_blocked_reason": {"unreadable": 1}, "blocked_over_24_hours": 0}
    try:
        rows = db.execute("SELECT blocked_reason,updated_at FROM stages WHERE state='blocked' AND updated_at>=?", (since,)).fetchall()
    except sqlite3.Error:
        return {"present": True, "by_blocked_reason": {"unreadable": 1}, "blocked_over_24_hours": 0}
    finally:
        db.close()
    return {"present": True, "by_blocked_reason": _count(_fixed(row[0], _SAFE_STAGE_REASONS) for row in rows),
            "blocked_over_24_hours": sum(1 for row in rows if isinstance(row[1], (int, float)) and now - row[1] > 86400)}


def _gh(argv: list[str]) -> tuple[int | None, str, bool]:
    """Run gh without a shell and retain at most one fixed-size capture.

    Readers continue draining after the cap so a noisy child cannot deadlock
    on a full pipe, but its output is neither retained nor emitted.
    """
    try:
        process = subprocess.Popen(argv, shell=False, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=False)
    except OSError:
        return None, "", False
    captured = bytearray()
    lock = threading.Lock()

    def drain(stream: Any) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            with lock:
                if len(captured) < _GH_OUTPUT_LIMIT:
                    captured.extend(chunk[:_GH_OUTPUT_LIMIT - len(captured)])

    readers = [threading.Thread(target=drain, args=(stream,), daemon=True)
               for stream in (process.stdout, process.stderr)]
    for reader in readers: reader.start()
    try:
        code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        code = None
    for reader in readers: reader.join()
    for stream in (process.stdout, process.stderr):
        stream.close()
    with lock:
        bounded = len(captured) < _GH_OUTPUT_LIMIT
        text = bytes(captured).decode("utf-8", "replace")
    return code, text, bounded


def _ci() -> dict[str, Any]:
    gh = shutil.which("gh")
    if not gh:
        return {"note": "gh_unavailable", "failure_rate_by_job_name": {}, "frequent_failing_test_ids": {}}
    try:
        code, output, bounded = _gh([gh, "run", "list", "--repo", "scotchua/agent-bridge", "--json", "databaseId", "--limit", "100"])
        runs = json.loads(output) if code == 0 and bounded else []
        if not isinstance(runs, list): raise ValueError
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {"note": "gh_unavailable", "failure_rate_by_job_name": {}, "frequent_failing_test_ids": {}}
    totals: Counter[str] = Counter(); failed: Counter[str] = Counter(); tests: Counter[str] = Counter()
    for run in runs[:100]:
        run_id = run.get("databaseId") if isinstance(run, dict) else None
        if not isinstance(run_id, int): continue
        try:
            code, output, bounded = _gh([gh, "run", "view", str(run_id), "--repo", "scotchua/agent-bridge", "--json", "jobs"])
            payload = json.loads(output) if code == 0 and bounded else {}
            jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
            log_code, logs, logs_bounded = _gh([gh, "run", "view", str(run_id), "--repo", "scotchua/agent-bridge", "--log-failed"])
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
            continue
        for job in jobs:
            if isinstance(job, dict) and isinstance(job.get("name"), str):
                totals[job["name"]] += 1
                if job.get("conclusion") == "failure": failed[job["name"]] += 1
        if logs_bounded and log_code in (0, 1):
            for test_id in re.findall(r"(?:FAILED|ERROR)\s+([A-Za-z0-9_./:-]+)", logs):
                if _TEST_ID.fullmatch(test_id): tests[test_id] += 1
    return {"note": "ok", "failure_rate_by_job_name": {name: {"failed": failed[name], "total": total, "rate": round(failed[name] / total, 4)} for name, total in sorted(totals.items())}, "frequent_failing_test_ids": dict(tests.most_common(20))}


def build_report(days: int = 7, *, ci: bool = False, now: float | None = None) -> dict[str, Any]:
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    now = time.time() if now is None else now
    since = now - days * 86400
    try:
        broker = broker_config.load()
        broker_root = Path(broker.state_root)
    except Exception:
        broker_root = Path(".") / "__unreadable__"
    orchestration_path = drift.discover_config()
    try:
        orchestration = orchestration_config.load(orchestration_path) if orchestration_path else None
    except Exception:
        orchestration = None
    report = {"window_days": days, "execution": _execution(None if orchestration is None else orchestration.execution_queue_root, since),
              "consultations": _consultations(broker_root, since), "local_queue": _local_queue(None if orchestration is None else orchestration.local_queue_root, since),
              "stage_health": _stages(None if orchestration is None else orchestration.capacity_db, since, now)}
    if ci: report["ci"] = _ci()
    return report


def _markdown(value: dict[str, Any]) -> str:
    lines = [f"# Bridge reliability report ({value['window_days']} days)"]
    for title, key in (("Execution", "execution"), ("Consultations", "consultations"), ("Local queue", "local_queue"), ("Stage health", "stage_health")):
        lines.extend(("", f"## {title}", "```json", json.dumps(value[key], sort_keys=True, indent=2), "```"))
    if "ci" in value: lines.extend(("", "## CI", "```json", json.dumps(value["ci"], sort_keys=True, indent=2), "```"))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Content-free bridge reliability report")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args(argv)
    try: report = build_report(args.days, ci=args.ci)
    except ValueError as exc: parser.error(str(exc))
    print(json.dumps(report, sort_keys=True) if args.json else _markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
