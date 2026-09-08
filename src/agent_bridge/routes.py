"""Who may consult whom, with what data, for what.

This replaces `PEER_OF = {"codex": "claude", "claude": "codex"}`, a two-element
"the other one" map that could not express a third participant at all.

Design, from two independent reviews that converged (Claude, and Codex job
20260908T224009Z-750f1f) on the same three conclusions:

  1. A local model is a TASK-RESTRICTED peer. Not symmetric, because the
     bridge validates the shape of a reply and not its content: a model that
     drops a line returns perfectly valid JSON containing a silent omission.
     The firm measured exactly that (a 7B returning 59 or 60 lines from a
     61-line input, on every run, while looking perfect on an 11-line
     fixture). Not subordinate either, because hiding it behind the cloud
     peers is the status quo that created three separate paths.

  2. Peers do NOT get equal data access. Local models run on the operator's
     own hardware and make no network call, so they may be permitted MORE
     than a third-party API, not less. Cloud peers are capped below
     client-derived today and that does not change here.

  3. Routes are DIRECTED. Claude may be allowed to consult a peer that Codex
     may not, and the reverse. A generalised "the other peer" cannot say that.

What this deliberately is not: a plugin framework. No discovery, no dynamic
loading, no lifecycle hooks. Adding a peer is one entry here plus one backend
module, both reviewed like any other change. One maintainer and no new
services favour auditable code over runtime extensibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Ordered least to most sensitive. A destination's ceiling admits everything
# at or below it. "client-derived" is deliberately last and, today, reachable
# by nothing: see CLIENT_DERIVED_GATE below.
CLASSIFICATIONS = ("public", "synthetic", "internal", "client-derived")


def rank(classification: str) -> int:
    try:
        return CLASSIFICATIONS.index(classification)
    except ValueError as exc:
        raise KeyError(f"unknown classification {classification!r}") from exc


#: No destination may receive client-derived material until counsel answers the
#: IRC 7216 questions in the work-product lane plan. Flipping a peer's ceiling
#: to "client-derived" is not enough on its own; this gate must be lifted too,
#: deliberately, in a reviewed change. Two locks on the same door, because the
#: cost of being wrong here is a federal criminal exposure rather than a bug.
CLIENT_DERIVED_GATE = False


@dataclass(frozen=True)
class Peer:
    """One participant, and what the bridge will let it receive."""

    name: str
    #: "cloud" (third-party API) or "local" (operator's own hardware, no
    #: network call). This drives the honest disclosure a caller sees, not
    #: just bookkeeping.
    locality: str
    #: Highest classification this destination may be sent. See rank().
    max_classification: str
    #: None means general consultation. A tuple restricts the peer to those
    #: task names, which is how a model with a measured competence envelope
    #: participates without being asked open-ended questions it cannot be
    #: trusted on.
    tasks: tuple[str, ...] | None = None
    #: When True, a route to this peer requires a current route certificate
    #: for the resolved model at dispatch time, not at config load. A
    #: certificate checked once at startup goes stale while requests continue.
    requires_certificate: bool = False
    #: Free text shown to the caller. Must state what is actually true about
    #: this destination, including what is NOT enforced.
    disclosure: str = ""


PEERS: dict[str, Peer] = {
    "claude": Peer(
        name="claude",
        locality="cloud",
        max_classification="internal",
        disclosure=(
            "Runs with customizations, MCP servers and built-in tools disabled, "
            "in an empty working directory, under a hard spend ceiling. Sent only "
            "your question. Third-party API: never send client-derived material."
        ),
    ),
    "codex": Peer(
        name="codex",
        locality="cloud",
        max_classification="internal",
        disclosure=(
            "Runs with no user config and no rules files, in an empty working "
            "directory, sandboxed against writes. The sandbox restricts writes, "
            "not reads, so treat the prompt itself as the confidentiality "
            "boundary. Third-party API: never send client-derived material."
        ),
    ),
    "local": Peer(
        name="local",
        locality="local",
        # Same ceiling as the cloud peers TODAY. Locality is what would justify
        # raising it, and counsel has not answered that yet. Recorded here so
        # the reason is visible at the point of change.
        max_classification="internal",
        tasks=("summarize", "triage", "classify", "extract", "redact"),
        requires_certificate=True,
        disclosure=(
            "Runs on this machine via Ollama and makes no network call, so nothing "
            "leaves the hardware. It is a small model restricted to five certified "
            "tasks. It can silently omit content while returning well-formed "
            "output: treat every reply as a draft to be checked, never as a "
            "complete account of the input."
        ),
    ),
}

#: Directed (source, destination) pairs. Absence is denial. A peer never
#: consults itself; that is asserted in the tests rather than filtered here, so
#: adding a self-route fails loudly instead of being silently dropped.
ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("claude", "codex"),
    ("codex", "claude"),
    ("claude", "local"),
    ("codex", "local"),
})


class RouteDenied(Exception):
    """A route, classification, or task the registry does not permit."""


def peer(name: str) -> Peer:
    try:
        return PEERS[name]
    except KeyError as exc:
        raise RouteDenied(f"unknown peer {name!r}") from exc


def destinations_for(caller: str) -> tuple[str, ...]:
    """Every peer `caller` may consult, in registry order.

    The MCP server builds one tool set per destination from this, which is how
    a third peer becomes visible to callers without a new parameter: the tool
    name already carries the destination.
    """
    if caller not in PEERS:
        raise RouteDenied(f"unknown caller {caller!r}")
    return tuple(n for n in PEERS if (caller, n) in ROUTES)


def authorize(caller: str, destination: str, classification: str,
              task: str | None = None) -> Peer:
    """Return the destination Peer, or raise RouteDenied.

    Every check that can deny is here, in one place, so no call path can reach
    a backend having skipped one. Certificate verification is NOT here: it
    happens at dispatch against the resolved model, because that is the only
    point where the digest is known.
    """
    if caller not in PEERS:
        raise RouteDenied(f"unknown caller {caller!r}")
    dest = peer(destination)
    if caller == destination:
        raise RouteDenied("a peer cannot consult itself")
    if (caller, destination) not in ROUTES:
        raise RouteDenied(f"no route from {caller} to {destination}")

    if classification not in CLASSIFICATIONS:
        raise RouteDenied(f"unknown classification {classification!r}")
    if classification == "client-derived" and not CLIENT_DERIVED_GATE:
        raise RouteDenied(
            "client-derived material is refused to every destination until the "
            "IRC 7216 determination is answered; see WORK-PRODUCT-LANE-PLAN.md")
    if rank(classification) > rank(dest.max_classification):
        raise RouteDenied(
            f"{destination} may receive at most {dest.max_classification!r}, "
            f"not {classification!r}")

    if dest.tasks is not None:
        if task is None:
            raise RouteDenied(
                f"{destination} is restricted to {', '.join(dest.tasks)} and "
                f"needs an explicit task; it does not take open questions")
        if task not in dest.tasks:
            raise RouteDenied(
                f"{destination} has no certified route for task {task!r}; "
                f"certified: {', '.join(dest.tasks)}")
    elif task is not None:
        raise RouteDenied(f"{destination} does not take a task parameter")
    return dest


def flow_matrix() -> list[dict[str, object]]:
    """Every permitted route as data, for the counsel-facing matrix.

    Generated from the registry above rather than maintained beside it, so the
    document counsel approves and the code that enforces it cannot drift.
    """
    rows: list[dict[str, object]] = []
    for source in PEERS:
        for dest_name in destinations_for(source):
            dest = PEERS[dest_name]
            allowed = [c for c in CLASSIFICATIONS
                       if rank(c) <= rank(dest.max_classification)
                       and not (c == "client-derived" and not CLIENT_DERIVED_GATE)]
            rows.append({
                "source": source,
                "destination": dest_name,
                "locality": dest.locality,
                "leaves_hardware": dest.locality == "cloud",
                "max_classification": dest.max_classification,
                "classifications_permitted": allowed,
                "tasks": list(dest.tasks) if dest.tasks else ["(general consultation)"],
                "certificate_required": dest.requires_certificate,
            })
    return rows
