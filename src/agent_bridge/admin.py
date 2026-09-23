"""Operator CLI: status, retention cleanup, and a ledger digest.

Retention is explicit and opt-in.  Nothing is deleted unless `cleanup` is run,
and `cleanup` without --apply only reports what it would remove.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from typing import Any

from . import registry, store
from .broker import error_response
from .config import Config, load as load_config
from .errors import BrokerError


def _inside(root: str, candidate: str) -> bool:
    """True only if candidate resolves to something strictly under root.

    Guards the one destructive command in this project. A workspace path is
    read back from a conversation JSON file, which is mutable state rather than
    trusted input, so a corrupted or tampered record must not be able to steer
    a recursive delete outside the bridge's own state tree.
    """
    root_real = os.path.realpath(root)
    target_real = os.path.realpath(candidate)
    if target_real == root_real:
        return False
    return target_real.startswith(root_real + os.sep)


def _has_symlink_component(root: str, target: str) -> bool:
    """True if any component of target below root is a symlink right now.

    Walks upward comparing realpath at each step rather than testing a string
    prefix. A prefix test is wrong here because the root and the target can be
    spelled differently for the same directory: on macOS /var is a symlink to
    /private/var, so a realpath'd root never prefixes a /var-spelled target and
    the walk silently inspected nothing.

    The target itself is never realpath'd. Resolving it would follow the exact
    symlink this function exists to detect.
    """
    root_real = os.path.realpath(root)
    cursor = os.path.abspath(target)
    guard = 0
    while guard < 256:
        guard += 1
        try:
            if os.path.islink(cursor):
                return True
        except OSError:
            return True
        try:
            if os.path.realpath(cursor) == root_real:
                return False
        except OSError:
            return True
        parent = os.path.dirname(cursor)
        if parent == cursor:
            # Walked past the root without ever matching it. Treat an
            # unrelatable path as suspect rather than safe.
            return True
        cursor = parent
    return True


def _age_days(path: str, fallback: str | None = None) -> float:
    """Age in days, falling back to another path when the first is missing.

    A job directory with no status.json otherwise reported an age of zero
    forever, so it looked perpetually new and retention never reached it.
    """
    for candidate in (path, fallback):
        if not candidate:
            continue
        try:
            return (dt.datetime.now().timestamp() - os.path.getmtime(candidate)) / 86400.0
        except OSError:
            continue
    return 0.0


def _hold_age_seconds(record: dict[str, Any], path: str, now: float) -> float:
    """Age of an indeterminate hold, with file age for legacy records."""
    raw = record.get("indeterminate_at")
    if raw:
        try:
            stamp = dt.datetime.fromisoformat(str(raw))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            return max(0.0, now - stamp.timestamp())
        except (TypeError, ValueError):
            pass
    try:
        return max(0.0, now - os.path.getmtime(path))
    except OSError:
        return 0.0


def cmd_status(cfg: Config, _: argparse.Namespace) -> int:
    jobs_root = cfg.state("jobs")
    counts: dict[str, int] = {}
    jobs = 0
    if os.path.isdir(jobs_root):
        for entry in os.scandir(jobs_root):
            if not entry.is_dir():
                continue
            jobs += 1
            status = store.read_json_or_none(os.path.join(entry.path, "status.json")) or {}
            key = str(status.get("status", "unknown"))
            counts[key] = counts.get(key, 0) + 1
    conversations_root = cfg.state("conversations")
    conversations = open_conversations = 0
    if os.path.isdir(conversations_root):
        for entry in os.scandir(conversations_root):
            if entry.name.endswith(".json"):
                conversations += 1
                record = store.read_json_or_none(entry.path) or {}
                if not record.get("closed"):
                    open_conversations += 1
    ledger_lines = 0
    if os.path.isfile(cfg.ledger_path):
        with open(cfg.ledger_path, encoding="utf-8") as handle:
            ledger_lines = sum(1 for line in handle if line.strip())
    indeterminate: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    if os.path.isdir(conversations_root):
        for entry in os.scandir(conversations_root):
            if entry.name.endswith(".json"):
                rec = store.read_json_or_none(entry.path) or {}
                if rec.get("indeterminate"):
                    age_seconds = _hold_age_seconds(rec, entry.path, now)
                    indeterminate.append({
                        "conversation_id": str(rec.get("conversation_id")),
                        "indeterminate_at": rec.get("indeterminate_at"),
                        "age_seconds": round(age_seconds, 1),
                        "age_days": round(age_seconds / 86400.0, 1),
                    })
    contamination = store.read_json_or_none(cfg.state("last-contamination.json"))
    if contamination:
        print("WARNING: a consultation was refused because instruction files "
              "were found near the isolated workspace.")
        print(f"  {contamination.get('reason')}")
        print(f"  directory: {contamination.get('directory')}")
        if contamination.get("files"):
            print(f"  files: {', '.join(contamination['files'])}")
        print(f"  detected: {contamination.get('detected_at')}")
        print("  Move or rename those files, then retry. This record is "
              "overwritten by the next occurrence.")
        print()

    if indeterminate:
        # Deliberately loud. A hold has no timeout by design, so a dormant one
        # would otherwise sit unnoticed until somebody happened to retry it.
        print(f"WARNING: {len(indeterminate)} conversation(s) HELD as "
              f"indeterminate and awaiting operator resolution.")
        for hold in indeterminate:
            print(f"  agent-bridge-admin resolve {hold['conversation_id']} "
                  f"(held for {hold['age_days']}d)")
        print()
    indeterminate_ids = [hold["conversation_id"] for hold in indeterminate]
    print(json.dumps({
        "state_root": cfg.state_root,
        "config": cfg.path,
        "contract_version": cfg.contract_version,
        "session_validation": "not_checked",
        "approval_granted": False,
        "contract_schema_sha256": cfg.load_schema()[1],
        "jobs_total": jobs,
        "jobs_by_status": counts,
        "jobs_active_now": registry.count_active(cfg),
        "conversations_total": conversations,
        "conversations_open": open_conversations,
        "conversations_indeterminate": len(indeterminate),
        "last_contamination": contamination,
        "indeterminate_conversation_ids": indeterminate_ids,
        "indeterminate_holds": indeterminate,
        "ledger_records": ledger_lines,
        "retention_days": cfg.raw["retention"],
    }, indent=2, sort_keys=True))
    return 0


def cmd_cleanup(cfg: Config, args: argparse.Namespace) -> int:
    """Remove job and conversation payloads past their retention window."""
    job_days = cfg.retention_days("job_retention_days")
    conv_days = cfg.retention_days("conversation_retention_days")
    removals: list[dict[str, Any]] = []

    jobs_root = cfg.state("jobs")
    if os.path.isdir(jobs_root):
        for entry in sorted(os.scandir(jobs_root), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            status = store.read_json_or_none(os.path.join(entry.path, "status.json")) or {}
            if status.get("status") not in registry.TERMINAL_STATUSES:
                # A non-terminal job is collectable only once it is provably not
                # running. Skipping these outright let a job whose worker never
                # started linger forever, immune to cleanup.
                if registry._job_still_running(cfg, entry.name):
                    continue
            age = _age_days(os.path.join(entry.path, "status.json"), entry.path)
            if age >= job_days:
                removals.append({"kind": "job", "path": entry.path, "age_days": round(age, 1)})

    conversations_root = cfg.state("conversations")
    if os.path.isdir(conversations_root):
        for entry in sorted(os.scandir(conversations_root), key=lambda e: e.name):
            if not entry.name.endswith(".json"):
                continue
            age = _age_days(entry.path)
            if age >= conv_days:
                record = store.read_json_or_none(entry.path) or {}
                removals.append({"kind": "conversation", "path": entry.path,
                                 "age_days": round(age, 1)})
                workspace = record.get("workspace")
                if workspace and os.path.isdir(workspace):
                    if _inside(cfg.state_root, str(workspace)):
                        removals.append({"kind": "workspace", "path": workspace,
                                         "age_days": round(age, 1)})
                    else:
                        print(f"REFUSING to remove workspace outside the state "
                              f"root: {workspace}")

    for item in removals:
        print(("REMOVE " if args.apply else "would remove ") + f"{item['kind']}: {item['path']} "
              f"({item['age_days']}d)")
        if args.apply:
            # Belt and braces: re-check containment immediately before deleting,
            # so nothing between the scan and here can widen the blast radius.
            if not _inside(cfg.state_root, item["path"]):
                print(f"REFUSING to delete outside the state root: {item['path']}")
                continue
            # Delete the RESOLVED path, and refuse if any component is a
            # symlink at this instant. shutil.rmtree on this platform reports
            # avoids_symlink_attacks, so once it starts it walks with
            # fd-relative calls; the exposure is only the top-level resolution,
            # which is what these two checks cover. A concurrent local actor
            # with write access inside the state root could still lose us a
            # race here; that is narrowed, not eliminated.
            # Symlink detection MUST run on the unresolved path. Resolving
            # first destroys the very evidence being looked for: an in-state
            # symlink such as workspaces/trap -> jobs/valuable passes
            # containment, resolves to a legitimate path with no symlink
            # components, and the delete lands on the target instead.
            original = item["path"]
            if _has_symlink_component(cfg.state_root, original):
                print(f"REFUSING, symlink in path: {original}")
                continue
            if not _inside(cfg.state_root, original):
                print(f"REFUSING, outside the state root: {original}")
                continue
            if os.path.islink(original):
                print(f"REFUSING, path is a symlink: {original}")
                continue
            target = original
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            elif os.path.isfile(target):
                try:
                    os.unlink(target)
                except OSError:
                    pass

    print(json.dumps({
        "candidates": len(removals),
        "applied": bool(args.apply),
        "note": ("The append-only ledger is never truncated by this command; "
                 "it is the audit record."),
    }, indent=2))
    return 0


def cmd_ledger(cfg: Config, args: argparse.Namespace) -> int:
    if not os.path.isfile(cfg.ledger_path):
        print("[]")
        return 0
    rows: list[dict[str, Any]] = []
    with open(cfg.ledger_path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    rows = rows[-args.tail :]
    if args.full:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for row in rows:
        print(json.dumps({
            key: row.get(key) for key in (
                "job_id", "caller", "peer", "status", "error_category",
                "source_classification", "attempt_count", "finished_at")
        }, sort_keys=True))
    return 0


# The fields below exist because a reviewer saw peer_observed_model and
# peer_cost_usd go missing from otherwise-successful Claude calls, on three
# calls, with no pattern that fresh-vs-resume explained. The bridge cannot fix
# that: it is the CLI's own envelope. What it can do is record whether the CLI
# reported them, so a null is attributable, and then let normal use accumulate
# enough exchanges to say something the original n=3 could not.
#
# This command is the "then look" half. Without it the accumulation plan
# depends on somebody remembering, which is not a plan.

def _reporting_rows(cfg: Config) -> list[dict[str, Any]]:
    if not os.path.isfile(cfg.ledger_path):
        return []
    rows: list[dict[str, Any]] = []
    with open(cfg.ledger_path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # One unreadable line must not hide every readable one.
                    continue
    return rows


def cmd_reporting(cfg: Config, args: argparse.Namespace) -> int:
    """How often a peer's CLI reports its model and cost, by version."""
    rows = _reporting_rows(cfg)
    peer = args.peer
    of_peer = [r for r in rows if r.get("peer") == peer]
    # Three distinct populations, and conflating them is the easy mistake:
    # records written before the instrumentation existed have no key at all,
    # records from a backend that does not report these carry an explicit
    # null, and only the rest can answer the question.
    pre = [r for r in of_peer if "peer_model_reported" not in r]
    unsupported = [r for r in of_peer
                   if "peer_model_reported" in r and r["peer_model_reported"] is None]
    sample = [r for r in of_peer if isinstance(r.get("peer_model_reported"), bool)]

    print(f"ledger records            {len(rows)}")
    print(f"  peer={peer}             {len(of_peer)}")
    print(f"  before instrumentation  {len(pre)}   (cannot contribute)")
    if unsupported:
        print(f"  backend reports neither {len(unsupported)}")
    print(f"  usable sample           {len(sample)}")
    if not sample:
        print(f"\nNothing to report yet. Every {peer} exchange from here on counts.")
        return 0

    def rate(subset: list[dict[str, Any]], key: str) -> str:
        if not subset:
            return "-"
        absent = sum(1 for r in subset if r.get(key) is False)
        return f"{absent}/{len(subset)}"

    print("\nabsent = the CLI did not report it. Not a bridge failure.")
    print(f"\n{'version':<28} {'resume':<7} {'n':>4} {'model absent':>13} {'cost absent':>12}")
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for row in sample:
        key = (str(row.get("peer_observed_version")), bool(row.get("resume")))
        groups.setdefault(key, []).append(row)
    for (version, resumed), subset in sorted(groups.items()):
        print(f"{version:<28} {str(resumed):<7} {len(subset):>4} "
              f"{rate(subset, 'peer_model_reported'):>13} "
              f"{rate(subset, 'peer_cost_reported'):>12}")

    model_absent = sum(1 for r in sample if r.get("peer_model_reported") is False)
    cost_absent = sum(1 for r in sample if r.get("peer_cost_reported") is False)
    print(f"\n{'total':<28} {'':<7} {len(sample):>4} "
          f"{f'{model_absent}/{len(sample)}':>13} {f'{cost_absent}/{len(sample)}':>12}")

    # A rate computed from a handful of calls is a number, not a finding, and
    # printing it without saying so is how n=3 becomes a characterisation.
    if len(sample) < args.min_sample:
        print(f"\nSample is {len(sample)}, below --min-sample {args.min_sample}. "
              "Report the counts, not a rate.")
    elif model_absent or cost_absent:
        print(f"\n{model_absent + cost_absent} absence(s) across {len(sample)} "
              "exchanges. This is the CLI's behaviour, not the bridge's, so the "
              "next step is an upstream report carrying the table above, broken "
              "down by version.")
    else:
        print(f"\nNo absences in {len(sample)} exchanges at or above the "
              "threshold. Nothing to report upstream from this machine.")
    return 0


