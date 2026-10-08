"""A small, deliberately non-installing upgrade guide for older bridge users."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from . import config, drift, onboard, store
from .chat.grok import ALLOWED_COMMANDS, REQUIRED_MANIFEST_KEYS
from .chat.windows_security import prepare_private_directory
from .orchestration import autoroute, delegation, gate

FEATURES = ("agent_room", "hermes", "grok", "gemma", "output_router", "delegation_gate")


def _base(home: str) -> str:
    return os.path.join(os.path.abspath(home), ".agent-bridge")


def _upgrade_path(home: str) -> str:
    return os.path.join(_base(home), "onboarding", "upgrade.json")


def _read_json(path: str) -> Any:
    return store.read_json(path)


def _exists(path: str) -> tuple[bool, OSError | None]:
    """Tell absence from an unreadable path apart without changing it."""
    try:
        os.stat(path)
        return True, None
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, exc


def _state(state: str, reason: str) -> dict[str, str]:
    return {"state": state, "reason": reason}


def _valid_manifest(path: str) -> bool:
    """Apply the manifest part of GrokAdapter.status without queue side effects."""
    manifest = _read_json(path)
    return (isinstance(manifest, dict) and set(manifest) >= REQUIRED_MANIFEST_KEYS
            and manifest.get("product") == "Grok Bot"
            and isinstance(manifest.get("version"), str)
            and isinstance(manifest.get("tools"), list)
            and isinstance(manifest.get("allowlist"), list)
            and set(manifest["allowlist"]) == ALLOWED_COMMANDS
            and set(manifest["tools"]) == ALLOWED_COMMANDS)


def _delegation_document(home: str) -> tuple[dict[str, Any] | None, str | None]:
    path = delegation.paths_for(home, config.REPO_ROOT)["config"]
    present, problem = _exists(path)
    if problem:
        return None, f"The delegation configuration cannot be read safely: {problem}."
    if not present:
        return None, None
    try:
        raw = _read_json(path)
        if not isinstance(raw, dict):
            raise ValueError("the delegation configuration is not a JSON object")
        return raw, None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, f"The delegation configuration cannot be read safely: {exc}."


def launcher_path(home: str, platform: str) -> str:
    suffix = ".command" if platform == "darwin" else ".cmd" if platform.startswith("win") else ".sh"
    return os.path.join(_base(home), "start-agent-room" + suffix)


def _launcher_args(path: str, platform: str) -> list[str] | None:
    """Read the generated launcher without executing any part of it."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if platform.startswith("win"):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) != 2 or lines[0].lower() != "@echo off":
            return None
        # launchers only use quoted tokens, and paths with quote or percent are
        # refused when they are created, so this is a sufficient parser here.
        import re
        tokens = re.findall(r'"([^\"]*)"', lines[1])
        return tokens if tokens and " ".join(f'"{token}"' for token in tokens) == lines[1] else None
    lines = text.splitlines()
    if len(lines) != 2 or lines[0] != "#!/bin/sh" or not lines[1].startswith("exec "):
        return None
    try:
        return shlex.split(lines[1][5:])
    except ValueError:
        return None


def _launcher_companions(home: str, platform: str) -> tuple[str | None, bool]:
    args = _launcher_args(launcher_path(home, platform), platform)
    if not args:
        return None, False
    hermes_path: str | None = None
    grok = False
    for index, arg in enumerate(args[:-1]):
        if arg == "--hermes-executable":
            hermes_path = args[index + 1]
        elif arg == "--grok-state-dir":
            grok = True
    return hermes_path, grok


