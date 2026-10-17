"""Conservative restart for the installed execution-worker LaunchAgent."""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time
from typing import Callable

from . import drift
from . import store
from .orchestration import delegation
from .orchestration.config import load
from .orchestration.execution_queue import ExecutionQueue, reserve_nothing

_CONFIRM_SECONDS = 30.0


def _running_jobs(queue_root: str) -> int | None:
    """Return a durable running-job count; unreadable state fails closed."""
    try:
        report = ExecutionQueue(queue_root, None, recover_interrupted=False,
                                model_reserved=reserve_nothing).state_report()
        return int(report.get("running", 0))
    except (OSError, ValueError, TypeError):
        return None


def _worker_status(path: str) -> dict[str, object] | None:
    value = store.read_json_or_none(path)
    if not isinstance(value, dict):
        return None
    pid, started_at = value.get("pid"), value.get("started_at")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(started_at, str) or not started_at:
        return None
    return {"pid": pid, "started_at": started_at}


def _is_new_worker(previous: dict[str, object] | None,
                   current: dict[str, object] | None) -> bool:
    if current is None:
        return False
    if previous is None:
        return True
    return (current["pid"] != previous["pid"]
            or current["started_at"] > previous["started_at"])


def restart(config_path: str | None = None, *, home: str | None = None,
            wait: float | None = None, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> str:
    """Restart only an installed, idle worker. It never signals a running job."""
    if wait is not None and (not math.isfinite(wait) or wait < 0):
        return "restart_worker_refused:wait_invalid"
    resolved = drift.discover_config(config_path, home=home)
    if resolved is None:
        return "restart_worker_not_installed"
    try:
        cfg = load(resolved)
    except (OSError, ValueError):
        return "restart_worker_not_installed"
    if cfg.execution_queue_root is None:
        return "restart_worker_not_installed"
    installed = delegation.launch_agent_status(os.path.abspath(home or os.path.expanduser("~")))
    if installed.get("status") != "active":
        return "restart_worker_not_installed"
    deadline = clock() + (wait or 0.0)
    request_path = cfg.execution_queue_root / ".execution-restart-request.json"
    restart_lock = str(cfg.execution_queue_root / ".execution-restart.lock")
    status_path = str(cfg.execution_queue_root / "execution-worker-status.json")
    previous = _worker_status(status_path)
    while True:
        try:
            with store.file_lock(restart_lock, timeout=0):
                running = _running_jobs(str(cfg.execution_queue_root))
                if running == 0:
                    store.atomic_write_json(str(request_path), {"restart": True})
                    break
        except TimeoutError:
            # A worker pass has the short hand-off lock.  Do not wait for a
            # provider run, and do not turn lock contention into an unsafe
            # "queue unavailable" verdict.
            running = 1
        except OSError:
            return "restart_worker_refused:queue_status_unavailable"
        if running is None:
            return "restart_worker_refused:queue_status_unavailable"
        if wait is None or clock() >= deadline:
            return "restart_worker_refused:execution_running"
        sleep(min(0.2, max(0.0, deadline - clock())))
    # A request is not a restart.  The old worker exits only at its next idle
    # pass, and KeepAlive starts the replacement.  Confirm that replacement
    # from its own fresh marker before saying it happened.
    remaining = max(0.0, deadline - clock()) if wait is not None else 0.0
    confirmation_deadline = clock() + _CONFIRM_SECONDS + remaining
    nudged = False
    while clock() < confirmation_deadline:
        if _is_new_worker(previous, _worker_status(status_path)):
            return "restart_worker_restarted"
        if not request_path.exists() and not nudged:
            status = delegation.launch_agent_status(os.path.abspath(home or os.path.expanduser("~")))
            target = status.get("target")
            if status.get("status") != "active" and isinstance(target, str) and target.startswith("gui/"):
                try:
                    subprocess.run(["launchctl", "kickstart", target],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=20, check=False,
                                   shell=False)
                except (OSError, subprocess.SubprocessError):
                    pass
            nudged = True
        sleep(min(0.2, max(0.0, confirmation_deadline - clock())))
    return "restart_worker_requested"


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="setup_bridge.py restart-worker")
    parser.add_argument("--config")
    parser.add_argument("--home")
    parser.add_argument("--wait", type=float, metavar="SECONDS")
    args = parser.parse_args(argv)
    outcome = restart(args.config, home=args.home, wait=args.wait)
    messages = {
        "restart_worker_restarted": "Execution worker restarted.",
        "restart_worker_requested": "Execution worker restart was requested; it will exit at its next idle pass.",
        "restart_worker_not_installed": "Execution worker is not installed; nothing was restarted.",
        "restart_worker_refused:execution_running": "Execution worker was not restarted because an execution job is running.",
        "restart_worker_refused:queue_status_unavailable": "Execution worker was not restarted because queue status is unavailable.",
        "restart_worker_refused:wait_invalid": "Execution worker was not restarted because --wait must be a non-negative finite number.",
    }
    out(messages[outcome])
    return 0 if outcome in {"restart_worker_restarted", "restart_worker_requested",
                            "restart_worker_not_installed"} else 1
