"""Capability-aware skips for tests run by a job verification harness.

This is intentionally not a test module.  A skip is possible only inside a
verification command the harness marked, and only after a small real probe
observes the relevant OS denial.  Normal local and CI test runs therefore
continue to exercise these tests unchanged.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge.execution import verification_diagnostics as diagnostics  # noqa: E402
from agent_bridge.execution import hostenv  # noqa: E402


def requires_nested_confinement(case) -> None:
    """Skip only an actually denied nested-confinement test under a job."""
    _skip_if_denied(case, diagnostics.NESTED_CONFINEMENT_UNAVAILABLE,
                    _nested_confinement_denied)


def requires_loopback(case) -> None:
    """Skip only an actually denied loopback test under a job."""
    _skip_if_denied(case, diagnostics.LOOPBACK_UNAVAILABLE, _loopback_denied)


def _skip_if_denied(case, reason: str, probe) -> None:
    if os.environ.get(diagnostics.JOB_VERIFICATION_MARKER) != "1" or not probe():
        return
    _record_skip(reason)
    case.skipTest(reason)


def _nested_confinement_denied() -> bool:
    """Whether this verification process is denied a nested real backend."""
    if sys.platform == "darwin" and os.path.isfile(hostenv.SANDBOX_EXEC):
        command = [hostenv.SANDBOX_EXEC, "-p", "(version 1) (allow default)",
                   sys.executable, "-c", "pass"]
    elif sys.platform.startswith("linux") and os.path.isfile(hostenv.UNSHARE):
        command = [hostenv.UNSHARE, "--user", "--map-root-user", "--mount",
                   "--net", "--fork", "--", sys.executable, "-c", "pass"]
    else:
        return False
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=3, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode == 0:
        return False
    return b"operation not permitted" in result.stderr.lower() or b"permission denied" in result.stderr.lower()


def _loopback_denied() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        return exc.errno in {errno.EACCES, errno.EPERM}
    return False


def _record_skip(reason: str) -> None:
    if reason not in diagnostics.SKIP_REASONS:
        return
    value = os.environ.get(diagnostics.SKIP_RECORD_PATH)
    if not value:
        return
    try:
        descriptor = os.open(value, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, reason.encode("ascii") + b"\n")
        finally:
            os.close(descriptor)
    except OSError:
        # The test's capability decision is independent of diagnostics.  A
        # record-write failure must not turn a known sandbox limitation into a
        # passing test body.
        return