def cmd_local_first(cfg: Config, args: argparse.Namespace) -> int:
    """Consultation accountability (design section 2.8, optional Phase 4).

    Counts, by peer, how a large consultation prompt carried a local_first
    declaration when the operator configured it as required. This is
    accountability, not enforcement: the assistant can type
    needs_judgment for any reason at all, and this report cannot tell a
    considered bypass from an impatient one. What it does is turn a leak
    that left no record into one that does.
    """
    rows = _reporting_rows(cfg)
    of_peer = [r for r in rows if r.get("peer") == args.peer]
    pre = [r for r in of_peer if "local_first" not in r]
    not_required = [r for r in of_peer if "local_first" in r and r.get("local_first") is None]
    declared = [r for r in of_peer if "local_first" in r and r.get("local_first") is not None]

    print(f"ledger records                 {len(rows)}")
    print(f"  peer={args.peer}                    {len(of_peer)}")
    print(f"  before this phase existed    {len(pre)}   (no local_first key at all)")
    print(f"  not required for the call    {len(not_required)}")
    print(f"  carried a declaration        {len(declared)}")
    if not declared:
        print("\nNothing declared yet in this window: either local-first "
              "accountability is not configured, or no prompt to this peer "
              "has reached the size threshold.")
        return 0

    by_reason: dict[str, int] = {}
    malformed = 0
    for row in declared:
        field = row.get("local_first")
        if isinstance(field, dict) and isinstance(field.get("digest_receipt_id"), str):
            key = "digest_receipt_id"
        elif isinstance(field, dict) and isinstance(field.get("bypass"), str):
            key = field["bypass"]
        else:
            malformed += 1
            continue
        by_reason[key] = by_reason.get(key, 0) + 1

    print(f"\n{'reason':<20} {'n':>6}")
    for reason, count in sorted(by_reason.items()):
        print(f"{reason:<20} {count:>6}")
    if malformed:
        print(f"{'malformed':<20} {malformed:>6}   (recorded but not one of the "
              "closed shapes; worth investigating, not counted above)")
    return 0


