"""Operator-only inspection, repair and re-enabling of local quality stops."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import drift
from .localq import quality
from .localq.spool import MECHANICAL_TASKS
from .orchestration.config import load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_bridge.py local-quality", description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--enable", choices=sorted(MECHANICAL_TASKS))
    action.add_argument("--repair", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = drift.discover_config(args.config)
        if config is None:
            raise ValueError("config_missing")
        cfg = load(config)
        if args.repair:
            quality.repair(cfg.state_root, cfg.local_queue_root / "localq.sqlite3",
                           cfg.local_backend, MECHANICAL_TASKS)
        if args.enable:
            quality.enable(cfg.state_root, cfg.local_backend, args.enable)
        state = quality.read_state(cfg.state_root)
        result = {"backend_id": cfg.local_backend, "state": state,
                  "disabled_for_quality": sorted(quality.disabled_types(cfg.state_root, cfg.local_backend))}
    except (OSError, ValueError, TimeoutError, sqlite3.Error):
        if args.json:
            print(json.dumps({"error": "local_quality:state_unavailable"}))
        else:
            print("local_quality:state_unavailable", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(json.dumps(state, indent=2, sort_keys=True))
        print("Disabled for quality: " + (", ".join(result["disabled_for_quality"]) or "none"))
        for line in quality.attention(cfg.state_root, cfg.local_backend):
            print(line)
    return 0
