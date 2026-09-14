"""Guided, opt-in "automatic delegation" for the additive orchestration MCP.

Everything here is inert until the guided onboarding flow's explicit
``automatic_delegation.enabled`` opt-in is set and its evidence gate passes.
This module never contacts a model, never applies a patch, never commits,
pushes or merges, and never installs or activates anything as root. It builds
a private, absolute-path orchestration configuration outside the checkout,
computes the caller-bound MCP registrations, renders a safe per-user macOS
LaunchAgent template, and validates offline-checkable evidence that the
synthetic verification directions actually ran and stayed inside their
bounds.

Live activation (loading the LaunchAgent, invoking a provider CLI) always
requires the coordinator's own machine: it cannot be proven from a file
alone, so every function here either reports a durable, checkable fact or
refuses to claim one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from .. import store

CONFIG_VERSION = "1"
LAUNCH_AGENT_LABEL = "com.agent-bridge.execution-worker"
VERIFICATION_PROFILE = "automatic-delegation-v1"
DIRECTIONS = ("codex->claude", "claude->codex")
PROVIDER_FOR_CALLER = {"codex": "claude", "claude": "codex"}
NO_WORKER_SENTINEL = "no-local-worker-configured"


class DelegationConfigError(ValueError):
    """The orchestration configuration cannot be built or trusted."""


class DelegationVerificationError(ValueError):
    """Delegation evidence is missing, malformed, or does not prove a pass."""


def _portable_python() -> str:
    # Framework and package-manager Python launchers are commonly symlinks.
    # Pin the resolved executable so later availability checks and the
    # background service are bound to a real file rather than rejecting a
    # normal, durable installation.
    executable = os.path.realpath(os.path.abspath(sys.executable))
    if os.name == "nt" and "windowsapps" in executable.replace("\\", "/").lower():
        raise DelegationConfigError(
            "the Microsoft Store WindowsApps Python alias cannot be used as a durable launcher")
    return executable


def private_root(home: str) -> str:
    """Everything this feature writes lives here, outside the checkout."""
    return os.path.join(os.path.abspath(home), ".agent-bridge", "orchestration")


def paths_for(home: str, root: str) -> dict[str, str]:
    """Every path this feature needs, fully resolved and absolute."""
    base = private_root(home)
    state = os.path.join(base, "state")
    return {
        "base": base,
        "config": os.path.join(base, "orchestration.json"),
        "state_root": state,
        "local_queue_root": os.path.join(state, "local-queue"),
        "capacity_db": os.path.join(state, "capacity.sqlite3"),
        "worker_state": os.path.join(state, "local-worker-state"),
        "execution_queue_root": os.path.join(state, "execution-queue"),
        "codex_task_executable": os.path.realpath(
            os.path.join(root, "src", "agent_bridge", "execution", "codex_task.py")),
        "claude_task_executable": os.path.realpath(
            os.path.join(root, "src", "agent_bridge", "execution", "claude_task.py")),
    }


def build_config(home: str, root: str, *, local_worker_executable: str | None,
                  interval_seconds: float = 5.0) -> dict[str, Any]:
    """Return the complete private orchestration config document.

    Every value is an absolute path. Nothing here is a credential, token, or
    account secret: provider sign-in keeps using each CLI's own
    subscription-backed session.
    """
    paths = paths_for(home, root)
    worker_executable = (os.path.abspath(os.path.expanduser(local_worker_executable))
                         if local_worker_executable else
                         os.path.join(paths["base"], NO_WORKER_SENTINEL))
    return {
        "config_version": CONFIG_VERSION,
        "state_root": paths["state_root"],
        "local_queue_root": paths["local_queue_root"],
        "capacity_db": paths["capacity_db"],
        "worker_executable": worker_executable,
        "worker_state": paths["worker_state"],
        "execution_queue_root": paths["execution_queue_root"],
        "codex_task_executable": paths["codex_task_executable"],
        "claude_task_executable": paths["claude_task_executable"],
        "python_executable": _portable_python(),
        "interval_seconds": float(interval_seconds),
    }


def render_config_bytes(cfg: dict[str, Any]) -> bytes:
    import json
    return (json.dumps(cfg, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def config_sha256(cfg: dict[str, Any]) -> str:
    return store.sha256_bytes(render_config_bytes(cfg))


def harness_availability(cfg: dict[str, Any]) -> dict[str, Any]:
    """Pure filesystem check. Never executes anything, never contacts a model."""
    def usable_file(path: Any) -> bool:
        return (isinstance(path, str) and os.path.isabs(path)
                and os.path.isfile(path) and not os.path.islink(path))

    python_ok = usable_file(cfg.get("python_executable")) and os.access(cfg["python_executable"], os.X_OK)
    codex_ok = usable_file(cfg.get("codex_task_executable"))
    claude_ok = usable_file(cfg.get("claude_task_executable"))
    worker_path = cfg.get("worker_executable")
    worker_configured = (usable_file(worker_path)
                         and os.path.basename(str(worker_path)) != NO_WORKER_SENTINEL)
    return {
        "python": python_ok,
        "codex_task_executable": codex_ok,
        "claude_task_executable": claude_ok,
        "execution_complete": python_ok and codex_ok and claude_ok,
        "local_worker_configured": worker_configured,
    }


def platform_boundary_report(platform_name: str | None = None) -> dict[str, Any]:
    """State the real, independently-verified support boundary per platform.

    Never claims a continuously installed execution-worker service on a
    platform where that has not been independently verified.
    """
    name = platform_name if platform_name is not None else sys.platform
    if name == "darwin":
        return {
            "platform": "darwin",
            "execution_worker_service": "guided per-user LaunchAgent in the login GUI domain; never installed as root",
            "continuous_service_verified": True,
            "resource_sampler": "macOS-specific sampler available",
            "registration_and_config": "supported",
        }
    if name.startswith("win"):
        return {
            "platform": "windows",
            "execution_worker_service": "not installed automatically; no continuously-running service is claimed",
            "continuous_service_verified": False,
            "resource_sampler": "no independently reviewed sampler; local-model jobs defer rather than assume spare capacity",
            "registration_and_config": "supported (portable MCP registration and private config generation)",
        }
    return {
        "platform": "linux_or_other",
        "execution_worker_service": "not installed automatically; no continuously-running service is claimed",
        "continuous_service_verified": False,
        "resource_sampler": "no independently reviewed sampler; local-model jobs defer rather than assume spare capacity",
        "registration_and_config": "supported (portable MCP registration and private config generation)",
    }


# ---------------------------------------------------------------------------
# Offline-checkable evidence gate for the three synthetic verification runs.
# ---------------------------------------------------------------------------

REQUIRED_SAFETY_FLAGS = (
    "no_patches_applied", "no_commits", "no_pushes", "no_merges",
    "no_paid_fallback", "no_client_data",
)
REQUIRED_TOP_KEYS = frozenset({
    "verification_profile", "effective_config_sha256", "created_at",
    "directions", "local_model", *REQUIRED_SAFETY_FLAGS,
})
_DIRECTION_ROW_KEYS = frozenset({
    "attempted", "reason", "state", "returncode", "source_classification",
    "worktree_removed", "permission_to_apply", "permission_to_commit",
    "permission_to_push", "permission_to_merge",
})


def _direction_status(direction: str, row: Any) -> str:
    if not isinstance(row, dict) or set(row) != _DIRECTION_ROW_KEYS:
        raise DelegationVerificationError(f"delegation evidence for {direction} has an unexpected shape")
    if row.get("attempted") is not True:
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason:
            raise DelegationVerificationError(f"delegation evidence for {direction} must name a reason when not attempted")
        return "blocked:" + reason
    if (row.get("state") != "complete" or row.get("returncode") != 0
            or row.get("source_classification") != "synthetic"
            or row.get("worktree_removed") is not True
            or any(row.get(key) is not False for key in
                   ("permission_to_apply", "permission_to_commit", "permission_to_push", "permission_to_merge"))):
        raise DelegationVerificationError(f"delegation evidence for {direction} is not an acceptable pass")
    return "enabled"


def validate_evidence(results: Any, cfg: dict[str, Any], *,
                       required_directions: tuple[str, ...],
                       local_worker_required: bool) -> dict[str, str]:
    """Return a per-lane status, or refuse evidence that does not prove a pass.

    This checks a local report for internal consistency and required shape.
    It does not, and cannot, prove the report was produced by a genuine live
    run rather than manufactured locally; that trust boundary is the same one
    the existing canary-evidence gate already accepts.
    """
    if not isinstance(results, dict) or set(results) != REQUIRED_TOP_KEYS:
        raise DelegationVerificationError("delegation results have an unexpected shape")
    if results.get("verification_profile") != VERIFICATION_PROFILE:
        raise DelegationVerificationError("delegation results use an unsupported verification profile")
    if results.get("effective_config_sha256") != config_sha256(cfg):
        raise DelegationVerificationError("delegation results do not match the staged orchestration config")
    for flag in REQUIRED_SAFETY_FLAGS:
        if results.get(flag) is not True:
            raise DelegationVerificationError(f"delegation results do not confirm {flag}")
    directions = results.get("directions")
    if not isinstance(directions, dict) or set(directions) != set(DIRECTIONS):
        raise DelegationVerificationError("delegation results must report both directions")
    if not required_directions or any(d not in DIRECTIONS for d in required_directions):
        raise DelegationVerificationError("required_directions must be a non-empty subset of the two directions")

    status: dict[str, str] = {}
    for direction in DIRECTIONS:
        if direction in required_directions:
            status[direction] = _direction_status(direction, directions[direction])
        else:
            status[direction] = "not_required"

    local_model = results.get("local_model")
    if not isinstance(local_model, dict) or "status" not in local_model:
        raise DelegationVerificationError("delegation results local_model entry is invalid")
    if local_worker_required:
        if (local_model.get("status") != "complete"
                or local_model.get("source_classification") != "synthetic"):
            raise DelegationVerificationError("local model lane was not proven complete")
        status["local_model"] = "enabled"
    else:
        if local_model.get("status") != "not_configured":
            raise DelegationVerificationError(
                "local model lane must be reported not_configured when no worker executable was supplied")
        status["local_model"] = "not_configured"

    # "not_required" (an unselected direction) and "not_configured" (an
    # intentionally-omitted local worker) are both a user choice, not a
    # failure, and must not turn an otherwise-complete pass into "partial".
    graded = [value for value in status.values() if value not in ("not_required", "not_configured")]
    if all(value == "enabled" for value in graded):
        overall = "enabled"
    elif any(value == "enabled" for value in graded):
        overall = "partial"
    else:
        overall = "blocked"
    status["overall"] = overall
    return status


def required_directions_for(callers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{caller}->{PROVIDER_FOR_CALLER[caller]}" for caller in callers)


# ---------------------------------------------------------------------------
# macOS per-user execution-worker LaunchAgent: render, then guide or install.
# ---------------------------------------------------------------------------

def launch_agent_path(home: str) -> str:
    return os.path.join(os.path.abspath(home), "Library", "LaunchAgents", LAUNCH_AGENT_LABEL + ".plist")


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_launch_agent(*, worker_binary: str, config_path: str, python_executable: str,
                         account: str, claude_bin_dir: str, codex_bin_dir: str | None = None,
                         stdout_log: str, stderr_log: str) -> bytes:
    """Render the safe template with resolved absolute paths. No credentials.

    ``codex_bin_dir`` defaults to ``claude_bin_dir`` when omitted or identical,
    which keeps the common case (both CLIs installed to the same bin
    directory) a single path. When the two CLIs live in different
    directories, both must be on the worker's PATH: `codex_task.py` and
    `claude_task.py` each fall back to a bare `shutil.which()` lookup for
    their own provider's executable when nothing is pinned, and a directory
    left off `PATH` here means that lookup fails closed with a clear
    "executable unavailable" error rather than silently searching elsewhere.
    """
    codex_bin_dir = codex_bin_dir or claude_bin_dir
    for name, value in (("worker_binary", worker_binary), ("config_path", config_path),
                        ("python_executable", python_executable),
                        ("claude_bin_dir", claude_bin_dir), ("codex_bin_dir", codex_bin_dir),
                        ("stdout_log", stdout_log), ("stderr_log", stderr_log)):
        if not isinstance(value, str) or not os.path.isabs(value):
            raise DelegationConfigError(f"launch agent field {name!r} must be an absolute path")
    if not account or "/" in account or "\\" in account:
        raise DelegationConfigError("launch agent account must be a bare account short name")
    e = _xml_escape
    bin_dirs = [claude_bin_dir] + ([codex_bin_dir] if codex_bin_dir != claude_bin_dir else [])
    bin_path = ":".join(e(p) for p in bin_dirs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'  <key>Label</key>\n  <string>{LAUNCH_AGENT_LABEL}</string>\n'
        '  <key>ProgramArguments</key>\n  <array>\n'
        f'    <string>{e(worker_binary)}</string>\n'
        '    <string>--config</string>\n'
        f'    <string>{e(config_path)}</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        '  <key>ProcessType</key><string>Background</string>\n'
        '  <key>EnvironmentVariables</key>\n  <dict>\n'
        '    <key>AGENT_BRIDGE_PYTHON</key>\n'
        f'    <string>{e(python_executable)}</string>\n'
        '    <key>USER</key>\n'
        f'    <string>{e(account)}</string>\n'
        '    <key>LOGNAME</key>\n'
        f'    <string>{e(account)}</string>\n'
        '    <key>PATH</key>\n'
        f'    <string>{bin_path}:/usr/bin:/bin:/usr/sbin:/sbin</string>\n'
        '  </dict>\n'
        '  <key>StandardOutPath</key>\n'
        f'  <string>{e(stdout_log)}</string>\n'
        '  <key>StandardErrorPath</key>\n'
        f'  <string>{e(stderr_log)}</string>\n'
        '  <key>Umask</key><integer>63</integer>\n'
        '</dict>\n</plist>\n'
    )
    return xml.encode("utf-8")


def _launchctl(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, timeout=timeout,
                          check=False, shell=False)


def launch_agent_status(home: str) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "unsupported_platform"}
    uid = os.getuid()
    target = f"gui/{uid}/{LAUNCH_AGENT_LABEL}"
    try:
        probe = _launchctl(["print", target])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "error": type(exc).__name__}
    return {"status": "active" if probe.returncode == 0 else "inactive", "target": target}


def activate_launch_agent(plist_path: str, *, apply: bool = False) -> dict[str, Any]:
    """Guide, or (with --apply) load, the per-user LaunchAgent. Never as root.

    Idempotent: an already-active agent is reported as such and never
    re-bootstrapped. Without ``apply`` this only stages the exact command; it
    never claims success it has not independently checked.
    """
    if sys.platform != "darwin":
        return {"status": "unsupported_platform"}
    if os.getuid() == 0:
        return {"status": "refused_root",
                "detail": "load this as the logged-in user's GUI launchd domain, never as root"}
    if not os.path.isabs(plist_path) or not os.path.isfile(plist_path):
        return {"status": "plist_missing", "plist_path": plist_path}
    uid = os.getuid()
    domain = f"gui/{uid}"
    target = f"{domain}/{LAUNCH_AGENT_LABEL}"
    command = ["launchctl", "bootstrap", domain, plist_path]
    try:
        probe = _launchctl(["print", target])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "activation_failed", "error": type(exc).__name__}
    if probe.returncode == 0:
        return {"status": "already_active", "target": target}
    if not apply:
        return {"status": "staged", "command": command}
    try:
        result = _launchctl(["bootstrap", domain, plist_path])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "activation_failed", "command": command, "error": type(exc).__name__}
    if result.returncode != 0:
        return {"status": "activation_failed", "command": command,
                "returncode": result.returncode,
                "stderr": result.stderr.decode("utf-8", "replace")[:2000]}
    return {"status": "activated", "target": target}


def deactivate_launch_agent(plist_path: str, *, apply: bool = False) -> dict[str, Any]:
    """Symmetric removal, used by the existing uninstall path. Never as root."""
    if sys.platform != "darwin":
        return {"status": "unsupported_platform"}
    if os.getuid() == 0:
        return {"status": "refused_root"}
    uid = os.getuid()
    domain = f"gui/{uid}"
    target = f"{domain}/{LAUNCH_AGENT_LABEL}"
    try:
        probe = _launchctl(["print", target])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "deactivation_failed", "error": type(exc).__name__}
    if probe.returncode != 0:
        return {"status": "already_inactive"}
    command = ["launchctl", "bootout", target]
    if not apply:
        return {"status": "staged", "command": command}
    try:
        result = _launchctl(command[1:])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "deactivation_failed", "error": type(exc).__name__}
    if result.returncode != 0:
        return {"status": "deactivation_failed", "returncode": result.returncode,
                "stderr": result.stderr.decode("utf-8", "replace")[:2000]}
    return {"status": "deactivated"}