def detect(home: str, *, platform: str | None = None) -> dict[str, dict[str, str]]:
    """Read the upgrade state. This function never creates, repairs, or raises."""
    home = os.path.abspath(home)
    result: dict[str, dict[str, str]] = {}
    paths = onboard._paths(home)
    receipt, answers = paths["receipt"], onboard.default_answers_path(home)
    receipt_present, receipt_problem = _exists(receipt)
    answers_present, answers_problem = _exists(answers)
    if receipt_problem or answers_problem:
        result["bridge_base"] = _state("uncertain", "An earlier setup record cannot be read safely.")
    elif receipt_present or answers_present:
        result["bridge_base"] = _state("on", "An earlier agent-bridge setup record is present.")
    else:
        result["bridge_base"] = _state("available", "No earlier agent-bridge setup record was found.")

    room = os.path.join(_base(home), "chat")
    room_present, room_problem = _exists(room)
    result["agent_room"] = (_state("uncertain", f"Agent Room storage cannot be read safely: {room_problem}.")
                            if room_problem else _state(
                                "on" if room_present else "available",
                                "Agent Room storage is present." if room_present else "Agent Room can be added without installing software."))

    existing_hermes, _ = _launcher_companions(home, platform or sys.platform)
    hermes = existing_hermes or shutil.which("hermes")
    result["hermes"] = _state(
        "on" if existing_hermes else "available" if hermes else "needs_something",
        "The Agent Room launcher already includes Hermes." if existing_hermes else
        "The Hermes command is available." if hermes else "Install and sign in to the Hermes command first.")

    grok_dir = os.path.join(_base(home), "grok")
    manifest = os.path.join(grok_dir, "bot-manifest.json")
    manifest_present, manifest_problem = _exists(manifest)
    grok_present, grok_problem = _exists(grok_dir)
    if manifest_problem or grok_problem:
        result["grok"] = _state("uncertain", f"Grok storage cannot be read safely: {manifest_problem or grok_problem}.")
    elif manifest_present:
        try:
            ok = _valid_manifest(manifest)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["grok"] = _state("uncertain", f"The Grok manifest cannot be read safely: {exc}.")
        else:
            result["grok"] = _state("on" if ok else "uncertain",
                                    "The approved Grok Bot manifest is present." if ok else
                                    "The Grok manifest is not an approved Grok Bot manifest.")
    elif grok_present:
        result["grok"] = _state("uncertain", "The Grok folder exists but has no approved manifest.")
    else:
        result["grok"] = _state("available", "Grok can be connected after its desktop app is ready.")

    delegation_doc, delegation_problem = _delegation_document(home)
    if delegation_problem:
        result["gemma"] = _state("uncertain", delegation_problem)
    elif delegation_doc and delegation_doc.get("local_backend") == "gemma_certified":
        result["gemma"] = _state("on", "The delegation configuration names the certified Gemma backend.")
    elif shutil.which("ollama") is None:
        result["gemma"] = _state("needs_something", "Install Ollama first, then run the certification steps.")
    else:
        result["gemma"] = _state("available", "Ollama is available for a later Gemma certification run.")

    if delegation_problem:
        result["output_router"] = _state("uncertain", delegation_problem)
    elif delegation_doc is None:
        result["output_router"] = _state("needs_something", "Set up delegation before choosing an output routing policy.")
    else:
        state_root = delegation_doc.get("state_root")
        if not isinstance(state_root, str) or not state_root:
            result["output_router"] = _state("uncertain", "The delegation configuration has no usable state_root.")
        else:
            policy = autoroute.policy_path(state_root)
            policy_present, policy_problem = _exists(policy)
            if policy_problem:
                result["output_router"] = _state("uncertain", f"The routing policy cannot be read safely: {policy_problem}.")
            elif not policy_present:
                result["output_router"] = _state("needs_something", "Create your routing policy before choosing its output router setting.")
            else:
                try:
                    raw_policy = _read_json(policy)
                    autoroute.parse_policy(raw_policy)
                    enabled = raw_policy.get("local_first", {}).get("inline_output_router") is True
                    result["output_router"] = _state("on" if enabled else "available",
                        "Your routing policy enables the local inline output router." if enabled else
                        "Your routing policy is present and leaves this choice to you.")
                except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
                    result["output_router"] = _state("uncertain", f"The routing policy cannot be read safely: {exc}.")

    gate_receipt = gate.install_paths(home)["receipt"]
    gate_present, gate_problem = _exists(gate_receipt)
    if gate_problem:
        result["delegation_gate"] = _state("uncertain", f"The delegation-gate record cannot be read safely: {gate_problem}.")
    elif gate_present:
        result["delegation_gate"] = _state("on", "The delegation gate installation record is present.")
    else:
        try:
            blocker = onboard.delegation_platform_blocker(platform_name=platform)
            result["delegation_gate"] = _state("needs_something" if blocker else "available",
                blocker or "This computer can use the guided delegation-gate setup.")
        except (OSError, ValueError, TypeError) as exc:
            result["delegation_gate"] = _state("uncertain", f"The delegation-gate availability check failed safely: {exc}.")
    return result


