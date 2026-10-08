"""Dependency drift checks and the optional per-user doctor schedule.

The fast check only compares metadata with the last successful component
baseline.  It never starts a provider or a local model.
"""
from __future__ import annotations

import argparse
import datetime as datetime
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import config as bridge_config
from . import store

CACHE_SECONDS = 600.0
QUICK_CHECK_TIMEOUT = 2
_quick_cache: dict[tuple[str | None, str], tuple[float, dict[str, Any]]] = {}

# Keep these beside the compatibility probe.  These are the documented long
# spellings of flags passed by the bridge's launchers.  The launchers often use
# Codex's short aliases (notably ``-m``, ``-s``, and ``-c``), but its help
# advertises the equivalent long forms.
CODEX_FLAGS = (
    "--json", "--model", "--output-last-message", "--output-schema",
    "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
    "--strict-config", "--sandbox", "--config", "--cd",
)
CLAUDE_FLAGS = (
    "--print", "--output-format", "--permission-mode", "--model", "--effort",
    "--setting-sources", "--settings", "--mcp-config", "--strict-mcp-config",
    "--tools", "--system-prompt", "--session-id", "--resume",
    "--no-session-persistence", "--json-schema", "--max-budget-usd",
    "--safe-mode",
)
DEFAULT_QUARANTINE_HOURS = 72.0
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?$")


def _home(home: str | None = None) -> str:
    return os.path.abspath(os.path.expanduser(home or os.environ.get("HOME", "~")))


def paths(home: str | None = None) -> dict[str, str]:
    root = os.path.join(_home(home), ".agent-bridge", "drift")
    return {"root": root, "baseline": os.path.join(root, "baseline.json"),
            "audit": os.path.join(root, "audit.jsonl"),
            "attention": os.path.join(root, "attention.json")}


def _config_candidates(home: str) -> list[str]:
    from .orchestration import delegation, gate
    candidates: list[str] = []
    receipt = gate.install_paths(home)["receipt"]
    try:
        installed = store.read_json(receipt)
        named = installed.get("config") if isinstance(installed, dict) else None
        if isinstance(named, str):
            candidates.append(os.path.abspath(os.path.expanduser(named)))
    except (OSError, ValueError):
        pass
    candidates.append(delegation.paths_for(home, bridge_config.REPO_ROOT)["config"])
    return candidates


def discover_config(config_path: str | None = None, *, home: str | None = None) -> str | None:
    """Find the installed orchestration config in the same order as install."""
    if config_path:
        candidate = os.path.abspath(os.path.expanduser(config_path))
        return candidate if os.path.isfile(candidate) else None
    for candidate in _config_candidates(_home(home)):
        if os.path.isfile(candidate):
            return candidate
    return None


def default_config_path(home: str | None = None) -> str | None:
    return discover_config(home=home)


def _sha256(path: str | None) -> str | None:
    if not path:
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _version(path: str | None, runner: Any | None = None) -> str | None:
    if not path:
        return None
    if runner is not None:
        completed = runner([path, "--version"], cwd=str(Path(path).parent), text="")
        text = _completed_text(completed).strip()
        return text.splitlines()[0] if completed and completed.returncode == 0 and text else None
    try:
        completed = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=10, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    return text.splitlines()[0] if text else None


def _bridge() -> Any | None:
    try:
        return bridge_config.load()
    except (OSError, ValueError):
        return None


def _executable(name: str, bridge: Any | None) -> str | None:
    configured = None
    if bridge is not None:
        try:
            configured = bridge.peer(name).get("executable")
        except (AttributeError, ValueError):
            configured = None
    value = os.path.expanduser(configured) if isinstance(configured, str) and configured else shutil.which(name)
    return os.path.realpath(value) if value and os.path.isfile(value) else None


def _normal_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.lower().removeprefix("sha256:")
    return value if value else None


def _ollama(raw: dict[str, Any], runner: Any | None = None) -> dict[str, Any]:
    executable = shutil.which("ollama")
    endpoint = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    endpoint = (endpoint if "://" in endpoint else "http://" + endpoint).rstrip("/")
    answer: dict[str, Any] = {"endpoint": endpoint, "version": _version(executable, runner),
                              "configured_digest": raw.get("gemma_model_digest"),
                              "model_present": "unknown"}
    try:
        with urllib.request.urlopen(endpoint + "/api/version", timeout=2) as reply:
            version = json.loads(reply.read().decode("utf-8")).get("version")
        if isinstance(version, str):
            answer["version"] = version
    except Exception:
        pass
    configured = _normal_digest(raw.get("gemma_model_digest"))
    if not configured:
        return answer
    try:
        with urllib.request.urlopen(endpoint + "/api/tags", timeout=2) as reply:
            models = json.loads(reply.read().decode("utf-8")).get("models", [])
        if isinstance(models, list):
            digests = {_normal_digest(item.get("digest")) for item in models if isinstance(item, dict)}
            answer["model_present"] = configured in digests
    except Exception:
        pass
    return answer


def _hook_state(home: str) -> dict[str, Any]:
    from .orchestration import gate
    locations = gate.install_paths(home)
    answer: dict[str, Any] = {}
    for client, name in (("claude", "claude_settings"), ("codex", "codex_hooks")):
        present = launcher_exists = False
        try:
            loaded = store.read_json(locations[name])
            entries = loaded.get("hooks", {}).get("PreToolUse", [])
            ours = [item for item in entries if gate._is_ours(item)] if isinstance(entries, list) else []
            present = bool(ours)
            if ours:
                import shlex
                command = " ".join(str(item.get("command", "")) for item in ours[0].get("hooks", []) if isinstance(item, dict))
                launcher_exists = bool(shlex.split(command)) and os.path.isfile(shlex.split(command)[0])
        except (OSError, ValueError, AttributeError):
            pass
        answer[client] = {"path": locations[name], "present": present, "launcher_exists": launcher_exists}
    try:
        answer["codex"]["trust"] = gate.codex_trust_state(locations["codex_toml"], locations["codex_hooks"])
    except (OSError, ValueError):
        answer["codex"]["trust"] = "unknown"
    return answer


