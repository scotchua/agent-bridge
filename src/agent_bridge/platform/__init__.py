"""Select and expose the platform implementation."""

from __future__ import annotations

from .base import Platform
from .posix import PosixPlatform

platform: Platform = PosixPlatform()

__all__ = ["Platform", "platform"]
