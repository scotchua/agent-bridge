"""Turn local usage snapshots into trusted capacity refusals, and nothing else.

The capacity snapshots written by the operator's usage tooling (one
``<source>.json`` per provider, schema 1) say how much of each usage window is
spent.  This feeder reads them and, when a fresh, well-formed window is at or
over the operator's ceiling, records that route as **unavailable** in the
routing ledger until the window resets.

**Refusals only.**  Every schema-1 snapshot carries
``account_binding: "unverified"``, and an unverified account binding cannot
support automatic routing, so a snapshot is never evidence that a route *is*
available.  It is good evidence that a route is not: usage inside a window
only rises until the window resets, so a window at or over the ceiling stays
there until ``resets_at``.  The feeder therefore never writes
``available=True``, never retracts a row, and never shortens a fresh refusal,
its own included.  Anyone able to run it can only narrow capacity.

**Fail closed.**  A missing, unreadable, malformed, stale or future-dated
snapshot, or one whose window has already reset, writes nothing.  Nothing
written means the ledger keeps whatever it had, and missing or stale capacity
already blocks reassignment.

**No default ceiling.**  The ceiling is the operator's number.  The command
refuses to run without one rather than invent it.

The validation follows the snapshot reader's own report check field for
field, and is stricter where a writer of trusted rows must be: a stale
snapshot, or a malformed value in any available bucket, rejects the whole
snapshot instead of only that bucket.  It is re-implemented, not imported,
because this package must not depend on the operator's private tooling.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

from ..capacity_router import (CapacityObservation, MAX_FRESHNESS_SECONDS,
                               RoutingError, StageRouter)
from .config import OrchestrationConfigError, load

#: Recorded on every refusal this feeder writes.  Not a replayed source, so
#: presence and declaration cannot clear it while it is fresh.
FEED_SOURCE = "quota:over-ceiling"
#: The snapshot format this feeder understands.  Anything else is unknown.
SNAPSHOT_SCHEMA = 1
#: Matches the snapshot reader: a bucket older than this is stale.
MAX_AGE_SECONDS = 15 * 60
#: Matches the snapshot reader: tolerated clock disagreement.
CLOCK_SKEW_SECONDS = 5 * 60
#: The latest timestamp the snapshot reader accepts (9999-12-31).
MAX_EPOCH = 253402300799
#: Snapshot files are a few hundred bytes; refuse anything absurd.
MAX_SNAPSHOT_BYTES = 64 * 1024
#: Snapshot source -> routing ledger route.  ``local`` has no usage window.
SOURCES = {"codex": "codex", "claude": "claude"}
_CODEX_LIMIT = re.compile(r"codex|legacy|bucket_[a-f0-9]{16}|bucket_[0-9]+")
#: The only bucket states other than ``available`` the snapshot writer emits.
_UNKNOWN_STATES = {"unknown (absent)", "unknown (partial)"}


def _number(value: Any, *, low: float | None = None,
            high: float | None = None) -> int | float | None:
    """A finite JSON number in bounds, never a string or a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if (low is not None and value < low) or (high is not None and value > high):
        return None
    return value


def load_snapshot(snapshot_dir: Path, source: str) -> dict | None:
    path = snapshot_dir / f"{source}.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SNAPSHOT_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_SNAPSHOT_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def usable_buckets(snapshot: dict | None, source: str, now: float) -> tuple[str, list[dict]]:
    """``(reason, buckets)``: the fresh, well-formed, unreset windows only.

    Any shape problem anywhere in the snapshot rejects the whole snapshot,
    including a value in an available bucket that is not a number in bounds,
    and so does a stale snapshot.  A bucket that is only absent, partial,
    stale, future-dated or reset is skipped on its own.
    """
    schema = snapshot.get("schema") if snapshot else None
    if type(schema) is not int or schema != SNAPSHOT_SCHEMA or snapshot.get("source") != source:
        return "snapshot_unknown", []
    if snapshot.get("status") != "available" or snapshot.get("account_binding") != "unverified":
        return "snapshot_unknown", []
    received = _number(snapshot.get("received_at"), low=0)
    if received is None or received > now + CLOCK_SKEW_SECONDS:
        return "snapshot_clock_anomaly", []
    if now - received > MAX_AGE_SECONDS:
        return "snapshot_stale", []
    buckets = snapshot.get("buckets")
    if not isinstance(buckets, list) or any(not isinstance(b, dict) for b in buckets):
        return "snapshot_malformed", []
    usable: list[dict] = []
    for bucket in buckets:
        if source == "claude" and bucket.get("window") not in {"five_hour", "seven_day"}:
            return "snapshot_malformed", []
        if source == "codex" and (bucket.get("scope") not in {"primary", "secondary"}
                                  or not isinstance(bucket.get("limit"), str)
                                  or not _CODEX_LIMIT.fullmatch(bucket["limit"])):
            return "snapshot_malformed", []
        if bucket.get("status") != "available":
            if bucket.get("status") not in _UNKNOWN_STATES:
                return "snapshot_malformed", []
            continue
        fields = {"used_percent": (0, 100), "freshness_at": (0, None),
                  "resets_at": (0, MAX_EPOCH)}
        if source == "codex":
            fields["window_minutes"] = (1, 525600)
        values = {name: _number(bucket.get(name), low=low, high=high)
                  for name, (low, high) in fields.items()}
        if any(values[name] is None and bucket.get(name) is not None for name in fields):
            # A value present but not a number in bounds is not one to skip
            # quietly: the rest of the snapshot is suspect too.
            return "snapshot_malformed", []
        if any(value is None for value in values.values()):
            continue
        used, fresh, reset = values["used_percent"], values["freshness_at"], values["resets_at"]
        if fresh > now + CLOCK_SKEW_SECONDS or now - fresh > MAX_AGE_SECONDS or reset <= now:
            continue
        usable.append({"used_percent": used, "resets_at": float(reset)})
    return ("ok" if usable else "no_usable_window"), usable


