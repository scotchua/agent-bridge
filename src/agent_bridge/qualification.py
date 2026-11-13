"""Durable, owner-only state for automatic dependency requalification.

This module deliberately knows nothing about provider commands.  It turns an
inventory snapshot into stable identities, applies the small automatic-run
budget, and leaves the actual checks to :mod:`agent_bridge.drift`.
"""
from __future__ import annotations

import datetime as datetime
import hashlib
import json
import os
import time
from typing import Any

from . import store
from .platform import platform as host_platform


CONTRACT = "auto-requalify-v1"
MAX_ATTEMPTS_PER_KEY = 2
MAX_AUTOMATIC_RUNS_PER_PROVIDER_PER_DAY = 3
BACKOFF_SECONDS = 15 * 60
LOCK_STALE_SECONDS = 2 * 60 * 60


def paths(home: str) -> dict[str, str]:
    root = os.path.join(os.path.abspath(home), ".agent-bridge", "drift", "qualification")
    return {"root": root, "lock": os.path.join(root, "state.lock"),
            "flight": os.path.join(root, "flight.json"),
            "ledger": os.path.join(root, "ledger.json")}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def identity(component: str, inventory: dict[str, Any]) -> dict[str, Any]:
    """Return the complete stable identity for one qualifying component."""
    if component in {"claude", "codex"}:
        entry = dict(inventory.get("executables", {}).get(component, {}))
        path = entry.get("path")
        entry["path"] = os.path.realpath(path) if isinstance(path, str) else path
        value: Any = {"path": entry.get("path"), "version": entry.get("version"),
                      "sha256": entry.get("sha256")}
    elif component == "ollama":
        value = inventory.get("ollama", {})
    elif component == "gemma_delegate":
        value = inventory.get("gemma", {}).get("delegate", {})
    elif component == "gemma_validator":
        value = inventory.get("gemma", {}).get("validator", {})
    else:
        raise ValueError("qualification_component_invalid")
    return {"contract": CONTRACT, "component": component, "identity": value}


def key(component: str, inventory: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(identity(component, inventory)).encode("utf-8")).hexdigest()


def provider(component: str) -> str:
    return component if component in {"claude", "codex"} else "local"


def _settings(home: str) -> dict[str, Any]:
    value = store.read_json_or_none(os.path.join(os.path.abspath(home), ".agent-bridge", "drift", "settings.json"))
    return value if isinstance(value, dict) else {}


def limits(home: str) -> tuple[int, int, float]:
    settings = _settings(home)
    attempts = settings.get("auto_requalify_max_attempts", MAX_ATTEMPTS_PER_KEY)
    daily = settings.get("auto_requalify_daily_budget", MAX_AUTOMATIC_RUNS_PER_PROVIDER_PER_DAY)
    backoff = settings.get("auto_requalify_backoff_seconds", BACKOFF_SECONDS)
    return (int(attempts) if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0 else MAX_ATTEMPTS_PER_KEY,
            int(daily) if isinstance(daily, int) and not isinstance(daily, bool) and daily >= 0 else MAX_AUTOMATIC_RUNS_PER_PROVIDER_PER_DAY,
            float(backoff) if isinstance(backoff, (int, float)) and not isinstance(backoff, bool) and backoff >= 0 else BACKOFF_SECONDS)


def enabled(home: str, schedule_path: str) -> bool:
    setting = _settings(home).get("auto_requalify")
    if setting is False:
        return False
    if setting is True:
        return True
    return os.path.isfile(schedule_path)


def _state_path(home: str, identity_key: str) -> str:
    return os.path.join(paths(home)["root"], identity_key + ".json")


def _read_state(home: str, identity_key: str) -> dict[str, Any]:
    value = store.read_json_or_none(_state_path(home, identity_key))
    return value if isinstance(value, dict) else {"attempts": 0}


def _pid_alive(pid: Any) -> bool:
    return isinstance(pid, int) and pid > 0 and host_platform.process_alive(pid)


