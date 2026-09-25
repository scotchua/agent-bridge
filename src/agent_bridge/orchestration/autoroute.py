"""Deterministic route selection, computed rather than requested.

The stage router (``capacity_router.StageRouter``) already decides who may
*own* a stage, but it decides it from ``allowed_routes`` the calling agent
supplies and then takes the first fresh preferred route. That is a scheduler
taking instructions, not a policy: an agent that wants to keep the work asks
for its own route and gets it.

This module is the policy. It takes what the host can observe and what the
operator has configured, and returns one route plus one reason code. It is
pure: no database, no filesystem writes, no subprocess, no clock of its own.
Everything it needs is passed in, so the same inputs always give the same
decision and a receipt can be re-derived from the inputs recorded beside it.

Four things decide a route, in this order, and the order is the point:

1. **Privacy and eligibility.** A repository the operator has not classified
   is retained, always. Capacity never overrides privacy, so this is checked
   first and nothing later can undo it.
2. **Task type.** Mechanical text work can go to a local model; implementation
   cannot.
3. **Hardware load.** A local model competes with the user's own machine, so a
   loaded or unknown-load host defers rather than assuming spare capacity.
4. **Capacity.** A peer route needs a fresh, available observation.

Anything that survives all four is dispatched. Anything that does not is
retained by the assistant that asked, which is itself a decision with a
reason, recorded like any other. There is no paid fallback here and no way to
express one: ``ROUTES`` is the complete set.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, replace
from collections.abc import Callable
from typing import Any, Mapping

from ..localq.spool import QueueCaps

#: Mirrors ``localfirst.MIN_LOCAL_IDLE_RATIO``/``QueueCaps.min_cpu_idle_ratio``:
#: a directly measured CPU idle fraction at or above this rescues the load
#: check below, for the same reason it rescues queue admission and
#: ``readiness()`` (load-average-per-core conflates waiting-on-I/O with
#: genuine CPU contention). Re-derived here rather than imported from
#: ``localfirst`` because that module already imports this one -- importing
#: back would cycle -- so both read the same ``QueueCaps`` default instead.
MIN_LOCAL_IDLE_RATIO = QueueCaps().min_cpu_idle_ratio

#: Every route that exists. A paid API route is not absent by configuration,
#: it is absent from the vocabulary.
ROUTES = ("claude", "codex", "local")
#: Not a route: the outcome where the asking assistant keeps the work.
RETAIN = "retain"
PEER_FOR_CLIENT = {"claude": "codex", "codex": "claude"}

#: Classifications this project admits at all. ``client_derived`` is listed so
#: a decision can refuse it by name rather than by falling off the end of a
#: lookup, and ``unclassified`` is what an operator has not spoken about.
CLASSIFICATIONS = ("synthetic", "public", "internal_nonclient",
                   "client_derived", "unclassified")
#: What the two provider lanes accept, matching ``execution_queue`` exactly.
PEER_CLASSIFICATIONS = frozenset({"synthetic", "public", "internal_nonclient"})
#: What the local worker accepts, matching ``localq.spool`` exactly. Wider
#: than the peers' set: the local model runs on this machine, so client-derived
#: text sent to it never leaves the host, and a local digest keeps it out of a
#: provider's context rather than adding to it (Scott, 2026-09-24).
LOCAL_CLASSIFICATIONS = frozenset({"synthetic", "public", "internal_nonclient", "client_derived"})

#: Work shapes the gate can tell apart from a tool call. Deliberately coarse:
#: a PreToolUse payload names files and commands, not intent, and inventing a
#: finer reading of it would be a guess dressed as a signal.
TASK_TYPES = ("implementation", "mechanical", "review", "unknown")

#: Load at or above this fraction of a core is "busy". The local worker's own
#: queue already applies ``QueueCaps.max_load_per_core`` when it runs a job;
#: this is the routing-time equivalent, so work is not sent to a lane that
#: will immediately defer it.
# Deliberately aggressive local-first policy. A load average can remain high
# after useful work ends, and Scott prefers starting locally then dialing back
# only after measured user-visible impact. The queue still independently stops
# for memory pressure, thermal pressure, or near-saturated CPU.
DEFAULT_MAX_LOCAL_LOAD = 1.25


class PolicyError(ValueError):
    """The operator's routing policy cannot be read or is self-contradictory."""


