"""The delegation audit: what was eligible, routed, retained, bypassed, failed.

``gate report`` says what the gate holds right now: receipts, recent events,
whether the hook is installed. That is a status page. This is the accounting
question, which is different: over a window, how much work was eligible to be
delegated, how much actually went somewhere, how much stayed put, what got
around the mechanism, what broke, and why in each case.

Every number here is derived from a durable record the system already wrote:
the routing audit ledger, the gate event ledger, the live receipts and
intents, the stage router, and the two job queues. Nothing is inferred from
model output and nothing is estimated.

The hard part of an honest audit is the bypass column, so it is split in two
and the split is the point:

* **Observed** bypasses are things the gate saw and can count: work it routed
  away that was never dispatched, and a client it is not installed for (where
  *every* call is a bypass).
* **Not countable** is everything the hook surface cannot see, enumerated
  rather than omitted: a Claude Desktop chat, the Codex desktop and web
  products, a person editing in a terminal, a host started with hooks off, a
  Codex hook not yet trusted, and shell writes the text heuristic misses. A
  report that left these out would read as "no bypasses" when the truthful
  answer is "none that this mechanism can see, and here is what it cannot".

A count of zero in the observed column therefore never means zero bypasses,
and this module says so in its own output rather than leaving a reader to
work it out.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable

from .. import store
from . import autodecide, autoroute, gate, localfirst

#: Decision codes that moved work off the deciding assistant.
ROUTED_PREFIX = "routed_"
#: Decision codes that kept it.
RETAINED_PREFIX = "retained_"

#: Gate event codes that are a failure of the mechanism rather than a verdict
#: about the work. Each one means the gate could not do its job and failed
#: closed, which an operator needs to see separately from an ordinary deny.
FAILURE_CODES = frozenset({
    "gate_error", "gate_auto_decision_failed", "gate_state_unavailable",
    "stage_db_unavailable", "hook_input_invalid", "client_invalid",
})

#: Codes that refuse a write aimed at the gate's own state or hook files.
#: Counted apart from other denials: one of these is an attempt to disable the
#: mechanism, not a routing outcome.
TAMPER_CODES = frozenset({"gate_state_protected"})

#: What no local mechanism can intercept. Carried through to the report so a
#: zero in the observed column is never read as a zero overall.
NOT_COUNTABLE = tuple(gate.NOT_COVERED)


def _read_ledger(path: str, since: float | None) -> list[dict[str, Any]]:
    """Every readable line, newest last. An unreadable line is kept as one.

    A ledger line that will not parse is itself a fact worth reporting: it is
    not silently dropped, because a dropped line is a number that quietly
    stops adding up.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return [{"error": "ledger_unreadable", "path": path}]
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            entries.append({"error": "unreadable_line"})
            continue
        if not isinstance(parsed, dict):
            entries.append({"error": "line_not_an_object"})
            continue
        stamp = parsed.get("at", parsed.get("decided_at", parsed.get("created_at")))
        if since is not None and isinstance(stamp, (int, float)) and stamp < since:
            continue
        entries.append(parsed)
    return entries


def _histogram(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _decision_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry.get("event") == "routing_decided"]


def _eligible(row: dict[str, Any]) -> bool:
    """Whether the operator's policy permitted this work to leave its caller.

    Read from the decision's own recorded ``considered`` block, so eligibility
    is judged by what the policy said at the time rather than by what the
    policy file says now. A decision written by an agent calling
    ``routing_decide`` carries no ``considered`` block; it is counted as
    eligible only if it actually routed, because nothing else about it is
    known.
    """
    considered = row.get("considered")
    if not isinstance(considered, dict):
        return str(row.get("code", "")).startswith(ROUTED_PREFIX)
    classification = considered.get("classification")
    if classification in (None, "unclassified", "client_derived"):
        return False
    allowed = considered.get("allowed_routes")
    if not isinstance(allowed, list) or not allowed:
        return False
    caller = considered.get("client")
    return any(route != caller for route in allowed)


