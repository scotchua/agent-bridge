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


def _running_jobs(queue_root: str) -> int | None:
    """Return a durable running-job count; unreadable state fails closed."""
    try:
        report = ExecutionQueue(queue_root, None, recover_interrupted=False,
                                model_reserved=reserve_nothing).state_report()
        return int(report.get("running", 0))
    except (OSError, ValueError, TypeError):
        return None


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
    while True:
        try:
            with store.file_lock(restart_lock):
                running = _running_jobs(str(cfg.execution_queue_root))
                if running == 0:
                    store.atomic_write_json(str(request_path), {"restart": True})
                    break
        except OSError:
            return "restart_worker_refused:queue_status_unavailable"
        if running is None:
            return "restart_worker_refused:queue_status_unavailable"
        if wait is None or clock() >= deadline:
            return "restart_worker_refused:execution_running"
        sleep(min(0.2, max(0.0, deadline - clock())))
    target = installed.get("target")
    if not isinstance(target, str) or not target.startswith("gui/"):
        return "restart_worker_refused:launchd_status_unavailable"
    try:
        result = subprocess.run(["launchctl", "kickstart", target],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20, check=False,
                                shell=False)
    except (OSError, subprocess.SubprocessError):
        return "restart_worker_refused:launchd_restart_failed"
    return "restart_worker_restarted" if result.returncode == 0 else "restart_worker_refused:launchd_restart_failed"


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="setup_bridge.py restart-worker")
    parser.add_argument("--config")
    parser.add_argument("--home")
    parser.add_argument("--wait", type=float, metavar="SECONDS")
    args = parser.parse_args(argv)
    outcome = restart(args.config, home=args.home, wait=args.wait)
    messages = {
        "restart_worker_restarted": "Execution worker restarted.",
        "restart_worker_not_installed": "Execution worker is not installed; nothing was restarted.",
        "restart_worker_refused:execution_running": "Execution worker was not restarted because an execution job is running.",
        "restart_worker_refused:queue_status_unavailable": "Execution worker was not restarted because queue status is unavailable.",
        "restart_worker_refused:launchd_status_unavailable": "Execution worker was not restarted because launchd status is unavailable.",
        "restart_worker_refused:launchd_restart_failed": "Execution worker restart failed safely.",
        "restart_worker_refused:wait_invalid": "Execution worker was not restarted because --wait must be a non-negative finite number.",
    }
    out(messages[outcome])
    return 0 if outcome in {"restart_worker_restarted", "restart_worker_not_installed"} else 1