@dataclass(frozen=True)
class RepoPolicy:
    """What the operator has said about one repository.

    ``classification`` is the operator's statement about the material in the
    repository, not the agent's. The gate has no agent-supplied field to
    trust: it sees a tool call. So this is the only place a classification
    can come from, and an absent entry means ``unclassified``, which is
    retained.
    """

    classification: str = "unclassified"
    allowed_routes: tuple[str, ...] = ()
    #: Whether mechanical text work in this repository may go to a local model.
    mechanical_ok: bool = False
    #: Glob patterns, matched against a POSIX-style path relative to this
    #: repository's root, naming the mechanical artifacts (logs, captured test
    #: output) a local digest may be compelled for before a cloud read. Empty
    #: means the gate falls back to ``LocalFirstConfig.default_globs``; it is
    #: not a second way to say "everything", so a narrow default stays narrow
    #: unless the operator names their own patterns here.
    mechanical_globs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise PolicyError(f"unknown classification {self.classification!r}")
        for route in self.allowed_routes:
            if route not in ROUTES:
                raise PolicyError(f"unknown route {route!r}")


#: Every key ``LocalFirstConfig`` recognises in the operator's document.
#: Held as a frozenset so ``parse_policy`` can fail closed on an unknown one
#: the same way it already does for the top level and for a repo entry.
LOCAL_FIRST_KEYS = frozenset({
    "enabled", "latency_budget_seconds", "read_gate_min_bytes",
    "digest_max_output_chars", "calibration_max_age_days",
    "digest_grace_seconds", "executor_liveness_seconds", "default_globs",
})