def outstanding_intents(state_root: str) -> list[dict[str, Any]]:
    """Work the gate routed away that has not been dispatched.

    The clearest observed bypass there is: a decision said the work belongs
    elsewhere, the edit was refused, and no job was ever submitted for it. The
    assistant declined to dispatch and did something else instead.
    """
    directory = os.path.join(gate.receipt_dir(state_root), autodecide.INTENT_DIR)
    if not os.path.isdir(directory):
        return []
    rows = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        loaded = store.read_json_or_none(os.path.join(directory, name))
        if not isinstance(loaded, dict):
            rows.append({"file": name, "error": "unreadable"})
            continue
        rows.append({"repo": loaded.get("repo"), "route": loaded.get("route"),
                     "code": loaded.get("code"), "reason": loaded.get("reason"),
                     "state": loaded.get("state"),
                     "next_call": loaded.get("next_call"),
                     "created_at": loaded.get("created_at")})
    return rows


def _hook_coverage(home: str) -> dict[str, Any]:
    """Which clients the gate is installed for, and whether it decides there.

    A client with no hook entry is total, silent bypass: the gate never runs,
    so nothing appears in any ledger for it. That is the one bypass an audit
    can state with certainty and must state first.
    """
    paths = gate.install_paths(home)
    coverage: dict[str, Any] = {}
    for client, path in (("claude", paths["claude_settings"]),
                         ("codex", paths["codex_hooks"])):
        entry = None
        if os.path.exists(path):
            loaded = store.read_json_or_none(path)
            if isinstance(loaded, dict):
                pre = loaded.get("hooks", {})
                pre = pre.get("PreToolUse", []) if isinstance(pre, dict) else []
                entry = next((item for item in pre if gate._is_ours(item)), None)
        if entry is None:
            coverage[client] = {"installed": False,
                                "consequence": "every call from this client is "
                                               "un-gated and leaves no record"}
            continue
        command = ""
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict):
                command = str(hook.get("command", ""))
        automatic = "--no-automatic-routing" not in command
        coverage[client] = {
            "installed": True,
            "automatic_routing": automatic,
            "consequence": None if automatic else
            "this client refuses un-decided work rather than deciding it, so a "
            "decision is made only when an assistant asks for one",
        }
    coverage["codex_trust"] = gate.codex_trust_state(paths["codex_toml"],
                                                     paths["codex_hooks"])
    if coverage["codex_trust"] == "needs_review":
        coverage["codex"]["consequence"] = (
            "the hook is installed but Codex has not trusted it, so it does "
            "not run and every Codex call is un-gated")
    return coverage


def _local_queue_report(root: str | None) -> dict[str, Any]:
    """Job states from the local queue's SQLite store, read-only.

    The local queue does not use per-job receipt directories the way the
    execution queue does, so the directory reader below reported an empty
    state map for a queue with work in it. Opened read-only and never
    created: an audit must not bring the thing it measures into existence.
    """
    if not root or not os.path.isdir(root):
        return {"configured": bool(root), "present": False}
    database = os.path.join(root, "localq.sqlite3")
    if not os.path.isfile(database):
        return {"configured": True, "present": True, "states": {},
                "note": "no local queue database yet"}
    import sqlite3

    uri = "file:" + os.path.realpath(database).replace("?", "%3F").replace("#", "%23") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return {"configured": True, "present": True,
                "error": type(exc).__name__}
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        failures = [
            {"job_id": row["job_id"], "status": row["status"],
             "task_type": row["task_type"], "caller": row["caller"],
             # The queue's own recorded reason, which is the point: the worker
             # child prints a fixed error_detail and the queue stores it.
             "error": row["error"]}
            for row in connection.execute(
                "SELECT job_id, status, task_type, caller, error FROM jobs "
                "WHERE status IN ('failed','cancelled') ORDER BY updated_at").fetchall()]
    except sqlite3.Error as exc:
        return {"configured": True, "present": True, "error": type(exc).__name__}
    finally:
        connection.close()
    return {"configured": True, "present": True,
            "states": {str(row["status"]): row["n"] for row in rows},
            "failures": failures}