def cmd_resolve(cfg: Config, args: argparse.Namespace) -> int:
    """Clear an indeterminate conversation after checking the peer side."""
    record = store.read_json_or_none(cfg.conversation_path(args.conversation_id))
    if not record:
        print(f"no such conversation: {args.conversation_id}")
        return 1
    if not record.get("indeterminate"):
        print("conversation is not marked indeterminate; nothing to do")
        return 0
    print(json.dumps({k: record.get(k) for k in (
        "conversation_id", "peer", "turns", "peer_session_id",
        "indeterminate_reason", "indeterminate_job_id", "indeterminate_at",
        "indeterminate_reaped")}, indent=2, sort_keys=True))
    # Fail closed against the ATTEMPT baseline, not against the reap report.
    # If reaping or its write failed, indeterminate_reaped can be absent or
    # short, and treating that as "nothing to worry about" would let resolve
    # wave through an unknown cleanup state.
    attempts_seen = record.get("indeterminate_attempts") or []
    reaped = record.get("indeterminate_reaped")
    unresolved = []
    if reaped is None:
        unresolved = [{"attempt": a.get("attempt"), "pgid": a.get("pgid"),
                       "identity": "no reaping outcome was ever recorded"}
                      for a in attempts_seen] or [
            {"attempt": "unknown", "pgid": None,
             "identity": "no reaping outcome was ever recorded"}]
    else:
        accounted = {str(r.get("attempt")) for r in reaped}
        unresolved = [r for r in reaped if r.get("manual_cleanup_may_be_needed")]
        for item in attempts_seen:
            if str(item.get("attempt")) not in accounted:
                unresolved.append({
                    "attempt": item.get("attempt"), "pgid": item.get("pgid"),
                    "identity": "attempt has no recorded reaping outcome"})
    if unresolved:
        print("\nWARNING: a peer process group could not be verified and was "
              "therefore NOT terminated. It may still be running and still "
              "driving this peer session:")
        for item in unresolved:
            print(f"  attempt {item.get('attempt')} pgid {item.get('pgid')}: "
                  f"{item.get('identity')}")
        print("Check and clean that up before resolving. Resolving while it is "
              "alive deliberately permits two drivers on one session.")
    if not args.apply:
        print("\nDry run. Check whether the peer completed that turn, then "
              "re-run with --apply to allow this conversation to continue.")
        return 0
    if unresolved and not args.acknowledge_live_process:
        print("\nREFUSING: an unverifiable peer process may still be alive. "
              "Re-run with --acknowledge-live-process once you have confirmed "
              "it is gone.")
        return 2
    registry.resolve_indeterminate(cfg, args.conversation_id)
    store.append_ledger(cfg.ledger_path, {
        "record_type": "conversation_resolved",
        "conversation_id": args.conversation_id,
        "resolved_at": store.utc_now(),
    })
    print("resolved; the conversation may continue")
    return 0


