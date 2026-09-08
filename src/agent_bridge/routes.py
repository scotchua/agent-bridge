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


#: No destination may receive client-derived material yet. Flipping a peer's
#: ceiling is not enough on its own; this gate must be lifted too, in a
#: reviewed change. Two locks on the same door, because IRC 7216 makes a
#: knowing or reckless unauthorized disclosure a crime, not a bug.
CLIENT_DERIVED_GATE = False

# ---------------------------------------------------------------- IRC 7216
# Framing by Scott Edwards, CPA, 2026-09-08, assuming the inputs are tax
# return information obtained in a return-preparation engagement. Recorded
# here because it decides the ceilings above, and because an earlier draft of
# this file asked counsel the wrong question.
#
# CLOUD destinations. Treat transmission to a third-party LLM API as a
# DISCLOSURE. Reg. 301.7216-1 defines disclosure broadly as making tax return
# information known to any person in any manner, and tax return information
# includes both client-furnished information and preparer-derived
# computations, worksheets and workpapers. So the correct default is not
# "cloud is capped because no disclosure occurs"; it is "cloud may receive
# only non-client-derived material unless a specific exception applies or the
# taxpayer has given a Reg. 301.7216-3 consent."
#
# A third-party technology provider is not automatically prohibited. Non-
# substantive processing, software and equipment services can be permissible,
# subject to the regulatory conditions: limit the disclosure to what is
# necessary, and give written notice of the 7216 and 6713 obligations where
# required. But if the provider makes substantive determinations or gives tax
# advice affecting liability, taxpayer consent is required first.
#
# LOCAL destinations. Inference on the firm's own hardware, with no network
# call and no access by anyone outside the same U.S. tax return preparer, is
# much more plausibly an internal USE than a disclosure to a third party.
# Reg. 301.7216-2 permits an officer, employee or member of the same U.S.
# preparer to use or disclose return information internally to assist in
# preparing the return or providing auxiliary services. That is why a local
# ceiling can sit ABOVE a cloud ceiling. It is not automatic: it holds only
# while every condition in LOCAL_INTERNAL_USE_CONDITIONS below is true.
#
# The questions worth putting to counsel are therefore NOT "is the API a
# disclosure", which the regulation makes a hard position to hold. They are:
#   1. Does any specific IRC 7216 / Reg. 301.7216-2 exception apply to the
#      contemplated cloud API use? If not, must the cloud ceiling remain
#      below client-derived tax return information absent taxpayer consent
#      under Reg. 301.7216-3?
#   2. Does the architecture genuinely keep local inference inside the same
#      U.S. tax return preparer, with no disclosure to a separate person or
#      non-U.S. personnel, and only for permitted return-preparation,
#      auxiliary-service, or other authorized uses?
#   3. If de-identification is later relied on, does removing names and
#      direct identifiers sufficiently remove identifiability where amounts,
#      dates, jurisdictions, entity facts, or filing details may still point
#      to a specific client?

#: Every one of these must hold before a local ceiling may exceed a cloud
#: ceiling. They are stated as deployment claims, not test-proven facts.
#: Raising a ceiling requires re-reading and affirming those claims. The code
#: can enforce the registry; it cannot prove the legal predicates behind it.
LOCAL_INTERNAL_USE_CONDITIONS = (
    "every access surface stays within the same U.S. tax return preparer: "
    "prompts, outputs, logs, model state, telemetry, backups, admin consoles "
    "and support channels",
    "no access by personnel outside the United States, because Reg. "
    "301.7216-2 requires consent for disclosure to non-U.S. personnel even "
    "within the same firm",
    "model operation stays within the same U.S. preparer: no hosted "
    "inference, no telemetry or crash reporting carrying return information",
    "administration of the machine stays within the same U.S. preparer",
    "the use is return preparation, an auxiliary service, or another use "
    "permitted under IRC 7216 and Reg. 301.7216-1 through -3",
)

