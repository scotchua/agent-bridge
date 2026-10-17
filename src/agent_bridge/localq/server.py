"""Standalone stdio MCP transport for the local mechanical-work queue."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
from pathlib import Path
from typing import Any

from . import mcp
from .intake import AutomaticIntake
from .service import Service

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agent-bridge-localq"
SERVER_VERSION = "0.1.0"
# Bound each pre-parse read.  Do not try to drain an oversized frame: a peer
# that does not send a newline must not make this transport retain unbounded
# input while looking for one.
MAX_FRAME_CHARS = 2 * 1024 * 1024
_HANDLER_ERROR_CODE = "localq_transport_handler_error"
_LOG = logging.getLogger(__name__)
INSTRUCTIONS = (
    "Local mechanical non-client work queue only. It accepts bounded text tasks, has no "
    "cloud fallback, and returns untrusted drafts for review. Confidential or "
    "client-derived material is refused."
)


class Server:
    def __init__(self, service: Service, *, interval: float = 5.0):
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.service, self.interval = service, interval
        self.tools = mcp.build_tools(service.queue)
        self.tools.update(mcp.build_intake_tools(AutomaticIntake(service.queue)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _executor(self) -> None:
        while not self._stop.is_set():
            try:
                self.service.once()
            except Exception:
                self._record_loop_error()
            self._stop.wait(self.interval)

    def _record_loop_error(self) -> None:
        reporter = getattr(self.service, "record_loop_error", None)
        if callable(reporter):
            try:
                reporter()
            except Exception:
                pass

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._executor, name="localq-executor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
            self._thread = None

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) or (isinstance(value, (int, float)) and not isinstance(value, bool) and (not isinstance(value, float) or math.isfinite(value)))

    @staticmethod
    def _content(payload: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
                "structuredContent": payload, "isError": not payload.get("ok", False)}

    def handle(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or not isinstance(request.get("method"), str):
            return self._error(None, -32600, "invalid request")
        request_id = request.get("id")
        if request_id is None:
            return None
        if not self._valid_id(request_id):
            return self._error(None, -32600, "invalid request")
        method = request["method"]
        if method == "initialize":
            result: Any = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method in {"tools/list", "tools/listChanged"}:
            result = {"tools": [{"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
                                for name, spec in self.tools.items()]}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return self._error(request_id, -32602, "invalid params")
            spec = self.tools.get(params["name"])
            if spec is None:
                return self._error(request_id, -32602, "unknown tool")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return self._error(request_id, -32602, "invalid params")
            try:
                result = self._content(spec["handler"](arguments))
            except Exception:  # noqa: BLE001 - a tool fault must not end the MCP session
                # Keep diagnostics content-free: never record exception text or request data.
                _LOG.error(_HANDLER_ERROR_CODE)
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
                    stdout.write(json.dumps(
                        self._error(None, -32600, "request exceeds frame limit")) + "\n")
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
    parser = argparse.ArgumentParser(description="Local queue MCP server. Does not install or activate configuration.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--worker-state", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    if not all(Path(value).is_absolute() for value in (args.root, args.worker, args.worker_state)):
        parser.error("root, worker, and worker-state must be absolute paths")
    return Server(Service(args.root, args.worker, args.worker_state), interval=args.interval).serve()


if __name__ == "__main__":
    raise SystemExit(main())
