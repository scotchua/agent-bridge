"""Create the routing decision automatically, before any implementation.

``autoroute`` decides. This module is the part with side effects: it reads the
operator's policy and the stage router, asks ``autoroute`` for a route,
establishes the stage ownership that route implies, and writes the durable
receipt the gate then enforces against.

Why it exists. The delegation gate already refused an edit without a routing
receipt, which is real enforcement, but the receipt could only be created by
an agent choosing to make three MCP calls (``stage_register``,
``stage_claim``, ``routing_decide``) and naming its own ``allowed_routes``. So
the *decision* was optional and agent-steered even though the *edit* was not.
A user still had to say "send this to Claude", and an assistant that wanted
to keep the work asked for its own route and got it.

Called from the PreToolUse hook, this closes that gap: by the time the first
edit in a repository is judged, a decision exists, it was computed from the
operator's policy rather than requested, and it left a receipt naming the
route and the reason. Nothing the agent says reaches it. The hook's own
``--client`` flag is the only provenance, and it is not agent-supplied.

Two honest limits, stated here because they belong next to the mechanism:

* **Capacity for this client is first-hand; capacity for a peer is a
  declaration.** This module records one short-lived capacity observation for
  the asking client's own route, because a client that just made a tool call
  is demonstrably running. It never observes the peer or the local model:
  there is no collector for those, and inventing one would be a guess. What
  it does instead is replay the operator's ``declared_available`` list from
  ``routing-policy.json`` into the same ledger, as a *declaration* rather
  than a measurement, with the same short freshness so that removing a route
  from the file stops routing to it on the next call. The receipt records
  which routes were eligible, the ledger records that each row came from the
  policy file, and neither claims the peer was health-checked.

  What is gone is the third way: an MCP tool called ``capacity_observe``,
  which let the assistant name the route, the availability, the source and a
  freshness window of any length. An adversarial review pointed out that
  "a fresh observation from an authorized source" then meant whatever the
  model typed, including availability for a route it knew nothing about,
  lasting years. The tool is removed, the ledger has a ``trusted`` column
  that only these two writers set, and rows an older version accepted are
  untrusted after the migration and no longer route anything.
* **A brief is not something a tool call contains.** When the decision routes
  work to a peer or a local model, this module writes a durable dispatch
  intent naming the route, the repository and the stage binding, and the gate
  denies the edit. The assistant then makes exactly one call
  (``execution_dispatch`` or ``work_route_local``) carrying the brief, with
  the identifiers the intent already holds. The user is never the messenger
  and the assistant cannot edit instead, but the brief's words are the
  assistant's, because a PreToolUse payload does not contain them.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass
from typing import Any

from .. import store
from ..capacity_router import (CapacityObservation, PRESENCE_SOURCE, RoutingError,
                               StageRouter, capacity_fingerprint as _fingerprint)
from . import autoroute, gate

#: How long an automatically claimed stage is leased for. Long enough that an
#: ordinary editing session does not re-decide on every call, short enough
#: that an abandoned session's claim expires without an operator.
DEFAULT_LEASE_SECONDS = 4 * 3600
#: How long the receipt the gate reads stays valid. Bounded by the lease in
#: ``gate.record_decision``, so this is a ceiling rather than a promise.
DEFAULT_TTL_SECONDS = 4 * 3600
#: Freshness of the "this client is running" observation. Deliberately short:
#: it is evidence about right now, not a standing claim.
CLIENT_PRESENCE_SECONDS = 900
#: The source string recorded for that observation, so an audit can tell it
#: apart from an operator-declared one. Defined in ``capacity_router`` because
#: the fingerprint has to recognise the row without importing this module.
CLIENT_PRESENCE_SOURCE = PRESENCE_SOURCE
#: The source string recorded for a route the operator declared available.
#: A different word from "observed" on purpose: nothing health-checked it.
DECLARED_SOURCE = "policy:operator-declared"
#: Freshness of a declaration. As short as the presence observation, because
#: it is re-read from the operator's file on every decision: keeping it short
#: is what makes deleting a route from the file take effect at once instead
#: of hours later.
DECLARED_SECONDS = 900

INTENT_DIR = "intents"
INTENT_VERSION = 1


class AutoDecisionError(RuntimeError):
    """The decision could not be created. The gate turns this into a deny."""


@dataclass(frozen=True)
class Outcome:
    """What happened, in terms the gate can act on directly."""

    decision: autoroute.Decision
    #: The receipt written, or None when no decision could be recorded.
    receipt: dict[str, Any] | None
    #: The dispatch intent written, for a route that is not the client's own.
    intent: dict[str, Any] | None = None

    @property
    def retained(self) -> bool:
        return self.decision.route == autoroute.RETAIN


def item_id_for(repo: str) -> str:
    """A stable identity for automatic work in one repository.

    Derived from the repository path so the same repository re-decides the
    same stage rather than accumulating one per tool call.
    """
    digest = hashlib.sha256(os.path.realpath(repo).encode("utf-8")).hexdigest()
    return f"auto-{digest[:16]}"


def owner_id_for(client: str) -> str:
    """Stable across hook invocations, because each tool call is a new process.

    A per-process owner would mean every tool call claimed a new stage and the
    lease would never be renewed, only abandoned.
    """
    return f"gate-auto:{client}"


def fresh_routes(router: StageRouter) -> frozenset[str]:
    """Routes the router holds a fresh, available observation for.

    Read through ``report()`` rather than the table, so the definition of
    fresh stays the router's and is not restated here. ``report`` already
    grades a stale observation as stale, which is never eligible.
    """
    capacity = router.report().get("capacity", {})
    return frozenset(route for route, value in capacity.items()
                     if value.get("status") == "available")


def _observe_client_presence(router: StageRouter, client: str) -> None:
    """Record that this client is running. First-hand, short-lived, itself only.

    The hook is executing because the client made a tool call, so this is an
    observation rather than an assumption. It is recorded for the client's own
    route and never for the peer or the local model. Trusted, because the
    writer is this code path reacting to a host event, not a model asserting
    something in a tool call.
    """
    now = float(router.clock())
    router.observe_capacity(CapacityObservation(
        route=client, observed_at=now, fresh_until=now + CLIENT_PRESENCE_SECONDS,
        available=True, source=CLIENT_PRESENCE_SOURCE), trusted=True)


def _observe_declared_routes(router: StageRouter, policy: autoroute.Policy,
                             *, client: str) -> None:
    """Replay the operator's standing declaration into the capacity ledger.

    Trusted, because the operator's policy file is the one input in this
    system a model cannot write: the gate refuses the editing tools on the
    gate's own state, which includes it.

    The client's own route is skipped, because presence already covers it
    first-hand and a declaration must not be able to keep a route eligible
    that the presence observation would not.

    Withdrawal is replayed too, and it has to be. Writing the declaration
    with a short freshness was not enough on its own: a route the operator
    deleted from the file kept the row the last replay wrote and stayed
    eligible until it aged out, so removing a route took up to fifteen
    minutes. A test caught exactly that. The withdrawal names
    ``DECLARED_SOURCE``, so it removes what the declaration put there and
    cannot remove a peer's own first-hand presence.
    """
    now = float(router.clock())
    for route in autoroute.ROUTES:
        if route == client or route in policy.declared_routes:
            continue
        router.retract_capacity(route, source=DECLARED_SOURCE)
    for route in policy.declared_routes:
        if route == client:
            continue
        router.observe_capacity(CapacityObservation(
            route=route, observed_at=now, fresh_until=now + DECLARED_SECONDS,
            available=True, source=DECLARED_SOURCE), trusted=True)


def capacity_fingerprint(router: StageRouter, *, client: str) -> str:
    """The digest of eligible capacity this decision depends on.

    Thin on purpose: the arithmetic lives in ``capacity_router`` so that the
    gate hook, which reads the same table read-only, computes the identical
    value from the identical code.
    """
    return _fingerprint(router.capacity_rows(), float(router.clock()),
                        exclude_client=client)


def _own_stage(router: StageRouter, *, item_id: str, stage: str, route: str,
               owner_id: str, lease_seconds: float) -> dict[str, Any]:
    """Make ``route`` the owner of this stage, or say why it cannot be.

    Idempotent across hook invocations: an existing stage this owner already
    holds is renewed rather than re-claimed, and a stage somebody else holds
    is reported as theirs rather than taken.
    """
    try:
        router.register(item_id, stage, allowed_routes=(route,), preferred_routes=(route,))
    except RoutingError as exc:
        if str(exc) != "stage_exists":
            raise AutoDecisionError(f"stage_register_failed:{exc}") from None
    try:
        current = router.get(item_id, stage)
    except RoutingError as exc:
        raise AutoDecisionError(f"stage_unreadable:{exc}") from None

    if current["state"] == "owned":
        if current["owner_id"] == owner_id and current["owner_route"] == route:
            try:
                return router.renew(item_id, stage, owner_id=owner_id,
                                    lease_seconds=lease_seconds,
                                    expected_revision=current["revision"])
            except RoutingError as exc:
                if str(exc) == "revision_conflict":
                    return router.get(item_id, stage)
                raise AutoDecisionError(f"stage_renew_failed:{exc}") from None
        # Somebody else owns it. Not taken, not renewed: reported.
        return current
    if current["state"] in ("complete", "blocked"):
        # Reached only when the caller handed a stage name that is already
        # terminal. ``stage_name`` picks an unused generation before this
        # runs, so this is the concurrent case: another process completed the
        # stage between that choice and this claim. The next call picks the
        # next generation.
        raise AutoDecisionError(f"stage_terminal:{current['state']}")
    try:
        return router.assign(item_id, stage, owner_id=owner_id,
                             lease_seconds=lease_seconds,
                             expected_revision=current["revision"])
    except RoutingError as exc:
        raise AutoDecisionError(f"stage_claim_failed:{exc}") from None


#: How many completed stages one repository may accumulate before the gate
#: refuses rather than searching forever.
MAX_STAGE_GENERATIONS = 1000


def stage_name(router: StageRouter, item_id: str, task_type: str, *,
               route: str | None = None, owner_id: str | None = None) -> str:
    """The stage this decision should use: the first generation it can own.

    A stage is a unit of work, and work completes. Reusing one name per
    repository meant that the first ``stage_complete`` left the repository
    permanently un-editable, because every later decision tried to claim a
    stage the router had already closed. Generations keep the common case
    (``implementation``) stable while giving the next piece of work its own
    stage, so a completed decision is history rather than a wall.

    ``route`` matters for the same reason, and skipping it was a worse bug.
    The router never reassigns an owned stage, by design. So when a decision
    changed route (the operator classified the repository, and work that had
    been retained now belongs to the peer) the old stage was still owned on
    the old route, ``_own_stage`` returned that record, and the receipt was
    written naming the *old* route while the decision said the new one. The
    gate then allowed the edit. A decision to delegate had silently become a
    decision to retain, which is the one failure this whole mechanism exists
    to prevent. A stage owned on a route this decision did not choose is
    therefore skipped, and one this decider owns itself is completed on the
    way past so it does not linger holding a lease.
    """
    for generation in range(1, MAX_STAGE_GENERATIONS + 1):
        candidate = task_type if generation == 1 else f"{task_type}#{generation}"
        try:
            current = router.get(item_id, candidate)
        except RoutingError:
            return candidate          # never registered: free
        if current.get("state") in ("complete", "blocked"):
            continue                  # terminal: history, try the next one
        if (current.get("state") == "owned" and owner_id is not None
                and current.get("owner_id") != owner_id):
            # Somebody else's stage, whatever route it is on. Adopting it
            # would write a receipt naming an owner this decider cannot
            # renew, so the next generation is used instead.
            continue
        if (route is not None and current.get("state") == "owned"
                and current.get("owner_route") != route):
            if owner_id is not None and current.get("owner_id") == owner_id:
                # Our own earlier decision, superseded. We own it, so we may
                # close it; leaving it owned would hold a lease for hours
                # against a route the policy no longer chooses.
                try:
                    router.complete(item_id, candidate, owner_id=owner_id,
                                    expected_revision=current["revision"])
                except RoutingError:
                    pass              # a race; the next generation is still free
            continue
        return candidate              # free, ours, or owned on this very route
    raise AutoDecisionError(f"stage_generations_exhausted:{MAX_STAGE_GENERATIONS}")


def intent_path(state_root: str, repo: str) -> str:
    return os.path.join(gate.receipt_dir(state_root), INTENT_DIR,
                        gate.receipt_name(repo))


def read_intent(state_root: str, repo: str) -> dict[str, Any] | None:
    path = intent_path(state_root, repo)
    if not os.path.exists(path):
        return None
    loaded = store.read_json(path)
    return loaded if isinstance(loaded, dict) else None


def _write_intent(state_root: str, *, receipt: dict[str, Any],
                  decision: autoroute.Decision, clock: Any) -> dict[str, Any]:
    """The durable, visible record that work is owed to another route.

    Written before the gate denies the edit, so the denial is never the only
    trace: an operator reading the audit sees what was routed away, where to,
    and what it is still waiting for.
    """
    intent = {
        "version": INTENT_VERSION,
        "repo": receipt["repo"],
        "route": decision.route,
        "item_id": receipt["item_id"],
        "stage": receipt["stage"],
        "owner_id": receipt["owner_id"],
        "stage_revision": receipt["stage_revision"],
        "code": decision.code,
        "reason": decision.reason,
        "considered": dict(decision.considered),
        "created_at": float(clock()),
        # The one thing the hook cannot supply, named rather than implied.
        "state": "awaiting_brief",
        "next_call": ("execution_dispatch" if decision.route in ("claude", "codex")
                      else "work_route_local"),
    }
    store.append_ledger(os.path.join(gate.receipt_dir(state_root), gate.AUDIT_LEDGER),
                        {"event": "dispatch_intent", **intent})
    store.atomic_write_json(intent_path(state_root, receipt["repo"]), intent)
    return intent


#: The intent fields a dispatch must match to retire it. Every one of them,
#: because any subset lets one job answer for another.
INTENT_BINDING = ("route", "item_id", "stage", "owner_id", "stage_revision")


def clear_intent(state_root: str, repo: str, *, clock: Any = time.time,
                 binding: dict[str, Any] | None = None) -> bool:
    """Retire the intent once *the work it names* has actually been dispatched.

    Called by the orchestration MCP when ``execution_dispatch`` accepts a job.
    Returns whether there was a matching one to retire.

    ``binding`` is the route, item, stage, owner and revision the accepted job
    was bound to, and every field must equal the intent's. Keyed on the
    repository alone, this retired whatever intent the repository had: an
    assistant that owned some other stage in the same repository could
    dispatch that, and the intent for the stage it was actually refused would
    be recorded as met. The audit's "routed but never dispatched" column is
    the thing that catches a route nobody honoured, so a cleanup that clears
    more than it dispatched is the one bug that column cannot survive.

    Omitting ``binding`` retires whatever is there, and is for a caller that
    is not answering an intent at all: ``ensure_decision`` retiring one it has
    just superseded. It is not reachable from a tool.
    """
    path = intent_path(state_root, repo)
    if not os.path.exists(path):
        return False
    existing = read_intent(state_root, repo) or {}
    if binding is not None:
        mismatched = [field for field in INTENT_BINDING
                      if existing.get(field) != binding.get(field)]
        if mismatched:
            store.append_ledger(
                os.path.join(gate.receipt_dir(state_root), gate.AUDIT_LEDGER),
                {"event": "dispatch_intent_unmatched", "repo": os.path.realpath(repo),
                 "route": existing.get("route"), "mismatched": mismatched,
                 "at": float(clock())})
            return False
    store.append_ledger(os.path.join(gate.receipt_dir(state_root), gate.AUDIT_LEDGER),
                        {"event": "dispatch_intent_met", "repo": os.path.realpath(repo),
                         "route": existing.get("route"), "at": float(clock())})
    os.unlink(path)
    return True


def retire_superseded_intent(state_root: str, repo: str, *, reason: str,
                             clock: Any = time.time) -> bool:
    """Drop an intent the current decision no longer owes.

    A decision that retains the work leaves nothing owed to another route, so
    an intent written by an earlier decision is not merely stale, it is
    wrong: the audit would keep reporting work as routed away and never
    dispatched when the policy has since decided to keep it. Recorded with
    its reason rather than deleted quietly.
    """
    path = intent_path(state_root, repo)
    if not os.path.exists(path):
        return False
    existing = read_intent(state_root, repo) or {}
    store.append_ledger(os.path.join(gate.receipt_dir(state_root), gate.AUDIT_LEDGER),
                        {"event": "dispatch_intent_superseded",
                         "repo": os.path.realpath(repo), "route": existing.get("route"),
                         "stage": existing.get("stage"), "reason": reason,
                         "at": float(clock())})
    os.unlink(path)
    return True


def ensure_decision(*, client: str, repo: str, state_root: str, capacity_db: str,
                    task_type: str = "implementation", is_review: bool = False,
                    author_route: str | None = None,
                    lease_seconds: float = DEFAULT_LEASE_SECONDS,
                    ttl_seconds: int = DEFAULT_TTL_SECONDS,
                    clock: Any = time.time,
                    load: autoroute.Load | None = None) -> Outcome:
    """Compute, establish and record the routing decision for one repository.

    Raises :class:`AutoDecisionError` for anything it cannot do, so the gate
    fails closed. It never returns a receipt it did not write and never
    reports a route the router does not actually show as owned.
    """
    if client not in autoroute.PEER_FOR_CLIENT:
        raise AutoDecisionError("client_invalid")
    repo_root = gate.repo_key(repo)
    if repo_root is None:
        raise AutoDecisionError("repo_not_a_repository")
    # A guard, not a value: every time below comes from the same clock, and a
    # non-finite one would produce a receipt whose validity window means
    # nothing. Refuse before anything is written rather than after.
    if not math.isfinite(float(clock())):
        raise AutoDecisionError("clock_invalid")

    try:
        # One read for both the rules and the digest stamped in the receipt.
        # Two reads let an operator's save land between them, producing a
        # receipt whose fingerprint described a policy the decision had not
        # used, and which then looked current for hours.
        policy, policy_digest = autoroute.load_policy_and_fingerprint(state_root)
    except (OSError, ValueError, autoroute.PolicyError) as exc:
        # An unreadable policy is never a permissive one.
        raise AutoDecisionError(f"policy_unreadable:{type(exc).__name__}") from None

    try:
        router = StageRouter(capacity_db, clock=clock)
    except Exception as exc:  # noqa: BLE001  sqlite and OS errors alike
        raise AutoDecisionError(f"router_unavailable:{type(exc).__name__}") from None

    signal = autoroute.Signal(client=client, repo=repo_root, task_type=task_type,
                              is_review=is_review, author_route=author_route)
    reading = load if load is not None else autoroute.probe_load()

    # Capacity is written before the decision reads it, because a retained
    # decision needs its own route to be a fresh eligible one for the router
    # to assign. Two writers and no others: this client's own presence, and
    # the operator's standing declaration. See the module docstring.
    try:
        _observe_client_presence(router, client)
        _observe_declared_routes(router, policy, client=client)
    except Exception as exc:  # noqa: BLE001  RoutingError, sqlite and OS alike
        raise AutoDecisionError(f"capacity_record_failed:{type(exc).__name__}") from None

    decision = autoroute.decide(signal, policy, fresh_routes=fresh_routes(router),
                                load=reading)
    # Read after the decision, from the same ledger the decision read, so the
    # receipt records the capacity it was actually made under.
    capacity_digest = capacity_fingerprint(router, client=client)
    route = client if decision.route == autoroute.RETAIN else decision.route
    item_id = item_id_for(repo_root)
    owner = owner_id_for(client)
    stage = stage_name(router, item_id, task_type, route=route, owner_id=owner)

    record = _own_stage(router, item_id=item_id, stage=stage, route=route,
                        owner_id=owner, lease_seconds=lease_seconds)
    if record.get("state") != "owned":
        raise AutoDecisionError(f"stage_not_owned_after_claim:{record.get('state')}")
    if record.get("owner_id") != owner:
        # A stage owned on the right route by somebody else is not this
        # decision's to speak for: a receipt naming a foreign owner points at
        # a lease this decider cannot renew, and ``stage_name`` skips such a
        # generation, so reaching here means one was claimed in between.
        raise AutoDecisionError(
            f"stage_owned_by_another_owner:{record.get('owner_id')}")
    if record.get("owner_route") != route:
        # The invariant, checked rather than assumed: a receipt must never
        # name a route the decision did not choose. ``stage_name`` picks a
        # generation this route can own, so reaching here means another
        # process took the stage in between. Fail closed; the gate denies and
        # the next call decides again.
        raise AutoDecisionError(
            f"stage_owned_by_another_route:{record.get('owner_route')}")
    reason = decision.reason[:gate.MAX_REASON]
    try:
        receipt = gate.record_decision(
            state_root, caller=client, stage_record=record, repo=repo_root,
            reason=reason, ttl_seconds=int(ttl_seconds), clock=clock,
            code=decision.code, considered=dict(decision.considered),
            automatic=True, policy_fingerprint=policy_digest,
            capacity_fingerprint=capacity_digest)
    except (RoutingError, OSError, ValueError) as exc:
        raise AutoDecisionError(f"receipt_write_failed:{type(exc).__name__}") from None

    intent = None
    if decision.dispatches and receipt["owner_route"] != client:
        try:
            intent = _write_intent(state_root, receipt=receipt, decision=decision,
                                   clock=clock)
        except OSError as exc:
            raise AutoDecisionError(f"intent_write_failed:{type(exc).__name__}") from None
    else:
        # Nothing is owed to another route now. An intent an earlier decision
        # wrote would otherwise sit in the audit's "routed but never
        # dispatched" column forever, describing a routing the policy has
        # since reversed.
        try:
            retire_superseded_intent(
                state_root, repo_root, clock=clock,
                reason=f"decision is now {decision.code}")
        except OSError:
            # The decision and its receipt stand; a tidy-up that failed is
            # reported by the audit, not a reason to deny the call.
            pass
    return Outcome(decision=decision, receipt=receipt, intent=intent)