def _fresh_refusal(rows: list[dict], route: str, now: float) -> dict | None:
    for row in rows:
        if (row["route"] == route and row["trusted"] and not row["available"]
                and row["fresh_until"] >= now):
            return row
    return None


def feed(router: StageRouter, snapshot_dir: Path, ceiling_percent: float, *,
         now: float | None = None, dry_run: bool = False) -> list[dict]:
    """One pass over every source.  Returns what was decided for each route."""
    if _number(ceiling_percent, low=0, high=100) is None or ceiling_percent <= 0:
        raise ValueError("ceiling_percent must be a number above 0 and at most 100")
    now = router.clock() if now is None else now
    results = []
    for source, route in sorted(SOURCES.items()):
        reason, usable = usable_buckets(load_snapshot(snapshot_dir, source), source, now)
        over = [b for b in usable if b["used_percent"] >= ceiling_percent]
        result: dict[str, Any] = {"route": route, "action": "none", "reason": reason,
                                  "usable_windows": len(usable), "over_ceiling": len(over)}
        if not over:
            if reason == "ok":
                result["reason"] = "under_ceiling"
            results.append(result)
            continue
        # Refused until the last over-ceiling window resets, within the
        # ledger's longest permitted claim.  A later pass extends it.
        fresh_until = min(now + MAX_FRESHNESS_SECONDS, max(b["resets_at"] for b in over))
        result.update(reason="over_ceiling", fresh_until=fresh_until,
                      max_used_percent=max(b["used_percent"] for b in over))
        existing = _fresh_refusal(router.capacity_rows(), route, now)
        if existing is not None and existing["fresh_until"] >= fresh_until:
            # Never shorten a fresh refusal, this feeder's own included: a
            # long window missing from this snapshot has not reset.
            result["action"] = "kept_existing_refusal"
        elif dry_run:
            result["action"] = "would_refuse"
        else:
            router.observe_capacity(CapacityObservation(
                route=route, observed_at=now, fresh_until=fresh_until,
                available=False, source=FEED_SOURCE), trusted=True)
            result["action"] = "refused"
        results.append(result)
    return results


def _ceiling(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(number) or not 0 < number <= 100:
        raise argparse.ArgumentTypeError("must be above 0 and at most 100")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record trusted capacity refusals for usage windows at or over a ceiling. "
                    "Never records a route as available.")
    parser.add_argument("--config", required=True, help="Private orchestration config path.")
    parser.add_argument("--snapshot-dir", required=True, type=Path,
                        help="Directory holding codex.json and claude.json usage snapshots.")
    parser.add_argument("--ceiling-percent", required=True, type=_ceiling,
                        help="Refuse a route when any fresh usage window is at or over this "
                             "percent. No default: the operator sets it.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be refused without writing.")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
    except (OSError, OrchestrationConfigError, ValueError) as exc:
        print(f"capacity feed: config unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        results = feed(StageRouter(config.capacity_db), args.snapshot_dir,
                       args.ceiling_percent, now=time.time(), dry_run=args.dry_run)
    except (OSError, RoutingError, ValueError) as exc:
        print(f"capacity feed failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"dry_run": args.dry_run, "ceiling_percent": args.ceiling_percent,
                      "routes": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
