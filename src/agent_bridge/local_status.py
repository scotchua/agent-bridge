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


def report(config_path: str, days: int = 7, *, now: float | None = None) -> dict[str, Any]:
    """Group current outcomes by submission day, within N local calendar days.

    Feedback includes only completed work submitted in this window. Rates
    divide by reviewed work (used + reworked + discarded); no reviews is null.
    Running, expired and unknown jobs retain their own outcome labels.
    """
    if days < 1:
        raise ValueError("days_invalid")
    cfg = load(config_path)
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now).date()
    first = today - dt.timedelta(days=days - 1)
    start = dt.datetime.combine(first, dt.time()).timestamp()
    jobs: Counter = Counter()
    feedback: dict[str, Counter] = defaultdict(Counter)
    # Do not select content, blob paths, disposition, or error text. Only
    # the fixed dependency class is derived inside SQLite.
    with closing(_connect(cfg.local_queue_root / "localq.sqlite3")) as db:
        rows = db.execute("""SELECT submitted_at, caller, purpose, task_type,
            CASE WHEN status='queued' AND error LIKE 'waiting_on_dependency:%'
                 THEN 'held waiting_on_dependency' ELSE status END,
            feedback_json FROM jobs WHERE submitted_at >= ? AND submitted_at <= ?""",
                          (start, now))
        for submitted, caller, purpose, task, outcome, raw_feedback in rows:
            day = dt.datetime.fromtimestamp(submitted).date().isoformat()
            jobs[(day, caller, purpose, outcome)] += 1
            if purpose == "work" and outcome == "complete":
                value = json.loads(raw_feedback) if raw_feedback is not None else {}
                observation = value.get("outcome", "unreviewed")
                if observation not in {"used", "reworked", "discarded", "unreviewed"}:
                    raise ValueError("feedback_invalid")
                feedback[task][observation] += 1
    with closing(_connect(cfg.local_queue_root / "routing.sqlite3")) as db:
        checkpoints = [{"caller": caller, "status": status, "count": count}
                       for caller, status, count in db.execute("""SELECT caller, status, COUNT(*)
                           FROM checkpoints WHERE created_at >= ? AND created_at <= ?
                           GROUP BY caller, status ORDER BY caller, status""", (start, now))]
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
    return 0
