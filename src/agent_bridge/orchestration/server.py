"""Standalone stdio MCP server for orchestration and automatic local intake."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
from typing import Any

from ..capacity_router import StageRouter
from ..localq.intake import AutomaticIntake
from ..localq.service import Service
from . import gate
from .config import load
from .execution_queue import ExecutionQueue
from .mcp import build_tools

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.2.0"
MAX_FRAME_CHARS = 2 * 1024 * 1024
INSTRUCTIONS = (
    "Durable work orchestration and local mechanical non-client processing. "
    "This is separate from peer consultation. Local drafts require review; "
    "client-derived or confidential input and silent cloud fallback are refused."
)


class Server:
    def __init__(self, caller: str, service: Service, router: StageRouter, *,
                 execution: ExecutionQueue | None = None, interval: float = 5.0,
                 state_root: str | None = None,
                 protected: tuple[str, ...] = ()):
        if caller not in {"claude", "codex"}:
            raise ValueError("caller_invalid")
        if interval <= 0:
            raise ValueError("interval_invalid")
        self.caller, self.service, self.router = caller, service, router
        self.execution, self.interval = execution, interval
        self.intake = AutomaticIntake(service.queue)
        self.tools = build_tools(caller, router, service.queue, self.intake, execution,
                                 state_root=state_root, protected=protected)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._executor, daemon=True,
                                            name="orchestration-local-worker")
            self._thread.start()

    def _executor(self) -> None:
        while not self._stop.is_set():
            try:
                self.service.once()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
            self._thread = None

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value)))

    @staticmethod
    def _content(payload: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
                "structuredContent": payload, "isError": not payload.get("ok", False)}

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return self._error(None, -32600, "invalid request")
        request_id = message.get("id")
        if request_id is None:
            return None
        if not self._valid_id(request_id):
            return self._error(None, -32600, "invalid request")
        method = message["method"]
        if method == "initialize":
            result: Any = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"agent-bridge-orchestration ({self.caller} caller)",
                               "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method in {"tools/list", "tools/listChanged"}:
            result = {"tools": [{"name": name, "description": spec["description"],
                                 "inputSchema": spec["inputSchema"]}
                                for name, spec in self.tools.items()]}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return self._error(request_id, -32602, "invalid params")
            spec = self.tools.get(params["name"])
            arguments = params.get("arguments", {})
            if spec is None:
                return self._error(request_id, -32602, "unknown tool")
            if not isinstance(arguments, dict):
                return self._error(request_id, -32602, "invalid params")
            try:
                result = self._content(spec["handler"](arguments))
            except Exception:
                result = self._content({"ok": False, "error": "internal_error"})
        else:
            return self._error(request_id, -32601, "method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, stdin: Any = None, stdout: Any = None) -> int:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        self.start()
        try:
            while True:
                line = stdin.readline(MAX_FRAME_CHARS + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME_CHARS:
                    stdout.write(json.dumps(self._error(None, -32600, "request exceeds frame limit")) + "\n")
                    stdout.flush()
                    return 1
                try:
                    request = json.loads(line)
                except (ValueError, RecursionError):
                    reply = self._error(None, -32700, "parse error")
                else:
                    reply = self.handle(request)
                if reply is not None:
                    stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
                    stdout.flush()
        finally:
            self.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Standalone orchestration and local-worker MCP server. Does not install configuration.")
    parser.add_argument("--caller", required=True, choices=["claude", "codex"])
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    cfg = load(args.config)
    cfg.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    service = Service(str(cfg.local_queue_root), str(cfg.worker_executable), str(cfg.worker_state))
    router = StageRouter(cfg.capacity_db)
    execution = None
    if cfg.execution_queue_root is not None:
        # Provider execution is deliberately not performed inside an MCP
        # process. Desktop app sandboxes can deny Keychain access.  This
        # surface only admits requests and reads durable status/receipts; the
        # standalone execution worker consumes the same queue.
        execution = ExecutionQueue(cfg.execution_queue_root, None,
                                   recover_interrupted=False)
    # The same protected list the delegation-first hook computes, so
    # work_digest_file refuses a target the hook would also refuse to write:
    # the gate's own state, the stage router's database, the local queue's
    # own database and heartbeat, and the hook installation files.
    protected = gate.protected_paths(str(cfg.state_root), args.config, os.path.expanduser("~"),
                                     str(cfg.capacity_db), str(cfg.local_queue_root))
    return Server(args.caller, service, router, execution=execution,
                  interval=cfg.interval_seconds, state_root=str(cfg.state_root),
                  protected=protected).serve()


if __name__ == "__main__":
    raise SystemExit(main())