def _previous_decisions(home: str) -> dict[str, str]:
    try:
        raw = _read_json(_upgrade_path(home))
        decisions = raw.get("decisions", {}) if isinstance(raw, dict) else {}
        return decisions if isinstance(decisions, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _yes(answer: str) -> bool:
    return answer.strip().lower() in {"y", "yes"}


def _yes_default(answer: str) -> bool:
    return not answer.strip() or _yes(answer)


def _card(out: Callable[[str], None], name: str, lines: tuple[str, str, str, str]) -> None:
    out(name)
    for label, value in zip(("What it does", "Where your text goes", "You need first", "What would change"), lines):
        out(f"{label}: {value}")


CARDS = {
    "agent_room": ("A local page to ask Claude and Codex, optionally Hermes or Grok, one question side by side.", "It stays on this computer, except for assistants you choose.", "Nothing extra.", "A private launcher is added to .agent-bridge."),
    "hermes": ("Adds Hermes as an optional Agent Room participant.", "Text goes to Hermes under your own account.", "The Hermes command and your account.", "Only the Agent Room launcher changes."),
    "grok": ("Adds your approved Grok Bot as an optional Agent Room participant.", "Text goes to Grok under your own account.", "The Grok Bot desktop app; you approve its two commands every time.", "A private manifest and the Agent Room launcher change."),
    "gemma": ("Runs Gemma locally through Ollama after certification.", "Text stays on this computer for that local route.", "Ollama and a certification run.", "This wizard only shows the next steps."),
    "output_router": ("Keeps long command output local and gives a short local summary.", "The full output stays on this computer.", "Your own routing-policy decision.", "This wizard only shows the policy setting."),
    "delegation_gate": ("Makes Claude and Codex record a routing decision before editing code.", "Routing records stay in your local bridge state.", "Automatic delegation enabled through onboarding.", "This wizard only shows the next steps."),
}


def _cmd_quote(value: str) -> str:
    if '"' in value or "%" in value:
        raise ValueError("Windows launcher paths cannot contain a double quote or percent sign")
    return f'"{value}"'


def launcher_bytes(home: str, platform: str, *, hermes_path: str | None, grok: bool) -> bytes:
    args = [sys.executable, os.path.join(config.REPO_ROOT, "start_chat.py"), "--open"]
    if hermes_path:
        args.extend(("--hermes-executable", os.path.abspath(hermes_path)))
    if grok:
        args.extend(("--grok-state-dir", os.path.join(_base(home), "grok")))
    if platform.startswith("win"):
        return ("@echo off\r\n" + " ".join(_cmd_quote(arg) for arg in args) + "\r\n").encode()
    return ("#!/bin/sh\nexec " + " ".join(shlex.quote(arg) for arg in args) + "\n").encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside_base(path: str, home: str) -> bool:
    try:
        base = _base(home)
        if os.path.islink(base):
            return False
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(base))) == os.path.realpath(base)
    except ValueError:
        return False


def _commit(home: str, updates: dict[str, bytes], created: list[str], *, platform: str,
            refuse_replace: set[str] | None = None, timestamp: str | None = None) -> list[dict[str, str]]:
    """Apply private files and restore every target if any write or hash check fails."""
    timestamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    originals: dict[str, bytes | None] = {}
    backups: dict[str, str] = {}
    touched: list[str] = []
    refuse_replace = refuse_replace or set()
    try:
        for path, data in updates.items():
            if not _inside_base(path, home):
                raise ValueError("upgrade refused a path outside ~/.agent-bridge")
            originals[path] = Path(path).read_bytes() if os.path.exists(path) else None
            if path in refuse_replace and originals[path] is not None:
                raise ValueError("an existing Grok manifest will not be overwritten")
            if originals[path] is not None:
                backup = f"{path}.bak-upgrade-{timestamp}"
                store.atomic_write_bytes(backup, originals[path])
                backups[path] = backup
            if path.endswith("bot-manifest.json"):
                prepare_private_directory(Path(os.path.dirname(path)))
            touched.append(path)
            store.atomic_write_bytes(path, data)
            if Path(path).read_bytes() != data:
                raise OSError(f"could not verify {path}")
            if path.endswith((".command", ".sh")):
                os.chmod(path, 0o700)
        return [{"path": path, "sha256": _sha256(updates[path])} for path in created]
    except BaseException:
        for path in reversed(touched):
            try:
                old = originals[path]
                if old is None:
                    os.unlink(path)
                elif path in backups:
                    os.replace(backups[path], path)
                else:
                    store.atomic_write_bytes(path, old)
            except OSError:
                pass
        for backup in backups.values():
            try:
                os.unlink(backup)
            except OSError:
                pass
        raise