def _queue_report(root: str | None) -> dict[str, Any]:
    """Job states from a durable queue directory, without importing its class.

    Read straight from the receipts so the audit never needs the queue's own
    privacy enforcement to succeed, and never creates the directory as a side
    effect of reporting on it.
    """
    if not root or not os.path.isdir(root):
        return {"configured": bool(root), "present": False}
    states: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    for name in sorted(os.listdir(root)):
        receipt = os.path.join(root, name, "receipt.json")
        if not os.path.isfile(receipt):
            continue
        loaded = store.read_json_or_none(receipt)
        if not isinstance(loaded, dict):
            states["unreadable"] = states.get("unreadable", 0) + 1
            continue
        state = str(loaded.get("state", "unknown"))
        states[state] = states.get(state, 0) + 1
        if state in ("failed", "blocked"):
            harness = loaded.get("harness") if isinstance(loaded.get("harness"), dict) else {}
            failures.append({
                "job_id": loaded.get("job_id"), "state": state,
                "caller": loaded.get("caller"), "provider": loaded.get("provider"),
                # The whole reason the diagnostic fields exist: a class name
                # alone is not evidence, so the audit prints the detail too.
                "error": loaded.get("error"),
                "error_detail": loaded.get("error_detail"),
                "harness_status": harness.get("harness_status"),
                "harness_verdict": harness.get("harness_verdict"),
                "harness_error": harness.get("error"),
                "harness_error_detail": harness.get("error_detail"),
            })
    return {"configured": True, "present": True, "states": dict(sorted(states.items())),
            "failures": failures}


#: The four gate-event codes a gated-shape read produces (design section 2.1).
READ_GATE_CODES = frozenset({
    "local_digest_required", "local_digest_pending", "local_digest_present", "local_first_waived",
})


