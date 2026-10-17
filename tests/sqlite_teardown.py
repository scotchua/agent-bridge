"""Bounded cleanup for test fixtures that own SQLite-backed workers.

Windows does not permit deleting an open SQLite file.  Fixtures using this
helper first stop their known owners and join their named worker threads.  A
remaining sharing violation gets a small, explicit settling window; every
other cleanup error fails immediately.
"""

from __future__ import annotations

import gc
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable


_RETRY_COUNT = 10
_RETRY_DELAY_SECONDS = 0.05
_WORKER_JOIN_TIMEOUT_SECONDS = 30


def _is_windows_sharing_violation(error: OSError) -> bool:
    """Whether ``error`` is precisely Windows' sharing-violation error."""

    return os.name == "nt" and getattr(error, "winerror", None) == 32


def _owner_diagnostic(worker_names: frozenset[str]) -> str:
    """Return fixed, content-free evidence for a failed fixture cleanup."""

    alive = sorted(
        thread.name for thread in threading.enumerate()
        if thread.name in worker_names and thread.is_alive())
    connections = sum(
        isinstance(candidate, sqlite3.Connection) for candidate in gc.get_objects())
    return ("sqlite_teardown owners: worker_threads="
            + (",".join(alive) if alive else "none")
            + "; child_processes=not_started_by_fixture"
            + f"; sqlite_connections={connections}")


def cleanup_temporary_directory(
        temporary, *, stop: Iterable[Callable[[], None]] = (),
        worker_names: Iterable[str] = ()) -> None:
    """Stop fixture owners and clean a temporary directory conservatively.

    ``stop`` is deliberately explicit: this helper never guesses at or closes
    arbitrary interpreter-wide SQLite connections.  That could mask a leak or
    close a resource owned by another test.  Known workers are joined before
    cleanup, then only WinError 32 is retried and only for this bounded window.
    """

    names = frozenset(worker_names)
    for stopper in stop:
        stopper()
    for thread in threading.enumerate():
        if thread.name in names and thread is not threading.current_thread():
            thread.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)

    for attempt in range(_RETRY_COUNT):
        try:
            temporary.cleanup()
            return
        except OSError as error:
            if not _is_windows_sharing_violation(error) or attempt == _RETRY_COUNT - 1:
                error.add_note(_owner_diagnostic(names))
                raise
            time.sleep(_RETRY_DELAY_SECONDS)
