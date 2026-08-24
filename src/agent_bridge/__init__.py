"""agent-bridge: a local Claude/Codex consultation bridge.

This package requires a POSIX platform. The check runs here, before any
submodule imports, so an unsupported platform gets an explanation rather than
`ModuleNotFoundError: No module named 'fcntl'`.
"""

from __future__ import annotations

import os
import sys

#: Why this is a refusal and not a warning. Each item is load-bearing for a
#: guarantee the documentation makes, so a build without it would be a
#: different tool wearing this one's name.
POSIX_REQUIREMENTS = (
    ("fcntl.flock", "every lock in the broker: job status, conversation "
                    "claims, the admission gate and the ledger"),
    ("os.killpg and process groups", "terminating a peer and everything it "
                                     "spawned, as one unit"),
    ("signal.SIGKILL", "the second stage of that termination"),
    ("os.fchmod", "the owner-only 0600 and 0700 guarantee on all state"),
    ("selectors over pipes", "reading peer output while enforcing size caps; "
                             "Windows select supports sockets only"),
)

UNSUPPORTED_MESSAGE = """agent-bridge requires a POSIX platform (macOS or Linux).

Detected: {platform} (os.name={osname!r})

This is a refusal rather than a warning because the missing pieces are the
safety machinery, not conveniences:
{requirements}

Do not work around this by stubbing the missing modules. Doing so produces a
build where file locking silently does nothing, a runaway peer process cannot
be terminated, and state files are not owner-only, while the documentation
still promises all three. That is worse than not running at all.

On Windows, use WSL (Windows Subsystem for Linux) and install this inside the
Linux environment. The Linux build is covered by continuous integration.
"""


def platform_supported() -> bool:
    return os.name == "posix"


def assert_platform_supported() -> None:
    if platform_supported():
        return
    requirements = "\n".join(
        f"  - {name}: {why}" for name, why in POSIX_REQUIREMENTS
    )
    raise RuntimeError(UNSUPPORTED_MESSAGE.format(
        platform=sys.platform, osname=os.name, requirements=requirements))


assert_platform_supported()