@dataclass(frozen=True)
class LocalFirstConfig:
    """The operator's local-first read-gate settings. See
    ``docs/LOCAL-FIRST-DESIGN.md``. Every default here is documented in that
    file's policy table, traced to an existing constant elsewhere in this
    project rather than invented for this feature.
    """

    #: Off by default. Installing this feature must not start compelling
    #: digests in a repository nobody has opted in, the same reasoning that
    #: keeps a fresh routing policy's ``repos`` empty.
    enabled: bool = False
    #: Matched against a calibrated median for the smallest calibrated size at
    #: or above the window being read. The direct local worker's own
    #: per-request timeout (``local_worker.LocalWorkerServer.timeout_seconds``).
    latency_budget_seconds: float = 30.0
    #: A file at or above this size is a candidate for the read gate. Equal to
    #: ``local_worker.MAX_OUTPUT_CHARS``: a file no larger than the largest
    #: possible draft cannot be shortened by digesting it.
    read_gate_min_bytes: int = 8_000
    #: Half of ``read_gate_min_bytes``, so a digest is always materially
    #: smaller than the smallest file that would have been gated.
    digest_max_output_chars: int = 4_000
    #: How long a calibration record is trusted before ``readiness`` reports
    #: ``calibration_stale``. This project's existing retention default for
    #: "old" (``DATA-RETENTION.md``'s 30-day cleanup window).
    calibration_max_age_days: float = 30.0
    #: How long a digest stays current when its file changes underneath it,
    #: and how long a digest intent stays open before it is reported
    #: ``declined`` rather than ``outstanding``. Equal to
    #: ``autodecide.CLIENT_PRESENCE_SECONDS``, this codebase's existing
    #: definition of "recent".
    digest_grace_seconds: float = 900.0
    #: How stale the local queue's own heartbeat file may be before
    #: ``readiness`` reports ``executor_not_running``. Twelve times the
    #: orchestration server's default 5-second service interval.
    executor_liveness_seconds: float = 60.0
    #: Used only when a repository entry names no ``mechanical_globs`` of its
    #: own. Narrow by design: a repository opts a file shape in, not "every
    #: large file".
    default_globs: tuple[str, ...] = ("**/*.log", "**/logs/**")

    def __post_init__(self) -> None:
        for name, value in (
            ("latency_budget_seconds", self.latency_budget_seconds),
            ("calibration_max_age_days", self.calibration_max_age_days),
            ("digest_grace_seconds", self.digest_grace_seconds),
            ("executor_liveness_seconds", self.executor_liveness_seconds),
        ):
            # math.isfinite rather than only `value <= 0`: Python's json
            # module parses the bare tokens Infinity/-Infinity/NaN by
            # default, and `float("inf") <= 0` is False, so an operator's
            # (or a corrupted) policy naming Infinity here would otherwise
            # parse successfully and permanently disable the readiness
            # check that field exists to bound. Found by an adversarial
            # review.
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise PolicyError(f"{name} must be a finite number above 0")
        for name, value in (
            ("read_gate_min_bytes", self.read_gate_min_bytes),
            ("digest_max_output_chars", self.digest_max_output_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PolicyError(f"{name} must be a positive integer")
        if not isinstance(self.enabled, bool):
            raise PolicyError("local_first.enabled must be true or false")
        if (not isinstance(self.default_globs, tuple)
                or not self.default_globs
                or any(not isinstance(glob, str) or not glob for glob in self.default_globs)):
            raise PolicyError("local_first.default_globs must be a non-empty list of non-empty strings")


def _parse_local_first(document: object) -> LocalFirstConfig:
    """Build a ``LocalFirstConfig`` from the operator's ``local_first`` block.

    Fail closed on shape, exactly like the rest of ``parse_policy``: an
    operator who mistypes a key here must see a refusal, not a silently
    ignored setting. An absent block parses as every default, which is
    ``enabled: False`` and therefore inert.
    """
    if not isinstance(document, dict):
        raise PolicyError("local_first must be an object")
    unknown = set(document) - LOCAL_FIRST_KEYS
    if unknown:
        raise PolicyError("local_first has unknown keys: " + ", ".join(sorted(unknown)))
    globs = document.get("default_globs", list(LocalFirstConfig.default_globs))
    if not isinstance(globs, list) or any(not isinstance(glob, str) for glob in globs):
        raise PolicyError("local_first.default_globs must be a list of strings")
    kwargs: dict[str, Any] = {"default_globs": tuple(dict.fromkeys(globs))}
    for key in LOCAL_FIRST_KEYS - {"default_globs"}:
        if key in document:
            kwargs[key] = document[key]
    return LocalFirstConfig(**kwargs)


@dataclass(frozen=True)
class Policy:
    """The operator's complete routing policy. Never agent-supplied.

    ``default`` applies to a repository with no entry. It is deliberately the
    empty, unclassified policy: automatic dispatch happens only for
    repositories somebody has actually classified, and everything else is
    retained. A default that allowed dispatch would mean installing this
    feature silently started sending unclassified repositories to providers.
    """

    repos: Mapping[str, RepoPolicy] = field(default_factory=dict)
    default: RepoPolicy = RepoPolicy()
    local_classifications: frozenset[str] = LOCAL_CLASSIFICATIONS
    peer_classifications: frozenset[str] = PEER_CLASSIFICATIONS
    #: Per-route narrowing of ``peer_classifications``. A route absent here
    #: uses the global set unchanged. Exists because the two peers can be
    #: different companies under different accounts, possibly different
    #: plans: if one side's terms are weaker, that side should be able to
    #: receive less classified material than the other -- the same
    #: reasoning the consultation bridge's own ``peer_allowed_classifications``
    #: (config.py) already applies on its side. Before this field existed,
    #: this routing/dispatch path had no equivalent: an operator who
    #: narrowed one peer through the consultation bridge got no such
    #: protection here, so the same material could still reach that peer
    #: through ``execution_dispatch`` (an adversarial review's finding).
    route_classifications: Mapping[str, frozenset[str]] = field(default_factory=dict)
    max_local_load_ratio: float = DEFAULT_MAX_LOCAL_LOAD
    #: Tie-break order when more than one route survives every check.
    #:
    #: Empty means no preference, and an eligible peer then wins, because
    #: that is what delegation-first means. The default used to be ``ROUTES``
    #: itself, which reads harmlessly and is not: it ranks claude above codex,
    #: so a Claude client kept every repository classified for both while a
    #: Codex client handed every one of them over. An asymmetry nobody chose
    #: does not belong in a default.
    prefer: tuple[str, ...] = ()
    #: Routes the operator states are installed and may receive work on this
    #: machine. This is the honest replacement for the ``capacity_observe``
    #: tool, which let an assistant declare any route available for as long
    #: as it liked. It is a *standing declaration*, not a live health check,
    #: and it is recorded as one: ``autodecide`` replays it into the capacity
    #: ledger on every decision with the ledger's ordinary short freshness,
    #: so removing a route from this list stops routing to it rather than
    #: waiting for an observation to age out. An empty list means the only
    #: capacity evidence is a peer that has itself run the hook recently.
    declared_routes: tuple[str, ...] = ()
    #: The local-first read-gate settings. Disabled by default, so an
    #: existing policy file with no ``local_first`` block keeps behaving
    #: exactly as it does today.
    local_first: LocalFirstConfig = field(default_factory=LocalFirstConfig)
    #: Model name fragments the operator reserves to themselves. A dispatch
    #: naming a model that contains one of these is refused unless the request
    #: carries the operator's own authorization.
    #:
    #: Matched as a case-folded substring rather than an exact name on purpose.
    #: The same model is spelled several ways across a provider's shorthand,
    #: its full identifier and its dated snapshots, and an exact-match list is
    #: one new snapshot away from being silently empty. Reserving the token
    #: ``astra`` covers ``gpt-6-astra`` and anything later carrying the name;
    #: an exact list reserving ``gpt-6-astra`` would not.
    #:
    #: This lives in the operator's policy file rather than the bridge config
    #: so that the list can be protected from the assistant the list restrains.
    #: Be precise about what protects it. ``gate.protected_paths`` covers the
    #: policy file, but the gate matches paths it can read in a tool call, so a
    #: script that assembles the path at runtime goes straight past it -- this
    #: was demonstrated, not assumed. The property this field needs therefore
    #: comes from the filesystem, not from the gate: the operator sets the
    #: macOS system-immutable flag (``sudo chflags schg``) on the policy file
    #: and on the config naming ``state_root``, after which write, truncate,
    #: unlink, rename and clearing the flag all require root.
    #:
    #: Without that flag this field is a convention, not a control. Anyone
    #: relying on it should check the flag is set rather than trusting the
    #: file's location.
    reserved_models: tuple[str, ...] = ()

    def for_repo(self, repo: str) -> RepoPolicy:
        """The entry for ``repo``, matched on the real path, else the default."""
        real = os.path.realpath(repo)
        found = self.repos.get(real)
        if found is None:
            found = self.repos.get(repo)
        if found is None:
            main = _main_worktree(real)
            inherited = self.repos.get(main) if main else None
            if inherited is not None:
                # A linked worktree (a landing or review checkout) inherits its
                # main repository's classification and local routing, but not
                # its peer routes: automatic hand-offs between assistants stay
                # limited to the checkouts the operator named.
                found = replace(inherited, allowed_routes=tuple(
                    route for route in inherited.allowed_routes if route == "local"))
        return found if found is not None else self.default


def _main_worktree(repo: str) -> str | None:
    """The main checkout of a linked git worktree, or None.

    A linked worktree's ``.git`` is a file reading ``gitdir: <main>/.git/
    worktrees/<name>``. Anything else (a real ``.git`` directory, an
    unreadable or malformed file, a gitdir that is not under a main
    checkout's ``.git/worktrees``) is not a linked worktree, and None keeps
    the caller on the default entry.
    """
    marker = os.path.join(repo, ".git")
    # A symlinked ``.git`` could point at a registered worktree's own file and
    # pass the backlink check below from an unregistered directory.
    if os.path.islink(marker) or not os.path.isfile(marker):
        return None
    try:
        with open(marker, encoding="utf-8") as handle:
            line = handle.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(repo, gitdir)
    gitdir = os.path.realpath(gitdir)
    worktrees = os.path.dirname(gitdir)
    dot_git = os.path.dirname(worktrees)
    if os.path.basename(worktrees) != "worktrees" or os.path.basename(dot_git) != ".git" \
            or not os.path.isdir(gitdir):
        return None
    # The main checkout must have registered this worktree: its
    # administrative directory's ``gitdir`` file points back at this very
    # ``.git`` file. A forged ``.git`` file borrowing another worktree's
    # administrative directory fails here.
    try:
        with open(os.path.join(gitdir, "gitdir"), encoding="utf-8") as handle:
            backlink = handle.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        return None
    # It must name a file called .git directly inside ``repo``. A relative
    # backlink (``git worktree add --relative-paths``) is relative to the
    # administrative directory, never to this process's working directory.
    # Only the directory part is resolved, so a symlinked parent spelling
    # still matches while the .git file itself is never followed.
    if backlink and not os.path.isabs(backlink):
        backlink = os.path.join(gitdir, backlink)
    if not backlink or os.path.basename(os.path.normpath(backlink)) != ".git" \
            or os.path.realpath(os.path.dirname(os.path.normpath(backlink))) != os.path.realpath(repo):
        return None
    return os.path.dirname(dot_git)


@dataclass(frozen=True)
class Load:
    """A portable hardware-load reading, or an admission that there is none.

    ``ratio`` is one-minute load average per core. ``known`` is False where
    the host exposes no load average (Windows), and an unknown load defers
    local work rather than assuming the machine is idle: the existing
    platform boundary report already promises exactly that.
    """

    ratio: float = 0.0
    known: bool = False

    @property
    def busy_at(self) -> str:
        return f"{self.ratio:.2f}" if self.known else "unknown"


def probe_load(cpu_count: int | None = None) -> Load:
    """Read this host's load average per core. Never raises.

    ``os.getloadavg`` exists on macOS and Linux and not on Windows, and a
    missing reading is reported as missing.
    """
    try:
        one_minute = os.getloadavg()[0]
    except (OSError, AttributeError):
        return Load(known=False)
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 0)
    if cores <= 0:
        return Load(known=False)
    return Load(ratio=one_minute / cores, known=True)


@dataclass(frozen=True)
class Signal:
    """What the caller could observe about the work, and nothing it asserted.

    Assembled by the gate from the PreToolUse payload plus the operator's
    policy. ``client`` is injected by the hook's own ``--client`` flag, so an
    agent cannot claim to be the other one.
    """

    client: str
    repo: str
    task_type: str = "implementation"
    is_review: bool = False
    #: For a review, the route that produced the work under review. A review
    #: must not be routed back to its own author.
    author_route: str | None = None

    def __post_init__(self) -> None:
        if self.client not in PEER_FOR_CLIENT:
            raise PolicyError(f"unknown client {self.client!r}")
        if self.task_type not in TASK_TYPES:
            raise PolicyError(f"unknown task type {self.task_type!r}")
        if self.author_route is not None and self.author_route not in ROUTES:
            raise PolicyError(f"unknown author route {self.author_route!r}")


@dataclass(frozen=True)
class Decision:
    """One route, one closed reason code, and what was considered.

    ``considered`` is recorded in the receipt beside the route so the decision
    can be re-derived rather than taken on trust. It holds no task content:
    a classification name, route names, a load figure.
    """

    route: str
    code: str
    reason: str
    considered: Mapping[str, object]

    @property
    def dispatches(self) -> bool:
        return self.route != RETAIN


#: Every code this module can return. Held as a set so a test can assert the
#: vocabulary is closed and a report can enumerate it.
CODES = frozenset({
    "retained_repo_unclassified",
    "retained_classification_ineligible",
    "retained_no_eligible_route",
    "retained_no_fresh_capacity",
    "retained_local_load_high",
    "retained_local_load_unknown",
    "retained_is_the_policy",
    "retained_review_independence",
    "routed_local_mechanical",
    "routed_peer_implementation",
    "routed_peer_review_independence",
})


def decide(signal: Signal, policy: Policy, *, fresh_routes: frozenset[str],
           load: Load, cpu_idle_ratio: float | None = None) -> Decision:
    """Select one route for ``signal``. Pure, total, and fail-closed.

    ``fresh_routes`` is the set of routes the stage router currently holds a
    fresh, available capacity observation for. An empty set is normal on a
    machine nobody has reported capacity for, and it means peers are not
    dispatched to: a capacity observation expires closed by design, and this
    function does not second-guess that.

    ``cpu_idle_ratio`` is an optional, directly measured CPU idle fraction
    (from the same heartbeat sample the caller already has, not re-probed
    here) that can rescue an otherwise-high load reading -- see
    ``MIN_LOCAL_IDLE_RATIO``. Passing ``None`` means no such reading exists;
    it is recorded honestly rather than treated as any particular value.
    """
    repo_policy = policy.for_repo(signal.repo)
    peer = PEER_FOR_CLIENT[signal.client]
    idle_known = isinstance(cpu_idle_ratio, (int, float)) and not isinstance(cpu_idle_ratio, bool)
    considered: dict[str, object] = {
        "client": signal.client,
        "peer": peer,
        "task_type": signal.task_type,
        "classification": repo_policy.classification,
        "allowed_routes": list(repo_policy.allowed_routes),
        "mechanical_ok": repo_policy.mechanical_ok,
        "fresh_routes": sorted(fresh_routes),
        "load_per_core": load.busy_at,
        "cpu_idle_ratio": cpu_idle_ratio if idle_known else None,
        "is_review": signal.is_review,
        "author_route": signal.author_route,
    }

    def retain(code: str, reason: str) -> Decision:
        return Decision(RETAIN, code, reason, considered)

    # 1. Privacy and eligibility. First, and nothing below can undo it.
    if repo_policy.classification == "unclassified":
        return retain(
            "retained_repo_unclassified",
            f"{signal.repo} has no operator classification, so no route is "
            f"eligible to receive it and the work stays with {signal.client}")
    if repo_policy.classification == "client_derived" and not (
            "local" in repo_policy.allowed_routes and repo_policy.mechanical_ok
            and signal.task_type == "mechanical"
            and "client_derived" in policy.local_classifications):
        return retain(
            "retained_classification_ineligible",
            "client-derived material goes only to the local model, and only as "
            "mechanical work; it is never dispatched to a peer")
    if not repo_policy.allowed_routes:
        return retain(
            "retained_no_eligible_route",
            f"the policy for {signal.repo} permits no route other than the "
            f"assistant already holding the work")

    # 2. Task type. Mechanical text work is the only kind a local model takes.
    local_eligible = (
        "local" in repo_policy.allowed_routes
        and repo_policy.mechanical_ok
        and signal.task_type == "mechanical"
        and repo_policy.classification in policy.local_classifications
    )

    # 3. Hardware load, but only where it can change the answer.
    if local_eligible:
        if not load.known:
            return retain(
                "retained_local_load_unknown",
                "this host exposes no load average, so local capacity is "
                "unknown and local work defers rather than assuming the "
                "machine is idle")
        idle_rescues = idle_known and cpu_idle_ratio >= MIN_LOCAL_IDLE_RATIO
        if load.ratio >= policy.max_local_load_ratio and not idle_rescues:
            detail = (f"; measured idle {cpu_idle_ratio:.2f} is below the "
                      f"{MIN_LOCAL_IDLE_RATIO} rescue threshold" if idle_known else "")
            return retain(
                "retained_local_load_high",
                f"load per core is {load.busy_at}, at or above the "
                f"{policy.max_local_load_ratio} ceiling{detail}, so the local "
                f"model would compete with the user's own machine")
        if "local" not in fresh_routes:
            return retain(
                "retained_no_fresh_capacity",
                "no fresh capacity observation for the local route, and a "
                "stale observation never makes a route eligible")
        return Decision("local", "routed_local_mechanical",
                        f"mechanical {repo_policy.classification} text work in a "
                        f"repository the operator marked eligible for a local model",
                        considered)

    # Backstop: the local branch above returns for every client-derived unit
    # the privacy check let through, so this is unreachable today. It stays
    # so that no later edit can let client-derived work reach a peer.
    if repo_policy.classification == "client_derived":
        return retain(
            "retained_classification_ineligible",
            "client-derived material goes only to the local model; it is never "
            "dispatched to a peer")

    # 4. Capacity, for the peer route.
    peer_classifications = policy.route_classifications.get(peer, policy.peer_classifications)
    peer_allowed = (peer in repo_policy.allowed_routes
                    and repo_policy.classification in peer_classifications)
    if signal.is_review and signal.author_route == peer:
        # The gap this closes. The guard below only fired when the client was
        # itself the author, so a review of the *peer's* work fell through to
        # the ordinary branch and was routed straight back to the peer, which
        # is the author. Independence is not a preference: the asking client
        # is the independent reviewer here, so it keeps the work.
        return retain(
            "retained_review_independence",
            f"this is a review of {peer}'s own work, so it is not routed back "
            f"to {peer}; {signal.client} is the independent reviewer")
    if signal.is_review and signal.author_route == signal.client:
        # Independence is not a preference. If the only other route is not
        # available, the work is retained and says so, never reviewed by its
        # own author.
        if not peer_allowed:
            return retain(
                "retained_no_eligible_route",
                f"this is a review of {signal.client}'s own work and no other "
                f"route is eligible, so it is not routed and not self-reviewed")
        if peer not in fresh_routes:
            return retain(
                "retained_no_fresh_capacity",
                f"this is a review of {signal.client}'s own work and {peer} has "
                f"no fresh capacity observation")
        return Decision(peer, "routed_peer_review_independence",
                        f"a review of {signal.client}'s own work goes to {peer} so the "
                        f"author does not review itself", considered)

    if peer_allowed and peer in fresh_routes:
        if signal.client in repo_policy.allowed_routes and \
                _prefers(policy, signal.client, peer):
            return retain(
                "retained_is_the_policy",
                f"both {signal.client} and {peer} are eligible and the policy "
                f"prefers {signal.client} for this repository")
        return Decision(peer, "routed_peer_implementation",
                        f"{signal.task_type} work in a {repo_policy.classification} "
                        f"repository, {peer} is allowed and has fresh capacity",
                        considered)

    if peer_allowed:
        return retain(
            "retained_no_fresh_capacity",
            f"{peer} is allowed for {signal.repo} but has no fresh capacity "
            f"observation, and a stale one never makes a route eligible")

    return retain(
        "retained_is_the_policy",
        f"the policy for {signal.repo} does not permit {peer} for "
        f"{repo_policy.classification} work, so {signal.client} keeps it")


def _prefers(policy: Policy, first: str, second: str) -> bool:
    """Whether ``first`` outranks ``second`` in the operator's tie-break order.

    A route absent from ``prefer`` ranks last rather than raising: the order
    is a preference, and a missing entry must not make a decision impossible.
    With no order at all, neither outranks the other, so the caller routes to
    the peer.
    """
    order = {route: index for index, route in enumerate(policy.prefer)}
    return order.get(first, len(order)) < order.get(second, len(order))


# ---------------------------------------------------------------------------
# Loading the operator's policy
# ---------------------------------------------------------------------------

POLICY_FILE = "routing-policy.json"
POLICY_VERSION = 1


def policy_path(state_root: str) -> str:
    return os.path.join(str(state_root), "routing", POLICY_FILE)


#: Recorded when no policy file exists, which is a real state and not an error.
NO_POLICY = "absent"


def policy_fingerprint(state_root: str) -> str:
    """A stable digest of the policy a decision was made under. Never raises.

    Recorded in every automatic receipt so that editing the policy takes
    effect on the next gated call instead of whenever the receipt happens to
    expire. Without it, classifying a repository left it retained for up to
    the receipt's four-hour TTL, which makes the operator's own document feel
    like it did nothing.

    An unreadable file returns a value that cannot match any recorded one, so
    the receipt is re-decided and the decision path then refuses through
    ``load_policy``. Failing closed by the longer route, rather than guessing
    here.
    """
    import hashlib

    path = policy_path(state_root)
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:32]
    except FileNotFoundError:
        return NO_POLICY
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