def _local_first_report(state_root: str, *, events: list[dict[str, Any]],
                        audit_entries: list[dict[str, Any]], local_root: str | None,
                        worker_executable: str | None, policy: "autoroute.Policy | None",
                        now: float) -> dict[str, Any]:
    """The local_first section (design section 2.6): every gated-shape read
    this window, partitioned so a reader can see where each one landed.

    ``compelled`` is every gate event carrying one of the four read-gate
    codes. Three of the five buckets below come straight from that code;
    the other two (``declined``/``outstanding``) come from the
    ``digest_intent`` audit-ledger line the same call already writes
    alongside its ``local_digest_required`` event, classified by whether
    that specific intent's own ``expires_at`` has passed -- not by whether
    the file was later digested, which is a different, later event and
    already counted for itself under ``digested``. The two counts line up
    (``compelled == digested + pending + waived + declined + outstanding``)
    by construction: every compelled event falls into exactly one bucket.
    """
    reads = [event for event in events if event.get("code") in READ_GATE_CODES]

    def count(code: str) -> int:
        return sum(1 for event in reads if event.get("code") == code)

    digested = count("local_digest_present")
    pending = count("local_digest_pending")
    waived_events = [event for event in reads if event.get("code") == "local_first_waived"]
    waived = len(waived_events)

    intents = [row for row in audit_entries if row.get("event") == "digest_intent"]
    declined = sum(1 for row in intents
                  if isinstance(row.get("expires_at"), (int, float)) and now >= row["expires_at"])
    outstanding_count = len(intents) - declined
    compelled = len(reads)

    if policy is not None and local_root and worker_executable:
        try:
            result = localfirst.readiness(
                policy=policy, state_root=state_root, local_queue_root=local_root,
                worker_executable=worker_executable, window_bytes=localfirst.MAX_WINDOW_BYTES)
            readiness_now: dict[str, Any] = {
                "ready": result.ready, "code": result.code, "reason": result.reason,
                "considered": result.considered}
        except Exception as exc:  # noqa: BLE001  a report never crashes
            readiness_now = {"ready": False, "error": type(exc).__name__}
    else:
        readiness_now = {"ready": False, "code": "unknown",
                         "reason": "local_queue_root, worker_executable or the routing "
                                   "policy is not available to this report"}

    return {
        "readiness_now": readiness_now,
        "read_gate_strength": {"claude": "deterministic", "codex": "heuristic"},
        "compelled": {
            "count": compelled,
            "definition": "gated-shape reads: passed the mechanical-artifact fast path "
                          "(in a repository, local_first enabled, mechanical_ok, a glob "
                          "match, at or above read_gate_min_bytes) and were judged",
            "by_client": _histogram(event.get("client") for event in reads),
            "by_repo": _histogram(repo for event in reads for repo in event.get("repos") or []),
            "by_glob": _histogram(event.get("matched_glob") for event in reads
                                  if event.get("matched_glob")),
        },
        "digested": {
            "count": digested,
            "meaning": "allowed because a matching digest had already completed "
                      "(local_digest_present); reading the file in full after that is "
                      "expected -- it is the cloud's own judgment pass, not a bypass",
        },
        "pending": {
            "count": pending,
            "meaning": "denied because a digest is queued or running; the caller was "
                      "told to retry with work_result",
        },
        "waived": {
            "count": waived,
            "by_reason": _histogram(event.get("waiver_reason") for event in waived_events),
            "meaning": "allowed uncompelled: the local lane was not ready, or the digest "
                      "that would have covered this file ended failed/unknown/cancelled/"
                      "expired/deferred, or the file changed inside the grace window",
        },
        "declined": {
            "count": declined,
            "meaning": "a digest was required and the intent expired before it was met; "
                      "the same honest label this project's write-gate accounting uses "
                      "(design section 6, finding 22): answering a deny with grep is not "
                      "a bypass this mechanism can tell apart from giving up",
        },
        "outstanding": {
            "count": outstanding_count,
            "meaning": "a digest was required within this window and has neither been "
                      "met nor expired yet",
        },
        "adds_up": compelled == digested + pending + waived + declined + outstanding_count,
        "bytes": {
            # Filtered by isinstance, not ``int(... or 0)``: the adjacent
            # ``declined`` count above already guards its own ledger field
            # this way, and bytes_estimate/window_bytes get no less -- a
            # non-numeric value in either (a hand-edited or otherwise
            # malformed ledger row) crashed int() and took the whole audit
            # report down with it, rather than just being counted as 0 like
            # an absent one already was.
            "estimated_reaching_cloud_context": sum(
                int(event["bytes_estimate"]) for event in reads
                if event.get("code") in ("local_first_waived", "local_digest_present")
                and isinstance(event.get("bytes_estimate"), (int, float))),
            "digested_locally_exact": sum(
                int(row["window_bytes"]) for row in audit_entries
                if row.get("event") == "digest_submitted"
                and isinstance(row.get("window_bytes"), (int, float))),
            "meaning": "the first is an upper bound (design section 2.5): the file's "
                      "on-disk size, or the caller's own range when one was given; the "
                      "second is exact, from the digest receipts",
        },
    }


