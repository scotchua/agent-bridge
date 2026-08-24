"""Runtime verification of the peer CLIs.

Re-verified on every job, not once at install time.  A version drift fails the
job closed with a deterministic category rather than silently running an
unpinned binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from . import store
from .config import Config
from .errors import BrokerError, ErrorCategory

def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def _record_contamination(record_to: str | None, detail: dict[str, Any]) -> None:
    """Write the specifics for an operator. Never raises, never reaches a caller."""
    if not record_to:
        return
    try:
        from . import store
        store.atomic_write_json(record_to, detail)
    except Exception:  # noqa: BLE001 - diagnostics must not break the refusal
        pass


#: Instruction files that would give a peer a project-level directive.
CONTAMINANTS = ("agents.md", ".rules", "claude.md", ".codexrules")


def observed_version(
    executable: str,
    version_argv: list[str],
    timeout: int = 20,
    extra_env: dict[str, str] | None = None,
) -> str:
    try:
        from . import runner
        proc = subprocess.run(
            [executable, *version_argv],
            capture_output=True, timeout=timeout, check=False, shell=False,
            env=runner.scrubbed_env(extra_env),
        )
    except FileNotFoundError as exc:
        raise BrokerError(ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrokerError(ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING) from exc
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        text = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return text.splitlines()[0].strip() if text else ""


#: What each peer's CLI is called on PATH, when the config does not pin a path.
DEFAULT_EXECUTABLE_NAMES = {"claude": "claude", "codex": "codex"}


def discover_executable(peer: str) -> str | None:
    """Find a peer CLI on PATH. Used when the config pins no absolute path.

    Pinning an absolute path is still the safer choice, and `agent-bridge-setup`
    writes one. Discovery exists so the bridge runs on a machine it has never
    seen without anyone editing a tracked file first.
    """
    name = DEFAULT_EXECUTABLE_NAMES.get(peer)
    return shutil.which(name) if name else None


def check_peer(cfg: Config, peer: str) -> dict[str, Any]:
    """Verify the peer executable exists and its version is in the allow-list."""
    spec = cfg.peer(peer)
    executable = spec.get("executable") or discover_executable(peer) or ""
    if not executable or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        raise BrokerError(ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING)
    version = observed_version(
        executable,
        list(spec.get("version_argv") or ["--version"]),
        extra_env=cfg.peer_extra_env(peer),
    )
    allowed = list(spec.get("allowed_versions") or [])
    if allowed and version not in allowed:
        raise BrokerError(ErrorCategory.PREFLIGHT_VERSION_MISMATCH)
    return {
        "executable": executable,
        "observed_version": version,
        "allowed_versions": allowed,
        "version_pinned": bool(allowed),
    }


def assert_state_root_secure(cfg: Config) -> dict[str, Any]:
    """Confirm the state root actually honours owner-only permissions.

    Every guarantee about state being readable only by you rests on chmod
    working. On some filesystems it does not: notably WSL's DrvFs, which is what
    you get if the state root ends up under /mnt/c. There, chmod appears to
    succeed and the mode does not stick, so the promise would be quietly false
    rather than loudly broken. That is the failure mode this project refuses
    everywhere else, so it refuses here too.

    Returns a small report for diagnostics. Raises if the filesystem cannot
    hold the permissions this tool documents.
    """
    root = store.secure_mkdir(cfg.state_root)
    observed_dir = os.stat(root).st_mode & 0o777
    probe_path = os.path.join(root, ".permission-probe")
    observed_file = None
    try:
        store.atomic_write_bytes(probe_path, b"probe\n")
        observed_file = os.stat(probe_path).st_mode & 0o777
    finally:
        try:
            os.unlink(probe_path)
        except OSError:
            pass
    report = {
        "state_root": root,
        "directory_mode": oct(observed_dir),
        "file_mode": oct(observed_file) if observed_file is not None else None,
        "honours_permissions": observed_dir == 0o700 and observed_file == 0o600,
    }
    if not report["honours_permissions"]:
        raise BrokerError(ErrorCategory.STATE_ROOT_INSECURE)
    return report


def assert_workspace_clean(workspace: str, stop_at: str | None = None,
                           record_to: str | None = None) -> None:
    """Refuse to run if the workspace or ANY ancestor holds instruction files.

    Walks all the way to the filesystem root by default.  An earlier version
    stopped at the bridge's own state root, which left a real hole: Codex
    discovers AGENTS.md by walking upward from its working directory, so a file
    at ~/AGENTS.md would have influenced every consultation without tripping
    this check. `--ignore-rules` covers .rules; nothing covers AGENTS.md, so
    this walk has to.

    stop_at is retained for tests that need a bounded walk. Production passes
    None, meaning walk to the root.
    """
    # realpath, not abspath. The peer's working directory is a physical path,
    # so a symlinked state root would otherwise make this walk inspect lexical
    # ancestors while the peer sees different physical ones.
    current = os.path.realpath(workspace)
    boundary = os.path.realpath(stop_at) if stop_at else None
    seen: set[str] = set()
    while True:
        if current in seen:
            break
        seen.add(current)
        try:
            names = {n.lower() for n in os.listdir(current)}
        except OSError as exc:
            # Fail closed. An ancestor that cannot be enumerated may still be
            # searchable, so a peer could open a known AGENTS.md inside it. An
            # unverifiable ancestor is not a clean one.
            _record_contamination(record_to, {
                "reason": "an ancestor directory could not be enumerated",
                "directory": current,
                "workspace": os.path.realpath(workspace),
                "detected_at": _now(),
            })
            raise BrokerError(ErrorCategory.WORKSPACE_UNVERIFIABLE) from exc
        offenders = sorted(names & set(CONTAMINANTS))
        if offenders:
            # The caller-visible error stays a closed category with a constant
            # hint. The specifics go to the operator instead: a stray
            # ~/AGENTS.md would otherwise fail every consultation with a message
            # that names nothing, which is a guaranteed confused bug report.
            _record_contamination(record_to, {
                "reason": "instruction files found in an ancestor",
                "directory": current,
                "files": offenders,
                "workspace": os.path.realpath(workspace),
                "detected_at": _now(),
            })
            raise BrokerError(ErrorCategory.WORKSPACE_CONTAMINATED)
        parent = os.path.dirname(current)
        if current == boundary or current == parent:
            break
        current = parent


def assert_workspace_empty(workspace: str) -> None:
    """The consultation workspace must contain nothing at all."""
    try:
        if any(os.scandir(workspace)):
            raise BrokerError(ErrorCategory.WORKSPACE_CONTAMINATED)
    except FileNotFoundError:
        pass


def assert_peer_home_has_no_config(codex_home: str) -> None:
    """Refuse to run if the isolated Codex home has a config.toml.

    `--ignore-user-config` already stops that file from being read, so this is
    belt and braces on the specific recursion vector: `codex mcp add` writes
    the bridge's own registration into $CODEX_HOME/config.toml, and a future
    operator who ran it against the isolated home (rather than the default one)
    would otherwise create a loop that no other control here would catch.
    """
    if os.path.isfile(os.path.join(os.path.expanduser(codex_home), "config.toml")):
        raise BrokerError(ErrorCategory.PEER_HOME_CONFIG_PRESENT)


def peer_home_inventory(codex_home: str) -> dict[str, Any]:
    """What the isolated Codex home actually contains.

    Codex populates its own home with system skills and a plugin marketplace
    cache regardless of --ignore-user-config, and no feature flag was found at
    0.147.0 to disable either.  Recording the inventory on every job keeps that
    unproven isolation gap visible in the data instead of only in prose.
    """
    home = os.path.expanduser(codex_home)
    def _count(*parts: str) -> int:
        target = os.path.join(home, *parts)
        if not os.path.isdir(target):
            return 0
        try:
            return len([e for e in os.listdir(target) if not e.startswith(".")])
        except OSError:
            return 0
    return {
        "path": home,
        "config_toml_present": os.path.isfile(os.path.join(home, "config.toml")),
        "auth_json_present": os.path.isfile(os.path.join(home, "auth.json")),
        "system_skills": _count("skills", ".system"),
        "user_skills": _count("skills"),
        "plugin_cache_entries": _count(".tmp", "plugins", "plugins"),
    }