def _absolute_paths(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: {"path": os.path.expanduser(value), "exists": os.path.exists(os.path.expanduser(value))}
            for name, value in raw.items() if isinstance(value, str) and os.path.isabs(os.path.expanduser(value))}


def _codex_default_model(home: str) -> str | None:
    try:
        import tomllib
        parsed = tomllib.loads(Path(home, ".codex", "config.toml").read_text(encoding="utf-8-sig"))
        return parsed.get("model") if isinstance(parsed, dict) and isinstance(parsed.get("model"), str) else None
    except (OSError, ValueError):
        return None


def inventory(config_path: str | None = None, *, home: str | None = None,
              bridge: Any | None = None, runner: Any | None = None) -> dict[str, Any]:
    home = _home(home)
    resolved = discover_config(config_path, home=home)
    raw: dict[str, Any] = {}
    if resolved:
        try:
            candidate = store.read_json(resolved)
            raw = candidate if isinstance(candidate, dict) else {}
        except (OSError, ValueError):
            pass
    bridge = _bridge() if bridge is None else bridge
    executables = {name: {"path": _executable(name, bridge)} for name in ("claude", "codex")}
    for item in executables.values():
        item.update(version=_version(item["path"], runner), sha256=_sha256(item["path"]))
    gems: dict[str, dict[str, Any]] = {}
    for key, field, pin in (("delegate", "gemma_delegate_executable", "gemma_delegate_sha256"),
                            ("validator", "gemma_receipt_validator_executable", "gemma_receipt_validator_sha256")):
        path = raw.get(field)
        path = os.path.expanduser(path) if isinstance(path, str) else None
        digest = _sha256(path)
        gems[key] = {"path": path, "exists": bool(path and os.path.isfile(path)), "sha256": digest,
                     "configured_sha256": raw.get(pin), "pin_matches": bool(digest and digest == raw.get(pin))}
    default_model = _codex_default_model(home)
    return {"version": 1, "at": time.time(), "config_path": resolved, "executables": executables,
            "ollama": _ollama(raw, runner), "gemma": gems, "paths": _absolute_paths(raw),
            "hooks": _hook_state(home), "codex_default_model": default_model,
            "consumers": _consumer_pins(home)}


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items() if key != "at"}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def changes(current: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    if not baseline:
        return []
    answer: list[str] = []
    for name in ("claude", "codex"):
        if current["executables"].get(name) != baseline.get("executables", {}).get(name): answer.append(name)
    if current.get("ollama") != baseline.get("ollama"): answer.append("ollama")
    for name in ("delegate", "validator"):
        if current["gemma"].get(name) != baseline.get("gemma", {}).get(name): answer.append("gemma_" + name)
    if current.get("paths") != baseline.get("paths"): answer.append("configured_path")
    if current.get("hooks") != baseline.get("hooks"): answer.append("hooks")
    return answer


def _read_baseline(home: str) -> dict[str, Any] | None:
    try:
        value = store.read_json(paths(home)["baseline"])
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_baseline(home: str, value: dict[str, Any]) -> None:
    store.secure_mkdir(paths(home)["root"])
    store.atomic_write_json(paths(home)["baseline"], value)


def _merge_good_baseline(old: dict[str, Any] | None, current: dict[str, Any], passed: set[str]) -> dict[str, Any]:
    """Keep a failed changed component at its last good inventory value."""
    if old is None:
        # There is no previous value to preserve.  Omit failed probes so they
        # compare unequal on the next doctor run instead of becoming trusted.
        merged = json.loads(json.dumps(current))
        for name in ("claude", "codex"):
            if name not in passed:
                merged["executables"].pop(name, None)
        if "gemma" not in passed:
            merged["ollama"] = {}
            merged["gemma"] = {}
        return merged
    merged = json.loads(json.dumps(old))
    merged["at"] = current["at"]
    # Hook and configured-path changes have no synthetic proof.  Retain an
    # old value when the new one is still under attention, rather than making
    # a passing CLI silently bless an unrelated configuration change.
    for section in ("paths", "hooks", "config_path"):
        if current[section] == old.get(section):
            merged[section] = current[section]
    merged["codex_default_model"] = current["codex_default_model"]
    for name in ("claude", "codex"):
        if name not in {"claude", "codex"} or name in passed or current["executables"][name] == old.get("executables", {}).get(name):
            merged.setdefault("executables", {})[name] = current["executables"][name]
    if "ollama" in passed or current["ollama"] == old.get("ollama"):
        merged["ollama"] = current["ollama"]
    for name in ("delegate", "validator"):
        component = "gemma_" + name
        if "gemma" in passed or current["gemma"][name] == old.get("gemma", {}).get(name):
            merged.setdefault("gemma", {})[name] = current["gemma"][name]
    return merged


def _run(argv: list[str], *, cwd: str, text: str, timeout: int = 20, env: dict[str, str] | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, cwd=cwd, input=text.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False, shell=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _quick_run(argv: list[str], *, cwd: str, text: str,
               timeout: int = QUICK_CHECK_TIMEOUT,
               env: dict[str, str] | None = None) -> subprocess.CompletedProcess | None:
    """Run the only CLI probes allowed from an executor loop.

    ``inventory`` only asks these commands for their versions.  Keep that
    narrow path independently bounded so a wedged CLI cannot consume an
    executor interval on a cache miss.
    """
    return _run(argv, cwd=cwd, text=text, timeout=min(timeout, QUICK_CHECK_TIMEOUT), env=env)


def _completed_text(result: subprocess.CompletedProcess | None) -> str:
    return result.stdout.decode("utf-8", "replace") if result else ""


def _help_has_flags(path: str | None, runner: Any = _run) -> dict[str, Any]:
    """Free CLI compatibility check.  A missing executable is actionable."""
    if not path:
        return {"ok": False, "missing": ["executable"]}
    codex = runner([path, "exec", "--help"], cwd=str(Path(path).parent), text="")
    resume = runner([path, "exec", "resume", "--help"], cwd=str(Path(path).parent), text="")
    text = _completed_text(codex) + "\n" + _completed_text(resume)
    missing = [flag for flag in CODEX_FLAGS if not re.search(re.escape(flag) + r"\b(?!-)", text)]
    return {"ok": bool(codex and resume and codex.returncode == 0 and resume.returncode == 0 and not missing),
            "missing": missing}


def _claude_help_has_flags(path: str | None, runner: Any = _run) -> dict[str, Any]:
    if not path:
        return {"ok": False, "missing": ["executable"]}
    result = runner([path, "--help"], cwd=str(Path(path).parent), text="")
    text = _completed_text(result)
    missing = [flag for flag in CLAUDE_FLAGS if not re.search(re.escape(flag) + r"\b(?!-)", text)]
    return {"ok": bool(result and result.returncode == 0 and not missing), "missing": missing}


def _model_catalog(path: str | None, runner: Any = _run) -> dict[str, Any] | None:
    """Return None when the free catalog cannot be read, never infer failure."""
    if not path:
        return None
    result = runner([path, "debug", "models"], cwd=str(Path(path).parent), text="")
    if not result or result.returncode:
        return None
    try:
        value = json.loads(_completed_text(result))
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("models"), list) else None


def _model_check(path: str | None, *, configured_model: str | None,
                 configured_effort: str | None, default_model: str | None,
                 runner: Any = _run) -> dict[str, Any]:
    catalog = _model_catalog(path, runner)
    if catalog is None:
        return {"ok": None, "status": "unknown", "reason": "model catalog unreadable"}
    models = {item.get("slug"): item for item in catalog["models"] if isinstance(item, dict) and isinstance(item.get("slug"), str)}
    missing = [model for model in (configured_model, default_model) if model and model not in models]
    unsupported = False
    if configured_model and configured_effort and configured_model in models:
        levels = models[configured_model].get("supported_reasoning_levels", [])
        supported = {item.get("effort") for item in levels if isinstance(item, dict)}
        unsupported = configured_effort not in supported
    if missing:
        return {"ok": False, "status": "needs_you", "reason": f"Codex model {missing[0]} is not available in this CLI's model catalog."}
    if unsupported:
        return {"ok": False, "status": "needs_you", "reason": f"Codex model {configured_model} does not support reasoning effort {configured_effort}."}
    return {"ok": True, "status": "ok"}


def _consumer_pins(home: str) -> list[dict[str, str]]:
    """Read, never modify, the live newest firm-tools plugin copies."""
    root = Path(home, ".claude", "plugins", "cache")
    answer: list[dict[str, str]] = []
    try:
        marketplaces = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return answer
    for marketplace in marketplaces:
        versions = sorted((item for item in (marketplace / "firm-tools").iterdir() if item.is_dir()), key=lambda item: _version_key(item.name), reverse=True) if (marketplace / "firm-tools").is_dir() else []
        if not versions:
            continue
        source = versions[0] / "skills" / "codex-job-runner" / "bridge.py"
        try:
            match = re.search(r"(?m)^\s*PINNED_CODEX_VERSION\s*=\s*['\"]([^'\"]+)['\"]", source.read_text(encoding="utf-8"))
        except OSError:
            continue
        if match:
            answer.append({"name": "firm-tools codex-job-runner", "version": match.group(1),
                           "source": "platform/firm-claude-plugins", "path": str(source)})
    return answer


def _version_key(value: str) -> tuple[int, ...]:
    match = _VERSION_RE.match(value.strip())
    return tuple(int(part or 0) for part in match.groups()) if match else (0,)


def _newer(candidate: str, installed: str | None) -> bool:
    return installed is None or _version_key(candidate) > _version_key(installed)


def _quarantine_hours(home: str, requested: float | None) -> float:
    if requested is not None:
        return max(0.0, requested)
    try:
        value = store.read_json(os.path.join(home, ".agent-bridge", "drift", "settings.json")).get("quarantine_hours")
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_QUARANTINE_HOURS


def _eligible_release(npm: Path, prefix: Path, *, quarantine_hours: float, runner: Any = _run,
                      now: float | None = None) -> tuple[str | None, str | None]:
    result = runner([str(npm), "view", "@openai/codex", "time", "--json"], cwd=str(prefix), text="")
    if not result or result.returncode:
        return None, None
    try:
        times = json.loads(_completed_text(result))
    except ValueError:
        return None, None
    if not isinstance(times, dict):
        return None, None
    cutoff = datetime.datetime.fromtimestamp((now if now is not None else time.time()) - quarantine_hours * 3600, datetime.timezone.utc)
    eligible: list[tuple[tuple[int, ...], str, str]] = []
    for version, published in times.items():
        if not isinstance(version, str) or not isinstance(published, str) or "-" in version or not _VERSION_RE.match(version):
            continue
        try:
            released = datetime.datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if released.tzinfo is not None and released <= cutoff:
            eligible.append((_version_key(version), version, published))
    if not eligible:
        return None, None
    _, version, published = max(eligible)
    return version, published


def run_cli_check(name: str, entry: dict[str, Any], *, model: str | None = None,
                  default_model: bool = False, claude_config_dir: str | None = None,
                  runner: Any = _run) -> dict[str, Any]:
    path = entry.get("path")
    if not path: return {"ok": False, "reason": "executable_missing"}
    with tempfile.TemporaryDirectory(prefix="agent-bridge-doctor-") as directory:
        if name == "claude":
            from .execution import claude_task
            target = Path(directory, "doctor-word.txt")
            # Reuse the generation command and its permission mode exactly.
            argv = claude_task._command(Path(path), model or "sonnet", "low")
            prompt = f"Create exactly {target.name} in the current directory containing the word DRIFT_DOCTOR_WORD."
            result = runner(argv, cwd=directory, text=prompt, env=claude_task._env(Path(claude_config_dir) if claude_config_dir else None))
            ok = bool(result and result.returncode == 0 and target.is_file() and "DRIFT_DOCTOR_WORD" in target.read_text(encoding="utf-8", errors="replace"))
            return {"ok": ok, "reason": "permission_mode_auto_write_declined" if not ok else None,
                    "receipt_id": "claude-synthetic"}
        argv = [path, "exec", "--skip-git-repo-check"]
        if model and not default_model: argv += ["-m", model]
        result = runner(argv, cwd=directory, text="Reply with exactly DRIFT_DOCTOR_WORD.")
        output = result.stdout.decode("utf-8", "replace") if result else ""
        ok = bool(result and result.returncode == 0 and "DRIFT_DOCTOR_WORD" in output)
        rejected = bool(default_model and re.search(r"(?:HTTP.?400|not supported|unsupported model|model .* rejected)", output, re.I))
        return {"ok": ok, "reason": "default_model_rejected" if rejected else ("synthetic_failed" if not ok else None),
                "output": output[:2000], "receipt_id": "codex-default" if default_model else "codex-synthetic"}


def run_gemma_check(config_path: str) -> dict[str, Any]:
    from .orchestration.config import load
    from .localq import gemma_child
    try:
        cfg = load(config_path)
        if cfg.local_backend != "gemma_certified": return {"ok": True, "receipt_id": "not-applicable"}
        delegate_hash, validator_hash = _sha256(str(cfg.gemma_delegate_executable)), _sha256(str(cfg.gemma_receipt_validator_executable))
        if not delegate_hash or not validator_hash: return {"ok": False, "reason": "configured_file_missing"}
        with tempfile.TemporaryDirectory(prefix="agent-bridge-drift-") as receipts:
            result = gemma_child.invoke({"job_id": "drift-synthetic", "task_type": "summarize", "input": "Synthetic drift check.", "params": {"instruction": ""}}, delegate=str(cfg.gemma_delegate_executable), python=str(cfg.gemma_python_executable), receipt_root=receipts, receipt_validator=str(cfg.gemma_receipt_validator_executable), model_digest=str(cfg.gemma_model_digest), delegate_sha256=delegate_hash, validator_sha256=validator_hash, timeout=cfg.gemma_timeout_seconds)
        return {"ok": True, "receipt_id": result.get("invocation_id")}
    except Exception as exc:
        return {"ok": False, "reason": str(exc) or type(exc).__name__}


def _audit(home: str, **entry: Any) -> None:
    store.secure_mkdir(paths(home)["root"])
    with open(paths(home)["audit"], "a", encoding="utf-8") as handle:
        os.chmod(paths(home)["audit"], 0o600)
        handle.write(json.dumps({"at": time.time(), **entry}, sort_keys=True) + "\n")


def doctor(config_path: str | None = None, *, home: str | None = None, accept_requalified: bool = False,
           force_checks: bool = False, bridge: Any | None = None, runner: Any = _run,
           prequalified_codex: bool = False) -> dict[str, Any]:
    home = _home(home); current = inventory(config_path, home=home, bridge=bridge, runner=runner); baseline = _read_baseline(home)
    needs: list[str] = []; checked: dict[str, Any] = {}; passed: set[str] = set(); changed = changes(current, baseline)
    if not current["config_path"]:
        needs.append("No installed orchestration config was found; pass --config or install the gate, then run setup_bridge.py doctor.")
    for name, state in current["paths"].items():
        if not state["exists"]: needs.append(f"Configured path {name} is missing; repair the orchestration config and run setup_bridge.py doctor.")
    hooks = current["hooks"]
    if not hooks["claude"]["present"] or not hooks["claude"]["launcher_exists"]: needs.append("Claude gate hook is missing or its launcher is absent; reinstall the gate hook, then run setup_bridge.py doctor.")
    if not hooks["codex"]["present"] or not hooks["codex"]["launcher_exists"]: needs.append("Codex gate hook is missing or its launcher is absent; reinstall the gate hook, then run setup_bridge.py doctor.")
    elif hooks["codex"].get("trust") != "recorded": needs.append("Codex gate hook is not trusted; open Codex /hooks and trust it, then run setup_bridge.py doctor.")
    to_check = {"claude", "codex"} if baseline is None or force_checks else set(changed) & {"claude", "codex"}
    if prequalified_codex:
        to_check.discard("codex")
    bridge = _bridge() if bridge is None else bridge
    peer = (lambda name: bridge.peer(name) if bridge else {})
    codex_spec = peer("codex")
    codex_effort = bridge.peer_reasoning_effort("codex") if bridge else None
    # These cost neither quota nor a model turn and deliberately run on every
    # foreground doctor invocation, including when the binary did not change.
    checked["codex_flags"] = _help_has_flags(current["executables"]["codex"].get("path"), runner)
    checked["claude_flags"] = _claude_help_has_flags(current["executables"]["claude"].get("path"), runner)
    for label, result in (("Codex", checked["codex_flags"]), ("Claude", checked["claude_flags"])):
        for flag in result.get("missing", []):
            needs.append(f"{label} no longer lists required flag {flag}; update the bridge before using this CLI.")
    checked["codex_models"] = _model_check(current["executables"]["codex"].get("path"),
                                             configured_model=codex_spec.get("model"),
                                             configured_effort=codex_effort,
                                             default_model=current.get("codex_default_model"), runner=runner)
    if checked["codex_models"].get("ok") is False:
        needs.append(str(checked["codex_models"]["reason"]))
        # F1 gates both paid Codex turns, even when this invocation would
        # otherwise reuse a prior staged qualification.
        checked["codex"] = {"ok": False, "reason": "model_check_failed"}
        checked["codex_default"] = {"ok": False, "reason": "model_check_failed"}
    installed = _npm_version_for_executable(current["executables"]["codex"].get("path"), runner)
    consumers = current.get("consumers", [])
    if installed is None:
        checked["consumer_pins"] = {"ok": None, "status": "unknown",
                                    "reason": "installed_version_unknown", "consumers": consumers}
    else:
        mismatches = [consumer for consumer in consumers if consumer.get("version") != installed]
        checked["consumer_pins"] = {"ok": not mismatches, "status": "ok" if not mismatches else "needs_you",
                                    "installed": installed, "consumers": consumers}
        for consumer in mismatches:
            needs.append(_consumer_message(str(consumer.get("version")), installed))
    for name in sorted(to_check):
        if name == "codex" and checked["codex_models"].get("ok") is False:
            checked[name] = {"ok": False, "reason": "model_check_failed"}
            continue
        claude_dir = current["paths"].get("claude_config_dir", {}).get("path")
        checked[name] = run_cli_check(name, current["executables"][name], model=peer(name).get("model"), claude_config_dir=claude_dir, runner=runner)
        if checked[name].get("ok"): passed.add(name)
        elif name == "claude": needs.append("Claude Write was declined in permission mode auto or its synthetic check failed; repair Claude permissions and run setup_bridge.py doctor.")
        else: needs.append("Codex changed but its configured-model synthetic check failed; repair it and run setup_bridge.py doctor.")
    if checked["codex_models"].get("ok") is False:
        pass
    elif (baseline is None or "codex" in changed or force_checks) and not prequalified_codex:
        checked["codex_default"] = run_cli_check("codex", current["executables"]["codex"], default_model=True, runner=runner)
        if checked["codex_default"].get("reason") == "default_model_rejected":
            model = current.get("codex_default_model") or "(unset)"
            needs.append(f"Your Codex default model {model} is rejected by this Codex version; pick another in Codex settings or update Codex.")
            # The executable is not fully qualified while its ordinary default
            # invocation is rejected, even if the bridge-pinned model worked.
            passed.discard("codex")
    elif prequalified_codex:
        # update() established the same configured and default-model synthetic
        # checks in private staging.  Its live post-switch check is purposely
        # free, so no job spends quota on an unchecked release.
        passed.add("codex")
    gemma_changed = baseline is None or bool({"ollama", "gemma_delegate", "gemma_validator"} & set(changed))
    if gemma_changed and current["config_path"]:
        checked["gemma"] = run_gemma_check(current["config_path"])
        if checked["gemma"].get("ok"): passed.add("gemma")
        else: needs.append("Gemma changed but its synthetic check failed; repair it and run setup_bridge.py doctor.")
    if baseline is None or passed:
        _write_baseline(home, _merge_good_baseline(baseline, current, passed))
    # A foreground doctor is the explicit resolution action.  Do not leave a
    # worker holding otherwise qualified jobs on a ten-minute cached diff.
    _quick_cache.clear()
    return {"ok": not needs, "inventory": current, "consumers": current.get("consumers", []), "changed": changed, "requalified": sorted(passed), "needs_you": needs, "checks": checked}


def quick_check(config_path: str | None = None, *, home: str | None = None) -> dict[str, Any]:
    home = _home(home); resolved = discover_config(config_path, home=home); key = (resolved, home)
    cached = _quick_cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS: return cached[1]
    # This is called from executor loops.  It is intentionally limited to
    # version probes whose per-process timeout is shorter than one interval.
    current, baseline = inventory(resolved, home=home, runner=_quick_run), _read_baseline(home)
    # A missing comparison point needs operator attention, but is not evidence
    # of drift.  Holding every provider on a fresh install would make it
    # impossible for the bridge to work until someone happened to run doctor.
    names = changes(current, baseline) if baseline else []
    if not resolved: names.append("orchestration_config")
    for name, state in current["paths"].items():
        if not state["exists"]: names.append(name)
    for key_name in ("delegate", "validator"):
        state = current["gemma"][key_name]
        if state.get("path") and not state.get("pin_matches"):
            names.append("gemma_" + key_name)
    attention = (["Run setup_bridge.py doctor once to record a baseline."]
                 if baseline is None else [])
    result = {"ok": not names, "changed": sorted(set(names)), "inventory": current,
              "baseline": "missing" if baseline is None else "recorded",
              "attention": attention}
    _quick_cache[key] = (time.monotonic(), result)
    return result


def scheduled_baseline(config_path: str | None = None, *, home: str | None = None,
                       bridge: Any | None = None, runner: Any = _run) -> dict[str, Any]:
    """Record the first scheduled baseline without spending a model turn.

    The scheduled path is allowed to inspect inventory, both free CLI
    compatibility checks, Codex's free model catalog, and the configured
    Gemma file pins.  It deliberately does not call either synthetic CLI
    check or the Gemma delegate.  A later nightly update can perform its
    normal staged qualification once there is a comparison baseline.
    """
    home = _home(home)
    baseline = _read_baseline(home)
    if baseline is not None:
        return {"ok": True, "baseline": "recorded", "recorded": False,
                "needs_you": [], "checks": {}}
    current = inventory(config_path, home=home, bridge=bridge, runner=runner)
    bridge = _bridge() if bridge is None else bridge
    peer = (lambda name: bridge.peer(name) if bridge else {})
    checks = {
        "codex_flags": _help_has_flags(current["executables"]["codex"].get("path"), runner),
        "claude_flags": _claude_help_has_flags(current["executables"]["claude"].get("path"), runner),
        "codex_models": _model_check(
            current["executables"]["codex"].get("path"),
            configured_model=peer("codex").get("model"),
            configured_effort=bridge.peer_reasoning_effort("codex") if bridge else None,
            default_model=current.get("codex_default_model"), runner=runner),
        "gemma_pins": {
            name: state.get("pin_matches")
            for name, state in current.get("gemma", {}).items()
        },
    }
    needs: list[str] = []
    if not checks["codex_flags"].get("ok"):
        needs.append("Codex free flag check failed; repair or update Codex, then run setup_bridge.py doctor.")
    if not checks["claude_flags"].get("ok"):
        needs.append("Claude free flag check failed; repair or update Claude, then run setup_bridge.py doctor.")
    if checks["codex_models"].get("ok") is False:
        needs.append(str(checks["codex_models"].get("reason", "Codex free model catalog check failed.")))
    for name, state in current.get("gemma", {}).items():
        if state.get("path") and not state.get("pin_matches"):
            needs.append(f"Gemma {name} pin does not match; repair the configured pin, then run setup_bridge.py doctor.")
    if needs:
        return {"ok": False, "baseline": "missing", "recorded": False,
                "inventory": current, "needs_you": needs, "checks": checks}
    _write_baseline(home, current)
    _quick_cache.clear()
    return {"ok": True, "baseline": "recorded", "recorded": True,
            "inventory": current, "needs_you": [], "checks": checks}


def schedule_path(home: str | None = None) -> str:
    return os.path.join(_home(home), "Library", "LaunchAgents", "com.agent-bridge.drift-doctor.plist")


def _schedule_launchctl(path: str, *, remove: bool = False) -> dict[str, Any]:
    from .orchestration import delegation
    if sys.platform != "darwin": return {"status": "unsupported_platform"}
    return delegation.deactivate_launch_agent(path, apply=True) if remove else delegation.activate_launch_agent(path, apply=True)


def install_schedule(config_path: str | None = None, *, home: str | None = None) -> str:
    if hasattr(os, "geteuid") and os.geteuid() == 0: raise PermissionError("doctor schedule refuses to run as root")
    home = _home(home); resolved = discover_config(config_path, home=home)
    program = [sys.executable, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "setup_bridge.py")), "doctor", "--update", "--scheduled", "--quiet"]
    if resolved: program += ["--config", resolved]
    data = {"Label": "com.agent-bridge.drift-doctor", "ProgramArguments": program, "RunAtLoad": True,
            "StartCalendarInterval": {"Hour": 3, "Minute": 30}, "ProcessType": "Background"}
    path = schedule_path(home); os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    store.atomic_write_bytes(path, plistlib.dumps(data), owner_only=True); _schedule_launchctl(path)
    return path


