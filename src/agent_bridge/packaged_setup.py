"""First-run setup for the Claude Code plugin distribution.

It deliberately configures only Claude -> Codex.  The usual clone onboarding
continues to own two-way registrations and advanced features.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

from . import config, packaged, setup_cmd, store


def _claude_config_paths(home: str) -> tuple[str, ...]:
    desktop_base = os.environ.get("APPDATA", home) if os.name == "nt" else os.path.join(
        home, "Library", "Application Support")
    return (os.path.join(home, ".claude.json"),
            os.path.join(desktop_base, "Claude", "claude_desktop_config.json"))


def _has_codex_peer(servers: Any) -> bool:
    return isinstance(servers, dict) and "codex-peer" in servers


def find_competing_registration(home: str, cwd: str | None = None) -> str | None:
    """Read only the codex-peer key names, never configuration values.

    Covers every scope Claude Code can register a server in: user and
    desktop (top-level mcpServers), local (projects[*].mcpServers in
    ~/.claude.json, any project, since the plugin is enabled everywhere) and
    project (.mcp.json in the working directory).
    """
    project_file = os.path.join(os.path.abspath(cwd or os.getcwd()), ".mcp.json")
    for path in (*_claude_config_paths(home), project_file):
        if not os.path.isfile(path):
            continue
        try:
            raw = store.read_json(path)
        except (OSError, ValueError):
            # A malformed host config cannot establish a safe absence.
            raise ValueError("Claude configuration cannot be read safely to check codex-peer")
        if not isinstance(raw, dict):
            continue
        if _has_codex_peer(raw.get("mcpServers")):
            return path
        projects = raw.get("projects")
        if isinstance(projects, dict) and any(
                isinstance(entry, dict) and _has_codex_peer(entry.get("mcpServers"))
                for entry in projects.values()):
            return path
    return None


def _pin_codex(active: config.Config) -> dict[str, Any]:
    candidates = [path for path in setup_cmd.find_all("codex") if setup_cmd.is_durable(path)]
    codex_home = str(active.peer("codex").get("codex_home", "~/.agent-bridge/codex-home"))
    for path in candidates:
        version = setup_cmd.version_of(path)
        if version and setup_cmd.codex_signed_in(path, codex_home) is True:
            return {"executable": path, "allowed_versions": [version]}
    raise ValueError("Codex needs a durable installed CLI with a confirmed browser login before setup")


def _check_launcher_python(python: str) -> None:
    # The POSIX launcher extracts this value from JSON with sed, so a quote,
    # backslash, control character, or a non-ASCII character (written as a
    # \u escape by the JSON writer) could change what it executes.
    if (not os.path.isabs(python) or not python.isascii() or any(ch in python for ch in '"\\')
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in python)):
        raise ValueError("the Python interpreter path contains characters the plugin launcher cannot use safely")


def build_overlay(active: config.Config, python: str) -> dict[str, Any]:
    _check_launcher_python(python)
    return {"launcher_python": python,
            "peers": {"codex": _pin_codex(active)}}


def _verify_result(path: str, candidate: config.Config) -> None:
    """Validate the full Claude -> Codex live matrix without enabling reverse MCP."""
    result = packaged.read_json(path)
    if not isinstance(result, dict) or result.get("effective_config_sha256") != config.effective_config_sha256(candidate.raw):
        raise ValueError("live verification is not bound to the staged packaged config")
    if result.get("runner_arguments", {}).get("direction") != "claude-to-codex":
        raise ValueError("live verification must test Claude to Codex")
    # The runner's own verdict is not used: its PASS profile requires both
    # directions, so a one-direction run is never PASS.  The same evidence is
    # checked here for the one direction this install enables.
    if result.get("blocked") or result.get("skipped_controls"):
        raise ValueError("live verification was blocked or skipped a control")
    requested = result.get("controls_requested")
    if not isinstance(requested, dict) or not requested or result.get("controls_executed") != requested:
        raise ValueError("live verification did not run every requested control")
    rows, timeouts = result.get("rows"), result.get("timeouts")
    if not isinstance(rows, list) or not isinstance(timeouts, list):
        raise ValueError("live verification is incomplete")
    ordinary = [row for row in rows if isinstance(row, dict)]
    if len([row for row in ordinary if str(row.get("kind", "")).startswith("one-turn ")]) < 10:
        raise ValueError("live verification did not complete ten Claude to Codex calls")
    first_turns = [row for row in ordinary if str(row.get("kind", "")).endswith(" t1")]
    if len(first_turns) < 3 or len([row for row in ordinary if str(row.get("kind", "")) == "schema-pressure"]) < 1:
        raise ValueError("live verification did not complete every required control")
    if not all(row.get("status") == "complete" and row.get("contract_valid") is True
                   for row in ordinary if row.get("kind") != "schema-pressure"):
        raise ValueError("live verification contains an unsuccessful consultation")
    if not all(row.get("acceptable") is True
               for row in ordinary if row.get("kind") == "schema-pressure"):
        raise ValueError("live verification schema-pressure control was not acceptable")
    configured = candidate.peer("codex").get("allowed_versions")
    observed = result.get("observed_versions", {}).get("codex")
    if not isinstance(configured, list) or observed != configured:
        raise ValueError("live verification is not bound to the pinned Codex version")
    # Success for the timeout canary is the runner's "timed_out" with no
    # orphaned process left behind.
    if not timeouts or any(not isinstance(row, dict) or row.get("status") != "timed_out"
                           or row.get("orphans") != 0 for row in timeouts):
        raise ValueError("live verification did not complete the timeout control")


def setup(*, home: str | None = None, verify: bool = False) -> None:
    if not packaged.enabled():
        raise ValueError("packaged setup must be run by the agent-bridge Claude plugin")
    if os.name == "nt":
        # The plugin's MCP command is a POSIX shell launcher, so native Windows
        # would verify a connection Claude Code cannot start.
        raise ValueError("the agent-bridge plugin supports macOS, Linux, and WSL; on native Windows use the clone install in INSTALL.md")
    home = os.path.abspath(os.path.expanduser(home or os.environ.get("HOME", "~")))
    conflict = find_competing_registration(home)
    if conflict:
        raise ValueError("the plugin's codex-peer and an existing codex-peer registration would conflict; keep the clone install, or remove the old registration yourself")
    packaged.prepare(home)
    python = os.path.abspath(sys.executable)
    if sys.version_info < (3, 11) or not os.path.isfile(python):
        raise ValueError("agent-bridge requires Python 3.11 or newer. Install Python 3.11+.")
    # Do not use config.load(): if a prior packaged config is damaged, staging
    # starts from shipped defaults and the private path remains untrusted until
    # it is replaced only after a successful live verification.
    active = config.build_effective()
    overlay = build_overlay(active, python)
    candidate_raw = config.merge(active.raw, overlay)
    candidate_path = packaged.private_path("candidate.json", home)
    results_path = packaged.private_path("canary-results.json", home)
    packaged.write_json(candidate_path, candidate_raw, home=home)
    packaged.validate_for_read(candidate_path)
    candidate = config.load_effective(candidate_path)
    if not verify:
        raise ValueError("live verification is required. Re-run the setup command and confirm the synthetic Claude to Codex verification.")
    # A stale results file is validated (never followed) before the runner
    # replaces it; the runner's atomic writer creates it owner-only.
    if os.path.lexists(results_path):
        packaged.validate_for_read(results_path)
    runner = os.path.join(packaged.plugin_root(), "canaries", "run_canaries.py")
    completed = subprocess.run(
        [python, runner, "--direction", "claude-to-codex", "--config", candidate_path,
         "--out", results_path], check=False, shell=False)
    if completed.returncode != 0:
        raise ValueError("live verification did not pass; see the canary report above")
    packaged.validate_for_read(results_path)
    _verify_result(results_path, candidate)
    packaged.write_json(packaged.config_path(home), overlay, home=home)
    packaged.record_verified(packaged.package_identity(), python=python, home=home)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-bridge setup")
    parser.add_argument("--packaged", action="store_true")
    parser.add_argument("--verify", action="store_true",
                        help="Run the full synthetic Claude to Codex live verification.")
    parser.add_argument("--home")
    args = parser.parse_args(argv)
    if not args.packaged:
        parser.error("only --packaged is supported by this command")
    try:
        setup(home=args.home, verify=args.verify)
    except (OSError, ValueError) as exc:
        print(f"Packaged setup refused: {exc}", file=sys.stderr)
        return 1
    print("Packaged Claude to Codex setup and live verification completed.")
    return 0