def parse_policy(document: object) -> Policy:
    """Build a Policy from the operator's document, refusing anything odd.

    Fail closed on shape: a policy file that cannot be understood must not
    degrade into a permissive default, because a permissive default here is
    automatic dispatch of material nobody classified.
    """
    if not isinstance(document, dict):
        raise PolicyError("routing policy must be a JSON object")
    if document.get("version") != POLICY_VERSION:
        raise PolicyError(f"routing policy version must be {POLICY_VERSION}")
    raw_repos = document.get("repos", {})
    if not isinstance(raw_repos, dict):
        raise PolicyError("routing policy repos must be an object")
    repos: dict[str, RepoPolicy] = {}
    for repo, entry in raw_repos.items():
        if not isinstance(repo, str) or not os.path.isabs(repo):
            raise PolicyError(f"routing policy repo key {repo!r} must be an absolute path")
        if not isinstance(entry, dict):
            raise PolicyError(f"routing policy entry for {repo!r} must be an object")
        unknown = set(entry) - {"classification", "allowed_routes", "mechanical_ok",
                                "mechanical_globs"}
        if unknown:
            raise PolicyError(f"routing policy entry for {repo!r} has unknown keys: "
                              + ", ".join(sorted(unknown)))
        routes = entry.get("allowed_routes", [])
        if not isinstance(routes, list) or any(not isinstance(r, str) for r in routes):
            raise PolicyError(f"allowed_routes for {repo!r} must be a list of strings")
        mechanical = entry.get("mechanical_ok", False)
        if not isinstance(mechanical, bool):
            raise PolicyError(f"mechanical_ok for {repo!r} must be true or false")
        globs = entry.get("mechanical_globs", [])
        if not isinstance(globs, list) or any(not isinstance(g, str) or not g for g in globs):
            raise PolicyError(f"mechanical_globs for {repo!r} must be a list of non-empty strings")
        repos[os.path.realpath(repo)] = RepoPolicy(
            classification=entry.get("classification", "unclassified"),
            allowed_routes=tuple(dict.fromkeys(routes)),
            mechanical_ok=mechanical,
            mechanical_globs=tuple(dict.fromkeys(globs)))
    ceiling = document.get("max_local_load_ratio", DEFAULT_MAX_LOCAL_LOAD)
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0 < ceiling <= 64:
        raise PolicyError("max_local_load_ratio must be a number above 0 and at most 64")
    # Absent means no preference, which routes to an eligible peer. See
    # Policy.prefer for why this is not ``list(ROUTES)``.
    prefer = document.get("prefer", [])
    if not isinstance(prefer, list) or any(route not in ROUTES for route in prefer):
        raise PolicyError("prefer must be a list of known routes")
    declared = document.get("declared_available", [])
    if not isinstance(declared, list) or any(route not in ROUTES for route in declared):
        raise PolicyError("declared_available must be a list of known routes")
    raw_route_classifications = document.get("route_classifications", {})
    if not isinstance(raw_route_classifications, dict):
        raise PolicyError("route_classifications must be an object")
    route_classifications: dict[str, frozenset[str]] = {}
    for route, classes in raw_route_classifications.items():
        if route not in PEER_FOR_CLIENT:
            raise PolicyError(f"route_classifications key {route!r} must be claude or codex")
        if (not isinstance(classes, list) or not classes
                or any(not isinstance(c, str) or c not in PEER_CLASSIFICATIONS for c in classes)):
            raise PolicyError(
                f"route_classifications[{route!r}] must be a non-empty list drawn from "
                + ", ".join(sorted(PEER_CLASSIFICATIONS)))
        route_classifications[route] = frozenset(classes)
    # Fail closed on shape here too. A misspelled ``declaredAvailable`` that
    # parsed silently would leave the operator believing they had declared a
    # route available when they had not. An underscore-prefixed key is a
    # comment: the scaffold the installer writes uses ``_comment`` and
    # ``_example`` to show the shape, and JSON has nowhere else to put them.
    unknown_top = {key for key in document
                   if not key.startswith("_")} - {"version", "repos",
                                                  "max_local_load_ratio", "prefer",
                                                  "declared_available", "local_first",
                                                  "route_classifications",
                                                  "reserved_models"}
    if unknown_top:
        raise PolicyError("routing policy has unknown keys: "
                          + ", ".join(sorted(unknown_top)))
    reserved = document.get("reserved_models", [])
    if (not isinstance(reserved, list)
            or any(not isinstance(m, str) or not m.strip() for m in reserved)):
        raise PolicyError("reserved_models must be a list of non-empty strings")
    local_first = _parse_local_first(document.get("local_first", {}))
    return Policy(repos=repos, local_classifications=LOCAL_CLASSIFICATIONS,
                  peer_classifications=PEER_CLASSIFICATIONS,
                  route_classifications=route_classifications,
                  max_local_load_ratio=float(ceiling), prefer=tuple(prefer),
                  declared_routes=tuple(dict.fromkeys(declared)),
                  local_first=local_first,
                  reserved_models=tuple(dict.fromkeys(
                      m.strip().casefold() for m in reserved)))


