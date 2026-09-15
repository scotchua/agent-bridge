"""Delegation-first gate: no implementation write without a routing receipt.

The stage router already decides *where* a stage runs (``stage_register``
then ``stage_claim`` pick the first fresh eligible route). What it could
not do was stop an agent from simply editing files without ever asking.
This module closes that gap for the two clients that expose a tool hook:

* a **routing receipt** is written by the orchestration MCP tool
  ``routing_decide`` only after the caller proves a stage binding (the
  stage is owned, by this owner, at this revision), exactly the proof
  ``execution_dispatch`` demands. The receipt names the repository, the
  stage, the route that owns it and when it stops being valid;
* a **PreToolUse hook** (``bin/agent-bridge-gate-hook``) runs inside
  Claude Code and the Codex CLI before every editing tool and every shell
  command. It allows an edit in a repository only while a fresh receipt
  says the stage is owned by *this* client's route. No receipt, an expired
  one, or a stage owned by the other route is a deny with a reason the
  agent can act on: claim, or dispatch, instead of editing.

What this is and is not. The hook is enforced by the host, not by a
sentence in an instruction file: Claude Code and Codex refuse the tool
call when the hook says deny. It covers Claude Code and the Codex CLI and
nothing else. A Claude Desktop chat, the Codex desktop app and the Codex
web product have no hook surface and cannot be intercepted; a human
editing in a terminal is not intercepted; Claude Code's ``--safe-mode``
or ``disableAllHooks`` and Codex's ``--dangerously-bypass-hook-trust``
switch hooks off; a Codex hook that has not been trusted once in its
``/hooks`` view does not run. The shell-command check is a heuristic over
the command text and is stated as such: it catches the ordinary ways a
shell writes a file and can be evaded by an agent that means to. The
editing tools are the deterministic part.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from .. import store
from ..capacity_router import RoutingError

CLIENTS = ("claude", "codex")
RECEIPT_DIR = "routing"
AUDIT_LEDGER = "audit.jsonl"
EVENT_LEDGER = "gate-events.jsonl"
RECEIPT_VERSION = 1
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 8 * 3600
MAX_REASON = 500
HOOK_NAME = "agent-bridge-gate-hook"

#: Tools whose call is an edit of named files.
EDIT_TOOLS = {
    "claude": frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"}),
    "codex": frozenset({"apply_patch"}),
}
#: Tools whose call runs a shell command.
SHELL_TOOLS = {
    "claude": frozenset({"Bash"}),
    "codex": frozenset({"local_shell", "shell", "shell_command", "exec_command"}),
}
PATH_FIELDS = ("file_path", "notebook_path", "path")
#: The matcher each client's hook entry carries.
MATCHERS = {
    "claude": "Edit|Write|MultiEdit|NotebookEdit|Bash",
    "codex": "apply_patch|local_shell|shell|shell_command|exec_command",
}

_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)

#: Command shapes that write to the working tree or repository. A heuristic,
#: matched against the command text; see the module docstring.
_WRITE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?<![<>])>{1,2}(?!&)",                 # redirection into a file
    r"(^|[\s;&|('\"])tee(\s|$)",
    r"(^|[\s;&|('\"])sed\s+(-[a-zA-Z]*i|--in-place)",
    r"(^|[\s;&|('\"])(rm|mv|cp|touch|mkdir|rmdir|chmod|chown|ln|install|truncate|dd|patch|unlink|shred)(\s|$)",
    r"(^|[\s;&|('\"])git\s+(commit|apply|am|push|checkout|switch|reset|merge|rebase|stash|cherry-pick|revert|clean|rm|mv|add|restore|worktree|branch\s+-[dDmM]|tag)(\s|$)",
    r"(^|[\s;&|('\"])(pip3?|npm|pnpm|yarn|cargo|go|uv|poetry|brew)\s+(install|add|remove|uninstall|update|upgrade|link)(\s|$)",
    r"(^|[\s;&|('\"])(python3?|node|ruby|perl|php)\s+-c\s",
    r"(^|[\s;&|('\"])(python3?|node|ruby|perl|bash|sh|zsh)\s+-\s*($|<)",
    r"<<-?\s*['\"]?\w+['\"]?",                # here-document
    r"(^|[\s;&|('\"])(black|ruff|isort|prettier|gofmt|rustfmt|autopep8|eslint\s+--fix)(\s|$)",
))


@dataclass(frozen=True)
class Decision:
    permission: str            # "allow" or "deny"
    code: str
    reason: str
    repos: tuple[str, ...] = ()
    receipt: dict[str, Any] | None = None
    logged: bool = True        # False for calls the gate does not judge at all

    @property
    def allowed(self) -> bool:
        return self.permission == "allow"


# ---------------------------------------------------------------- receipts


def repo_key(path: str) -> str | None:
    """The repository that contains ``path``: the nearest ancestor holding
    ``.git`` (a directory, or the file a worktree keeps). None outside any
    repository. Symlinks are resolved so two spellings key alike."""
    current = os.path.realpath(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def receipt_name(repo: str) -> str:
    return hashlib.sha256(os.path.realpath(repo).encode("utf-8")).hexdigest()[:32] + ".json"


def receipt_dir(state_root: str) -> str:
    return os.path.join(str(state_root), RECEIPT_DIR)


def receipt_path(state_root: str, repo: str) -> str:
    return os.path.join(receipt_dir(state_root), receipt_name(repo))


def route_decision(caller: str, owner_route: str) -> str:
    if owner_route == caller:
        return "self"
    if owner_route in CLIENTS:
        return "peer"
    return "local"


def record_decision(state_root: str, *, caller: str, stage_record: dict[str, Any],
                    repo: str, reason: str, ttl_seconds: int,
                    clock: Any = time.time) -> dict[str, Any]:
    """Write the receipt for an owned stage and return it.

    ``stage_record`` is the router's current view of the stage, already
    checked by the caller for ownership at the expected revision; this
    function re-checks the fields it copies so a receipt never describes a
    stage that is not owned. ``repo`` must be an existing absolute
    directory inside a repository (any path inside it keys to the same
    receipt).
    """
    if caller not in CLIENTS:
        raise RoutingError("caller_invalid")
    if not isinstance(repo, str) or not os.path.isabs(repo) or not os.path.isdir(repo):
        raise RoutingError("repo_invalid")
    repo_root = repo_key(repo)
    if repo_root is None:
        raise RoutingError("repo_not_a_repository")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise RoutingError("reason_invalid")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) \
            or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:
        raise RoutingError("ttl_invalid")
    if stage_record.get("state") != "owned" or not stage_record.get("owner_id") \
            or stage_record.get("owner_route") not in ("claude", "codex", "local"):
        raise RoutingError("execution_stage_binding_invalid")
    now = float(clock())
    valid_until = now + ttl_seconds
    lease_until = stage_record.get("lease_until")
    if isinstance(lease_until, (int, float)):
        valid_until = min(valid_until, float(lease_until))
    if valid_until <= now:
        raise RoutingError("stage_lease_expired")
    receipt = {
        "version": RECEIPT_VERSION,
        "repo": repo_root,
        "item_id": stage_record["item_id"],
        "stage": stage_record["stage"],
        "owner_id": stage_record["owner_id"],
        "owner_route": stage_record["owner_route"],
        "stage_revision": stage_record["revision"],
        "decision": route_decision(caller, stage_record["owner_route"]),
        "reason": reason.strip(),
        "caller": caller,
        "decided_at": now,
        "valid_until": valid_until,
    }
    store.atomic_write_json(receipt_path(state_root, repo_root), receipt)
    store.append_ledger(os.path.join(receipt_dir(state_root), AUDIT_LEDGER),
                        {"event": "routing_decided", **receipt})
    return receipt


def read_receipt(state_root: str, repo: str) -> dict[str, Any] | None:
    """The receipt for ``repo`` or None; ValueError for one that is not a receipt."""
    path = receipt_path(state_root, repo)
    if not os.path.exists(path):
        return None
    loaded = store.read_json(path)
    if not isinstance(loaded, dict) or loaded.get("version") != RECEIPT_VERSION \
            or not isinstance(loaded.get("valid_until"), (int, float)) \
            or loaded.get("owner_route") not in ("claude", "codex", "local") \
            or loaded.get("repo") != os.path.realpath(repo):
        raise ValueError("routing receipt is not one this gate wrote")
    return loaded


def list_receipts(state_root: str) -> list[dict[str, Any]]:
    directory = receipt_dir(state_root)
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            loaded = store.read_json(os.path.join(directory, name))
        except (OSError, ValueError):
            out.append({"file": name, "error": "unreadable"})
            continue
        out.append(loaded if isinstance(loaded, dict) else {"file": name, "error": "not an object"})
    return out


# ---------------------------------------------------------------- judgment


def patch_paths(text: str) -> list[str]:
    """Every file an ``apply_patch`` body names."""
    paths = []
    for match in _PATCH_FILE.finditer(text):
        value = match.group(1) or match.group(2)
        if value:
            paths.append(value)
    return paths


def shell_writes(command: str) -> bool:
    """Whether ``command`` looks like it writes. A heuristic; see the module docstring."""
    return any(pattern.search(command) for pattern in _WRITE_PATTERNS)


def _command_text(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        value = tool_input.get("command", tool_input.get("cmd"))
    else:
        value = tool_input
    if isinstance(value, list):
        return shlex.join(str(part) for part in value)
    return value if isinstance(value, str) else ""


def _edit_paths(client: str, tool_input: Any, cwd: str) -> list[str]:
    paths: list[str] = []
    if isinstance(tool_input, dict):
        for key in PATH_FIELDS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict):
                    for key in PATH_FIELDS:
                        value = edit.get(key)
                        if isinstance(value, str) and value:
                            paths.append(value)
        if client == "codex":
            for value in tool_input.values():
                if isinstance(value, str) and "*** Begin Patch" in value:
                    paths.extend(patch_paths(value))
    elif isinstance(tool_input, str) and "*** Begin Patch" in tool_input:
        paths.extend(patch_paths(tool_input))
    if not paths:
        paths.append(cwd)
    return [path if os.path.isabs(path) else os.path.join(cwd, path) for path in paths]


def classify(client: str, tool_name: str, tool_input: Any, cwd: str) -> tuple[str, list[str]]:
    """``("edit", paths)``, ``("shell", [cwd])`` for a writing command,
    ``("shell_read", [])`` for one that does not look like it writes, or
    ``("other", [])``."""
    if tool_name in EDIT_TOOLS[client]:
        return "edit", _edit_paths(client, tool_input, cwd)
    if tool_name in SHELL_TOOLS[client]:
        if shell_writes(_command_text(tool_input)):
            return "shell", [cwd]
        return "shell_read", []
    return "other", []


def judge(client: str, tool_name: str, tool_input: Any, cwd: str, *,
          state_root: str, clock: Any = time.time) -> Decision:
    """Allow or deny one tool call. Fails closed when the gate's own state
    cannot be read."""
    if client not in CLIENTS:
        return Decision("deny", "client_invalid", "gate configured with an unknown client")
    kind, paths = classify(client, tool_name, tool_input, cwd)
    if kind == "other":
        return Decision("allow", "not_gated", "tool is not an implementation write", logged=False)
    if kind == "shell_read":
        return Decision("allow", "shell_read_only_heuristic",
                        "command does not look like it writes (heuristic)", logged=False)
    repos = tuple(sorted({repo for repo in (repo_key(path) for path in paths) if repo}))
    if not repos:
        return Decision("allow", "outside_repository",
                        "no repository holds the target; the gate covers repositories")
    now = float(clock())
    for repo in repos:
        try:
            receipt = read_receipt(state_root, repo)
        except (OSError, ValueError) as exc:
            return Decision("deny", "gate_state_unavailable",
                            f"delegation-first gate: routing state could not be read "
                            f"({type(exc).__name__}); nothing is implemented until it can", repos)
        if receipt is None:
            return Decision(
                "deny", "no_routing_receipt",
                f"delegation-first gate: no routing receipt for {repo}. Register and claim "
                f"the stage through agent-orchestration (stage_register, stage_claim), then "
                f"call routing_decide with this repository; if the claim routes to the other "
                f"provider, use execution_dispatch instead of editing here.", repos)
        if receipt["valid_until"] <= now:
            return Decision(
                "deny", "routing_receipt_expired",
                f"delegation-first gate: the routing receipt for {repo} (stage "
                f"{receipt.get('item_id')}/{receipt.get('stage')}) expired. Renew the stage "
                f"(stage_renew) and call routing_decide again.", repos, receipt)
        if receipt["owner_route"] != client:
            return Decision(
                "deny", "routed_elsewhere",
                f"delegation-first gate: stage {receipt.get('item_id')}/{receipt.get('stage')} "
                f"in {repo} is owned by route {receipt['owner_route']}; this client does not "
                f"implement it. Dispatch through execution_dispatch or wait for that stage to "
                f"complete.", repos, receipt)
    receipt = read_receipt(state_root, repos[0])
    return Decision("allow", "routing_receipt_valid",
                    f"stage {receipt.get('item_id')}/{receipt.get('stage')} is owned by this route",
                    repos, receipt)


def hook_output(decision: Decision) -> dict[str, Any]:
    """The PreToolUse wire shape both hosts read (``hookSpecificOutput``)."""
    if decision.allowed:
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": decision.reason,
    }}


def record_event(state_root: str, client: str, tool_name: str, decision: Decision,
                 clock: Any = time.time) -> None:
    record = {"at": float(clock()), "client": client, "tool": tool_name,
              "permission": decision.permission, "code": decision.code,
              "repos": list(decision.repos)}
    if decision.receipt:
        record["item_id"] = decision.receipt.get("item_id")
        record["stage"] = decision.receipt.get("stage")
        record["owner_route"] = decision.receipt.get("owner_route")
    store.append_ledger(os.path.join(receipt_dir(state_root), EVENT_LEDGER), record)


def state_root_from_config(config_path: str) -> str:
    loaded = store.read_json(config_path)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("state_root"), str):
        raise ValueError("orchestration config has no state_root")
    return loaded["state_root"]


def run_hook(client: str, state_root: str, payload: dict[str, Any], *,
             clock: Any = time.time) -> Decision:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        return Decision("deny", "hook_input_invalid", "delegation-first gate: hook input has no tool_name")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    decision = judge(client, tool_name, payload.get("tool_input"), cwd,
                     state_root=state_root, clock=clock)
    if decision.logged:
        try:
            record_event(state_root, client, tool_name, decision, clock)
        except OSError:
            if decision.allowed:
                decision = Decision("deny", "gate_state_unavailable",
                                    "delegation-first gate: the event ledger could not be written",
                                    decision.repos, decision.receipt)
    return decision


# ------------------------------------------------------------ installation


def hook_command(root: str, client: str, config_path: str) -> str:
    launcher = os.path.join(os.path.realpath(root), "bin", HOOK_NAME)
    if os.name == "nt":
        launcher += ".cmd"
    return f"{shlex.quote(launcher)} --client {client} --config {shlex.quote(os.path.realpath(config_path))}"


def hook_entry(root: str, client: str, config_path: str) -> dict[str, Any]:
    return {"matcher": MATCHERS[client],
            "hooks": [{"type": "command", "command": hook_command(root, client, config_path),
                       "timeout": 10}]}


def _is_ours(entry: Any) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(hook, dict) and HOOK_NAME in str(hook.get("command", ""))
        for hook in entry.get("hooks", []) or [])


def hooks_file_update(path: str, entry: dict[str, Any], previous: dict[str, Any] | None,
                      *, remove: bool = False) -> bytes | None:
    """The bytes ``path`` (a Claude ``settings.json`` or Codex ``hooks.json``)
    should hold with our PreToolUse entry present (or, with ``remove``,
    absent). None when nothing changes. Refuses to touch an entry of ours
    that someone edited, and never touches anyone else's entry."""
    raw: dict[str, Any] = {}
    if os.path.exists(path):
        loaded = store.read_json(path)
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must be a JSON object")
        raw = loaded
    hooks = raw.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} has a non-object hooks setting")
    pre = hooks.get("PreToolUse", [])
    if not isinstance(pre, list):
        raise ValueError(f"{path} has a non-array hooks.PreToolUse")
    ours = [item for item in pre if _is_ours(item)]
    if len(ours) > 1:
        raise ValueError(f"{path} holds more than one {HOOK_NAME} entry; resolve by hand")
    current = ours[0] if ours else None
    if remove:
        if current is None:
            return None
        if current != previous and current != entry:
            raise ValueError(f"{path} {HOOK_NAME} entry was edited; preserve it for review")
        new_pre = [item for item in pre if not _is_ours(item)]
    else:
        if current == entry:
            return None
        if current is not None and current != previous:
            raise ValueError(f"{path} {HOOK_NAME} entry was edited; preserve it for review")
        new_pre = [item for item in pre if not _is_ours(item)] + [entry]
    new_hooks = {**hooks, "PreToolUse": new_pre}
    if not new_pre:
        new_hooks.pop("PreToolUse")
    updated = {**raw, "hooks": new_hooks}
    if not new_hooks:
        updated.pop("hooks")
    return (json.dumps(updated, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


HOOKS_FLAG_NAME = "hooks-enabled"


def codex_hooks_flag_update(path: str, *, remove: bool = False) -> bytes | None:
    """``hooks = true`` under ``[features]`` in Codex's config.toml, inside a
    managed block, unless the file already sets it. Refuses a file that sets
    it to false: turning hooks on there is the operator's decision, not this
    installer's. Codex keeps its own ``[hooks.state.*]`` trust tables in the
    same file; they are left alone."""
    import tomllib
    from ..onboard import BEGIN, END
    text = ""
    if os.path.exists(path):
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8")
    begin, end = BEGIN.format(name=HOOKS_FLAG_NAME), END.format(name=HOOKS_FLAG_NAME)
    if text.count(begin) != text.count(end) or text.count(begin) > 1:
        raise ValueError(f"{path} has damaged agent-bridge managed markers")
    newline = "\r\n" if "\r\n" in text else "\n"
    if remove:
        if begin not in text:
            return None
        before, rest = text.split(begin, 1)
        _, after = rest.split(end, 1)
        updated = before.rstrip() + newline + after.lstrip("\r\n")
        if not after.strip():
            updated = before.rstrip() + newline
        tomllib.loads(updated)
        return updated.encode("utf-8") if updated != text else None
    parsed = tomllib.loads(text) if text else {}
    if begin in text:
        return None
    features = parsed.get("features")
    if features is not None and not isinstance(features, dict):
        raise ValueError(f"{path} sets features to something other than a table")
    if isinstance(features, dict) and "hooks" in features:
        if features["hooks"] is True:
            return None
        raise ValueError(f"{path} sets features.hooks to something other than true; enable hooks explicitly first")
    lines = text.split(newline) if text else []
    header = next((i for i, line in enumerate(lines) if line.strip() == "[features]"), None)
    block = [begin, "hooks = true", end]
    if header is None:
        # A new table goes at the end, after every existing table.
        lines = [line for line in lines] if text else []
        if lines and lines[-1] != "":
            lines.append("")
        lines += ["[features]"] + block
    else:
        lines[header + 1:header + 1] = block
    updated = newline.join(lines)
    if not updated.endswith(newline):
        updated += newline
    parsed = tomllib.loads(updated)
    if parsed.get("features", {}).get("hooks") is not True:
        raise ValueError(f"{path} could not be updated to enable hooks")
    return updated.encode("utf-8")


def install_paths(home: str) -> dict[str, str]:
    from ..onboard import _paths
    onboarding = _paths(home)
    codex_home = os.path.dirname(onboarding["codex_toml"])
    return {"claude_settings": os.path.join(home, ".claude", "settings.json"),
            "codex_hooks": os.path.join(codex_home, "hooks.json"),
            "codex_toml": onboarding["codex_toml"],
            "receipt": os.path.join(home, ".agent-bridge", "onboarding", "gate-installation.json")}


def plan_install(home: str, root: str, config_path: str, clients: tuple[str, ...],
                 *, remove: bool = False) -> tuple[dict[str, bytes], dict[str, bytes | None], dict[str, Any]]:
    from ..onboard import _bytes
    paths = install_paths(home)
    originals = {path: _bytes(path) for path in paths.values()}
    receipt_raw = originals[paths["receipt"]]
    receipt = json.loads(receipt_raw) if receipt_raw else {}
    if not isinstance(receipt, dict) or (receipt and receipt.get("version") != 1):
        raise ValueError("unrecognized gate installation receipt; preserve it for review")
    previous = receipt.get("entries", {}) if isinstance(receipt.get("entries"), dict) else {}
    updates: dict[str, bytes] = {}
    entries: dict[str, Any] = {}
    for client in clients:
        if client not in CLIENTS:
            raise ValueError("client_invalid")
        entry = hook_entry(root, client, config_path)
        target = paths["claude_settings"] if client == "claude" else paths["codex_hooks"]
        content = hooks_file_update(target, entry, previous.get(client), remove=remove)
        if content is not None:
            updates[target] = content
        if not remove:
            entries[client] = entry
        if client == "codex":
            flag = codex_hooks_flag_update(paths["codex_toml"], remove=remove)
            if flag is not None:
                updates[paths["codex_toml"]] = flag
    if not remove:
        new_receipt = (json.dumps({"version": 1, "entries": entries, "config": os.path.realpath(config_path)},
                                  indent=2, sort_keys=True) + "\n").encode("utf-8")
        if receipt_raw != new_receipt:
            updates[paths["receipt"]] = new_receipt
    return updates, originals, paths


def install(home: str, root: str, config_path: str, clients: tuple[str, ...], *,
            apply: bool, remove: bool = False) -> dict[str, Any]:
    from ..onboard import _commit_updates
    updates, originals, paths = plan_install(home, root, config_path, clients, remove=remove)
    report: dict[str, Any] = {"planned_files": sorted(updates), "applied": False,
                              "remove": remove, "clients": list(clients)}
    if apply:
        report["backups"] = _commit_updates(updates, originals)
        if remove and os.path.exists(paths["receipt"]):
            os.unlink(paths["receipt"])
        report["applied"] = True
    report["codex_note"] = (
        "Codex runs a new or changed hook only after it is trusted once in its /hooks view "
        "(or with --dangerously-bypass-hook-trust, which is not recommended). Until then the "
        "Codex side of the gate is inert; `gate report` shows the trust state.")
    report["not_covered"] = NOT_COVERED
    return report


NOT_COVERED = [
    "Claude Desktop chats, the Codex desktop app and Codex on the web: no hook surface exists there",
    "a person editing files in a terminal or editor",
    "Claude Code started with --safe-mode or with disableAllHooks set",
    "Codex started with --dangerously-bypass-hook-trust, or a hook not yet trusted in /hooks",
    "shell commands that write in a way the text heuristic does not recognise",
]


# ------------------------------------------------------------------ report


def codex_trust_state(codex_toml: str, hooks_path: str) -> str:
    """``trusted``, ``needs_review`` or ``not_installed`` for our Codex entry."""
    import tomllib
    if not os.path.exists(hooks_path):
        return "not_installed"
    try:
        loaded = store.read_json(hooks_path)
        pre = loaded.get("hooks", {}).get("PreToolUse", [])
    except (OSError, ValueError, AttributeError):
        return "not_installed"
    index = next((i for i, item in enumerate(pre) if _is_ours(item)), None)
    if index is None:
        return "not_installed"
    if not os.path.exists(codex_toml):
        return "needs_review"
    with open(codex_toml, "rb") as handle:
        parsed = tomllib.loads(handle.read().decode("utf-8"))
    state = parsed.get("hooks", {}) if isinstance(parsed.get("hooks"), dict) else {}
    key = f"{os.path.realpath(hooks_path)}:pre_tool_use:{index}:0"
    entry = state.get("state", {}).get(key) if isinstance(state.get("state"), dict) else None
    if isinstance(entry, dict) and entry.get("trusted_hash"):
        return "trusted"
    return "needs_review"


def report(home: str, state_root: str, *, events: int = 20, clock: Any = time.time) -> dict[str, Any]:
    paths = install_paths(home)
    now = float(clock())
    receipts = []
    for item in list_receipts(state_root):
        valid_until = item.get("valid_until")
        status = ("error" if "error" in item else
                  "valid" if isinstance(valid_until, (int, float)) and valid_until > now else "expired")
        receipts.append({**item, "status": status})
    ledger = os.path.join(receipt_dir(state_root), EVENT_LEDGER)
    recent: list[dict[str, Any]] = []
    if os.path.exists(ledger):
        with open(ledger, "rb") as handle:
            lines = handle.read().decode("utf-8", "replace").splitlines()
        for line in lines[-events:]:
            try:
                recent.append(json.loads(line))
            except ValueError:
                recent.append({"error": "unreadable event"})
    installed = {}
    for client, path in (("claude", paths["claude_settings"]), ("codex", paths["codex_hooks"])):
        present = False
        if os.path.exists(path):
            try:
                loaded = store.read_json(path)
                present = any(_is_ours(item) for item in loaded.get("hooks", {}).get("PreToolUse", []))
            except (OSError, ValueError, AttributeError):
                present = False
        installed[client] = present
    return {
        "state_root": state_root,
        "installed": installed,
        "codex_trust": codex_trust_state(paths["codex_toml"], paths["codex_hooks"]),
        "receipts": receipts,
        "recent_events": recent,
        "denials_in_window": sum(1 for event in recent if event.get("permission") == "deny"),
        "not_covered": NOT_COVERED,
    }


# -------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=HOOK_NAME, description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    parser.add_argument("--client", choices=CLIENTS)
    parser.add_argument("--config", help="private orchestration config (state_root is read from it)")
    parser.add_argument("--state-root", help="alternative to --config")
    ins = sub.add_parser("install", help="register the PreToolUse hook in Claude Code and/or Codex")
    ins.add_argument("--home"); ins.add_argument("--root", required=True); ins.add_argument("--config", required=True)
    ins.add_argument("--clients", default="claude,codex"); ins.add_argument("--apply", action="store_true")
    ins.add_argument("--remove", action="store_true")
    rep = sub.add_parser("report", help="receipts, recent gate events, installation and trust state")
    rep.add_argument("--home"); rep.add_argument("--config"); rep.add_argument("--state-root")
    rep.add_argument("--events", type=int, default=20)
    args = parser.parse_args(argv)
    home = os.path.expanduser("~")
    if args.command == "install":
        clients = tuple(part.strip() for part in args.clients.split(",") if part.strip())
        result = install(args.home or home, args.root, args.config, clients,
                         apply=args.apply, remove=args.remove)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "report":
        state_root = args.state_root or state_root_from_config(args.config)
        print(json.dumps(report(args.home or home, state_root, events=args.events),
                         indent=2, sort_keys=True))
        return 0
    # Hook mode: judge the call described on stdin. Always exit 0; the
    # decision travels in the JSON so the host applies it, and a deny is
    # a deny whatever went wrong on the way to it.
    try:
        if not args.client:
            raise ValueError("--client is required in hook mode")
        state_root = args.state_root or state_root_from_config(args.config or "")
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        decision = run_hook(args.client, state_root, payload)
    except Exception as exc:  # noqa: BLE001  fail closed, name only the class
        decision = Decision("deny", "gate_error",
                            f"delegation-first gate: could not judge this call ({type(exc).__name__}); "
                            f"nothing is implemented until the gate can")
    sys.stdout.write(json.dumps(hook_output(decision), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
