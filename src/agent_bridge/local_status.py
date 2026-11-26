"""Content-free, read-only local queue reporting. Never constructs a queue."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

from . import drift
from .localq import quality
from .localq.spool import COORDINATOR_CHORES, MECHANICAL_TASKS
from .localq.intake import CHORE_WAIVER_REASONS
from .orchestration.config import load, OrchestrationConfigError


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _inline_output_counts(state_root: Path, start: float, now: float) -> tuple[list[dict[str, Any]], int]:
    counts: Counter = Counter()
    undated = 0
    path = state_root / "routing" / "inline-output-routing.jsonl"
    try:
        stream = path.open(encoding="utf-8")
    except FileNotFoundError:
        return [], 0
    with stream:
        for line in stream:
            row = json.loads(line)
            at = row.get("at")
            # Earlier router versions wrote no timestamp. Never invent a
            # date or silently count those records in the selected window.
            if not isinstance(at, (int, float)) or isinstance(at, bool):
                undated += 1
                continue
            if not start <= at <= now:
                continue
            caller = row.get("caller", "unknown")
            if caller not in {"claude", "codex"}:
                caller = "unknown"
            routed = row.get("routed")
            reason = None if routed else row.get("waiver_reason")
            if not isinstance(routed, bool) or (not routed and not isinstance(reason, str)):
                raise ValueError("inline_output_record_invalid")
            counts[(caller, routed, reason)] += 1
    return [{"caller": caller, "routed": routed, "waiver_reason": reason, "count": count}
            for (caller, routed, reason), count in sorted(counts.items())], undated


def _chore_counts(db: sqlite3.Connection, start: float, now: float) -> dict[str, Any]:
    # Older installations have no waiver table. Report that accounting is
    # unavailable instead of manufacturing a zero count of silent skips.
    available = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chore_waivers'").fetchone()
    if not available:
        return {"chore_accounting_available": False, "chore_waivers": [], "unaccounted_chores": []}
    waivers = []
    for caller, task, reason, count in db.execute("""SELECT caller, task_type, reason, COUNT(*)
            FROM chore_waivers WHERE created_at >= ? AND created_at <= ?
            GROUP BY caller, task_type, reason ORDER BY caller, task_type, reason""", (start, now)):
        if reason not in CHORE_WAIVER_REASONS or task not in COORDINATOR_CHORES or caller not in {"claude", "codex"}:
            raise ValueError("chore_waiver_invalid")
        waivers.append({"caller": caller, "task_type": task, "reason": reason, "count": count})
    unaccounted = [{"caller": caller, "task_type": task, "status": status, "count": count}
        for caller, task, status, count in db.execute("""SELECT c.caller, c.task_type, c.status, COUNT(*)
            FROM checkpoints c WHERE c.created_at >= ? AND c.created_at <= ?
            AND c.status != 'no_eligible_unit' AND c.task_type IN ('log_triage', 'summarize')
            AND NOT EXISTS (SELECT 1 FROM routing_receipts r
                            WHERE r.checkpoint_id=c.checkpoint_id AND r.job_id IS NOT NULL)
            AND NOT EXISTS (SELECT 1 FROM chore_waivers w WHERE w.checkpoint_id=c.checkpoint_id)
            GROUP BY c.caller, c.task_type, c.status ORDER BY c.caller, c.task_type, c.status""", (start, now))]
    return {"chore_accounting_available": True, "chore_waivers": waivers, "unaccounted_chores": unaccounted}


def _execution_counts(root: Path | None, start: float, now: float) -> dict[str, Any]:
    counts: Counter = Counter()
    undated = 0
    if root is not None and root.exists():
        for directory in sorted(root.iterdir()):
            if len(directory.name) != 32 or any(char not in "0123456789abcdef" for char in directory.name):
                continue
            if not directory.is_dir():
                continue
            request_path = directory / "request.json"
            # Source preparation creates a directory before admission. Only
            # publication of request.json makes it an execution job.
            if not request_path.is_file():
                continue
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if request.get("provider") != "codex" or request.get("kind", "execution") != "execution":
                continue
            at = request.get("submitted_at")
            if not isinstance(at, (int, float)) or isinstance(at, bool):
                undated += 1
                continue
            if start <= at <= now:
                receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
                state = receipt.get("state")
                if state not in {"queued", "running", "complete", "failed", "blocked", "cancelled", "unknown"}:
                    raise ValueError("execution_state_invalid")
                counts[state] += 1
    return {"execution_configured": root is not None,
            "codex_execution_jobs": [{"state": state, "count": count} for state, count in sorted(counts.items())],
            "codex_execution_undated_records": undated}


def report(config_path: str, days: int = 7, *, now: float | None = None) -> dict[str, Any]:
    """Group current outcomes by submission day, within N local calendar days.

    Feedback includes only completed work submitted in this window. Rates
    divide by reviewed work (used + reworked + discarded); no reviews is null.
    Running, expired and unknown jobs retain their own outcome labels.
    Quality is separate: a fixed 14-day window keyed on review recording,
    scoped to the configured backend and reset by operator re-enabling.
    """
    if days < 1:
        raise ValueError("days_invalid")
    cfg = load(config_path)
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now).date()
    first = today - dt.timedelta(days=days - 1)
    start = dt.datetime.combine(first, dt.time()).timestamp()
    jobs: Counter = Counter()
    split_jobs: Counter = Counter()
    feedback: dict[str, Counter] = defaultdict(Counter)
    # Do not select content, blob paths, disposition, or error text. Only
    # the fixed dependency class is derived inside SQLite.
    with closing(_connect(cfg.local_queue_root / "localq.sqlite3")) as db:
        rows = db.execute("""SELECT submitted_at, caller, purpose, task_type,
            CASE WHEN status='queued' AND error LIKE 'waiting_on_dependency:%'
                 THEN 'held waiting_on_dependency'
                 WHEN status='queued' AND error LIKE 'quality_disabled:%'
                 THEN 'held quality_disabled' ELSE status END,
            feedback_json FROM jobs WHERE submitted_at >= ? AND submitted_at <= ?""",
                          (start, now))
        for submitted, caller, purpose, task, outcome, raw_feedback in rows:
            day = dt.datetime.fromtimestamp(submitted).date().isoformat()
            jobs[(day, caller, purpose, outcome)] += 1
            observation = "-"
            if purpose == "work" and outcome == "complete":
                value = json.loads(raw_feedback) if raw_feedback is not None else {}
                observation = value.get("outcome", "unreviewed")
                if observation not in {"used", "reworked", "discarded", "unreviewed"}:
                    raise ValueError("feedback_invalid")
                feedback[task][observation] += 1
            if purpose == "work":
                split_jobs[(task, outcome, observation)] += 1
        quality_jobs = quality.job_rows(db)
    try:
        quality_state = quality.read_state(cfg.state_root).get(cfg.local_backend, {})
        disabled = frozenset(task for task, row in quality_state.items() if row["disabled_at"] is not None)
    except (OSError, ValueError):
        quality_state = {}
        disabled = MECHANICAL_TASKS
    quality_rows = quality.evaluate(quality_jobs, backend_id=cfg.local_backend, now=now,
                                    previous=quality_state, task_types=MECHANICAL_TASKS)
    for task, row in quality_rows.items():
        stored_at = quality_state.get(task, {}).get("disabled_at")
        if task in disabled:
            row["verdict"] = "below the quality bar"
        elif row["disabled_at"] is not None:
            row["verdict"] = "pending evaluation"
        row["disabled_at"] = stored_at
    with closing(_connect(cfg.local_queue_root / "routing.sqlite3")) as db:
        checkpoints = [{"caller": caller, "status": status, "count": count}
                       for caller, status, count in db.execute("""SELECT caller, status, COUNT(*)
                           FROM checkpoints WHERE created_at >= ? AND created_at <= ?
                           GROUP BY caller, status ORDER BY caller, status""", (start, now))]
        chores = _chore_counts(db, start, now)
    feedback_rows = []
    for task, counts in sorted(feedback.items()):
        reviewed = sum(counts[key] for key in ("used", "reworked", "discarded"))
        feedback_rows.append({"task_type": task,
                              **{key: counts[key] for key in ("used", "reworked", "discarded", "unreviewed")},
                              "rework_rate": counts["reworked"] / reviewed if reviewed else None,
                              "discard_rate": counts["discarded"] / reviewed if reviewed else None})
    inline_output, undated = _inline_output_counts(cfg.state_root, start, now)
    return {"days": days, "first_local_day": first.isoformat(),
            "last_local_day": today.isoformat(),
            "jobs": [{"day": day, "caller": caller, "purpose": purpose,
                      "outcome": outcome, "count": count}
                     for (day, caller, purpose, outcome), count in sorted(jobs.items())],
            "feedback": feedback_rows, "checkpoints": checkpoints,
            "quality_backend_id": cfg.local_backend, "quality_window_days": 14,
            "quality": [{"task_type": task, **row} for task, row in quality_rows.items()],
            "disabled_for_quality": sorted(disabled),
            "work_split": {"local_jobs": [{"task_type": task, "outcome": outcome,
                                            "feedback": observation, "count": count}
                                           for (task, outcome, observation), count in sorted(split_jobs.items())],
                           **chores, **_execution_counts(cfg.execution_queue_root, start, now),
                           "inline_output_waivers": [row for row in inline_output if not row["routed"]]},
            "inline_output_routing": inline_output, "inline_output_undated_records": undated}


def _days(value: str) -> int:
    try:
        days = int(value)
        if days > 0:
            return days
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("days must be a positive integer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_bridge.py local-status",
                                     description=__doc__)
    parser.add_argument("--config", help="installed orchestration configuration")
    parser.add_argument("--days", type=_days, default=7)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--work-split", action="store_true", help="show local, Codex and cloud-done chore counts")
    args = parser.parse_args(argv)
    try:
        config_path = drift.discover_config(args.config)
        if config_path is None:
            raise OrchestrationConfigError("config_missing")
        result = report(config_path, args.days)
    except (OSError, ValueError, sqlite3.Error, OverflowError, TypeError, AttributeError) as exc:
        # Fixed classes only: SQLite/config exception messages may contain
        # paths or data. Missing databases are errors, never created here.
        error = ("local_status:config_error" if isinstance(exc, OrchestrationConfigError)
                 else "local_status:database_error" if isinstance(exc, sqlite3.Error)
                 else "local_status:report_error")
        if args.json:
            print(json.dumps({"error": error}))
        else:
            print(error, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif args.work_split:
        print(f"Local days {result['first_local_day']} through {result['last_local_day']}")
        split = result["work_split"]
        print("Local work jobs by type, current outcome and feedback:")
        for row in split["local_jobs"]:
            print("{task_type}\t{outcome}\t{feedback}\t{count}".format(**row))
        print("Codex execution jobs by submission time and current state:")
        for row in split["codex_execution_jobs"]:
            print("codex\t{state}\t{count}".format(**row))
        if not split["execution_configured"]:
            print("Execution queue: not configured")
        print(f"Undated Codex execution records excluded: {split['codex_execution_undated_records']}")
        print("Cloud-done chores (explicit waivers, by recording time):")
        for row in split["chore_waivers"]:
            print("{caller}\t{task_type}\t{reason}\t{count}".format(**row))
        print("Inline output waivers (output returned; cloud completion not observed):")
        for row in split["inline_output_waivers"]:
            print("{caller}\t{waiver_reason}\t{count}".format(**row))
        print("Unaccounted chores (pending or skipped; no local job or waiver):")
        for row in split["unaccounted_chores"]:
            print("{caller}\t{task_type}\t{status}\t{count}".format(**row))
        if not split["chore_accounting_available"]:
            print("Chore accounting unavailable: start the updated orchestration server to migrate")
    else:
        print(f"Local days {result['first_local_day']} through {result['last_local_day']}")
        print("Jobs by submission day (current outcome):")
        for row in result["jobs"]:
            print("{day}\t{caller}\t{purpose}\t{outcome}\t{count}".format(**row))
        print("Feedback: completed work; rates among reviewed work (n/a if none)")
        for row in result["feedback"]:
            rates = ["n/a" if row[key] is None else f"{row[key]:.1%}"
                     for key in ("rework_rate", "discard_rate")]
            print(("{task_type}\tused={used}\treworked={reworked}\tdiscarded={discarded}"
                   "\tunreviewed={unreviewed}").format(**row)
                  + f"\trework_rate={rates[0]}\tdiscard_rate={rates[1]}")
        print(f"Quality ({result['quality_backend_id']}): feedback recorded in the last 14 days, completed work by type")
        for row in result["quality"]:
            rate = "n/a" if row["used_rate"] is None else f"{row['used_rate']:.1%}"
            print(f"{row['task_type']}\tused_rate={rate}\tn={row['n']}\t{row['verdict']}")
        print("Disabled for quality: " + (", ".join(result["disabled_for_quality"]) or "none"))
        print("Checkpoints by caller and status:")
        for row in result["checkpoints"]:
            print("{caller}\t{status}\t{count}".format(**row))
        print("Inline output routing by caller and waiver reason:")
        for row in result["inline_output_routing"]:
            outcome = "routed" if row["routed"] else "waived"
            reason = row["waiver_reason"] or "-"
            print(f"{row['caller']}\t{outcome}\t{reason}\t{row['count']}")
        if result["inline_output_undated_records"]:
            print(f"Undated inline output records excluded from window: {result['inline_output_undated_records']}")
        split = result["work_split"]
        print("Unaccounted chores (pending or skipped): " +
              (str(sum(row["count"] for row in split["unaccounted_chores"]))
               if split["chore_accounting_available"] else "accounting unavailable"))
    return 0