def undo(home: str, *, out: Callable[[str], None] = print) -> int:
    record_path = _upgrade_path(home)
    present, problem = _exists(record_path)
    if problem:
        out(f"Nothing was undone: the upgrade record cannot be read safely: {problem}.")
        return 1
    if not present:
        out("Nothing to undo: no upgrade record was found.")
        return 0
    try:
        record = _read_json(record_path)
        created = record.get("created", []) if isinstance(record, dict) else []
        if not isinstance(created, list):
            raise ValueError("created is not a list")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        out(f"Nothing was undone: the upgrade record cannot be read safely: {exc}.")
        return 1
    removed = 0
    for entry in created:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
            continue
        path = entry["path"]
        if not _inside_base(path, home):
            out(f"Left alone unsafe recorded path: {path}")
            continue
        try:
            current = Path(path).read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            out(f"Left alone unreadable file: {path}")
            continue
        if _sha256(current) != entry["sha256"]:
            out(f"Left changed file alone: {path}")
            continue
        os.unlink(path)
        removed += 1
        out(f"Removed: {path}")
    try:
        os.unlink(record_path)
    except OSError as exc:
        out(f"Files were handled, but the upgrade record was left in place: {exc}.")
        return 1
    out(f"Undo complete: removed {removed} unchanged file(s) and the upgrade record.")
    return 0


