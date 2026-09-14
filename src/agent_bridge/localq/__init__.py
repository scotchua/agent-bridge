"""Durable, local-only mechanical-work queue.

This package is deliberately transport-neutral.  The MCP adapter lives in
``localq.mcp`` and the bridge's peer-consultation tools do not import it.
"""

from .spool import (AdmissionError, FakeBackend, JobNotFound, LocalQueue,
                    QueueCaps, ResourceSnapshot, SubprocessBackend)

__all__ = [
    "AdmissionError", "FakeBackend", "JobNotFound", "LocalQueue", "QueueCaps",
    "ResourceSnapshot", "SubprocessBackend",
]
