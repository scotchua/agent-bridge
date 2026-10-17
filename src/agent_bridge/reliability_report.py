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
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from . import config as broker_config
from . import drift, store
from .errors import ErrorCategory
from .orchestration import config as orchestration_config
from . import runner

# Every code ExecutionAdmissionError can carry, by its leading token.  A
# test scans src/ for those literals and fails when one is missing here, so a
# new refusal cannot silently report as "unrecognized".
_QUEUE_PRIVACY_REASONS = (
    "acl_unenforceable", "acl_unverified", "directory_absent",
    "directory_not_owner_only", "file_absent", "file_changed_while_reading",
    "file_not_owner_only", "file_not_regular", "file_owned_by_another_account",
    "file_too_large", "file_unreadable", "lock_unavailable", "lock_unsupported",
    "path_traverses_a_link", "path_unreadable", "replace_failed",
    "replace_identity_changed", "secure_writer_required",
)
_SAFE_EXECUTION_CODES = frozenset({
    # Written by the queue or the report itself, not raised.
    "interrupted_requires_reconciliation", "command_timeout",
    "source_snapshot_unavailable", "source_repository_changed", "model_reserved",
    "unreadable",
    # Raised as ExecutionAdmissionError.
    "brief_changed_after_admission", "brief_invalid", "brief_not_utf8",
    "brief_size_invalid", "classification_not_eligible",
    "claude_config_dir_unavailable", "claude_verification_required",
    "cleanup_timeout_invalid", "client_derived_dispatch_not_admitted",
    "execution_configuration_missing", "execution_deadline_invalid",
    "execution_harness_timeout", "execution_job_not_found",
    "execution_job_not_terminal", "execution_outcome_harness_ok_invalid",
    "execution_outcome_harness_status_invalid",
    "execution_outcome_harness_verdict_invalid", "execution_outcome_incomplete",
    "execution_outcome_inconsistent", "execution_outcome_not_a_mapping",
    "execution_outcome_returncode_invalid", "execution_receipt_invalid",
    "execution_receipt_unreadable", "execution_worker_not_configured",
    "execution_worker_platform_unsupported", "idempotency_conflict",
    "interval_invalid", "job_id_invalid", "job_kind_invalid", "model_invalid",
    "model_reserved_to_operator", "paid_fallback_forbidden",
    "provider_not_eligible_for_caller", "python_harness_not_executable",
    "repo_and_brief_must_be_absolute", "repo_invalid",
    "reservation_policy_check_failed", "review_direction_invalid",
    "review_harness_unavailable", "review_provider_unsupported",
    "review_receipt_outside_job", "review_request_invalid",
    "review_verify_argv_forbidden", "stage_revision_invalid", "timeout_invalid",
    "total_timeout_invalid", "verify_argv_invalid", "verify_argv_rejected",
    "verify_not_runnable", "verify_timeout_invalid",
    "windows_wsl_configuration_missing",
    # The stage-binding recheck in PR #61 (R5).
    "execution_stage_binding_invalid", "execution_stage_ledger_unavailable",
    "stage_assignment_invalid",
    *(f"queue_{reason}" for reason in _QUEUE_PRIVACY_REASONS),
})
# Exception class names the queue records in a receipt's top-level "error".
_SAFE_ERROR_CLASSES = frozenset({
    "ExecutionAdmissionError", "OSError", "PermissionError", "FileNotFoundError",
    "IsADirectoryError", "NotADirectoryError", "TimeoutError", "ValueError",
    "TypeError", "KeyError", "RuntimeError", "UnicodeDecodeError",
    "JSONDecodeError", "TaskError", "RoutingError", "CalledProcessError",
    "TimeoutExpired",
})
# localq/spool.py: _failure_text names, its client-data refusals, lease and
# timeout outcomes, and the prefixes of queued-job notes.  A test keeps this a
# superset of spool._BACKEND_EXCEPTION_NAMES.
_SAFE_LOCAL_CODES = frozenset({
    "executor_lease_lost", "execution_timeout", "cancel_requested",
    "waiting_on_dependency", "deferred", "backend_failure",
    "client_data_backend_unsupported", "client_data_health_unverified",
    "RuntimeError", "ValueError", "TypeError", "OSError", "PermissionError",
    "FileNotFoundError", "KeyError", "IndexError", "UnicodeError",
    "UnicodeDecodeError", "JSONDecodeError", "TimeoutExpired", "unreadable",
})
_LOCAL_FAILURE_STATES = frozenset({"failed", "unknown", "cancelled"})
_SAFE_SOURCE_REASONS = frozenset({
    "source_snapshot_unavailable", "git_config_changed", "head_changed",
    "status_changed", "dirty_content_changed", "source_repository_changed",
})
_SAFE_STAGE_REASONS = frozenset({
    "interrupted_requires_reconciliation", "liveness_reconciliation_required",
    "lease_expired", "no_fresh_eligible_route", "unreadable",
})
_TEST_ID = re.compile(r"^[A-Za-z0-9_./:-]{1,240}$")
_CODE = re.compile(r"[a-z][a-z0-9_]*")
_CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
# pytest prints "FAILED path::test"; unittest prints "FAIL: name (dotted.id)"
# and "ERROR: name (dotted.id)".  GitHub log lines carry a timestamp prefix.
_PYTEST_FAILURE = re.compile(r"(?:FAILED|ERROR)\s+([A-Za-z0-9_./:-]+)")
_UNITTEST_FAILURE = re.compile(r"\b(?:FAIL|ERROR): [A-Za-z_][A-Za-z0-9_]* \(([A-Za-z0-9_.]+)\)")
_GH_OUTPUT_LIMIT = 1_000_000
_SOURCE_INTEGRITY_PREFIX = "source repository integrity changed: "
_COMMAND_TIMEOUT = re.compile(r"^command timed out after [0-9]+s$")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


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