def cmd_indeterminate(cfg: Config, _: argparse.Namespace) -> int:
    """List conversations held as indeterminate."""
    root = cfg.state("conversations")
    held = []
    if os.path.isdir(root):
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if not entry.name.endswith(".json"):
                continue
            record = store.read_json_or_none(entry.path) or {}
            if record.get("indeterminate"):
                held.append({k: record.get(k) for k in (
                    "conversation_id", "peer", "turns",
                    "indeterminate_reason", "indeterminate_at")})
    print(json.dumps(held, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    store.set_umask()
    parser = argparse.ArgumentParser(prog="agent-bridge-admin")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show broker state.")
    health = sub.add_parser("health", help="Check pinned CLI and local login visibility; no login changes.")
    health.add_argument("--peer", choices=("claude", "codex"))
    health.add_argument("--json", action="store_true")
    cleanup = sub.add_parser("cleanup", help="Apply retention. Dry run unless --apply.")
    cleanup.add_argument("--apply", action="store_true", help="Actually delete.")
    ledger = sub.add_parser("ledger", help="Show recent ledger records.")
    ledger.add_argument("--tail", type=int, default=20)
    ledger.add_argument("--full", action="store_true")
    reporting = sub.add_parser(
        "reporting", help="How often a peer's CLI reported its model and cost.")
    reporting.add_argument("--peer", default="claude", choices=("claude", "codex"))
    reporting.add_argument("--min-sample", type=int, default=30,
                           help="Below this, print counts rather than a rate.")
    local_first = sub.add_parser(
        "local-first", help="Consultation accountability: local_first declarations by peer and reason.")
    local_first.add_argument("--peer", default="claude", choices=("claude", "codex"))
    sub.add_parser("indeterminate", help="List conversations held as indeterminate.")
    resolve = sub.add_parser("resolve", help="Clear an indeterminate conversation.")
    resolve.add_argument("conversation_id")
    resolve.add_argument("--apply", action="store_true")
    resolve.add_argument("--acknowledge-live-process", action="store_true",
                         help="Required when an unverifiable peer process may "
                              "still be alive.")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        from .health import cmd_health
        return {"health": cmd_health, "status": cmd_status, "cleanup": cmd_cleanup, "ledger": cmd_ledger,
                "reporting": cmd_reporting, "local-first": cmd_local_first,
                "indeterminate": cmd_indeterminate, "resolve": cmd_resolve}[
            args.command](cfg, args)
    except BrokerError as exc:
        print(json.dumps(error_response(exc.category), sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
