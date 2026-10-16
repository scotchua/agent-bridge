"""Fixed, private diagnostics emitted by verification test helpers.

The environment names here are created only by the two execution harnesses
when they start an already-admitted verification command.  They are not part
of a brief or verification argv, both of which have been validated before
this point.
"""

from __future__ import annotations

from pathlib import Path


JOB_VERIFICATION_MARKER = "AGENT_BRIDGE_JOB_VERIFICATION"
SKIP_RECORD_PATH = "AGENT_BRIDGE_VERIFICATION_SKIP_RECORD"
NESTED_CONFINEMENT_UNAVAILABLE = "nested_confinement_unavailable"
LOOPBACK_UNAVAILABLE = "loopback_unavailable"
SKIP_REASONS = frozenset({NESTED_CONFINEMENT_UNAVAILABLE, LOOPBACK_UNAVAILABLE})
SKIP_RECORD_NAME = ".agent-bridge-verification-skips"
MAX_SKIP_RECORD_BYTES = 64 * 1024


def skip_counts(path: Path) -> dict[str, int]:
    """Return only bounded counts for the fixed skip-reason vocabulary.

    The record is written inside the verification scratch directory, which is
    deliberately writable by the test process.  It is diagnostic evidence,
    never an admission or completion decision, so malformed data is ignored.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SKIP_RECORD_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > MAX_SKIP_RECORD_BYTES:
        raw = raw[:MAX_SKIP_RECORD_BYTES]
    counts = {reason: 0 for reason in SKIP_REASONS}
    for line in raw.splitlines():
        try:
            reason = line.decode("ascii")
        except UnicodeDecodeError:
            continue
        if reason in counts:
            counts[reason] += 1
    return {reason: counts[reason] for reason in sorted(counts) if counts[reason]}