def _execution_detail(value: object) -> tuple[str, list[str]]:
    """Convert known harness diagnostics to fixed report labels only."""
    if not isinstance(value, str):
        return "unrecognized", []
    if value.startswith(_SOURCE_INTEGRITY_PREFIX):
        reasons = value.removeprefix(_SOURCE_INTEGRITY_PREFIX).split(",")
        return "source_repository_changed", [
            reason for reason in reasons if reason in _SAFE_SOURCE_REASONS
        ]
    if _COMMAND_TIMEOUT.fullmatch(value):
        return "command_timeout", []
    # The queue writes "verify_not_runnable: <reason>" and
    # "model_reserved_to_operator:<model>"; only the leading code is reported.
    code = value.split(":", 1)[0].strip()
    return (_fixed(code, _SAFE_EXECUTION_CODES) if _CODE.fullmatch(code)
            else "unrecognized"), []


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


_EXECUTION_BUCKETS = ("provider", "state", "harness_status", "harness_verdict",
                      "error_detail", "error_class", "source_integrity_reasons")


def _empty_execution(present: bool) -> dict[str, Any]:
    return {"present": present, **{f"by_{key}": {} for key in _EXECUTION_BUCKETS},
            "queue_wait_seconds": {"median": None, "p90": None},
            "run_seconds": {"median": None, "p90": None}}


def _config_unreadable(section: dict[str, Any], key: str) -> dict[str, Any]:
    """An installed but unreadable config is never reported as absent or clean."""
    return {**section, "present": True, "config": "unreadable", key: {"unreadable": 1}}


def _execution(root: Path | None, since: float) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {key: [] for key in _EXECUTION_BUCKETS}
    waits: list[float] = []
    runs: list[float] = []
    if root is None or not root.is_dir():
        return _empty_execution(False)
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
        # Queued, running, blocked and refused jobs have no harness outcome.
        # They are absent from the harness buckets rather than "unrecognized".
        harness = receipt.get("harness")
        if isinstance(harness, dict):
            buckets["harness_status"].append(_fixed(harness.get("harness_status"), frozenset({"complete", "failed", "aborted", "verification_failed", "no_patch"})))
            buckets["harness_verdict"].append(_fixed(harness.get("harness_verdict"), frozenset({"guest_receipt", "runtime_aborted", "receipt_absent", "receipt_not_an_object", "receipt_status_unknown", "receipt_unparseable", "read", "read_failure", "refused", "translation_refused"})))
        else:
            harness = {}
        # At most one error_detail entry per job.  The top-level "error" is an
        # exception class name, counted on its own, except for the fixed code
        # a reconciliation block writes there with no detail.
        detail = receipt.get("error_detail", harness.get("error_detail"))
        error = receipt.get("error")
        if isinstance(error, str) and _CODE.fullmatch(error):
            if detail is None:
                detail = error
        elif error is not None:
            buckets["error_class"].append(
                _fixed(error, _SAFE_ERROR_CLASSES) if isinstance(error, str)
                and _CLASS_NAME.fullmatch(error) else "unrecognized")
        if detail is not None:
            code, reasons = _execution_detail(detail)
            buckets["error_detail"].append(code)
            buckets["source_integrity_reasons"].extend(reasons)
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
        if category is not None and category != "ok":
            categories.append(_fixed(category, known_categories))
    return {"present": True, "by_direction": _count(directions), "by_outcome": _count(outcomes), "by_error_category": _count(categories)}


