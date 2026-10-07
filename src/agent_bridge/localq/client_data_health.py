"""Fail-closed health and delegate binding check for client-derived work.

The command and every path here come from protected operator configuration.
This module deliberately has no request-facing inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


HEALTH_UNVERIFIED = "client_data_health_unverified"
HEALTH_FAILED = "client_data_health_failed"
HEALTH_BINDING_MISMATCH = "client_data_health_binding_mismatch"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_STATUS_LIMIT = 4096
_WRAPPER_FILES = ("delegate.py", "local_endpoint.py", "config.json")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


class ClientDataHealthGate:
    """Run the operator's Ollama check and bind it to the pinned delegate."""

    def __init__(self, *, command: tuple[str, ...], path: str, timeout_seconds: float,
                 record: Path, delegate_executable: Path | None,
                 delegate_sha256: str | None):
        self.command = command
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.record = record
        self.delegate_executable = delegate_executable
        self.delegate_sha256 = delegate_sha256

    @classmethod
    def from_config(cls, cfg: Any) -> "ClientDataHealthGate | None":
        """Build exclusively from protected configuration.

        All health fields being absent is the intentional unconfigured state.
        Any other incomplete or malformed set is an error, so callers cannot
        mistake a partly-read configuration for a working gate.
        """
        names = ("client_data_health_command", "client_data_health_path",
                 "client_data_health_timeout_seconds", "client_data_health_record")
        values = [getattr(cfg, name, None) for name in names]
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            # ``OrchestrationConfig`` deliberately retains the documented
            # timeout default when the whole optional group is absent.  The
            # loader has already rejected a JSON document that supplied only
            # this key, so these three absent paths identify that unconfigured
            # dataclass representation rather than a permissive partial gate.
            if (values[0] is None and values[1] is None and values[3] is None
                    and values[2] == 30.0):
                return None
            raise ValueError("client_data_health_configuration_incomplete")
        command, path, timeout, record = values
        if (not isinstance(command, (tuple, list)) or not command
                or not all(isinstance(part, str) and part for part in command)
                or not os.path.isabs(command[0]) or tuple(command[-2:]) != ("status", "--json")):
            raise ValueError("client_data_health_command_invalid")
        if not isinstance(path, str) or not path:
            raise ValueError("client_data_health_path_invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("client_data_health_timeout_seconds_invalid")
        if not isinstance(record, Path) or not record.is_absolute():
            raise ValueError("client_data_health_record_invalid")
        return cls(command=tuple(command), path=path, timeout_seconds=float(timeout), record=record,
                   delegate_executable=getattr(cfg, "gemma_delegate_executable", None),
                   delegate_sha256=getattr(cfg, "gemma_delegate_sha256", None))

    @staticmethod
    def _kill_group(proc: subprocess.Popen[bytes]) -> None:
        if os.name != "posix":  # pragma: no cover - Popen handles Windows process termination
            proc.kill()
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _status(self) -> tuple[int, bytes] | None:
        """Return a bounded status response, or None for any check failure.

        A reader thread permits a strict output bound without letting a child
        that keeps stdout open evade the command timeout.  On every abnormal
        path the isolated process group is killed and reaped.
        """
        try:
            proc = subprocess.Popen(
                self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, start_new_session=True,
                env={"PATH": self.path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
        except (OSError, ValueError):
            return None
        assert proc.stdout is not None
        response: list[bytes] = []
        reader = threading.Thread(target=lambda: response.append(proc.stdout.read(_STATUS_LIMIT)), daemon=True)
        started = time.monotonic()
        reader.start()
        reader.join(self.timeout_seconds)
        if reader.is_alive():
            self._kill_group(proc)
            proc.wait()
            reader.join()
            proc.stdout.close()
            return None
        output = response[0] if response else b""
        # A valid fixed-shape status object is far smaller than this limit.
        # Treat an exactly-full bounded read as oversize too: it may be the
        # prefix of a longer stream, and we intentionally never read beyond
        # the stated 4096-byte maximum to find out.
        if len(output) >= _STATUS_LIMIT:
            self._kill_group(proc)
            proc.wait()
            proc.stdout.close()
            return None
        remaining = self.timeout_seconds - (time.monotonic() - started)
        try:
            proc.wait(timeout=max(0.0, remaining))
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            proc.wait()
            proc.stdout.close()
            return None
        code = proc.returncode
        proc.stdout.close()
        return code, output

    def _binding_reason(self, reported_hash: str) -> str | None:
        try:
            record_bytes = self.record.read_bytes()
        except OSError:
            return HEALTH_BINDING_MISMATCH
        if hashlib.sha256(record_bytes).hexdigest() != reported_hash:
            return HEALTH_BINDING_MISMATCH
        try:
            record = json.loads(record_bytes)
        except (TypeError, ValueError):
            return HEALTH_BINDING_MISMATCH
        if (not isinstance(record, dict) or record.get("state") != "verified"
                or record.get("full_passed") is not True):
            return HEALTH_BINDING_MISMATCH
        hashes = record.get("wrapper_hashes")
        if not isinstance(hashes, dict) or set(hashes) != set(_WRAPPER_FILES):
            return HEALTH_BINDING_MISMATCH
        if not isinstance(self.delegate_executable, Path) or not _is_sha256(self.delegate_sha256):
            return HEALTH_BINDING_MISMATCH
        try:
            actual = {
                name: hashlib.sha256((self.delegate_executable.parent / name).read_bytes()).hexdigest()
                for name in _WRAPPER_FILES
            }
        except OSError:
            return HEALTH_BINDING_MISMATCH
        if any(hashes.get(name) != actual[name] for name in _WRAPPER_FILES):
            return HEALTH_BINDING_MISMATCH
        if hashes["delegate.py"] != self.delegate_sha256:
            return HEALTH_BINDING_MISMATCH
        return None

    def check(self) -> str | None:
        """Return a fixed refusal reason, or ``None`` only for a full pass."""
        status = self._status()
        if status is None:
            return HEALTH_UNVERIFIED
        code, output = status
        try:
            value = json.loads(output)
        except (TypeError, ValueError):
            return HEALTH_UNVERIFIED
        if (not isinstance(value, dict) or set(value) != {"schema", "result", "record_sha256"}
                or type(value.get("schema")) is not int or value["schema"] != 1
                or value.get("result") not in {"verified", "not verified", "failed"}
                or (value["record_sha256"] is not None and not _is_sha256(value["record_sha256"]))):
            return HEALTH_UNVERIFIED
        result, record_hash = value["result"], value["record_sha256"]
        if code == 2 and result == "failed":
            return HEALTH_FAILED
        if code == 1 and result == "not verified":
            return HEALTH_UNVERIFIED
        if code != 0 or result != "verified" or not _is_sha256(record_hash):
            return HEALTH_UNVERIFIED
        return self._binding_reason(record_hash)