#: Why there is no "de-identified" tier in CLASSIFICATIONS, and why adding one
#: would be a consequential change rather than a convenience.
#:
#: Reg. 301.7216-2(o) requires anonymized or statistical information to be in
#: a form that cannot be associated with, or otherwise identify, DIRECTLY OR
#: INDIRECTLY, a particular taxpayer. Stripping names does not meet that.
#: Exact amounts, dates, jurisdictions, entity facts, filing details and
#: unique transactions can each function as a cell-of-one identifier. In a
#: two-office practice the population is small enough that a single Ketchikan
#: borough filing with exact revenue may identify its client on its own.
#:
#: So redaction is a certified TASK the local peer may perform. It is not a
#: downgrade: its output does not become a lower classification.
DEIDENTIFICATION_IS_NOT_A_DOWNGRADE = True

#: NOT A CHECK. Nothing reads this constant. authorize() never sees the work,
#: only a task label from an allowlist, so it cannot establish that the actual
#: work is non-substantive. Recorded here because it is the line the task
#: envelope was drawn against.
#:
#: The regulatory line that decides whether taxpayer consent is needed:
#: non-substantive processing may be permissible without it, while substantive
#: determinations or tax advice affecting liability require consent first.
#:
#: The five tasks the local peer is certified for are all non-substantive
#: processing. That was chosen for a capability reason, because the model can
#: silently omit content, and it happens to land on the same side of the
#: regulatory line. The two arguments are independent and agree, which is why
#: the envelope is worth keeping even if one of them later changes.
SUBSTANTIVE_WORK_REQUIRES_CONSENT = True


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
        # Same ceiling as the cloud peers TODAY. The 7216 basis for raising it
        # above them is real (internal use by the same preparer, Reg.
        # 301.7216-2) but conditional: see LOCAL_INTERNAL_USE_CONDITIONS. No
        # test here can prove those conditions, so the ceiling stays put until
        # someone asserts them deliberately.
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

    Every check this registry makes is here, in one place. That is not the
    same as complete mediation, and this docstring said it was: seven call
    sites still resolve peers directly and never reach this function, so
    today the registry is a description, not a boundary. Until one dispatch
    gateway owns backend access and every call goes through it, calling
    authorize() is a convention that a new caller can silently skip.

    Success here is deliberately NOT sufficient permission to execute.
    Certificate verification happens at dispatch against the resolved model,
    because that is the only point where the digest is known, and the model
    identity that was verified must be the one invoked.
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
            "client-derived material is refused to every destination. For a "
            "cloud peer this is a disclosure under Reg. 301.7216-1 and needs a "
            "specific exception or a 301.7216-3 consent; for a local peer it "
            "needs every condition in LOCAL_INTERNAL_USE_CONDITIONS to hold. "
            "Neither is established.")
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


def _permits(caller: str, destination: str, classification: str,
             task: str | None) -> bool:
    try:
        authorize(caller, destination, classification, task)
    except RouteDenied:
        return False
    return True


def flow_matrix() -> list[dict[str, object]]:
    """Every permitted route as data, for the counsel-facing matrix.

    Derived by ASKING authorize() rather than by recomputing its rules. The
    first version reimplemented the classification filter, which let the
    published matrix and the enforced behaviour disagree: a peer with
    `tasks=()` was advertised as general consultation at three classifications
    while authorize() refused every call to it, and a self-route added to
    ROUTES was published while authorize() denied it. A matrix counsel has
    approved that does not match the code is worse than no matrix, because it
    is a control everyone believes in.
    """
    rows: list[dict[str, object]] = []
    for source in PEERS:
        for dest_name in destinations_for(source):
            dest = PEERS[dest_name]
            probes: tuple[str | None, ...] = (
                dest.tasks if dest.tasks is not None else (None,))
            grid = {(c, t): _permits(source, dest_name, c, t)
                    for c in CLASSIFICATIONS for t in probes}
            allowed = [c for c in CLASSIFICATIONS
                       if any(grid[(c, t)] for t in probes)]
            if not allowed:
                # authorize() refuses this pair outright. Publishing it would
                # advertise access the code does not grant.
                continue
            tasks = [t for t in probes
                     if any(grid[(c, t)] for c in CLASSIFICATIONS)]
            rows.append({
                "source": source,
                "destination": dest_name,
                "locality": dest.locality,
                "leaves_hardware": dest.locality == "cloud",
                "max_classification": dest.max_classification,
                "classifications_permitted": allowed,
                "tasks": tasks if dest.tasks is not None
                         else ["(general consultation)"],
                "certificate_required": dest.requires_certificate,
            })
    return rows