def run(*, home: str, ask: Callable[[str], str] = input, out: Callable[[str], None] = print,
        platform: str | None = None) -> int:
    platform = platform or sys.platform
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        out("Refusing to run as root. Run this upgrade as your normal user.")
        return 1
    home = os.path.abspath(home)
    out("This checks first and changes nothing until you say yes at the end.")
    states = detect(home, platform=platform)
    if states["bridge_base"]["state"] == "available":
        out("This looks like a first install. See INSTALL.md, then run the normal onboard questionnaire.")
        return 0
    if states["bridge_base"]["state"] != "on":
        out(states["bridge_base"]["reason"])
        return 1

    previous = _previous_decisions(home)
    existing_hermes, launcher_grok = _launcher_companions(home, platform)
    already = [name for name in FEATURES if states[name]["state"] == "on"]
    if already:
        out("You already have: " + ", ".join(already) + ".")
    decisions: dict[str, str] = {name: "yes" for name in already}
    chosen_hermes: str | None = None
    chosen_grok = False
    create_manifest: bytes | None = None
    record_baseline = False
    install_nightly = False

    for feature in FEATURES:
        item = states[feature]
        if item["state"] == "on":
            continue
        _card(out, feature.replace("_", " ").title(), CARDS[feature])
        if item["state"] in {"needs_something", "uncertain"}:
            out(item["reason"])
            decisions[feature] = "skipped"
            continue
        if feature in {"gemma", "output_router", "delegation_gate"}:
            if feature == "gemma":
                out("Next step: docs/orchestration-mcp.md#certified-gemma-local-backend")
            elif feature == "output_router":
                doc, _ = _delegation_document(home)
                policy = autoroute.policy_path(str(doc.get("state_root"))) if doc else "your routing policy file"
                out(f'Next step: in {policy}, set "inline_output_router": true under local_first. The repository also needs mechanical_ok, the local route and a local classification.')
            else:
                out("Next step: re-run `onboard questionnaire`, answer yes to automatic delegation, then stage and apply as INSTALL.md says.")
            decisions[feature] = "guided"
            continue
        if previous.get(feature) == "not_now":
            out("You said Not now last time.")
        if not _yes(ask("Turn this on? [y/N] ")):
            decisions[feature] = "not_now"
            continue
        if feature in {"hermes", "grok"} and decisions.get("agent_room") != "yes" and states["agent_room"]["state"] != "on":
            out("This needs Agent Room. Not now.")
            decisions[feature] = "not_now"
            continue
        if feature == "hermes":
            chosen_hermes = shutil.which("hermes")
            decisions[feature] = "yes"
        elif feature == "grok":
            version = ask("Which Grok Bot version are you running? (shown in its About box) ").strip()
            if not version:
                out("Not now.")
                decisions[feature] = "not_now"
            else:
                create_manifest = (json.dumps({"product": "Grok Bot", "version": version,
                    "tools": sorted(ALLOWED_COMMANDS), "allowlist": sorted(ALLOWED_COMMANDS)}, indent=2, sort_keys=True) + "\n").encode()
                chosen_grok = True
                decisions[feature] = "yes"
        else:
            decisions[feature] = "yes"

    # Doctor cannot make a useful comparison record until an orchestration
    # config has been installed.  Keep this separate from feature choices so
    # an operator can accept the one-off baseline but decline the nightly
    # updater independently.
    config_path = drift.discover_config(home=home)
    baseline_missing = config_path is not None and drift._read_baseline(home) is None
    if baseline_missing:
        out("Doctor baseline: records this installation's free inventory before future changes are compared.")
        record_baseline = _yes_default(ask("Run doctor once to record a baseline? [Y/n] "))
        decisions["doctor_baseline"] = "yes" if record_baseline else "not_now"
        if platform == "darwin":
            out("Nightly doctor: checks for a quarantined Codex update each night. It is optional.")
            install_nightly = _yes(ask("Install the nightly doctor schedule? [y/N] "))
            decisions["doctor_schedule"] = "yes" if install_nightly else "not_now"

    include_hermes = ((shutil.which("hermes") if (existing_hermes or chosen_hermes) else None)
                      or chosen_hermes or existing_hermes)
    include_grok = launcher_grok or states["grok"]["state"] == "on" or chosen_grok
    updates: dict[str, bytes] = {}
    created: list[str] = []
    if (decisions.get("agent_room") == "yes" or
            (states["agent_room"]["state"] == "on" and (chosen_hermes or chosen_grok))):
        path = launcher_path(home, platform)
        try:
            updates[path] = launcher_bytes(home, platform, hermes_path=include_hermes, grok=include_grok)
        except ValueError as exc:
            out(f"Upgrade could not prepare the launcher: {exc}.")
            return 1
        if not os.path.exists(path):
            created.append(path)
    if create_manifest is not None:
        path = os.path.join(_base(home), "grok", "bot-manifest.json")
        if os.path.exists(path):
            out("The existing Grok manifest was left alone.")
            decisions["grok"] = "skipped"
        else:
            updates[path] = create_manifest
            created.append(path)
    record_path = _upgrade_path(home)
    record = {"version": 1, "decisions": decisions, "created": [], "updated_at": datetime.now(UTC).isoformat()}
    record["created"] = [{"path": path, "sha256": _sha256(updates[path])} for path in created]
    updates[record_path] = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    preview_paths = list(updates)
    preview_paths.extend(f"{path}.bak-upgrade-{timestamp}" for path in updates if os.path.exists(path))
    if launcher_path(home, platform) in updates:
        companions = [name for name, enabled in (("Hermes", bool(include_hermes)), ("Grok", include_grok)) if enabled]
        out("Preview: launcher will include " + (", ".join(companions) if companions else "no optional companions") + ".")
    else:
        out("Preview: no Agent Room launcher will be changed.")
    out("Preview: files that would be created or replaced:")
    for path in preview_paths:
        out(path)
    out("Already active after git pull: the macOS execution write fence, the verify-program check, and peer-round payload clearing.")
    if not _yes(ask("Apply these changes? [y/N] ")):
        out("No changes were made.")
        return 0
    try:
        _commit(home, updates, created, platform=platform, refuse_replace={os.path.join(_base(home), "grok", "bot-manifest.json")} if create_manifest else set(), timestamp=timestamp)
    except (OSError, ValueError) as exc:
        out(f"Upgrade could not be applied; changes were rolled back: {exc}.")
        return 1
    changed = [path for path in updates if path != record_path]
    out("Changed: " + (", ".join(changed) if changed else "recorded the guidance choices only") + ".")
    if decisions.get("agent_room") == "yes":
        out("Start Agent Room with: " + launcher_path(home, platform))
    if record_baseline:
        try:
            report = drift.scheduled_baseline(config_path, home=home)
            if report.get("ok"):
                out("Doctor baseline recorded.")
            else:
                out("Doctor baseline could not be recorded: " + "; ".join(report.get("needs_you", [])))
        except (OSError, ValueError, PermissionError) as exc:
            out(f"Doctor baseline could not be recorded: {exc}.")
    if install_nightly:
        try:
            drift.install_schedule(config_path, home=home)
            out("Nightly doctor schedule installed.")
        except (OSError, ValueError, PermissionError) as exc:
            out(f"Nightly doctor schedule could not be installed: {exc}.")
    out("To undo files created by this upgrade: python setup_bridge.py upgrade --undo")
    return 0


def main(argv: list[str] | None = None, *, ask: Callable[[str], str] = input,
         out: Callable[[str], None] = print, home: str | None = None,
         platform: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_bridge.py upgrade")
    parser.add_argument("--home")
    parser.add_argument("--undo", action="store_true")
    args = parser.parse_args(argv)
    selected_home = os.path.abspath(home or args.home or os.path.expanduser("~"))
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        out("Refusing to run as root. Run this upgrade as your normal user.")
        return 1
    if args.undo:
        return undo(selected_home, out=out)
    return run(home=selected_home, ask=ask, out=out, platform=platform)