def _readonly(path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect("file:" + path.resolve().as_posix().replace("?", "%3F").replace("#", "%23") + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None


def _local_failure_code(error: object) -> str:
    if not isinstance(error, str):
        return "unrecognized"
    return _fixed(error.split(":", 1)[0].strip(), _SAFE_LOCAL_CODES)


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
            "by_failure_code": _count(_local_failure_code(row[1]) for row in rows
                                      if row[0] in _LOCAL_FAILURE_STATES and row[1])}


def _stages(path: Path | None, now: float) -> dict[str, Any]:
    """Current stuck stages, whenever they got stuck.

    ``blocked`` is terminal and ``reconcile_required`` waits for an operator,
    so neither row is touched again.  Filtering by the report window would
    hide exactly the stages stuck longest.
    """
    if path is None or not path.is_file():
        return {"present": False, "by_blocked_reason": {}, "blocked_over_24_hours": 0}
    db = _readonly(path)
    if db is None:
        return {"present": True, "by_blocked_reason": {"unreadable": 1}, "blocked_over_24_hours": 0}
    try:
        rows = db.execute("SELECT state,blocked_reason,updated_at FROM stages "
                          "WHERE state IN ('blocked','reconcile_required')").fetchall()
    except sqlite3.Error:
        return {"present": True, "by_blocked_reason": {"unreadable": 1}, "blocked_over_24_hours": 0}
    finally:
        db.close()
    reasons = ("liveness_reconciliation_required" if row[0] == "reconcile_required"
               else _fixed(row[1], _SAFE_STAGE_REASONS) for row in rows)
    return {"present": True, "by_blocked_reason": _count(reasons),
            "blocked_over_24_hours": sum(1 for row in rows if isinstance(row[2], (int, float))
                                         and not isinstance(row[2], bool) and now - row[2] > 86400)}


def _gh(argv: list[str], *, cwd: Path) -> tuple[int | None, str, bool]:
    """Run gh with the bounded, descendant-safe project process runner."""
    try:
        result = runner.run(
            argv, cwd=str(cwd), env=os.environ.copy(), stdin_data="", timeout=15,
            grace=1, stdout_cap=_GH_OUTPUT_LIMIT, stderr_cap=_GH_OUTPUT_LIMIT,
        )
    except OSError:
        return None, "", False
    bounded = not (result.spawn_failed or result.timed_out or result.cap_exceeded
                   or result.descendant_held_pipes)
    return result.returncode, result.stdout.decode("utf-8", "replace"), bounded


def _ci(repo: str | None = None, since: float | None = None) -> dict[str, Any]:
    gh = shutil.which("gh")
    if not gh:
        return {"note": "gh_unavailable", "failure_rate_by_job_name": {}, "frequent_failing_test_ids": {}}
    try:
        if repo is None:
            code, output, bounded = _gh([gh, "repo", "view", "--json", "nameWithOwner"], cwd=_REPOSITORY_ROOT)
            repository = json.loads(output).get("nameWithOwner") if code == 0 and bounded else None
            if not isinstance(repository, str) or not repository:
                raise ValueError
        else:
            repository = repo
        code, output, bounded = _gh([gh, "run", "list", "--repo", repository, "--json", "databaseId,createdAt", "--limit", "100"], cwd=_REPOSITORY_ROOT)
        runs = json.loads(output) if code == 0 and bounded else []
        if not isinstance(runs, list): raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        return {"note": "gh_unavailable", "failure_rate_by_job_name": {}, "frequent_failing_test_ids": {}}
    totals: Counter[str] = Counter(); failed: Counter[str] = Counter(); tests: Counter[str] = Counter()
    for run in runs[:100]:
        run_id = run.get("databaseId") if isinstance(run, dict) else None
        if not isinstance(run_id, int): continue
        # Keep CI rates on the same window as the rest of the report.
        created = _epoch(run.get("createdAt"))
        if since is not None and (created is None or created < since): continue
        try:
            code, output, bounded = _gh([gh, "run", "view", str(run_id), "--repo", repository, "--json", "jobs"], cwd=_REPOSITORY_ROOT)
            payload = json.loads(output) if code == 0 and bounded else {}
            jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for job in jobs:
            if isinstance(job, dict) and isinstance(job.get("name"), str):
                totals[job["name"]] += 1
                if job.get("conclusion") == "failure": failed[job["name"]] += 1
        if not any(isinstance(job, dict) and job.get("conclusion") == "failure" for job in jobs):
            continue
        try:
            log_code, logs, logs_bounded = _gh([gh, "run", "view", str(run_id), "--repo", repository, "--log-failed"], cwd=_REPOSITORY_ROOT)
        except OSError:
            continue
        if logs_bounded and log_code in (0, 1):
            for test_id in (*_PYTEST_FAILURE.findall(logs), *_UNITTEST_FAILURE.findall(logs)):
                if _TEST_ID.fullmatch(test_id): tests[test_id] += 1
    return {"note": "ok", "failure_rate_by_job_name": {name: {"failed": failed[name], "total": total, "rate": round(failed[name] / total, 4)} for name, total in sorted(totals.items())}, "frequent_failing_test_ids": dict(tests.most_common(20))}


def build_report(days: int = 7, *, ci: bool = False, repo: str | None = None,
                 now: float | None = None) -> dict[str, Any]:
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    now = time.time() if now is None else now
    since = now - days * 86400
    # broker_config.load() falls back to the shipped defaults when nothing is
    # installed, so any failure here is an unreadable config, not an absent one.
    try:
        broker_root: Path | None = Path(broker_config.load().state_root)
    except Exception:
        broker_root = None
    orchestration = None
    orchestration_state = "absent"
    try:
        orchestration_path = drift.discover_config()
        if orchestration_path:
            orchestration = orchestration_config.load(orchestration_path)
            orchestration_state = "ok"
    except Exception:
        orchestration_state = "unreadable"
    if broker_root is None:
        consultations = _config_unreadable(
            {"by_direction": {}, "by_outcome": {}, "by_error_category": {}}, "by_outcome")
    else:
        consultations = _consultations(broker_root, since)
    if orchestration_state == "unreadable":
        execution = _config_unreadable(_empty_execution(True), "by_state")
        local_queue = _config_unreadable({"by_state": {}, "by_failure_code": {}}, "by_state")
        stage_health = _config_unreadable(
            {"by_blocked_reason": {}, "blocked_over_24_hours": 0}, "by_blocked_reason")
    else:
        execution = _execution(None if orchestration is None else orchestration.execution_queue_root, since)
        local_queue = _local_queue(None if orchestration is None else orchestration.local_queue_root, since)
        stage_health = _stages(None if orchestration is None else orchestration.capacity_db, now)
    report = {"window_days": days,
              "config": {"broker": "unreadable" if broker_root is None else "ok",
                         "orchestration": orchestration_state},
              "execution": execution, "consultations": consultations,
              "local_queue": local_queue, "stage_health": stage_health}
    if ci: report["ci"] = _ci(repo, since)
    return report


def _markdown(value: dict[str, Any]) -> str:
    lines = [f"# Bridge reliability report ({value['window_days']} days)"]
    lines.extend(("", "## Config", "```json", json.dumps(value["config"], sort_keys=True, indent=2), "```"))
    for title, key in (("Execution", "execution"), ("Consultations", "consultations"), ("Local queue", "local_queue"), ("Stage health", "stage_health")):
        lines.extend(("", f"## {title}", "```json", json.dumps(value[key], sort_keys=True, indent=2), "```"))
    if "ci" in value: lines.extend(("", "## CI", "```json", json.dumps(value["ci"], sort_keys=True, indent=2), "```"))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Content-free bridge reliability report")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--repo", help="GitHub repository owner/name for --ci")
    args = parser.parse_args(argv)
    try: report = build_report(args.days, ci=args.ci, repo=args.repo)
    except ValueError as exc: parser.error(str(exc))
    print(json.dumps(report, sort_keys=True) if args.json else _markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
