"""Report whether a written-limit batch should run now or wait for a quota reset.

A night-shift batch is a run of approved units with a written limit (a unit
count, an end time, or a usage ceiling).  This helper reads the same usage
snapshots as the capacity feeder and says, for one provider, whether the
batch has headroom now, should wait until the window holding it back resets,
or should not start at all.

**Report only.**  It writes nothing, schedules nothing, starts nothing, and
never buys credits, redeems a reset or enables paid fallback.  Whoever runs a
batch reads the report and decides.

**The review reserve is a check, not a reservation.**  The operator names a
share of each provider's allowance to keep for required reviews.  The batch's
effective ceiling is ``100 - reserve`` (or the batch's own usage ceiling, when
lower).  Nothing holds that share back; the report only refuses to call a
window under the reserve line "headroom".

**No default reserve.**  The reserve is the operator's number.  The command
refuses to run without one rather than invent it.

**Fail closed.**  A snapshot the feeder would not trust, or one with any
window missing, partial, stale or reset, is never reported as headroom.

**Limits are recorded, not enforced.**  ``max_units`` and ``until`` are the
batch's declared limits; whoever runs the batch counts units and watches the
clock.  This helper cannot know how many units fit in the headroom.  An
account binding of ``unverified`` means even headroom is an observation, not
routing authority, and the report says so.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from .capacity_feed import MAX_EPOCH, SOURCES, _number, load_snapshot, usable_buckets

#: The windows each provider's allowance has.  Headroom needs every one.
REQUIRED_WINDOWS = {"claude": {"five_hour", "seven_day"}, "codex": {"primary", "secondary"}}


def _covers_required_windows(provider: str, buckets: list[dict]) -> bool:
    """Whether the buckets include every window the provider has.

    Claude names its windows; Codex names a scope per limit, so each Codex
    limit present needs both scopes.  A window left out of the snapshot
    entirely could be the one at the ceiling.
    """
    if provider == "claude":
        return {b.get("window") for b in buckets} >= REQUIRED_WINDOWS["claude"]
    scopes: dict[Any, set] = {}
    for bucket in buckets:
        scopes.setdefault(bucket.get("limit"), set()).add(bucket.get("scope"))
    return bool(scopes) and all(s >= REQUIRED_WINDOWS["codex"] for s in scopes.values())


def _limits(max_units: Any, until: Any, stop_at_percent: Any) -> dict:
    """The batch's written limits, validated.  Raises ValueError on a bad one."""
    limits: dict[str, Any] = {}
    if max_units is not None:
        if isinstance(max_units, bool) or not isinstance(max_units, int) or max_units < 1:
            raise ValueError("max_units must be a whole number of at least 1")
        limits["max_units"] = max_units
    if until is not None:
        if _number(until, low=0, high=MAX_EPOCH) is None or until <= 0:
            raise ValueError("until must be an epoch time after 1970 and before 10000")
        limits["until"] = float(until)
    if stop_at_percent is not None:
        if _number(stop_at_percent, low=0, high=100) is None or stop_at_percent <= 0:
            raise ValueError("stop_at_percent must be above 0 and at most 100")
        limits["stop_at_percent"] = stop_at_percent
    return limits