def remove_schedule(*, home: str | None = None) -> bool:
    if hasattr(os, "geteuid") and os.geteuid() == 0: raise PermissionError("doctor schedule refuses to run as root")
    path = schedule_path(home); _schedule_launchctl(path, remove=True)
    try: os.unlink(path); return True
    except FileNotFoundError: return False


def _npm_for_prefix(prefix: Path, codex: Path) -> Path | None:
    direct = prefix / "bin" / "npm"
    if direct.is_file(): return direct
    node = shutil.which("node", path=str(codex.parent))
    if node:
        candidate = Path(node).with_name("npm")
        if candidate.is_file(): return candidate
    return None


def _codex_prefix(path: str) -> Path | None:
    resolved = Path(path).resolve()
    marker = Path("lib/node_modules/@openai/codex")
    try:
        base = resolved if resolved.is_dir() else resolved.parent
        for candidate in (base, *base.parents):
            if (candidate / marker).is_dir(): return candidate
    except OSError:
        return None
    return None


def _npm_version(npm: Path, prefix: Path) -> str | None:
    result = _run([str(npm), "list", "-g", "--prefix", str(prefix), "@openai/codex", "--depth=0", "--json"], cwd=str(prefix), text="")
    if not result: return None
    match = re.search(r'"version"\s*:\s*"([^"]+)"', result.stdout.decode("utf-8", "replace"))
    return match.group(1) if match else None


