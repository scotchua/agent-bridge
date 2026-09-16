"""Local-first read gate: readiness, glob matching, window arithmetic,
and the calibration record. See ``docs/LOCAL-FIRST-DESIGN.md``.

This module owns the parts of the local-first feature that are pure or
read-only: whether the local lane may be compelled right now (``readiness``),
whether a file's path matches the operator's mechanical-artifact patterns
(``compile_glob``/``matches_any``), how much of a file a digest may read
(``digest_window``), and the calibration record's shape and paths.

It does not judge a tool call (that is ``gate.py``, in a later phase) and it
does not write a digest receipt or dispatch a job (that is
``orchestration/mcp.py``, in a later phase). Keeping this module import-free
of ``gate`` is deliberate: ``gate.py`` will import this module to judge reads,
and a module-level import back would be a cycle. Where this module needs a
value ``gate.py`` also defines (the ``routing`` directory name), it restates
the one-line constant rather than importing it, and says so at the point of
restatement.

``readiness`` never raises and never denies by itself. Every branch that is
not the final ``ready`` reports a reason and leaves the caller to allow the
read: a compute preference must never deadlock work the assistant could
already do (design section 3, invariant 6). An unreadable or malformed
record here is "not ready", not an exception a caller has to translate into
a deny.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import store
from ..localq.spool import QueueCaps
from . import autoroute, delegation

#: Matches ``gate.RECEIPT_DIR``. See the module docstring for why this is a
#: restatement rather than an import.
ROUTING_DIR = "routing"

CALIBRATION_DIR = "local"
CALIBRATION_FILE = "calibration.json"
CALIBRATION_VERSION = 1
#: Repeated per size so one slow or failed run cannot decide a whole tier.
CALIBRATION_RUNS_PER_SIZE = 3

#: ``LocalQueue.submit`` admits on the byte length of the *whole serialized
#: submission* (task_type, priority, classification, caller, purpose and
#: params, not only the input text: see ``LocalQueue.submit``'s own
#: ``normalized = self._normalized(payload)`` check). A window of text at
#: exactly the queue's own cap would therefore submit a total payload
#: *larger* than the queue admits and be refused as ``input_too_large`` the
#: moment it reached a real submission. Measured: a realistic
#: ``work_digest_file``-shaped submission (its task type, priority,
#: classification, caller, purpose, and either lane's own instruction
#: params) adds on the order of 150-200 bytes of JSON envelope around the
#: text. 1,000 bytes of headroom covers every task type's own instruction
#: template with room to spare, so the longest window a digest may ever
#: read is the queue's cap minus that headroom, not the bare cap itself.
JSON_ENVELOPE_HEADROOM_BYTES = 1_000
MAX_WINDOW_BYTES = QueueCaps().max_input_bytes - JSON_ENVELOPE_HEADROOM_BYTES

#: The byte sizes calibration measures at, smallest first. The largest tier
#: is ``MAX_WINDOW_BYTES`` itself, so calibration never measures a window a
#: real digest could not also request.
CALIBRATION_SIZES = (8_000, 16_000, MAX_WINDOW_BYTES)

#: Every code ``readiness`` can return, ``ready`` included. Held as a set so
#: a test can assert the vocabulary is closed, the same discipline
#: ``autoroute.CODES`` already applies to routing decisions.
READINESS_CODES = frozenset({
    "ready",
    "local_first_disabled",
    "local_not_declared",
    "worker_not_configured",
    "calibration_missing",
    "calibration_stale",
    "calibration_worker_changed",
    "executor_not_running",
    "resource_deferred",
    "load_unknown",
    "load_high",
    "over_latency_budget",
})


def routing_dir(state_root: str) -> str:
    return os.path.join(str(state_root), ROUTING_DIR)


def calibration_path(state_root: str) -> str:
    return os.path.join(str(state_root), CALIBRATION_DIR, CALIBRATION_FILE)


def heartbeat_path(local_queue_root: str) -> str:
    return os.path.join(str(local_queue_root), "runtime-state.json")


# --------------------------------------------------------------------- globs


class GlobError(ValueError):
    """An operator-supplied glob pattern cannot be compiled."""


def compile_glob(pattern: str) -> "re.Pattern[str]":
    """Compile one operator-supplied glob into an anchored regex.

    Matched against a POSIX-style path relative to the repository root.
    ``**`` crosses directory boundaries, including zero of them; ``*`` and
    ``?`` do not cross a ``/``.

    Deliberately not ``pathlib.PurePath.match``/``full_match``: their
    ``**`` handling differs between Python 3.11 and 3.13 (this project's
    own CI matrix runs both, see ``.github/workflows/tests.yml``), so a
    pattern that matched on one would not reliably match the same way on
    the other. A direct regex translation gives an identical answer on
    every interpreter this project supports.
    """
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        raise GlobError(f"glob pattern {pattern!r} is invalid")
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*" and pattern[i:i + 2] == "**":
            j = i + 2
            if j < n and pattern[j] == "/":
                out.append(r"(?:.*/)?")
                j += 1
            else:
                out.append(r".*")
            i = j
            continue
        if char == "*":
            out.append(r"[^/]*")
            i += 1
            continue
        if char == "?":
            out.append(r"[^/]")
            i += 1
            continue
        if char == "/":
            out.append("/")
            i += 1
            continue
        out.append(re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def relative_posix_path(path: str, repo_root: str) -> str:
    """``path`` relative to ``repo_root``, forward slashes on every host.

    Raises ``ValueError`` exactly when ``os.path.relpath`` does: on Windows,
    ``ntpath.relpath`` refuses a pair of paths on different drives (``path
    is on mount 'D:', start on mount 'C:'``), which a symlink or junction
    pointing at a second drive makes an ordinary occurrence, not an edge
    case. This function does not swallow that here, because it is a thin
    wrapper over ``os.path.relpath`` and callers other than ``matches_any``
    may want to see it; ``matches_any`` is what promises "never matches" and
    is where that promise is kept.
    """
    relative = os.path.relpath(path, repo_root)
    if os.sep != "/":
        relative = relative.replace(os.sep, "/")
    if os.altsep:
        relative = relative.replace(os.altsep, "/")
    return relative


def matches_any(path: str, repo_root: str, globs: "tuple[str, ...] | list[str]") -> bool:
    """Whether ``path`` (inside ``repo_root``) matches any of ``globs``.

    A path that resolves outside ``repo_root`` (a symlink target, most
    plausibly) never matches: these globs are the operator's statement about
    *this repository's* artifacts, and a pattern must not reach outside it
    by following a link the operator never named. On Windows that includes a
    link whose target lives on a different drive letter, which
    ``os.path.relpath`` cannot even express as a relative path and raises
    ``ValueError`` for rather than returning one starting with ``..``; that
    is exactly the same fact (the target is outside this repository) by a
    different signal, and is treated identically.
    """
    if not globs:
        return False
    try:
        relative = relative_posix_path(path, repo_root)
    except ValueError:
        return False
    if relative == ".." or relative.startswith("../") or relative.startswith("..\\"):
        return False
    return any(compile_glob(glob).match(relative) for glob in globs)


def effective_globs(repo_policy: "autoroute.RepoPolicy",
                    local_first: "autoroute.LocalFirstConfig") -> tuple[str, ...]:
    """The globs that govern one repository: its own, or the operator's
    project-wide default when it names none of its own. Not a second way to
    say "everything": a repository not naming its own patterns gets the
    narrow default, never a wildcard invented here."""
    return repo_policy.mechanical_globs or local_first.default_globs


# ------------------------------------------------------------ window arithmetic


class WindowError(ValueError):
    """The requested digest window cannot be satisfied."""


def digest_window(size: int, *, offset: "int | None" = None,
                  max_window_bytes: int = MAX_WINDOW_BYTES) -> tuple[int, int]:
    """``(offset, length)`` for the bytes one digest job may read.

    The window length is fixed at ``min(size, max_window_bytes)``; only the
    offset may vary, and an offset that would make the window shorter than
    that fixed length is refused (``window_too_small``). This is what closes
    the cheap-receipt attack in the design's adversarial review (section 6,
    finding 3): a caller cannot digest a tiny slice of a large file and use
    the receipt as license to read the rest in the cloud, because the window
    this function hands back is always the full fixed length or nothing.

    The default offset is the tail (``size - window``), because a log's
    failure is usually at its end.
    """
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise WindowError("size must be a non-negative integer")
    if max_window_bytes <= 0:
        raise WindowError("max_window_bytes must be positive")
    window = min(size, max_window_bytes)
    default_offset = max(0, size - window)
    if offset is None:
        return default_offset, window
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > size:
        raise WindowError("offset out of range")
    if size - offset < window:
        raise WindowError("window_too_small")
    return offset, window


# ------------------------------------------------------------------ readiness


@dataclass(frozen=True)
class Readiness:
    """Whether the local lane may be compelled for one window right now.

    ``considered`` holds no task content, the same rule
    ``autoroute.Decision.considered`` follows: numbers and code names, so a
    denial or a waiver can be explained and re-derived rather than taken on
    trust.
    """

    ready: bool
    code: str
    reason: str
    considered: dict[str, Any] = field(default_factory=dict)


def _not_ready(code: str, reason: str, considered: dict[str, Any]) -> Readiness:
    return Readiness(False, code, reason, considered)


def readiness(*, policy: "autoroute.Policy", state_root: str, local_queue_root: str,
             worker_executable: str, window_bytes: int,
             clock: Callable[[], float] = time.time,
             load: "autoroute.Load | None" = None) -> Readiness:
    """Whether the local lane is ready to digest a window of ``window_bytes``.

    Ten reasons a caller cannot be compelled, checked in the order the
    design table fixes, first failure wins; the tenth (``over_latency_budget``)
    is deliberately last, because it is the only one that needs the
    calibration record's own numbers rather than just its presence, and
    every earlier check has to pass before those numbers mean anything.
    """
    lf = policy.local_first
    window = max(0, min(int(window_bytes), MAX_WINDOW_BYTES))
    considered: dict[str, Any] = {"window_bytes": window,
                                  "budget_s": lf.latency_budget_seconds}

    if not lf.enabled:
        return _not_ready(
            "local_first_disabled",
            "local_first.enabled is not true in the operator's routing policy",
            considered)
    if "local" not in policy.declared_routes:
        return _not_ready(
            "local_not_declared",
            "\"local\" is not in declared_available; the operator has not "
            "declared this machine's local route installed",
            considered)

    try:
        worker_name = os.path.basename(str(worker_executable))
        worker_exists = os.path.isfile(worker_executable)
    except (OSError, TypeError, ValueError):
        worker_name, worker_exists = "", False
    if worker_name == delegation.NO_WORKER_SENTINEL or not worker_exists:
        return _not_ready(
            "worker_not_configured",
            "no local worker executable is configured, or the configured "
            "path does not exist",
            considered)

    now = float(clock())
    calibration = store.read_json_or_none(calibration_path(state_root))
    if not isinstance(calibration, dict):
        return _not_ready(
            "calibration_missing",
            f"no calibration record at {calibration_path(state_root)}; run "
            "agent-bridge-orchestration-verify calibrate first",
            considered)
    created_at = calibration.get("created_at")
    max_age_seconds = lf.calibration_max_age_days * 86400.0
    if (not isinstance(created_at, (int, float)) or isinstance(created_at, bool)
            or now - created_at > max_age_seconds):
        return _not_ready(
            "calibration_stale",
            f"the calibration record is missing a timestamp or is older "
            f"than {lf.calibration_max_age_days} days; recalibrate",
            considered)
    considered["calibration_created_at"] = created_at
    recorded_sha = calibration.get("worker_sha256")
    try:
        current_sha = store.sha256_file(worker_executable)
    except OSError:
        current_sha = None
    if not isinstance(recorded_sha, str) or recorded_sha != current_sha:
        return _not_ready(
            "calibration_worker_changed",
            "the configured worker executable no longer matches the one "
            "this calibration measured; recalibrate",
            considered)

    heartbeat = store.read_json_or_none(heartbeat_path(local_queue_root))
    if not isinstance(heartbeat, dict):
        return _not_ready(
            "executor_not_running",
            f"no local-queue heartbeat at {heartbeat_path(local_queue_root)}; "
            "the orchestration MCP server's background executor is not running",
            considered)
    updated_at = heartbeat.get("updated_at")
    if (not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool)
            or now - updated_at > lf.executor_liveness_seconds):
        return _not_ready(
            "executor_not_running",
            "the local-queue heartbeat is missing a timestamp or is older "
            f"than {lf.executor_liveness_seconds}s; the orchestration MCP "
            "server's background executor appears to have stopped",
            considered)

    queue_state = heartbeat.get("queue")
    resource = queue_state.get("resource") if isinstance(queue_state, dict) else None
    verdict = resource.get("verdict") if isinstance(resource, dict) else None
    if isinstance(verdict, dict):
        if verdict.get("interactive") != "admissible":
            return _not_ready(
                "resource_deferred",
                "the local queue's own resource sample defers interactive "
                "work on this machine right now",
                considered)
    else:
        # The bare-string fallback shape ``LocalQueue.state_report`` writes
        # when its own sampler raised (``{"verdict": "deferred", "reason":
        # ...}``), or a heartbeat too malformed to carry either shape.
        deferred_reason = (resource.get("reason") if isinstance(resource, dict)
                           else "heartbeat_malformed")
        return _not_ready(
            "resource_deferred",
            f"the local queue's resource sample is unavailable ({deferred_reason})",
            considered)

    reading = load if load is not None else autoroute.probe_load()
    considered["load_per_core"] = reading.busy_at
    if not reading.known:
        return _not_ready(
            "load_unknown",
            "this host exposes no load average, so local capacity is unknown",
            considered)
    if reading.ratio >= policy.max_local_load_ratio:
        return _not_ready(
            "load_high",
            f"load per core is {reading.busy_at}, at or above the "
            f"{policy.max_local_load_ratio} ceiling",
            considered)

    covering_size, covering_median = _covering_calibration(calibration, window)
    considered["calibrated_size"] = covering_size
    considered["calibrated_median_s"] = covering_median
    if covering_median is None:
        return _not_ready(
            "over_latency_budget",
            "no calibrated size at or above this window's bytes completed "
            "in every measured run; recalibrate",
            considered)
    if covering_median > lf.latency_budget_seconds:
        return _not_ready(
            "over_latency_budget",
            f"the calibrated median for {covering_size} bytes is "
            f"{covering_median}s, at or above the {lf.latency_budget_seconds}s budget",
            considered)

    return Readiness(
        True, "ready",
        f"local lane ready: {covering_size} bytes calibrated at "
        f"{covering_median}s against a {lf.latency_budget_seconds}s budget",
        considered)


#: A calibrated size is a byte count; nothing plausible needs more than this
#: many digits (10**18 bytes is already an exabyte). Filtered before any
#: ``int()`` call on an operator- or attacker-influenced string, because
#: Python 3.11+ refuses to convert a digit string past
#: ``sys.set_int_max_str_digits`` (4,300 by default) and raises ``ValueError``
#: instead, which ``readiness()`` must never do: found by an adversarial
#: review feeding ``calibration.json`` a "sizes" key over 4,300 digits long.
_MAX_SIZE_KEY_DIGITS = 18


def _covering_calibration(calibration: dict[str, Any],
                          window_bytes: int) -> tuple["int | None", "float | None"]:
    """The smallest calibrated size at or above ``window_bytes`` whose every
    measured run reached ``complete``, and its median. ``(None, None)`` when
    no such size is on record: a median over a run that never completed is
    not a latency, so an ineligible or absent size never grants readiness."""
    sizes = calibration.get("sizes")
    if not isinstance(sizes, dict):
        return None, None
    numeric = sorted((key for key in sizes
                      if str(key).lstrip("-").isdigit()
                      and len(str(key).lstrip("-")) <= _MAX_SIZE_KEY_DIGITS), key=int)
    for key in numeric:
        if int(key) < window_bytes:
            continue
        entry = sizes[key]
        if not isinstance(entry, dict):
            continue
        outcomes = entry.get("outcomes")
        median = entry.get("median_s")
        if (isinstance(outcomes, list) and outcomes
                and all(outcome == "complete" for outcome in outcomes)
                and isinstance(median, (int, float)) and not isinstance(median, bool)):
            return int(key), float(median)
    return None, None


# ------------------------------------------------------------ calibration record


def build_calibration_record(*, worker_executable: str, worker_state: str,
                             sizes: dict[str, dict[str, Any]],
                             sampler_snapshot: dict[str, Any], host: dict[str, Any],
                             clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """The durable shape ``delegation_verify calibrate`` writes.

    A pure assembler: it does no I/O and makes no provider or model calls,
    so it can be unit-tested without a real local worker.
    """
    return {
        "version": CALIBRATION_VERSION,
        "created_at": float(clock()),
        "worker_sha256": store.sha256_file(worker_executable),
        "worker_state": worker_state,
        "sizes": sizes,
        "sampler": sampler_snapshot,
        "host": host,
    }


def write_calibration_record(state_root: str, record: dict[str, Any]) -> None:
    store.atomic_write_json(calibration_path(state_root), record)


def load_calibration_record(state_root: str) -> "dict[str, Any] | None":
    loaded = store.read_json_or_none(calibration_path(state_root))
    return loaded if isinstance(loaded, dict) else None