def report(state_root: str, *, home: str | None = None,
           config_path: str | None = None, since_hours: float = 24.0,
           clock: Any = time.time) -> dict[str, Any]:
    """The audit. Every field is derived from a durable record."""
    home = home if home is not None else os.path.expanduser("~")
    now = float(clock())
    since = now - since_hours * 3600 if since_hours > 0 else None

    routing = gate.receipt_dir(state_root)
    audit_entries = _read_ledger(os.path.join(routing, gate.AUDIT_LEDGER), since)
    events = _read_ledger(os.path.join(routing, gate.EVENT_LEDGER), since)
    decisions = _decision_rows(audit_entries)

    def moved(row: dict[str, Any]) -> bool:
        return (str(row.get("code", "")).startswith(ROUTED_PREFIX)
                or row.get("decision") in ("peer", "local"))

    # Partitioned by predicate, not by ``row not in routed``: two decisions
    # with identical contents compare equal as dicts, so membership put both
    # in whichever bucket the first one landed in and the counts stopped
    # adding up to the total.
    routed = [row for row in decisions if moved(row)]
    retained = [row for row in decisions if not moved(row)]
    eligible = [row for row in decisions if _eligible(row)]

    denials = [event for event in events if event.get("permission") == "deny"]
    failures = [event for event in denials if event.get("code") in FAILURE_CODES]
    tamper = [event for event in denials if event.get("code") in TAMPER_CODES]

    capacity_db = None
    execution_root = None
    local_root = None
    worker_executable = None
    if config_path and os.path.exists(config_path):
        loaded = store.read_json_or_none(config_path)
        if isinstance(loaded, dict):
            capacity_db = loaded.get("capacity_db")
            execution_root = loaded.get("execution_queue_root")
            local_root = loaded.get("local_queue_root")
            worker_executable = loaded.get("worker_executable")

    stages: dict[str, Any] = {"available": False}
    if capacity_db and os.path.exists(capacity_db):
        try:
            from ..capacity_router import StageRouter

            stages = {"available": True, **StageRouter(capacity_db).report()}
        except Exception as exc:  # noqa: BLE001  a report never crashes
            stages = {"available": False, "error": type(exc).__name__}

    unmet = outstanding_intents(state_root)
    coverage = _hook_coverage(home)

    policy_obj = None
    try:
        policy_obj = autoroute.load_policy(state_root)
        policy_state: dict[str, Any] = {
            "readable": True,
            "classified_repositories": len(policy_obj.repos),
            "default_allows_dispatch": bool(policy_obj.default.allowed_routes),
        }
    except Exception as exc:  # noqa: BLE001
        policy_state = {"readable": False, "error": type(exc).__name__,
                        "consequence": "the gate denies every gated call until "
                                       "the policy can be read"}

    local_first_section = _local_first_report(
        state_root, events=events, audit_entries=audit_entries, local_root=local_root,
        worker_executable=worker_executable, policy=policy_obj, now=now)

    return {
        "generated_at": now,
        "window_hours": since_hours,
        "state_root": state_root,
        "policy": policy_state,

        "eligible": {
            "count": len(eligible),
            "definition": "decisions whose operator policy permitted a route "
                          "other than the assistant that asked",
            "by_code": _histogram(row.get("code") for row in eligible),
        },
        "routed": {
            "count": len(routed),
            "by_route": _histogram(row.get("owner_route") for row in routed),
            "by_code": _histogram(row.get("code") for row in routed),
            "reasons": [{"repo": row.get("repo"), "route": row.get("owner_route"),
                         "code": row.get("code"), "reason": row.get("reason")}
                        for row in routed],
        },
        "retained": {
            "count": len(retained),
            "by_code": _histogram(row.get("code") for row in retained),
            "reasons": [{"repo": row.get("repo"), "code": row.get("code"),
                         "reason": row.get("reason")} for row in retained],
        },
        "automatic_share": {
            "decisions": len(decisions),
            "made_automatically": sum(1 for row in decisions if row.get("automatic")),
            "made_by_an_agent_calling_routing_decide":
                sum(1 for row in decisions if not row.get("automatic")),
        },

        "bypasses": {
            "observed": {
                "routed_but_never_dispatched": {
                    "count": len(unmet),
                    "meaning": "the gate refused the edit and named the route; "
                               "no job was submitted for it",
                    "items": unmet,
                },
                "clients_without_the_hook": sorted(
                    client for client in ("claude", "codex")
                    if not coverage.get(client, {}).get("installed")),
                "writes_aimed_at_the_gate_itself": {
                    "count": len(tamper),
                    "codes": _histogram(event.get("code") for event in tamper),
                },
            },
            "not_countable": list(NOT_COUNTABLE),
            "warning": "a zero above means none this mechanism can see, not "
                       "none. The not_countable list is what it cannot see.",
        },

        "failures": {
            "gate": {
                "count": len(failures),
                "by_code": _histogram(event.get("code") for event in failures),
                "items": [{"at": event.get("at"), "client": event.get("client"),
                           "tool": event.get("tool"), "code": event.get("code"),
                           "repos": event.get("repos")} for event in failures],
            },
            "execution_queue": _queue_report(execution_root),
            "local_queue": _local_queue_report(local_root),
        },

        "gate_events": {
            "count": len(events),
            "by_code": _histogram(event.get("code") for event in events),
            "denials": len(denials),
        },
        "hook_coverage": coverage,
        "stages": stages,
        "local_first": local_first_section,
    }