def _npm_version_for_executable(executable: str | None, runner: Any = _run) -> str | None:
    if not executable:
        return None
    prefix = _codex_prefix(executable)
    npm = _npm_for_prefix(prefix, Path(executable)) if prefix else None
    if not npm or not prefix:
        return None
    result = runner([str(npm), "list", "-g", "--prefix", str(prefix), "@openai/codex", "--depth=0", "--json"], cwd=str(prefix), text="")
    match = re.search(r'"version"\s*:\s*"([^"]+)"', _completed_text(result))
    return match.group(1) if match else None


def _consumer_message(pin: str, candidate: str) -> str:
    return (f"firm-tools codex-job-runner pins {pin}; update that pin to {candidate} first "
            "(source platform/firm-claude-plugins), then the nightly update will proceed.")


def _staging_checks(executable: str, *, bridge: Any | None, default_model: str | None,
                    runner: Any) -> dict[str, Any]:
    spec = bridge.peer("codex") if bridge else {}
    effort = bridge.peer_reasoning_effort("codex") if bridge else None
    models = _model_check(executable, configured_model=spec.get("model"), configured_effort=effort,
                          default_model=default_model, runner=runner)
    flags = _help_has_flags(executable, runner)
    # A catalog that cannot be read is unknown, not a failed qualification.
    synthetic = {"ok": False, "reason": "model_check_failed"} if models.get("ok") is False else run_cli_check(
        "codex", {"path": executable}, model=spec.get("model"), runner=runner)
    default = {"ok": False, "reason": "model_check_failed"} if models.get("ok") is False else run_cli_check(
        "codex", {"path": executable}, default_model=True, runner=runner)
    return {"models": models, "flags": flags, "codex": synthetic, "codex_default": default,
            "ok": bool(flags.get("ok") and synthetic.get("ok") and default.get("ok") and models.get("ok") is not False)}


