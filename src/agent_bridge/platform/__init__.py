"""Select and expose the platform implementation."""

from __future__ import annotations

import os

from .base import Platform

if os.name == "nt":
    from .windows import WindowsPlatform
    platform: Platform = WindowsPlatform()
else:
    from .posix import PosixPlatform
    platform = PosixPlatform()

__all__ = ["Platform", "platform"]