def render(document: dict[str, Any]) -> str:
    """A short readable summary. The JSON stays the record of account."""
    lines: list[str] = []
    window = document.get("window_hours")
    lines.append(f"Delegation audit, last {window} hours")
    lines.append("")
    policy = document.get("policy", {})
    if not policy.get("readable", True):
        lines.append(f"  POLICY UNREADABLE ({policy.get('error')}): "
                     f"{policy.get('consequence')}")
        lines.append("")
    else:
        lines.append(f"  classified repositories: {policy.get('classified_repositories')}")
    share = document.get("automatic_share", {})
    lines.append(f"  decisions: {share.get('decisions')} "
                 f"({share.get('made_automatically')} automatic, "
                 f"{share.get('made_by_an_agent_calling_routing_decide')} agent-requested)")
    lines.append(f"  eligible to delegate: {document['eligible']['count']}")
    lines.append(f"  routed away:          {document['routed']['count']} "
                 f"{document['routed']['by_route'] or ''}")
    lines.append(f"  retained:             {document['retained']['count']}")
    lines.append("")
    observed = document["bypasses"]["observed"]
    lines.append("  bypasses the gate can see:")
    lines.append(f"    routed but never dispatched: "
                 f"{observed['routed_but_never_dispatched']['count']}")
    missing = observed["clients_without_the_hook"]
    lines.append(f"    clients without the hook:    {', '.join(missing) if missing else 'none'}")
    lines.append(f"    writes aimed at the gate:    "
                 f"{observed['writes_aimed_at_the_gate_itself']['count']}")
    lines.append(f"  {document['bypasses']['warning']}")
    lines.append("")
    capacity = (document.get("stages") or {}).get("capacity") or {}
    if capacity:
        lines.append("  capacity, and who said so:")
        for route in sorted(capacity):
            entry = capacity[route]
            lines.append(f"    {route}: {entry.get('status')} "
                         f"(source {entry.get('source')})")
    else:
        lines.append("  capacity: nothing recorded, so no peer is dispatched to")
    lines.append("")
    lines.append(f"  gate failures: {document['failures']['gate']['count']} "
                 f"{document['failures']['gate']['by_code'] or ''}")
    for name in ("execution_queue", "local_queue"):
        queue = document["failures"][name]
        if queue.get("present"):
            lines.append(f"  {name}: {queue.get('states')}")
            for failure in queue.get("failures", []):
                detail = failure.get("error_detail") or failure.get("harness_error_detail")
                lines.append(f"    {failure.get('job_id')} {failure.get('state')}: "
                             f"{failure.get('error')} / {detail}")
    lines.append("")
    for client in ("claude", "codex"):
        entry = document["hook_coverage"].get(client, {})
        state = "installed" if entry.get("installed") else "NOT INSTALLED"
        if entry.get("installed") and not entry.get("automatic_routing"):
            state += ", automatic routing off"
        lines.append(f"  hook ({client}): {state}")
        if entry.get("consequence"):
            lines.append(f"    {entry['consequence']}")
    lines.append(f"  codex hook trust: {document['hook_coverage'].get('codex_trust')}")
    lf = document.get("local_first", {})
    if lf:
        lines.append("")
        lines.append(f"  local-first read gate: ready now = {lf['readiness_now'].get('ready')} "
                     f"({lf['readiness_now'].get('code')})")
        lines.append(f"    compelled {lf['compelled']['count']}: digested {lf['digested']['count']}, "
                     f"pending {lf['pending']['count']}, waived {lf['waived']['count']}, "
                     f"declined {lf['declined']['count']}, outstanding {lf['outstanding']['count']}"
                     f"{'' if lf['adds_up'] else '  MISMATCH'}")
        strength = lf["read_gate_strength"]
        lines.append(f"    read gate: claude {strength['claude']}, codex {strength['codex']}")
    return "\n".join(lines) + "\n"
