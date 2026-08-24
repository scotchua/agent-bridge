"""Conversation registry and job status, all on disk.

Nothing here is held in memory across calls, so the MCP process can restart
mid-consultation and a later poll still resolves correctly.  Status documents
carry a monotonic `seq` and are written atomically.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from typing import Any

from . import store
from .config import Config
from .errors import BrokerError, ErrorCategory

TERMINAL_STATUSES = ("complete", "failed", "timed_out", "cancelled")
ALL_STATUSES = ("queued", "running", *TERMINAL_STATUSES)

#: Status rank. A write may raise the rank or restate the same status; it may
#: never lower it, and a terminal status is final.
#:
#: This exists because the broker necessarily learns the worker's pid only
#: after spawning it, so its pid-recording write races the worker's own first
#: write. Without an ordering rule that late write could stamp `queued` over a
#: `running` or even `complete` job, after which reconcile() would see a
#: queued job with a dead pid and report a successful consultation as
#: worker_died. Rank enforcement makes the race harmless; `attach_worker_pid`
#: removes it from the status path entirely.
_RANK: dict[str, int] = {"queued": 0, "running": 1,
                         **{s: 2 for s in TERMINAL_STATUSES}}


class IllegalTransition(Exception):
    """A status write that would move a job backwards, or past terminal."""


# ---------------------------------------------------------------- conversations
def create_conversation(cfg: Config, record: dict[str, Any]) -> None:
    path = cfg.conversation_path(record["conversation_id"])
    store.secure_mkdir(os.path.dirname(path))
    store.atomic_write_json(path, record)


def load_conversation(cfg: Config, conversation_id: str) -> dict[str, Any]:
    path = cfg.conversation_path(conversation_id)
    data = store.read_json_or_none(path)
    if data is None:
        raise BrokerError(ErrorCategory.CONVERSATION_NOT_FOUND)
    return data


def claim_conversation_slot(
    cfg: Config, conversation_id: str, caller: str, job_id: str, max_turns: int
) -> dict[str, Any]:
    """Admit one continuation, atomically.

    Every admission check and the claim itself happen inside one lock on the
    conversation record, so two MCP server processes cannot both conclude the
    conversation is idle and resume the same peer session concurrently. The
    claim is the `active_job_id` field; the worker clears it on the way out.
    """
    path = cfg.conversation_path(conversation_id)
    # A timeout here means another process holds this conversation's lock, which
    # is "busy", not the global admission gate timing out. Reporting GATE_TIMEOUT
    # would name the right genus and the wrong species.
    try:
        lock = store.file_lock(path + ".lock")
    except TimeoutError as exc:
        raise BrokerError(ErrorCategory.CONVERSATION_BUSY) from exc
    with lock:
        record = store.read_json_or_none(path)
        if record is None:
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_FOUND)
        if record.get("caller") != caller:
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_FOUND)
        if record.get("closed"):
            raise BrokerError(ErrorCategory.CONVERSATION_CLOSED)
        if not record.get("peer_session_id"):
            raise BrokerError(ErrorCategory.PEER_SESSION_ID_MISSING)
        if int(record.get("turns", 0)) >= max_turns:
            raise BrokerError(ErrorCategory.CONVERSATION_TURN_LIMIT)
        if record.get("indeterminate"):
            # A previous worker died with a peer call in flight, so the remote
            # side may have completed it. Resuming this session as though the
            # call failed would break the single-driver property, so recovery
            # is deliberate: agent-bridge-admin resolve.
            raise BrokerError(ErrorCategory.CONVERSATION_INDETERMINATE)
        active = record.get("active_job_id")
        if active and _job_still_running(cfg, str(active), _claim_epoch(record)):
            raise BrokerError(ErrorCategory.CONVERSATION_BUSY)
        if active:
            # Displacing a dead claim. Two orderings matter here.
            #
            # First, the predicate is durable in-flight EVIDENCE, not the
            # reaper's body count: a crashed attempt whose group already exited
            # leaves the remote call just as indeterminate as a live one.
            #
            # Second, the hold is persisted BEFORE anything is signalled. The
            # reverse order has a crash window in which the only local evidence
            # (a live group) is destroyed and no hold is recorded, after which a
            # later admission sees nothing and reuses the session.
            markers = inflight_attempts(cfg, str(active))
            if markers:
                record["indeterminate"] = True
                record["indeterminate_reason"] = "worker died with a peer call in flight"
                record["indeterminate_job_id"] = str(active)
                record["indeterminate_at"] = store.utc_now()
                record["indeterminate_attempts"] = [
                    {k: m.get(k) for k in ("attempt", "pgid", "leader_pid", "spawned_at")}
                    for m in markers
                ]
                store.atomic_write_json(path, record)   # durable, before signalling
                record["indeterminate_reaped"] = reap_orphaned_peers(cfg, str(active))
                store.atomic_write_json(path, record)   # outcome, second write
                raise BrokerError(ErrorCategory.CONVERSATION_INDETERMINATE)
        record["active_job_id"] = job_id
        record["active_job_claimed_at"] = store.utc_now()
        record["updated_at"] = store.utc_now()
        store.atomic_write_json(path, record)
        return record


#: Default seconds a claim with no worker pid yet is treated as live.
#: Overridable via limits.claim_grace_seconds. With the worker's ownership
#: handshake in place this is an availability tuning value, not a correctness
#: boundary: too low abandons launches unnecessarily, too high delays crash
#: recovery. It can no longer allow two workers to reach the same peer session.
CLAIM_GRACE_SECONDS = 30


def claim_grace(cfg: Config) -> float:
    try:
        return float(cfg.limit("claim_grace_seconds"))
    except (ValueError, KeyError):
        return float(CLAIM_GRACE_SECONDS)


def assert_conversation_ownership(cfg: Config, conversation_id: str, job_id: str) -> None:
    """Confirm, under the conversation lock, that job_id still owns the slot.

    This is the handshake that makes the claim grace a tuning value instead of
    a correctness boundary. A launcher that stalled past its grace could
    otherwise lose the slot to a second continuation and still go on to invoke
    the peer, so two workers would drive the same peer session and only the
    losing release would notice, after the call.

    The worker calls this AFTER publishing its pid in the `running` status and
    BEFORE any peer invocation. The order is what closes the window: once the
    pid is visible, a rival's liveness check sees a live process and cannot
    steal the slot; and if a rival got there first, this raises and no peer
    call happens.
    """
    path = cfg.conversation_path(conversation_id)
    with store.file_lock(path + ".lock"):
        record = store.read_json_or_none(path)
        if record is None:
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_FOUND)
        if record.get("active_job_id") != job_id:
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_OWNED)


def _job_still_running(cfg: Config, job_id: str, claimed_at: float | None = None) -> bool:
    """Whether a claimed job should still be considered in flight.

    claimed_at is the epoch time the claim was recorded, and it is what covers
    the window between claiming a conversation slot and writing the job's first
    status file. Without it a claim whose status file did not exist yet was
    read as dead immediately, so a second job could take the slot while the
    first was still launching and both would resume the same peer session.
    """
    grace = claim_grace(cfg)
    now = time.time()
    if claimed_at is not None and claimed_at > now:
        # A future timestamp is corrupt, so it is not honoured. Clamping it to
        # `now` was worse than useless: it restarted the grace window on every
        # check, which is exactly the unbounded lease it was meant to prevent.
        # Correctness no longer rests on this value anyway, because the worker
        # verifies ownership under lock before calling the peer.
        claimed_at = None
    status = store.read_json_or_none(os.path.join(cfg.job_dir(job_id), "status.json"))
    if not status:
        if claimed_at is None:
            return False
        return (now - claimed_at) < grace
    if status.get("status") in TERMINAL_STATUSES:
        return False
    pid = status.get("worker_pid")
    if pid is None:
        reference = claimed_at
        if reference is None:
            try:
                reference = os.path.getmtime(
                    os.path.join(cfg.job_dir(job_id), "status.json"))
            except OSError:
                return False
        if reference > now:
            return False
        return (now - reference) < grace
    return pid_alive(pid)


def _claim_epoch(record: dict[str, Any]) -> float | None:
    raw = record.get("active_job_claimed_at")
    if raw is None:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def retire_attempt_markers(cfg: Config, job_id: str) -> list[str]:
    """Retire in-flight markers after the worker has durably committed.

    This is the only place a marker is removed on the success path. Retiring it
    any earlier, for instance when the peer process exited, would reopen the
    window between local process exit and durable commit.
    """
    retired: list[str] = []
    for marker in inflight_attempts(cfg, job_id):
        path = marker.get("marker_path")
        if not path:
            continue
        try:
            os.replace(path, path + ".committed")
            retired.append(str(marker.get("attempt")))
        except OSError:
            pass
    return retired


def resolve_indeterminate(cfg: Config, conversation_id: str) -> dict[str, Any]:
    """Clear the indeterminate flag after a human has checked the peer side."""
    path = cfg.conversation_path(conversation_id)
    with store.file_lock(path + ".lock"):
        record = store.read_json_or_none(path)
        if record is None:
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_FOUND)
        # Retire the markers. Leaving them in place would re-trigger the hold
        # on the very next admission, making resolve useless.
        retired = []
        for marker in inflight_attempts(cfg, str(record.get("indeterminate_job_id") or "")):
            source = marker.get("marker_path")
            if not source:
                continue
            try:
                os.replace(source, source + ".resolved")
                retired.append(marker.get("attempt"))
            except OSError:
                pass
        record["indeterminate_markers_retired"] = retired
        record["indeterminate"] = False
        record["indeterminate_resolved_at"] = store.utc_now()
        record["active_job_id"] = None
        record["active_job_claimed_at"] = None
        record["updated_at"] = store.utc_now()
        store.atomic_write_json(path, record)
        return record


def release_conversation_slot(
    cfg: Config, conversation_id: str, job_id: str, **changes: Any
) -> dict[str, Any]:
    """Clear the claim and apply end-of-turn changes, ONLY if job_id owns it.

    Ownership gates every change, not just clearing the claim. An earlier
    version gated only the clear and applied the turn increment and session id
    unconditionally, so a job that had lost the slot could still mutate the
    conversation. Every job holds a claim, initial turns included, so this rule
    applies universally rather than having an exception that reintroduces the
    hole.

    The increment happens inside the lock that guards its own read, so a
    concurrent update cannot lose it and let the turn limit be exceeded.
    """
    path = cfg.conversation_path(conversation_id)
    with store.file_lock(path + ".lock"):
        record = store.read_json_or_none(path)
        if record is None:
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_FOUND)
        if record.get("active_job_id") != job_id:
            # Not the owner. Change nothing and say so, rather than silently
            # applying a turn increment on someone else's conversation.
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_OWNED)
        if changes.pop("increment_turns", False):
            record["turns"] = int(record.get("turns", 0)) + 1
        record.update(changes)
        record["active_job_id"] = None
        record["active_job_claimed_at"] = None
        record["updated_at"] = store.utc_now()
        store.atomic_write_json(path, record)
        return record


def update_conversation(cfg: Config, conversation_id: str, **changes: Any) -> dict[str, Any]:
    path = cfg.conversation_path(conversation_id)
    with store.file_lock(path + ".lock"):
        data = store.read_json_or_none(path)
        if data is None:
            raise BrokerError(ErrorCategory.CONVERSATION_NOT_FOUND)
        data.update(changes)
        data["updated_at"] = store.utc_now()
        store.atomic_write_json(path, data)
    return data


# ------------------------------------------------------------------------ jobs
def write_status(
    cfg: Config,
    job_id: str,
    status: str,
    *,
    error_category: str | None = None,
    strict: bool = False,
    expected_seq: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Write a job status under lock, refusing an illegal transition.

    With strict=False an illegal transition is a no-op returning the current
    document, because the losing side of a benign race should not crash. With
    strict=True it raises, which the tests use to prove the rule is real.
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"invalid status {status!r}")
    job_dir = store.secure_mkdir(cfg.job_dir(job_id))
    path = os.path.join(job_dir, "status.json")
    with store.file_lock(os.path.join(job_dir, "status.lock")):
        previous = store.read_json_or_none(path) or {}
        current = previous.get("status")
        if current is not None:
            # A terminal document is immutable, INCLUDING a same-status
            # rewrite. Allowing failed -> failed let a stale reconciler replace
            # a real peer failure with worker_died: it read `running`, the
            # worker then wrote its own terminal record and exited, the
            # reconciler saw the pid gone and rewrote the category. Same rank
            # is not the same fact.
            if _RANK[current] == 2:
                if strict:
                    raise IllegalTransition(f"{current} -> {status} (terminal is final)")
                return previous
            if _RANK[status] < _RANK[current]:
                if strict:
                    raise IllegalTransition(f"{current} -> {status}")
                return previous
        if expected_seq is not None and int(previous.get("seq", 0)) != int(expected_seq):
            # Compare-and-set for a caller that decided based on an earlier
            # read. Anything that changed since invalidates the decision.
            if strict:
                raise IllegalTransition(
                    f"seq moved {previous.get('seq')} != {expected_seq}")
            return previous
        document: dict[str, Any] = {
            "job_id": job_id,
            "status": status,
            "error_category": error_category,
            "seq": int(previous.get("seq", 0)) + 1,
            "updated_at": store.utc_now(),
            "created_at": previous.get("created_at") or store.utc_now(),
        }
        for key in ("conversation_id", "peer", "caller", "worker_pid", "started_at",
                    "finished_at", "attempts", "label", "retries_exhausted",
                    "orphaned_peers_reaped"):
            if key in previous and key not in extra:
                document[key] = previous[key]
        document.update(extra)
        store.atomic_write_json(path, document)
        _index_active(cfg, job_id, status)
        return document


def attach_worker_pid(cfg: Config, job_id: str, pid: int) -> dict[str, Any]:
    """Record the worker pid without touching status.

    The broker cannot know the pid before spawning, so this is deliberately not
    a status write. If the worker has already reached a terminal state the pid
    is not recorded at all: nothing needs it, and writing it would only muddy
    the record.
    """
    job_dir = cfg.job_dir(job_id)
    path = os.path.join(job_dir, "status.json")
    with store.file_lock(os.path.join(job_dir, "status.lock")):
        document = store.read_json_or_none(path) or {}
        if document.get("status") in TERMINAL_STATUSES:
            return document
        document["worker_pid"] = pid
        document["seq"] = int(document.get("seq", 0)) + 1
        document["updated_at"] = store.utc_now()
        store.atomic_write_json(path, document)
        return document


def read_status(cfg: Config, job_id: str) -> dict[str, Any]:
    path = os.path.join(cfg.job_dir(job_id), "status.json")
    data = store.read_json_or_none(path)
    if data is None:
        raise BrokerError(ErrorCategory.JOB_NOT_FOUND)
    return data


def read_result(cfg: Config, job_id: str) -> dict[str, Any] | None:
    return store.read_json_or_none(os.path.join(cfg.job_dir(job_id), "result.json"))


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


#: Name of the durable in-flight marker a worker leaves while a peer call runs.
INFLIGHT_MARKER = "peer.pgid"


def inflight_attempts(cfg: Config, job_id: str) -> list[dict[str, Any]]:
    """Durable evidence that a peer call was in flight when the worker stopped.

    Marker PRESENCE is the predicate that blocks admission, deliberately not
    the number of processes a reaper managed to kill. A crashed attempt can
    leave a marker whose local group has already exited on its own, and the
    remote side of that call is exactly as indeterminate as if the group were
    still alive. Counting kills would wave that case through.
    """
    found: list[dict[str, Any]] = []
    attempts_root = os.path.join(cfg.job_dir(job_id), "attempts")
    if not os.path.isdir(attempts_root):
        return found
    for entry in sorted(os.scandir(attempts_root), key=lambda e: e.name):
        marker_path = os.path.join(entry.path, INFLIGHT_MARKER)
        if not os.path.isfile(marker_path):
            continue
        raw = store.read_json_or_none(marker_path)
        # A marker may be unreadable, or may parse as valid JSON that is not an
        # object: an older bare-integer marker parses to an int, for example.
        # Either way its PRESENCE is the evidence that matters. Identity is then
        # unverifiable, so it will hold admission but never be signalled.
        marker: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {
            "unparseable_marker": True,
            "raw_repr": repr(raw)[:80],
        }
        marker["attempt"] = entry.name
        marker["marker_path"] = marker_path
        found.append(marker)
    return found


def _group_identity_matches(marker: dict[str, Any]) -> tuple[bool, str]:
    """Confirm the recorded group is still the one we spawned.

    Returns (safe_to_signal, reason). A numeric PGID alone is not identity: it
    can be recycled. If identity cannot be established, the answer is no. An
    unreaped orphan is a bounded nuisance; signalling an unrelated process
    group is not.
    """
    if marker.get("phase") == "peer_exited_uncommitted":
        # The peer process is gone, so there is nothing to signal, but the
        # worker never committed the outcome. The remote turn may have
        # completed, so the conversation still holds.
        return False, "peer exited but the worker never committed the outcome"
    if marker.get("phase") == "pre_spawn":
        # The worker died around process creation. Whether a peer exists at all
        # is unknown, so there is nothing safe to signal, and the hold stands.
        # This is reported as needing a look precisely because an unrecorded
        # peer may exist.
        return False, "worker died at spawn time; no process identity recorded"
    pgid = marker.get("pgid")
    leader_pid = marker.get("leader_pid")
    if not isinstance(pgid, int) or pgid <= 1:
        return False, "no usable pgid recorded"
    if pgid == os.getpgrp():
        return False, "refuses to signal the broker's own group"
    if not isinstance(leader_pid, int):
        return False, "no leader pid recorded"
    try:
        probe = subprocess.run(  # noqa: S603 - fixed argv, shell off
            ["ps", "-o", "lstart=,pgid=", "-p", str(leader_pid)],
            capture_output=True, timeout=10, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return False, "process identity could not be verified"
    text = probe.stdout.decode("utf-8", "replace").strip()
    if not text:
        return False, "group leader is gone"
    parts = text.rsplit(None, 1)
    if len(parts) != 2:
        return False, "unreadable ps output"
    observed_start, observed_pgid = parts[0].strip(), parts[1].strip()
    if observed_pgid != str(pgid):
        return False, "leader pid no longer belongs to the recorded group"
    recorded_start = str(marker.get("leader_start") or "").strip()
    if not recorded_start:
        return False, "no spawn identity recorded"
    if observed_start != recorded_start:
        return False, "pid was recycled: start time differs"
    return True, "identity verified"


def reap_orphaned_peers(cfg: Config, job_id: str) -> list[dict[str, Any]]:
    """Kill peer process groups left behind by a dead worker.

    A peer runs in its own session so the worker can kill it as a unit on
    timeout. The cost of that design is that a worker killed mid-call leaves
    the peer running with nothing watching it. The pgid is recorded at spawn
    time precisely so this function can reap it.

    Only a pgid file that OUTLIVED its run is a candidate: the runner deletes
    the file when a peer finishes normally. A recycled process-group number can
    therefore only be signalled if a crash left the file behind, which bounds
    the reuse exposure to genuinely interrupted attempts.
    """
    from . import runner
    outcomes: list[dict[str, Any]] = []
    for marker in inflight_attempts(cfg, job_id):
        safe, reason = _group_identity_matches(marker)
        record: dict[str, Any] = {
            "attempt": marker.get("attempt"),
            "pgid": marker.get("pgid"),
            "identity": reason,
            "signalled": False,
        }
        if not safe:
            # Not signalled. The hold stays regardless, and an operator is told
            # that manual cleanup may be needed.
            record["manual_cleanup_may_be_needed"] = reason not in (
                "group leader is gone", "refuses to signal the broker's own group")
            outcomes.append(record)
            continue
        record.update(runner._kill_group(int(marker["pgid"]), 2.0))
        record["signalled"] = True
        outcomes.append(record)
    return outcomes


def reconcile(cfg: Config, job_id: str) -> dict[str, Any]:
    """Read status, repairing a job whose worker died without finishing.

    This is what makes polling restart-safe in the bad case as well as the good
    one: a `running` job whose worker pid is gone becomes `failed/worker_died`
    rather than hanging as `running` forever.
    """
    status = read_status(cfg, job_id)
    if status.get("status") in TERMINAL_STATUSES:
        return status
    pid = status.get("worker_pid")
    if status.get("status") == "queued" and pid is None:
        age = time.time() - os.path.getmtime(
            os.path.join(cfg.job_dir(job_id), "status.json")
        )
        # Use the configured grace, not a second hardcoded 30, so there is one
        # tunable governing the spawn window rather than two that can diverge.
        if age < claim_grace(cfg):
            return status
    if not pid_alive(pid):
        # A dead worker may have left a live peer behind. Reap it before
        # declaring the job dead, so the peer cannot keep running against a
        # session a later continuation is about to reuse.
        reaped = reap_orphaned_peers(cfg, job_id)
        # CAS on the seq this decision was based on. If the worker wrote its
        # own terminal record in the meantime, this write is refused and the
        # worker's substantive category survives.
        return write_status(
            cfg, job_id, "failed",
            error_category=ErrorCategory.WORKER_DIED.value,
            finished_at=store.utc_now(),
            expected_seq=int(status.get("seq", 0)),
            orphaned_peers_reaped=reaped or None,
        )
    return status


def _active_marker(cfg: Config, job_id: str) -> str:
    return cfg.state("active", job_id)


def _index_active(cfg: Config, job_id: str, status: str) -> None:
    """Maintain a small index of jobs in flight.

    count_active runs inside the global admission lock, so scanning every
    retained job directory made admission cost grow with retention rather than
    with concurrency: with a 30 day window that is thousands of status reads
    per start, serialised across every broker process. This index is bounded by
    the number of jobs actually in flight.

    The index is a cache, never the source of truth. count_active still
    confirms liveness per entry and drops stale ones, so losing or corrupting
    it degrades throughput, never correctness.
    """
    try:
        marker = _active_marker(cfg, job_id)
        if status in TERMINAL_STATUSES:
            try:
                os.unlink(marker)
            except OSError:
                pass
        else:
            store.secure_mkdir(os.path.dirname(marker))
            if not os.path.exists(marker):
                store.atomic_write_json(marker, {"job_id": job_id})
    except OSError:
        pass


def count_active(cfg: Config) -> int:
    """Count jobs in flight, including ones still inside their spawn window.

    An earlier version required a live worker pid, which excluded every job in
    the window between its `queued` write and its pid being attached. Under a
    burst those uncounted jobs let the global limit be exceeded.
    """
    root = cfg.state("active")
    if not os.path.isdir(root):
        return 0
    active = 0
    for entry in os.scandir(root):
        if _job_still_running(cfg, entry.name):
            active += 1
        else:
            # Stale index entry, for instance a worker killed before it could
            # write a terminal status. Drop it so the index cannot grow without
            # bound when jobs die badly.
            try:
                os.unlink(entry.path)
            except OSError:
                pass
    return active


@contextlib.contextmanager
def admission_gate(cfg: Config) -> Any:
    """Serialise global admission across every broker process.

    Counting then launching is a check-then-act, so two MCP server processes
    could both count three jobs against a limit of four and both launch. The
    per-conversation lock does not help: this is global admission control, so
    it needs a global lock. Held only for the count plus the first status
    write, which is a directory scan and one file, not a peer call.
    """
    store.secure_mkdir(cfg.state_root)
    with store.file_lock(cfg.state("admission.lock"), timeout=30.0):
        yield


def conversation_has_active_job(cfg: Config, conversation_id: str) -> bool:
    """Read-only view of the claim, for reporting. Admission uses the lock."""
    record = store.read_json_or_none(cfg.conversation_path(conversation_id))
    if not record:
        return False
    active = record.get("active_job_id")
    # Must pass the claim epoch, exactly as claim_conversation_slot does.
    # Omitting it made this helper report a pre-status claim as idle while
    # admission correctly held it, so the two disagreed about the same fact.
    return bool(active) and _job_still_running(cfg, str(active), _claim_epoch(record))