def plan_batch(snapshot_dir: Path, provider: str, reserve_percent: float, *,
               max_units: int | None = None, until: float | None = None,
               stop_at_percent: float | None = None, now: float) -> dict:
    """One provider's report.  ``decision`` is one of:

    ``ask_scott``          the batch has no written limit
    ``stop``               a limit has passed, or ends before the next reset
    ``hold``               capacity unknown: no usable or complete snapshot
    ``wait_for_reset``     a window is at or over the effective ceiling
    ``headroom_observed``  every window is under it (observation, not authority)
    """
    if provider not in SOURCES:
        raise ValueError(f"provider must be one of {sorted(SOURCES)}")
    if _number(reserve_percent, low=0, high=100) is None or reserve_percent >= 100:
        raise ValueError("reserve_percent must be at least 0 and below 100")
    if _number(now, low=0, high=MAX_EPOCH) is None:
        raise ValueError("now must be an epoch time after 1970 and before 10000")
    limits = _limits(max_units, until, stop_at_percent)
    ceiling = 100 - reserve_percent
    if "stop_at_percent" in limits:
        ceiling = min(ceiling, limits["stop_at_percent"])
    report: dict[str, Any] = {"provider": provider, "authority": "report_only",
                              "reserve_percent": reserve_percent,
                              "effective_ceiling_percent": ceiling, "limits": limits}
    if not limits:
        report.update(decision="ask_scott", reason="no_written_limit")
        return report
    if "until" in limits and limits["until"] <= now:
        report.update(decision="stop", reason="time_limit_passed")
        return report
    snapshot = load_snapshot(snapshot_dir, provider)
    reason, usable = usable_buckets(snapshot, provider, now)
    report["usable_windows"] = len(usable)
    if not usable:
        report.update(decision="hold", reason=reason)
        return report
    report["max_used_percent"] = max(b["used_percent"] for b in usable)
    # usable_buckets drops window names, so coverage is read from the
    # snapshot's own buckets, which are all usable when the counts match.
    complete = (len(usable) == len(snapshot["buckets"])
                and _covers_required_windows(provider, snapshot["buckets"]))
    over = [b for b in usable if b["used_percent"] >= ceiling]
    if over:
        # A skipped window may also be over and reset later, so this is the
        # earliest the batch could start, not a promise that it can.
        not_before = max(b["resets_at"] for b in over)
        report["not_before"] = not_before
        report["not_before_is_lower_bound"] = not complete
        if "until" in limits and not_before >= limits["until"]:
            report.update(decision="stop", reason="limit_ends_before_reset")
        else:
            report.update(decision="wait_for_reset", reason="at_or_over_ceiling")
        return report
    if not complete:
        # An absent, partial, stale, reset or omitted window could be the one
        # at the ceiling: never call the rest headroom.
        report.update(decision="hold", reason="incomplete_windows")
        return report
    report.update(decision="headroom_observed", reason="under_ceiling",
                  headroom_percent=ceiling - report["max_used_percent"],
                  next_reset=min(b["resets_at"] for b in usable),
                  account_binding="unverified")
    return report


def _percent(low_inclusive: bool):
    def parse(value: str) -> float:
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a number") from None
        ok = (0 <= number < 100) if low_inclusive else (0 < number <= 100)
        if not math.isfinite(number) or not ok:
            raise argparse.ArgumentTypeError(
                "must be at least 0 and below 100" if low_inclusive
                else "must be above 0 and at most 100")
        return number
    return parse


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _epoch(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be epoch seconds") from None
    if not math.isfinite(number) or not 0 < number <= MAX_EPOCH:
        raise argparse.ArgumentTypeError("must be epoch seconds before year 10000")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report whether a written-limit batch has headroom now or should wait for "
                    "a quota reset. Writes and schedules nothing.")
    parser.add_argument("--snapshot-dir", required=True, type=Path,
                        help="Directory holding codex.json and claude.json usage snapshots.")
    parser.add_argument("--provider", required=True, choices=sorted(SOURCES))
    parser.add_argument("--reserve-percent", required=True, type=_percent(True),
                        help="Share of the allowance kept for required reviews. "
                             "No default: the operator sets it.")
    parser.add_argument("--max-units", type=_positive_int, help="Written limit: unit count.")
    parser.add_argument("--until", type=_epoch, help="Written limit: end time, epoch seconds.")
    parser.add_argument("--stop-at-percent", type=_percent(False),
                        help="Written limit: stop when any window reaches this percent.")
    args = parser.parse_args(argv)
    try:
        report = plan_batch(args.snapshot_dir, args.provider, args.reserve_percent,
                            max_units=args.max_units, until=args.until,
                            stop_at_percent=args.stop_at_percent, now=time.time())
    except ValueError as exc:
        print(f"night shift: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
