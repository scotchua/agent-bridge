"""agent-bridge: a local Claude/Codex consultation bridge.

This package supports POSIX and Windows. The check runs here, before any
submodule imports, so any other platform gets an explanation rather than a
platform-specific import error.
"""

from __future__ import annotations

import os
import sys

#: Why this is a refusal and not a warning. Each item is load-bearing for a
#: guarantee the documentation makes, so a build without it would be a
#: different tool wearing this one's name.
PLATFORM_REQUIREMENTS = (
    ("exclusive locks", "every lock in the broker: job status, conversation "
                        "claims, the admission gate and the ledger"),
    ("killable process trees", "terminating a peer and everything it spawned"),
    ("owner-only permissions", "protecting all state and consultation history"),
    ("capped pipe reads", "reading peer output while enforcing size caps"),
)

UNSUPPORTED_MESSAGE = """agent-bridge supports POSIX (macOS or Linux) and Windows.

Detected: {platform} (os.name={osname!r})

This is a refusal rather than a warning because the missing pieces are the
safety machinery, not conveniences:
{requirements}

Do not work around this by stubbing the missing modules. Doing so produces a
build where file locking silently does nothing, a runaway peer process cannot
be terminated, and state files are not owner-only, while the documentation
still promises all three. That is worse than not running at all.

Use macOS, Linux, Windows, or WSL (Windows Subsystem for Linux). Those builds
are covered by continuous integration.
"""


def platform_supported() -> bool:
    return os.name in ("posix", "nt")


def assert_platform_supported() -> None:
    if platform_supported():
        return
    requirements = "\n".join(
        f"  - {name}: {why}" for name, why in PLATFORM_REQUIREMENTS
    )
    raise RuntimeError(UNSUPPORTED_MESSAGE.format(
        platform=sys.platform, osname=os.name, requirements=requirements))


assert_platform_supported()