def _post_switch_checks(executable: str, *, candidate: str, bridge: Any | None, default_model: str | None,
                        runner: Any) -> dict[str, Any]:
    """The post-switch check is intentionally free: no live model turn."""
    version = runner([executable, "--version"], cwd=str(Path(executable).parent), text="")
    spec = bridge.peer("codex") if bridge else {}
    effort = bridge.peer_reasoning_effort("codex") if bridge else None
    models = _model_check(executable, configured_model=spec.get("model"), configured_effort=effort,
                          default_model=default_model, runner=runner)
    flags = _help_has_flags(executable, runner)
    version_text = _completed_text(version).strip()
    return {"version": version_text, "candidate": candidate, "models": models, "flags": flags,
            "ok": bool(version and version.returncode == 0 and candidate in version_text and flags.get("ok") and models.get("ok") is not False)}


def _prune_staging(home: str) -> None:
    root = Path(home, ".agent-bridge", "codex-staging")
    try:
        old = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)[2:]
    except OSError:
        return
    for item in old:
        try:
            shutil.rmtree(item)
        except OSError:
            pass


def update(config_path: str | None = None, *, home: str | None = None,
           path_env: str | None = None, bridge: Any | None = None, runner: Any = _run,
           npm_view: Any | None = None, quarantine_hours: float | None = None) -> dict[str, Any]:
    """Qualify one quarantined release in private staging before any switch.

    Injectable dependencies are intentional: tests must never discover PATH or
    invoke npm outside their temporary root.
    """
    home = _home(home); bridge = _bridge() if bridge is None else bridge
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    candidates: set[str] = set()
    if bridge:
        value = bridge.peer("codex").get("executable")
        if isinstance(value, str): candidates.add(value)
    for item in path_env.split(os.pathsep):
        candidate = os.path.join(item, "codex")
        if os.path.isfile(candidate): candidates.add(candidate)
    events: list[dict[str, Any]] = []; needs: list[str] = []
    qualifications: dict[str, dict[str, Any]] = {}
    qhours = _quarantine_hours(home, quarantine_hours)
    for executable in sorted(candidates):
        prefix = _codex_prefix(executable)
        if prefix is None:
            continue
        npm = _npm_for_prefix(prefix, Path(executable))
        writable_bits = prefix.stat().st_mode & 0o222
        if not npm or not writable_bits or not os.access(prefix, os.W_OK):
            events.append({"prefix": str(prefix), "status": "skipped_not_writable"}); continue
        previous = _npm_version_for_executable(executable, runner)
        candidate, published = (npm_view(npm, prefix, qhours) if npm_view else _eligible_release(npm, prefix, quarantine_hours=qhours, runner=runner))
        if not candidate:
            events.append({"prefix": str(prefix), "status": "npm_view_failed_or_no_eligible_release"})
            needs.append(f"Codex release metadata could not be read or has no release older than {qhours:g} hours; the existing install was left untouched.")
            continue
        if not _newer(candidate, previous):
            events.append({"prefix": str(prefix), "status": "not_newer", "previous": previous, "candidate": candidate, "published": published})
            continue
        qualification = qualifications.get(candidate)
        if qualification is None:
            staging = Path(home, ".agent-bridge", "codex-staging", candidate)
            staging.mkdir(parents=True, exist_ok=True)
            install = runner([str(npm), "install", "-g", "--prefix", str(staging), f"@openai/codex@{candidate}"], cwd=str(prefix), text="", timeout=120)
            staged_bin = str(staging / "bin" / "codex")
            if not install or install.returncode:
                qualification = {"ok": False, "staging_npm_failed": True}
            else:
                qualification = _staging_checks(staged_bin, bridge=bridge,
                                                 default_model=_codex_default_model(home), runner=runner)
            qualifications[candidate] = qualification
        if qualification.get("staging_npm_failed"):
            events.append({"prefix": str(prefix), "status": "staging_npm_failed", "candidate": candidate, "published": published})
            needs.append(f"Codex {candidate} could not be installed in private staging; the existing install was left untouched.")
            continue
        if not qualification["ok"]:
            events.append({"prefix": str(prefix), "status": "staged_failed", "candidate": candidate, "published": published, "checks": qualification})
            needs.append(f"Codex {candidate} failed private staging checks; the existing install was left untouched.")
            continue
        pin = next((item for item in _consumer_pins(home) if item.get("version") == previous), None)
        if pin:
            events.append({"prefix": str(prefix), "status": "blocked_by_consumer", "previous": previous, "candidate": candidate, "published": published})
            needs.append(_consumer_message(previous or "(unknown)", candidate))
            continue
        switched = runner([str(npm), "install", "-g", "--prefix", str(prefix), f"@openai/codex@{candidate}"], cwd=str(prefix), text="", timeout=120)
        if not switched or switched.returncode:
            events.append({"prefix": str(prefix), "status": "npm_failed", "previous": previous, "candidate": candidate, "published": published})
            needs.append(f"Codex update failed for {prefix}; the existing install was left untouched.")
            continue
        post = _post_switch_checks(executable, candidate=candidate, bridge=bridge,
                                   default_model=_codex_default_model(home), runner=runner)
        if not post["ok"] and previous:
            rollback = runner([str(npm), "install", "-g", "--prefix", str(prefix), f"@openai/codex@{previous}"], cwd=str(prefix), text="", timeout=120)
            rollback_checks = _post_switch_checks(executable, candidate=previous, bridge=bridge,
                                                  default_model=_codex_default_model(home), runner=runner)
            if rollback and rollback.returncode == 0 and rollback_checks["ok"]:
                events.append({"prefix": str(prefix), "status": "rolled_back", "previous": previous, "current": candidate, "published": published, "checks": post, "rollback_checks": rollback_checks, "switched": True, "executable": executable})
                needs.append(f"Codex updated to {candidate}, was rolled back to {previous}, because its post-switch check failed.")
            else:
                events.append({"prefix": str(prefix), "status": "rollback_failed", "previous": previous, "current": candidate, "published": published, "checks": post, "rollback_checks": rollback_checks, "switched": True, "executable": executable})
                needs.append(f"Codex rollback failed for {prefix}: candidate {candidate} could not be verified and recorded version {previous} was not restored and rechecked.")
        elif not post["ok"]:
            events.append({"prefix": str(prefix), "status": "post_switch_failed_no_rollback", "previous": previous, "current": candidate, "published": published, "checks": post, "switched": True, "executable": executable})
            needs.append(f"Codex post-switch check failed for {prefix} at candidate {candidate}; no recorded previous version was available for rollback.")
        else:
            events.append({"prefix": str(prefix), "status": "updated", "previous": previous, "current": candidate, "published": published, "checks": post, "switched": True, "executable": executable})
    _prune_staging(home)
    for event in events: _audit(home, action="codex_update", **event)
    bridge_executable = bridge.peer("codex").get("executable") if bridge else None
    bridge_executable = os.path.realpath(os.path.expanduser(bridge_executable)) if isinstance(bridge_executable, str) else None
    switched = [event for event in events if event.get("switched")]
    qualified = bool(bridge_executable and switched and all(event.get("status") == "updated" for event in switched)
                     and any(os.path.realpath(event["executable"]) == bridge_executable for event in switched))
    report = doctor(config_path, home=home, force_checks=False, bridge=bridge, runner=runner,
                    prequalified_codex=qualified)
    report["updates"], report["needs_you"] = events, [*needs, *report["needs_you"]]
    report["ok"] = not report["needs_you"]
    return report


