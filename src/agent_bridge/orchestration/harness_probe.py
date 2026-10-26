"""Broker-owned, non-inference readiness checks for execution harnesses.

No tool, command-line interface, or caller-supplied readiness assertion lives
here. Resolve each CLI once from the broker's execution environment and run
only version and subscription login-status commands in the lane's own store.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping

from .. import runner
from ..execution import claude_config

PROBE_TIMEOUT_SECONDS = 4.0
PROBE_OUTPUT_BYTES = 16 * 1024


class HarnessReadinessProbe:
    def __init__(self, executables: Mapping[str, Path], *, home: Path,
                 env: Mapping[str, str], claude_config_dir: Path | None,
                 run: Callable = runner.run,
                 monotonic: Callable[[], float] = time.monotonic):
        self.executables = dict(executables)
        self.home = home
        # Match the harness's stripped environment: no inherited API keys,
        # plugins, config selectors, or caller-provided CLI arguments.
        self.env = {key: env[key] for key in
                    ("PATH", "USER", "LOGNAME", "SYSTEMROOT", "COMSPEC", "PATHEXT")
                    if key in env}
        self.env.update(HOME=str(home), LANG="C.UTF-8")
        self.claude_config_dir = claude_config_dir
        self.run = run
        self.monotonic = monotonic

    @classmethod
    def for_config(cls, cfg):
        # Native POSIX harness readiness says nothing about the WSL lane.
        executables = {}
        if os.name == "posix":
            for provider in ("claude", "codex"):
                harness = getattr(cfg, f"{provider}_task_executable")
                if (cfg.python_executable and cfg.python_executable.is_file()
                        and os.access(cfg.python_executable, os.X_OK)
                        and harness and harness.is_file()):
                    resolved = shutil.which(provider)
                    if resolved:
                        executables[provider] = Path(resolved).resolve()
        return cls(executables, home=Path.home(), env=os.environ,
                   claude_config_dir=cfg.claude_config_dir)

    def __call__(self, provider: str) -> bool:
        binary = self.executables.get(provider)
        if (binary is None or not binary.is_absolute() or not binary.is_file()
                or not os.access(binary, os.X_OK)):
            return False
        env = dict(self.env)
        if provider == "codex":
            directory = self.home / ".agent-bridge" / "codex-home"
            env["CODEX_HOME"] = str(directory)
            auth = ["login", "status"]
        elif provider == "claude":
            directory = self.claude_config_dir
            if not claude_config.is_ready(directory, home=self.home):
                return False
            env["CLAUDE_CONFIG_DIR"] = str(directory)
            auth = ["--safe-mode", "--setting-sources", "", "auth", "status", "--json"]
        else:
            return False
        if directory is None or not directory.is_dir():
            return False
        deadline = self.monotonic() + PROBE_TIMEOUT_SECONDS
        try:
            for arguments in (["--version"], auth):
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    return False
                result = self.run(
                    [str(binary), *arguments], cwd=str(directory), env=env,
                    stdin_data="", timeout=remaining, deadline=deadline, grace=0,
                    stdout_cap=PROBE_OUTPUT_BYTES, stderr_cap=PROBE_OUTPUT_BYTES)
                if (result.returncode != 0 or result.spawn_failed or result.timed_out
                        or result.cap_exceeded or result.descendant_held_pipes):
                    return False
                if not (result.stdout + result.stderr).strip():
                    return False
            if provider == "codex":
                text = (result.stdout + result.stderr).decode("utf-8", "replace").lower()
                return ("logged in using chatgpt" in text
                        and "not logged in" not in text and "no credentials" not in text)
            status = json.loads(result.stdout)
            return (isinstance(status, dict) and status.get("loggedIn") is True
                    and status.get("authMethod") == "claude.ai"
                    and status.get("subscriptionType") in
                    ("pro", "max", "team", "enterprise", "business"))
        except (OSError, TimeoutError, ValueError, subprocess.SubprocessError):
            return False