def _clear_stale_flight(home: str, now: float) -> None:
    path = paths(home)["flight"]
    flight = store.read_json_or_none(path)
    if not isinstance(flight, dict):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    started = flight.get("started_at")
    stale = not _pid_alive(flight.get("pid")) or not isinstance(started, (int, float)) or now - started > LOCK_STALE_SECONDS
    if stale:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def reserve(home: str, components: list[str], inventory: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Reserve one detached run, recording attempts before any model work."""
    now = time.time() if now is None else now
    locations = paths(home); store.secure_mkdir(locations["root"])
    with store.file_lock(locations["lock"], timeout=2.0):
        _clear_stale_flight(home, now)
        if os.path.exists(locations["flight"]):
            return {"started": False, "reason": "single_flight"}
        attempt_limit, daily_limit, backoff = limits(home)
        selected = [(item, key(item, inventory)) for item in sorted(set(components))]
        states = {item: _read_state(home, item_key) for item, item_key in selected}
        skipped: list[dict[str, Any]] = []
        eligible: list[tuple[str, str]] = []
        for item, item_key in selected:
            state = states[item]
            if int(state.get("attempts", 0)) >= attempt_limit:
                skipped.append({"component": item, "key": item_key, "reason": "attempts_exhausted"})
                continue
            next_at = state.get("next_eligible_at")
            if isinstance(next_at, (int, float)) and now < next_at:
                skipped.append({"component": item, "key": item_key, "reason": "backoff",
                                "next_eligible_at": next_at})
                continue
            eligible.append((item, item_key))
        day = datetime.datetime.fromtimestamp(now).date().isoformat()
        ledger = store.read_json_or_none(locations["ledger"])
        ledger = ledger if isinstance(ledger, dict) else {}
        usage = ledger.get(day, {}) if isinstance(ledger.get(day, {}), dict) else {}
        providers = sorted({provider(item) for item, _ in eligible})
        permitted: list[tuple[str, str]] = []
        for item_provider in providers:
            if int(usage.get(item_provider, 0)) >= daily_limit:
                skipped.extend({"component": item, "key": item_key, "reason": "daily_budget_exhausted",
                                "provider": item_provider}
                               for item, item_key in eligible if provider(item) == item_provider)
            else:
                permitted.extend((item, item_key) for item, item_key in eligible if provider(item) == item_provider)
        if not permitted:
            first = skipped[0] if skipped else {"reason": "no_eligible_components"}
            return {"started": False, "reason": first["reason"], "component": first.get("component"),
                    "key": first.get("key"), "provider": first.get("provider"), "skipped": skipped}
        keys: dict[str, dict[str, Any]] = {}
        for item, item_key in permitted:
            state = states[item]
            state.update({"component": item, "key": item_key, "identity": identity(item, inventory),
                          "attempts": int(state.get("attempts", 0)) + 1, "last_attempt_at": now,
                          "last_result": "running"})
            state.pop("next_eligible_at", None)
            store.atomic_write_json(_state_path(home, item_key), state)
            keys[item] = {"key": item_key, "identity": state["identity"]}
        for item_provider in {provider(item) for item, _ in permitted}:
            usage[item_provider] = int(usage.get(item_provider, 0)) + 1
        ledger[day] = usage
        store.atomic_write_json(locations["ledger"], ledger)
        # pid is replaced by the parent immediately after a successful spawn.
        store.atomic_write_json(locations["flight"], {"pid": os.getpid(), "started_at": now,
                                                        "components": sorted(keys), "keys": keys})
        return {"started": True, "components": sorted(keys), "keys": keys, "skipped": skipped,
                "provider_usage": usage, "day": day}


def set_flight_pid(home: str, pid: int) -> None:
    locations = paths(home)
    with store.file_lock(locations["lock"], timeout=2.0):
        flight = store.read_json_or_none(locations["flight"])
        if isinstance(flight, dict):
            flight["pid"] = pid
            store.atomic_write_json(locations["flight"], flight)


def flight(home: str) -> dict[str, Any] | None:
    value = store.read_json_or_none(paths(home)["flight"])
    return value if isinstance(value, dict) else None


def finish(home: str, passed: set[str], *, now: float | None = None) -> dict[str, Any]:
    """Record a child result and return keys that now need one attention item."""
    now = time.time() if now is None else now
    locations = paths(home); attention: list[dict[str, Any]] = []
    with store.file_lock(locations["lock"], timeout=2.0):
        active = store.read_json_or_none(locations["flight"])
        if not isinstance(active, dict):
            return {"attention": []}
        limit, _daily, backoff = limits(home)
        for component, detail in active.get("keys", {}).items():
            if not isinstance(detail, dict) or not isinstance(detail.get("key"), str):
                continue
            state = _read_state(home, detail["key"])
            if component in passed:
                state.update({"last_result": "passed", "last_result_at": now})
            else:
                exhausted = int(state.get("attempts", 0)) >= limit
                state.update({"last_result": "attempts_exhausted" if exhausted else "failed", "last_result_at": now,
                              "next_eligible_at": now + backoff})
                if exhausted and not state.get("attention_reported"):
                    state["attention_reported"] = True
                    attention.append({"component": component, "key": detail["key"],
                                      "identity": detail.get("identity", {})})
            store.atomic_write_json(_state_path(home, detail["key"]), state)
        try:
            os.unlink(locations["flight"])
        except FileNotFoundError:
            pass
    return {"attention": attention}


def report_refusal(home: str, component: str, inventory: dict[str, Any], reason: str) -> dict[str, Any] | None:
    """Mark one exhausted/budget-refused identity for a single attention item."""
    identity_key = key(component, inventory)
    locations = paths(home); store.secure_mkdir(locations["root"])
    with store.file_lock(locations["lock"], timeout=2.0):
        state = _read_state(home, identity_key)
        if state.get("attention_reported"):
            if state.get("last_result") != reason:
                state.update({"component": component, "key": identity_key,
                              "identity": identity(component, inventory), "last_result": reason})
                store.atomic_write_json(_state_path(home, identity_key), state)
            return None
        state.update({"component": component, "key": identity_key,
                      "identity": identity(component, inventory), "last_result": reason,
                      "attention_reported": True})
        store.atomic_write_json(_state_path(home, identity_key), state)
        return {"component": component, "key": identity_key, "identity": state["identity"]}


def status(home: str, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    locations = paths(home); rows: list[dict[str, Any]] = []
    try:
        names = sorted(name for name in os.listdir(locations["root"]) if name.endswith(".json") and name not in {"flight.json", "ledger.json"})
    except OSError:
        names = []
    for name in names:
        value = store.read_json_or_none(os.path.join(locations["root"], name))
        if isinstance(value, dict):
            rows.append({"key": value.get("key", name[:-5]), "component": value.get("component"),
                         "attempts": value.get("attempts", 0), "last_result": value.get("last_result"),
                         "next_eligible_at": value.get("next_eligible_at")})
    day = datetime.datetime.fromtimestamp(now).date().isoformat()
    ledger = store.read_json_or_none(locations["ledger"])
    return {"keys": rows, "today": (ledger or {}).get(day, {}) if isinstance(ledger, dict) else {}}


def attention(home: str, components: list[str], inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Return durable final-failure state for the current component identities.

    Callers use this only to add normal quick-status attention lines.  A bad or
    absent state file is deliberately indistinguishable from no final failure.
    """
    answer: list[dict[str, Any]] = []
    try:
        for component in sorted(set(components)):
            identity_key = key(component, inventory)
            state = _read_state(home, identity_key)
            reason = state.get("last_result")
            if reason in {"attempts_exhausted", "daily_budget_exhausted"}:
                answer.append({"component": component, "key": identity_key, "reason": reason,
                               "identity": state.get("identity", identity(component, inventory))})
    except Exception:
        return []
    return answer
