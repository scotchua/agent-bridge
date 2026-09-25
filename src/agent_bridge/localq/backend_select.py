"""Chooses the local queue's execution backend from strict, server-only
configuration.

No assistant-facing request selects a backend. The choice is made exactly
once, when the queue's own :class:`~.service.Service` is built from its
configuration file (:func:`~.service.Service.for_config`, called by
``orchestration/server.py``'s ``main`` and nowhere else in production); the
MCP tool surface a caller actually talks to (``localq.mcp``,
``orchestration.server``) never reads a "backend" field out of a request and
has no such field in its schemas.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from . import gemma_child, worker_child
from .spool import Backend, MECHANICAL_TASKS, QueueCaps, SubprocessBackend

#: The certified delegate's own budget, unchanged from its existing
#: certification. This adapter never shortens it; see
#: ``gemma_queue_caps_for``.
GEMMA_CERTIFIED_TIMEOUT_SECONDS = 600.0
#: Headroom the *queue's own* per-job timeout/lease need over the child
#: timeout actually handed to the delegate, so the queue's own SIGKILL on
#: an inner timeout never races the delegate's own clean exit at the same
#: instant. Purely a margin around the delegate's certified figure, not a
#: second certified number of its own.
GEMMA_QUEUE_MARGIN_SECONDS = 60.0

LOCAL_BACKENDS = frozenset({"private_worker", "gemma_certified"})


class BackendSelectionError(ValueError):
    """The configured ``local_backend`` cannot be built."""


def gemma_queue_caps_for(cfg: Any) -> QueueCaps:
    """``QueueCaps`` sized for the certified delegate's own budget.

    ``QueueCaps()``'s defaults (60s timeout, 120s lease) are sized for the
    private worker's own short-lived calls, not this delegate's 600s
    certified budget: at the defaults, the queue itself would SIGKILL the
    child (and, per the fix in ``spool.SubprocessBackend``, its whole
    process group) long before a legitimate certified run could finish, and
    a lease that expired mid-run would be recorded ``executor_lease_lost``
    while the job was still genuinely running. Both numbers are widened
    here, never narrowed below the configured child timeout, so "make the
    queue's own timeout/lease long enough" is a fact about this
    configuration rather than a hope about ``QueueCaps``'s unrelated
    defaults.
    """
    timeout = float(getattr(cfg, "gemma_timeout_seconds", GEMMA_CERTIFIED_TIMEOUT_SECONDS))
    return QueueCaps(timeout_seconds=timeout + GEMMA_QUEUE_MARGIN_SECONDS,
                     lease_seconds=timeout + (2 * GEMMA_QUEUE_MARGIN_SECONDS))


def build_backend_and_caps(cfg: Any, queue_root: str) -> "tuple[Backend, QueueCaps, frozenset[str]]":
    """Return ``(backend, caps, allowed_task_types)`` for ``cfg.local_backend``.

    Raises :class:`BackendSelectionError` for anything else -- an
    unsupported name, or a ``gemma_certified`` selection missing one of its
    required paths -- rather than silently falling back to the private
    worker (that would be exactly the substitution this adapter exists to
    prevent).
    """
    name = getattr(cfg, "local_backend", "private_worker")
    if name not in LOCAL_BACKENDS:
        raise BackendSelectionError("local_backend_unsupported")

    if name == "private_worker":
        command = [sys.executable, str(Path(worker_child.__file__).resolve()),
                  "--worker", str(cfg.worker_executable), "--state", str(cfg.worker_state),
                  "--queue-root", str(queue_root)]
        return SubprocessBackend(command), QueueCaps(), MECHANICAL_TASKS

    # name == "gemma_certified"
    if os.name != "posix":
        # This backend nests the installed delegate under an adapter. Until
        # the local queue has a verified native-Windows tree-termination
        # implementation, admitting it there could leave the delegate alive
        # after an adapter timeout. Refuse at construction, not after work.
        raise BackendSelectionError("gemma_certified_platform_unsupported")
    required = ("gemma_delegate_executable", "gemma_python_executable",
               "gemma_receipt_root", "gemma_receipt_validator_executable",
               "gemma_delegate_sha256", "gemma_receipt_validator_sha256",
               "gemma_model_digest")
    missing = [field for field in required if getattr(cfg, field, None) is None]
    if missing:
        raise BackendSelectionError("gemma_certified_configuration_incomplete")
    timeout = float(getattr(cfg, "gemma_timeout_seconds", GEMMA_CERTIFIED_TIMEOUT_SECONDS))
    command = [sys.executable, str(Path(gemma_child.__file__).resolve()),
              "--delegate", str(cfg.gemma_delegate_executable),
              "--python", str(cfg.gemma_python_executable),
              "--receipt-root", str(cfg.gemma_receipt_root),
              "--receipt-validator", str(cfg.gemma_receipt_validator_executable),
              "--delegate-sha256", str(cfg.gemma_delegate_sha256),
              "--validator-sha256", str(cfg.gemma_receipt_validator_sha256),
              "--model-digest", str(cfg.gemma_model_digest),
              "--timeout", str(timeout)]
    return SubprocessBackend(command), gemma_queue_caps_for(cfg), frozenset({"summarize"})
