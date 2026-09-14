"""Local CLI health checks, never login, refresh, or inference requests.

Only allowlisted status fields leave the subprocess. A cached login is not a
network check, and a negative result in a sandbox is not proof of revoked auth.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from . import preflight, runner, store
from .config import Config, effective_config_sha256
from .errors import BrokerError


def peer_env(cfg: Config, peer: str) -> dict[str, str]:
    extra = cfg.peer_extra_env(peer)
    if peer == "codex":
        extra["CODEX_HOME"] = os.path.expanduser(cfg.peer(peer)["codex_home"])
    return runner.scrubbed_env(extra)


def auth_status(peer: str, executable: str, env: dict[str, str]) -> dict[str, Any]:
    argv = [executable, *(["--safe-mode", "--setting-sources", "", "auth", "status"]
                         if peer == "claude" else ["login", "status"])]
    # No caller cwd, stdin, raw output, or credentials enter the report.
    with tempfile.TemporaryDirectory(prefix="agent-bridge-health-") as cwd:
        result = runner.run(argv, cwd=cwd, env=env, stdin_data="", timeout=20,
                            grace=1, stdout_cap=32768, stderr_cap=32768)
    report: dict[str, Any] = {"state": "unknown", "returncode": result.returncode,
                              "network_verified": False}
    if result.spawn_failed or result.timed_out or result.cap_exceeded or result.descendant_held_pipes:
        report["probe_error"] = ("spawn_failed" if result.spawn_failed else
                                 "timeout" if result.timed_out else "output_incomplete")
        return report
    if peer == "claude":
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            return report
        if not isinstance(value, dict):
            return report
        signed = value.get("loggedIn")
        if signed is True and result.returncode == 0:
            report["state"] = "signed_in"
        elif signed is False and result.returncode in (0, 1):
            report["state"] = "not_signed_in"
        # Never forward account IDs, email addresses, or unknown values.
        for key, allowed in {
            "authMethod": ("claude.ai", "api_key", "none"),
            "apiProvider": ("firstParty", "bedrock", "vertex", "foundry"),
            "subscriptionType": ("free", "pro", "max", "team", "enterprise"),
        }.items():
            if value.get(key) in allowed:
                report[key] = value[key]
    else:
        text = (result.stdout + result.stderr).decode("utf-8", "replace").lower()
        if "not logged in" in text or "no credentials" in text:
            report["state"] = "not_signed_in"
        elif result.returncode == 0 and "logged in using chatgpt" in text:
            report.update(state="signed_in", authMethod="chatgpt")
        elif result.returncode == 0 and "logged in using an api key" in text:
            report.update(state="signed_in", authMethod="api_key")
    return report


def inspect_peer(cfg: Config, peer: str) -> dict[str, Any]:
    spec = cfg.peer(peer)
    env = peer_env(cfg, peer)
    path_cli = preflight.discover_executable(peer)
    executable = spec.get("executable") or path_cli
    report: dict[str, Any] = {
        "peer": peer, "executable": executable,
        "realpath": os.path.realpath(executable) if executable else None,
        "path_executable": path_cli,
        "path_differs": bool(path_cli and executable and
                             os.path.realpath(path_cli) != os.path.realpath(executable)),
        "allowed_versions": spec.get("allowed_versions") or [],
        "context": runner.credential_context(env),
        "auth": {"state": "not_checked", "network_verified": False},
    }
    validated: dict[str, Any] | None = None
    try:
        # check_peer runs the configured executable's pinned version command.
        # PATH discovery is reported for diagnostics only and is never run.
        validated = preflight.check_peer(cfg, peer)
        if peer == "codex":
            preflight.assert_peer_home_has_no_config(env["CODEX_HOME"])
        report.update(
            executable=validated["executable"],
            realpath=validated["realpath"],
            observed_version=validated["observed_version"],
        )
        report["preflight"] = "ok"
    except BrokerError as exc:
        report["preflight"] = exc.category.value
    if report["preflight"] == "ok":
        validated_target = validated["realpath"]
        report["auth"] = auth_status(peer, validated_target, env)
        report["operator_login_argv"] = [
            validated_target,
            *(["auth", "login", "--claudeai"] if peer == "claude" else ["login"]),
        ]
    else:
        report["operator_login_argv"] = None
    if peer == "claude":
        storage = env.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        if storage is None:
            storage = env.get("CLAUDE_CONFIG_DIR")
        storage = storage or os.path.join(env.get("HOME", os.path.expanduser("~")), ".claude")
        report["fallback_file_present"] = os.path.isfile(os.path.join(storage, ".credentials.json"))
        report["recovery"] = (
            "If sign-in is absent, repeat this health check in a normal Terminal with "
            "macOS Keychain access before changing the login. If it works there, "
            "investigate the launching process's Keychain access. If it fails there too, "
            "an operator can authorize login using the pinned executable and the context above. "
            "Repeated refresh failures need investigation; restarting desktop apps is not a repair."
        )
    else:
        report["auth_file_present"] = os.path.isfile(os.path.join(env["CODEX_HOME"], "auth.json"))
        report["recovery"] = (
            "Use the isolated CODEX_HOME shown above when checking or authorizing login. "
            "The desktop/default-home login is separate. File presence alone does not "
            "prove a usable token. Do not copy credentials between homes."
        )
    return report


def cmd_health(cfg: Config, args: Any) -> int:
    peers = (args.peer,) if args.peer else ("claude", "codex")
    reports = [inspect_peer(cfg, peer) for peer in peers]
    report = {"checked_at": store.utc_now(),
              "effective_config_sha256": effective_config_sha256(cfg.raw),
              "scope": "local credential visibility; no network or token-refresh test",
              "peers": reports}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Bridge health: local checks only; a live consultation is needed to verify connectivity.")
        for row in reports:
            print(f"\n{row['peer']}: {row['auth']['state']} (preflight: {row['preflight']})")
            print(f"  Bridge CLI: {row['executable']} ({row.get('observed_version', 'unknown')})")
            if row["path_differs"]:
                print(f"  Different PATH CLI: {row['path_executable']}")
                print("  The bridge keeps its tested pin; it does not follow PATH upgrades.")
            print(f"  Credential context: {json.dumps(row['context'], sort_keys=True)}")
            print(f"  {row['recovery']}")
        print("\nNo login or configuration changes were requested by this check.")
    return 0 if all(r["preflight"] == "ok" and r["auth"]["state"] == "signed_in"
                    for r in reports) else 1