def report_attention(home: str | None, report: dict[str, Any]) -> None:
    """Persist drift state and notify only when its stable signature changes."""
    actual_home = _home(home)
    attention = report.get("attention", report.get("needs_you", []))
    signature = json.dumps({"changed": sorted(report.get("changed", [])),
                            "needs_you": sorted(attention)}, sort_keys=True)
    previous = None
    try:
        old = store.read_json(paths(actual_home)["attention"])
        previous = old.get("signature") if isinstance(old, dict) else None
    except (OSError, ValueError):
        pass
    try:
        store.secure_mkdir(paths(actual_home)["root"])
        store.atomic_write_json(paths(actual_home)["attention"], {**report, "signature": signature})
    except OSError:
        # An execution worker may be launched in a restricted test or service
        # account.  Its queue-hold decision remains valid even if the optional
        # desktop notification state cannot be written there.
        return
    if (report.get("ok", False) and not attention) or previous == signature:
        return
    try: subprocess.run(["osascript", "-e", "display notification \"Run setup_bridge.py doctor\" with title \"agent-bridge needs attention\""], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, shell=False)
    except (OSError, subprocess.SubprocessError): pass


def _notify_attention(home: str, report: dict[str, Any]) -> None:
    report_attention(home, report)


def _notify_updated(version: str) -> None:
    try:
        subprocess.run(["osascript", "-e", f'display notification "Codex updated to {version}; all checks passed" with title "agent-bridge"'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, shell=False)
    except (OSError, subprocess.SubprocessError): pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_bridge.py doctor")
    parser.add_argument("--config"); parser.add_argument("--json", action="store_true"); parser.add_argument("--quiet", action="store_true"); parser.add_argument("--accept-requalified", action="store_true"); parser.add_argument("--update", action="store_true"); parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--quarantine-hours", type=float, default=None)
    modes = parser.add_mutually_exclusive_group(); modes.add_argument("--install-schedule", action="store_true"); modes.add_argument("--remove-schedule", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.install_schedule: print(install_schedule(args.config)); return 0
        if args.remove_schedule: remove_schedule(); return 0
        if args.scheduled and _read_baseline(_home()) is None:
            report = scheduled_baseline(args.config)
        else:
            report = update(args.config, quarantine_hours=args.quarantine_hours) if (args.update or args.scheduled) else doctor(args.config, accept_requalified=args.accept_requalified)
    except (OSError, ValueError, PermissionError) as exc: print(str(exc), file=sys.stderr); return 1
    if not report["ok"] and args.quiet: _notify_attention(_home(), report)
    elif (args.update or args.scheduled) and args.quiet:
        versions = [str(event.get("current")) for event in report.get("updates", []) if event.get("status") == "updated"]
        if versions: _notify_updated(", ".join(versions))
    if args.json: print(json.dumps(report, sort_keys=True))
    elif not args.quiet:
        for line in report["needs_you"]: print(line)
        if report["ok"] and not report["changed"]: print("Dependencies are unchanged.")
    return 0 if report["ok"] else 1
