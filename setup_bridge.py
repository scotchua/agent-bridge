#!/usr/bin/env python3
"""Portable entry point used by generated MCP registrations."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 11):
        print("agent-bridge requires Python 3.11 or newer.", file=sys.stderr)
        return 2
    if not raw or raw[0] in {"-h", "--help"}:
        print("usage: setup_bridge.py {onboard,serve-peer,serve-local} ...")
        return 0
    command, rest = raw[0], raw[1:]
    if command == "onboard":
        from agent_bridge import onboard
        return onboard.main(rest)
    if command == "serve-peer":
        import argparse
        parser = argparse.ArgumentParser(prog="setup_bridge.py serve-peer")
        parser.add_argument("--caller", choices=("codex", "claude"), required=True)
        parser.add_argument("--config")
        args = parser.parse_args(rest)
        from agent_bridge import mcp_server
        forwarded = ["--caller", args.caller]
        if args.config:
            forwarded.extend(("--config", args.config))
        return mcp_server.main(forwarded)
    if command == "serve-local":
        from agent_bridge import local_worker
        return local_worker.main(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
