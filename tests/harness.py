"""Test harness: an isolated broker instance backed by the fake executables."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from agent_bridge import broker, config, registry, store  # noqa: E402

FAKES = os.path.join(REPO, "tests", "fakes")

# Mirror production: the MCP server and worker both set this.
store.set_umask()

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"   [{detail}]" if not condition and detail else ""))
    return condition


class Sandbox:
    """A throwaway state root plus a config pointed at the fake executables."""

    def __init__(self, **overrides: Any):
        self.root = tempfile.mkdtemp(prefix="agent-bridge-test-")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state, mode=0o700, exist_ok=True)
        base = json.load(open(os.path.join(REPO, "config", "broker.json")))
        base["state_root"] = self.state
        # The fakes are .py files with a shebang. Windows cannot execute those
        # directly, so a peer invocation would fail with WinError 193 before
        # any bridge logic ran. Wrap them in .cmd shims there. This is a test
        # fixture concern only: a real peer is a native executable, so no
        # production code needs to know about it.
        fake_claude = self._executable_for(os.path.join(FAKES, "fake_claude.py"))
        fake_codex = self._executable_for(os.path.join(FAKES, "fake_codex.py"))
        base["peers"]["claude"].update({
            "executable": fake_claude,
            "allowed_versions": ["2.1.229 (Claude Code)"],
            "timeout_seconds": 20, "grace_seconds": 1,
        })
        base["peers"]["codex"].update({
            "executable": fake_codex,
            "allowed_versions": ["codex-cli 0.147.0"],
            "codex_home": os.path.join(self.state, "codex-home"),
            "timeout_seconds": 20, "grace_seconds": 1,
        })
        for key, value in overrides.items():
            if key == "limits":
                base["limits"].update(value)
            elif key == "retention":
                base["retention"].update(value)
            elif key == "worker":
                base.setdefault("worker", {}).update(value)
            elif key.startswith("claude."):
                base["peers"]["claude"][key.split(".", 1)[1]] = value
            elif key.startswith("codex."):
                base["peers"]["codex"][key.split(".", 1)[1]] = value
            else:
                base[key] = value
        self.config_path = os.path.join(self.root, "broker.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(base, handle, indent=2)
        self.cfg = config.load(self.config_path)

    def _executable_for(self, script: str) -> str:
        """A path this OS can actually execute for a Python test fake."""
        if os.name != "nt":
            return script
        shim = os.path.join(self.root, os.path.basename(script) + ".cmd")
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write(
                "@echo off\r\n"
                f'"{sys.executable}" "{script}" %*\r\n')
        return shim

    def env(self, **extra: str) -> None:
        """Route FAKE_* control vars to the peer's declared extra_env.

        Deliberately not os.environ: the worker and the peer both run with a
        scrubbed environment, so a test that leaned on inheritance would be
        testing a path production does not have.  Driving the fakes through
        declared config exercises the same code path a real deployment uses.
        """
        base = json.load(open(self.config_path))
        for key, value in extra.items():
            peer = "claude" if key.startswith("FAKE_CLAUDE") else "codex"
            base["peers"][peer].setdefault("extra_env", {})[key] = value
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(base, handle, indent=2)
        self.cfg = config.load(self.config_path)

    def clear_env(self) -> None:
        for key in list(os.environ):
            if key.startswith(("FAKE_CLAUDE", "FAKE_CODEX")):
                del os.environ[key]

    def marker(self, name: str) -> str:
        return os.path.join(self.root, name)

    def wait(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = registry.reconcile(self.cfg, job_id)
            if status.get("status") in registry.TERMINAL_STATUSES:
                return status
            time.sleep(0.1)
        return registry.reconcile(self.cfg, job_id)

    def consult(self, caller: str, prompt: str = "Is this design sound?",
                classification: str = "internal", **extra: Any) -> dict[str, Any]:
        args = {"prompt": prompt, "source_classification": classification, **extra}
        return broker.start(self.cfg, caller, args)

    def run_to_completion(self, caller: str, **kwargs: Any) -> tuple[dict, dict, dict]:
        started = self.consult(caller, **kwargs)
        status = self.wait(started["job_id"])
        result = broker.read(self.cfg, caller, {"job_id": started["job_id"]})
        return started, status, result

    def provenance(self, job_id: str) -> dict[str, Any]:
        return store.read_json(os.path.join(self.cfg.job_dir(job_id), "provenance.json"))

    def ledger(self) -> list[dict[str, Any]]:
        path = self.cfg.ledger_path
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def mcp(self, caller: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drive the MCP server as a real subprocess over stdio."""
        payload = "".join(json.dumps(m) + "\n" for m in messages)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(REPO, "src")
        proc = subprocess.run(
            [sys.executable, "-m", "agent_bridge.mcp_server",
             "--caller", caller, "--config", self.config_path],
            input=payload.encode(), capture_output=True, cwd=REPO, env=env, timeout=60,
        )
        out = []
        for line in proc.stdout.decode().splitlines():
            if line.strip():
                out.append(json.loads(line))
        if not out:
            # The server produced nothing, which means it refused to start.
            # Surface why. Without this the caller sees an IndexError from an
            # empty list and learns nothing about the actual cause.
            raise AssertionError(
                "MCP server produced no response.\n"
                f"  exit code: {proc.returncode}\n"
                f"  stderr: {proc.stderr.decode(errors='replace')[:2000]}\n"
                f"  stdout: {proc.stdout.decode(errors='replace')[:500]}")
        return out

    def cleanup(self) -> None:
        self.clear_env()
        shutil.rmtree(self.root, ignore_errors=True)


def summary(skipped: list[str] | None = None) -> int:
    print()
    skipped = skipped or []
    print(f"passed: {len(PASSED)}   failed: {len(FAILED)}   skipped: {len(skipped)}")
    if skipped:
        print("\nSKIPPED (environmental, not a pass):")
        for item in skipped:
            print(f"  - {item}")
    if FAILED:
        print("\nFAILURES:")
        for name, detail in FAILED:
            print(f"  - {name}: {detail}")
        return 1
    return 0
