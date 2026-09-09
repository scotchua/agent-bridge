"""An OS-executable path for a Python test fake.

Windows cannot execute a `.py` directly. `CreateProcess` on one raises
`OSError: [WinError 193] %1 is not a valid Win32 application`, because the
shebang line POSIX honours means nothing to it. Everything here is invoked as
a configured peer executable, through the same `subprocess.run` the product
uses, so there is no interpreter to fall back on.

That makes the breakage invisible from macOS, which is where this project is
developed. Measured 2026-09-08 in a Windows 11 guest on Python 3.12.7 against
a fresh clone of main: 47 of 49 tests passed, and both failures traced here.

Two callers need this, which is why it is a module and not a method:

  - `tests/harness.py`, for the fakes a Sandbox configures as peers.
  - `canaries/run_canaries.py`, whose timeout canary points a stub config at
    `fake_<peer>.py`. That one is not a test-only concern: the canary suite is
    the evidence gate this project runs before trusting a CLI version, and it
    could not run at all on Windows.

The shim is written NEXT TO the fake, not into the caller's temporary
directory. `agent_bridge.setup_cmd.is_durable()` refuses any executable under
a temp root, reasoning that a periodically-cleaned path works today and fails
tomorrow, and that a wrapper shim is not the binary whose behaviour this
project measured. A shim in `TemporaryDirectory()` is precisely what that
check exists to reject, so a test that pinned one was arguing with its own
product and losing. `tests/fakes/` is inside the checkout and survives.
"""
from __future__ import annotations

import os
import sys
import tempfile

WINDOWS = os.name == "nt"


def executable_for(script: str) -> str:
    """A path this OS can execute for `script`, a Python test fake.

    On POSIX, the script itself. On Windows, a `.cmd` beside it that invokes
    the current interpreter, created on demand and left in place.
    """
    if not WINDOWS:
        return script
    script = os.path.abspath(script)
    shim = script + ".cmd"
    # `sys.executable` and `script` can both contain spaces (the default
    # install is C:\Program Files\Python312), so both are quoted. %* forwards
    # the caller's arguments, including --version.
    body = ('@echo off\r\n'
            f'"{sys.executable}" "{script}" %*\r\n')
    # newline="" on BOTH sides, or the comparison below can never succeed.
    # Text mode translates on write, so the \r\n above lands on disk as
    # \r\r\n, and universal newlines strip the CRs back out on read. Measured
    # on Windows: b'@echo off\r\r\n...'. The shim still ran, so the only
    # symptom was that every call rewrote and re-published the file, which is
    # what turns the concurrent-replace risk below from theory into practice.
    try:
        with open(shim, "r", encoding="utf-8", newline="") as handle:
            if handle.read() == body:
                return shim
    except (OSError, UnicodeDecodeError):
        # Unreadable or not valid UTF-8, e.g. a truncated file from a killed
        # run. Fall through and republish; a real write failure still raises.
        pass
    # Written through a temporary file in the same directory and renamed, so a
    # second process starting concurrently either sees the old shim or the new
    # one, never a half-written batch file it would then try to execute.
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(shim), suffix=".cmd.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        os.replace(temp, shim)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return shim