def reserved_model_match(model: object, reserved: tuple[str, ...]) -> str | None:
    """The reserved token ``model`` contains, or None if it is not reserved.

    Returns the token rather than a bool so the refusal can name which
    reservation was hit, which is the difference between an operator seeing
    "astra is reserved" and seeing "refused".

    A non-string or empty model is not reserved here. That is not leniency:
    those are shape errors the dispatch validator already refuses on its own,
    and duplicating the check would give two different messages for one fault.
    Callers must keep running their own shape validation, not lean on this.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    folded = model.casefold()
    for token in reserved:
        if token in folded:
            return token
    return None


def model_reserved_for(state_root: str) -> Callable[[str], str | None]:
    """The operator's reservation, read fresh from their policy on each call.

    Handed to the execution queue so that the layer which actually creates a
    job can refuse a reserved model without importing this module's policy
    handling, and so that both layers match names through the one
    implementation in :func:`reserved_model_match` rather than two copies.

    Reads the file on every call on purpose. Submits are rare, and a list
    captured when a worker started would leave that worker enforcing whatever
    the policy said at boot, which for a long-running process means an
    operator's edit does nothing until they notice and restart it.
    ``load_policy`` raises on an unreadable or malformed file; the queue turns
    that into a refusal, which is the only direction a reservation can fail.
    """
    def matcher(model: str) -> str | None:
        return reserved_model_match(model, load_policy(state_root).reserved_models)

    return matcher


def load_policy(state_root: str) -> Policy:
    """The operator's policy, or the retain-everything default when absent.

    An absent file is not an error: it is a machine where nobody has
    classified anything yet, and the correct behaviour there is to retain
    every repository, which the default Policy does. An unreadable or
    malformed file *is* an error, and it propagates: the gate turns it into a
    deny rather than proceeding under a policy it could not read.
    """
    return load_policy_and_fingerprint(state_root)[0]


def load_policy_and_fingerprint(state_root: str) -> tuple[Policy, str]:
    """The policy and the digest of the exact bytes it was parsed from.

    One read, because two were a real defect. ``ensure_decision`` used to call
    ``load_policy`` and then ``policy_fingerprint``, each opening the file
    separately, so an operator saving the file between the two produced a
    receipt stamped with the digest of one policy and decided under another.
    The receipt then looked current for four hours while describing a decision
    the policy on disk would not have made. A single read cannot disagree with
    itself.
    """
    import hashlib

    path = policy_path(state_root)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return Policy(), NO_POLICY
    digest = hashlib.sha256(raw).hexdigest()[:32]
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"routing policy is not readable JSON: {type(exc).__name__}") from None
    return parse_policy(document), digest
