"""Composes the exact text sent to a peer.

Two rules govern this module.

One: nothing is added that the calling agent did not supply, beyond a fixed
instruction preamble and the response contract.  No transcript, no working
tree, no environment, no memories, no nearby files.  The caller composes the
bounded question; the broker only frames it.

Two: the corrective retry message is generated from a closed error code plus
schema metadata only.  Quarantined peer text is never read back into a prompt.
"""

from __future__ import annotations

import json
from typing import Any

PREAMBLE = """You are being consulted as an independent second opinion by another AI agent, through a local consultation bridge. You are not that agent's subordinate and you are not its reviewer of record.

Rules for this consultation:
- Answer only the question below. No repository content, transcript, or environment has been supplied to you, and inspecting the filesystem is prohibited: do not read files, do not explore directories, and do not ask for filesystem access. This is a rule you are required to follow, not a capability you lack.
- Do not run shell commands. Do not propose that the bridge run commands on your behalf.
- Disagree explicitly where you genuinely disagree with a position the caller has actually stated, and name the position you are disagreeing with. If the caller stated no position, or you have no disagreement, return an empty "disagreements" list. Do not manufacture disagreement and do not argue against a position nobody took: a fabricated objection is worse than none, because the caller may act on it.
- Agreeing is a legitimate answer when you agree. What is not acceptable is agreeing because agreement is easier, or hedging to avoid taking a side.
- If you genuinely cannot answer without more context, set status to "needs_context" and put the specific missing facts in "questions".
- If you decline, set status to "refusal" and say why in "summary".

Respond with a single JSON object conforming exactly to the response contract. No prose outside the JSON, no markdown fences."""

CORRECTIVE_TEMPLATE = """Your previous reply in this conversation did not satisfy the response contract and was discarded unread.

Failure code: {code}
Structural violations detected by the broker (paths and expected types only):
{violations}

Reply again with a single JSON object conforming exactly to this contract. No prose outside the JSON, no markdown fences.

Contract (JSON Schema, version {contract_version}):
{schema}"""

#: Cap on how much structural violation text is fed back, to bound prompt size.
MAX_VIOLATIONS = 12
MAX_VIOLATION_CHARS = 240


def build_initial(prompt: str, schema: dict[str, Any], contract_version: str) -> str:
    """Frame a caller-composed question with the preamble and the contract."""
    return (
        f"{PREAMBLE}\n\n"
        f"Response contract (JSON Schema, version {contract_version}):\n"
        f"{json.dumps(schema, indent=2, sort_keys=True)}\n\n"
        f"--- BEGIN CONSULTATION QUESTION ---\n{prompt}\n--- END CONSULTATION QUESTION ---"
    )


def build_continuation(prompt: str, contract_version: str) -> str:
    """A follow-up turn. The peer session already carries the contract."""
    return (
        "Continuing the same consultation. Same rules, same response contract "
        f"(version {contract_version}): reply with a single conforming JSON object, "
        "no prose outside the JSON.\n\n"
        f"--- BEGIN FOLLOW-UP QUESTION ---\n{prompt}\n--- END FOLLOW-UP QUESTION ---"
    )


def build_corrective(
    error_code: str,
    violations: list[str],
    schema: dict[str, Any],
    contract_version: str,
) -> str:
    """Build the single corrective retry prompt.

    `violations` come from the broker's own validator, which emits paths and
    expected types only.  Callers must not pass peer text here; the assertion
    below is a guard rail, not a substitute for that discipline.
    """
    trimmed: list[str] = []
    for violation in violations[:MAX_VIOLATIONS]:
        if not isinstance(violation, str):
            raise TypeError("violations must be strings from the broker validator")
        trimmed.append("- " + violation[:MAX_VIOLATION_CHARS])
    if not trimmed:
        trimmed = ["- response did not parse as a single JSON object"]
    return CORRECTIVE_TEMPLATE.format(
        code=error_code,
        violations="\n".join(trimmed),
        contract_version=contract_version,
        schema=json.dumps(schema, indent=2, sort_keys=True),
    )
