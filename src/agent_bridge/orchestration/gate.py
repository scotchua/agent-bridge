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

Two more things the hook does so the receipt means what it says. It
refuses the editing tools on the gate's own state (the receipts, the
ledgers, the orchestration configuration) and on the hook files
themselves (Claude's settings.json, Codex's hooks.json and config.toml),
so an agent cannot write itself a receipt or unhook itself with the same
tools the gate covers; a shell command that the text heuristic already
reads as a write is refused when it names one of those paths. And on every allow it re-reads the
stage router's database (read-only) and requires the stage the receipt
names to be owned, now, by the same owner on the same route with an unexpired lease: a receipt is a pointer to live
ownership, not a token. A forged receipt therefore has to name a stage the
router really assigned to this route, which is the delegation-first flow
itself. Whatever the shell heuristic misses is logged when it is seen and
stated as not covered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .. import store
from ..capacity_router import RoutingError, capacity_fingerprint
from . import autoroute, localfirst

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
    # Codex names its shell tool "Bash" in hook input (its hooks documentation);
    # the internal names are kept so an older or differently built host still matches.
    "codex": frozenset({"Bash", "local_shell", "shell", "shell_command", "exec_command"}),
}
PATH_FIELDS = ("file_path", "notebook_path", "path")
#: Tools whose call reads a named file whole. Claude Code's ``Read`` is
#: deterministic: the read gate judges every call, the same way ``EDIT_TOOLS``
#: is judged, rather than reading command text. Codex has no equivalent tool;
#: its reads are shell commands and are recognised by ``_shell_whole_file_read``
#: instead, so its entry here is deliberately empty (see ``classify``).
READ_TOOLS = {
    "claude": frozenset({"Read"}),
    "codex": frozenset(),
}
#: The matcher each client's hook entry carries.
MATCHERS = {
    "claude": "Edit|Write|MultiEdit|NotebookEdit|Bash|Read",
    "codex": "apply_patch|Bash|local_shell|shell|shell_command|exec_command",
}
#: The matcher each client's *PostToolUse* hook entry carries (Phase 5,
#: design section 2.9). Claude only: Phase 0 (docs/verified-cli-behaviour.md)
#: confirmed ``PostToolUse`` fires for the installed Claude Code CLI, firing
#: after a tool call has already succeeded and unable to undo it, but no
#: Codex CLI was available in that sandbox to confirm whether Codex exposes
#: the event at all -- per the design's own stated fallback ("if Codex lacks
#: the event, build the Claude side and state the Codex column is not
#: countable"), Codex is deliberately absent here rather than installed on
#: an unconfirmed capability. ``codex`` is not a key in this dict at all, not
#: merely an empty one, so a caller that indexes it by client gets a clear
#: ``KeyError`` instead of a silently-empty matcher.
POST_MATCHERS = {
    "claude": "Bash",
}

_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)

#: Command shapes that write to the working tree or repository. A heuristic,
#: matched against the command text; see the module docstring.
#: One shell word: quoted (spaces allowed) or bare.
_WORD = r"('[^']*'|\"[^\"]*\"|\S+)"

_WRITE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?<![<>])>{1,2}(?!&)",                 # redirection into a file
    r"(^|[\s;&|('\"])tee(\s|$)",
    r"(^|[\s;&|('\"])sed\s+(-[a-zA-Z]*i|--in-place)",
    r"(^|[\s;&|('\"])(rm|mv|cp|rsync|touch|mkdir|rmdir|chmod|chown|ln|install|truncate|dd|patch|unlink|shred)(\s|$)",
    # ``init``, ``clone`` and ``submodule`` are here because creating a
    # repository is how the nested-.git routing escape was manufactured: a
    # second receipt key inside a repository the caller was routed away from.
    # The trailing (\s|$) keeps ``git init-db-not-a-verb`` a read.
    r"(^|[\s;&|('\"])git(\s+(-[Cc]\s+" + _WORD + r"|--[\w-]+(=" + _WORD + r")?))*\s+(commit|apply|am|push|checkout|switch|reset|merge|rebase|stash|cherry-pick|revert|clean|rm|mv|add|restore|worktree|init|clone|submodule|branch\s+-[dDmM])(\s|$)",
    r"(^|[\s;&|('\"])(pip3?|npm|pnpm|yarn|cargo|go|uv|poetry|brew)\s+(install|add|remove|uninstall|update|upgrade|link)(\s|$)",
    r"(^|[\s;&|('\"])(python3?|node|ruby|perl|php)\s+-c\s",
    r"(^|[\s;&|('\"])(python3?|node|ruby|perl|bash|sh|zsh)\s+-\s*($|<)",
    r"<<-?\s*['\"]?\w+['\"]?",                # here-document
    r"(^|[\s;&|('\"])tar(\s+(-C\s+" + _WORD + r"|--[\w-]+(=" + _WORD + r")?))*\s+(-?[A-Za-z]*[xc][A-Za-z]*|--extract|--create|--get|--update|--append)(\s|$)",
    r"(^|[\s;&|('\"])(unzip|zip|gunzip|gzip|bunzip2|bzip2|xz|unxz)(\s|$)",
    r"(^|[\s;&|('\"])find\s.*(\s-delete(\s|$)|\s-exec\s+(rm|mv|cp|sed\s+-i|chmod|chown|truncate)(\s|$))",
    r"(^|[\s;&|('\"])(gofmt\s+-w|rustfmt|eslint\s+--fix|ruff\s+format|ruff\s+(check\s+)?--fix)(\s|$)",
    # Windows. The heuristic listed only POSIX verbs, so a cmd.exe call
    # writing with a built-in read as a read: `del`, `move` and `ren` are the
    # everyday ones and none of them was here. Case-insensitive, inline, so
    # the POSIX alternations above stay case-sensitive, where `RM` is not
    # `rm`. Found while checking the Windows command quoting.
    r"(^|[\s;&|('\"])(?i:del|erase|move|copy|xcopy|robocopy|ren|rename|rd|md|"
    r"mklink|attrib|icacls|takeown|fsutil)(\s|$)",
    # PowerShell, whose cmdlets are the same verbs spelled differently.
    r"(^|[\s;&|('\"])(?i:Remove-Item|Set-Content|Add-Content|Clear-Content|New-Item|"
    r"Copy-Item|Move-Item|Rename-Item|Out-File|Set-ItemProperty|New-ItemProperty|"
    r"Remove-ItemProperty|Set-Acl|Start-Process)(\s|$)",
    # The bridge's own state-changing launchers. Their boundary set is widened
    # with a path separator, forward and back, because all three are almost
    # always invoked by their documented ``bin/`` relative path
    # (``./bin/agent-bridge-gate-hook install``), which the boundary set every
    # other pattern here uses does not recognise as a command start: the
    # character right before the name is ``/``, not whitespace or a shell
    # operator. ``agent-bridge-orchestration-verify`` makes real provider
    # calls or, with the local-first read gate, measures this machine's own
    # latency for the calibration record the gate then trusts; blocked
    # outright regardless of subcommand, the same posture the README already
    # asks a human to take toward live verification. ``gate-hook install``
    # and ``onboard apply`` change the gate's own installation and are
    # refused by the protected-path rule the moment they name the
    # orchestration config, which both require as an argument; only their
    # write-shape is added here; ``gate-hook report``/``audit`` are read-only
    # and stay unmatched.
    #
    # Case-insensitive on the launcher name and, where present, the second
    # word, for the same reason the Windows verbs above are: NTFS is
    # case-insensitive and case-preserving, so ``Agent-Bridge-Orchestration-
    # Verify.cmd`` is the same file as the lowercase spelling and the two
    # were not recognised as one until this was checked. Repeating finding
    # 57's already-fixed mistake for a different set of names is exactly the
    # kind of thing an adversarial review exists to catch.
    r"(^|[\s;&|('\"/\\])(?i:agent-bridge-orchestration-verify)(?i:\.cmd)?(\s|$)",
    r"(^|[\s;&|('\"/\\])(?i:agent-bridge-gate-hook)(?i:\.cmd)?\b[^;&|]*\b(?i:install)\b",
    r"(^|[\s;&|('\"/\\])(?i:setup_bridge\.py|agent-bridge-setup)(?i:\.cmd)?\b[^;&|]*\b(?i:onboard\s+apply)\b",
    # Every one of the three patterns above matches only the launcher
    # SCRIPT's own filename. Both ``orchestration/delegation_verify.py`` and
    # ``orchestration/gate.py`` are ordinary modules with their own
    # ``if __name__ == "__main__"`` guard, exactly like the launcher scripts
    # that ``exec`` into them (see ``bin/agent-bridge-gate-hook``'s own
    # ``exec "$PY" -P -m agent_bridge.orchestration.gate "$@"``), so
    # ``python3 -m agent_bridge.orchestration.delegation_verify --config ...``
    # reaches the exact same ``main()`` the launcher does while matching
    # none of the three patterns above at all: found live by an adversarial
    # review, which measured it allowed and unlogged (``shell_read_only_
    # heuristic``) for the full live-verification path, which makes real,
    # cost-incurring provider calls, not merely for calibrate. Blocked here
    # by the ``-m`` invocation itself; ``onboard.py`` has no ``__main__``
    # guard, so it has no equivalent ``-m`` form to close (its only
    # subprocess path is ``python3 -c "from agent_bridge import onboard; ..."``,
    # already caught by the ``-c`` pattern above).
    r"(?i:(^|[\s;&|('\"])-m\s+agent_bridge\.orchestration\.delegation_verify(\s|$))",
    r"(?i:(^|[\s;&|('\"])-m\s+agent_bridge\.orchestration\.gate)\b[^;&|]*\b(?i:install)\b",
))
#: Formatters that write unless asked only to report.
_FORMATTERS = re.compile(r"(^|[\s;&|('\"])(black|isort|prettier|autopep8)(\s|$)")
_FORMATTER_REPORT_ONLY = re.compile(r"(^|\s)(--check(-only)?|--diff|--dry-run|-l|--list-different)(\s|$)")
#: ``git tag`` writes unless it only lists or inspects.
_GIT_TAG = re.compile(r"(^|[\s;&|('\"])git\s+tag(\s+(?P<rest>[^;&|]*))?")
_GIT_TAG_READ_FLAGS = ("-l", "--list", "-n", "--contains", "--no-contains", "--points-at",
                       "--merged", "--no-merged", "--sort", "--format", "--verify", "-v", "--column")

#: Codex whole-file readers (design section 2.1): invoking one of these
#: reads a file's entire content, the read-gate equivalent of Claude's
#: ``Read`` tool. ``head``, ``tail``, ``sed -n``, ``grep`` and ``rg`` are
#: deliberately absent -- exact or bounded reads, the "exact tools first"
#: the instruction file already asks for, and allowed on purpose. Windows'
#: ``type`` and PowerShell's ``Get-Content`` are the same word other contexts
#: use for something else (a POSIX shell builtin that reports what a name
#: resolves to; a bare ``Get-Content`` invocation without a matching argument);
#: a false match here still only widens what gets a digest offered before a
#: read, never what gets refused outright, and an operand that turns out not
#: to be a regular file falls straight through the fast-path allow below.
#: Matched by resolved program name (``_program_name``, the same
#: Windows-executable-suffix stripping this file already applies to
#: recognize a shell by name) rather than a raw substring: a regex bounded
#: only by whitespace/punctuation never matched an explicitly-suffixed
#: spelling (``cat.exe``, ``type.exe``, ``more.com``), which Git-for-Windows
#: and MSYS2 command captures use routinely, and separately matched a reader
#: name used as a plain argument to an unrelated command (``echo cat``).
_WHOLE_FILE_READER_NAMES = frozenset({"cat", "less", "more", "bat", "type", "get-content"})
#: Test/build runners for Phase 5's inline output measurement (design
#: section 2.9): matched by resolved program name, the identical
#: command-name-position scan ``_WHOLE_FILE_READER_NAMES`` already uses
#: above, not a raw substring search over the whole command text. Only
#: tools that ARE a test or build runner whenever invoked by their own
#: name, regardless of arguments, are here. A general-purpose wrapper --
#: ``npm``, ``yarn``, ``pnpm``, ``make``, ``cargo``, ``go``, ``mvn``,
#: ``gradle``, ``dotnet`` -- is deliberately absent: each one runs many
#: subcommands that are not a test or build (``npm install``, ``go vet``,
#: ``cargo new``, ``make clean``), and a name-only match would mislabel
#: most of what it actually does as test/build output, misinforming the
#: audit rather than narrowing it. That gap is named in ``NOT_COVERED``
#: rather than closed by guessing at subcommands.
_TEST_BUILD_RUNNERS = frozenset({
    "pytest", "tox", "nose2", "rspec", "jest", "mocha", "vitest", "phpunit", "ctest", "rake",
})
#: ``Get-Content -TotalCount N`` / ``-Tail N`` is a bounded read, like
#: ``head``/``tail``; checked over the whole command text rather than scoped
#: to one invocation, the same simplification ``_formatter_writes`` already
#: makes for report-only flags in this file. ``:`` joins the boundary
#: alongside whitespace and ``=`` because PowerShell also colon-binds a
#: flag's value (``-Tail:5``); missing it under-recognized the bounded form
#: rather than over-recognizing it (the read gate still ran, exactly as it
#: does for the same command spelled with a space), so this widens a
#: precision gap, not a bypass.
_BOUNDED_READ_FLAGS = re.compile(r"(^|\s)(?i:-TotalCount|-Tail)(\s|=|:|$)")
#: Operators that separate one shell command from the next: a fresh
#: "command name" position follows each of these (and the start of the
#: whole command), for ``_shell_reader_words``.
_COMMAND_SEPARATORS = frozenset({";", "&", "&&", "||", "|"})
_REDIRECT_OPERATORS = frozenset({"<", ">", ">>"})
#: A leading ``NAME=value`` shell-assignment prefix (``LANG=C cat a.log``)
#: names no program; recognized so it does not consume the command-name
#: position ``_shell_reader_words`` is looking for.
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _shell_reader_words(command: str, names: frozenset[str] = _WHOLE_FILE_READER_NAMES) -> list[str]:
    """Every word, in order, that occupies a command-name position and names
    one of ``names`` (default ``_WHOLE_FILE_READER_NAMES``): the first word
    since the start of the command or the last ``;``/``&``/``&&``/``||``/``|``
    that is not a flag, an environment-assignment prefix, or a redirection
    target (``< a.log cat`` names ``a.log`` first, but ``a.log`` is not the
    command). Shared between ``_shell_whole_file_read`` (does this command
    read a file whole at all), ``_shell_read_paths`` (which resolved path
    is the reader's own name, to exclude it from the files actually read),
    and, with ``names=_TEST_BUILD_RUNNERS``, Phase 5's inline-measurement
    match over a Claude ``Bash`` command -- the same tokenizing this file
    already trusts for a Codex shell command, rather than a second,
    independently-written parser that could drift from it and reintroduce
    the exact redirection/chaining bugs the first one had already found.
    """
    words = _shell_words(command)
    expect_name = True
    previous = ""
    found = []
    for raw in words:
        word = raw.strip("'\"")
        if word in _COMMAND_SEPARATORS:
            expect_name = True
            previous = word
            continue
        is_redirect_target = word[:1] in "<>" or previous in _REDIRECT_OPERATORS
        previous = word
        if is_redirect_target or word.startswith("-") or _ENV_ASSIGNMENT.match(word):
            continue
        if expect_name:
            expect_name = False
            if _program_name(word).lower() in names:
                found.append(word)
    return found


def _shell_whole_file_read(command: str) -> bool:
    """Whether ``command`` reads a named file whole. A heuristic, scoped to
    Codex only (see ``classify``); Claude's Bash tool is not judged by it."""
    return bool(_shell_reader_words(command)) and not _BOUNDED_READ_FLAGS.search(command)


@dataclass(frozen=True)
class Decision:
    permission: str            # "allow" or "deny"
    code: str
    reason: str
    repos: tuple[str, ...] = ()
    receipt: dict[str, Any] | None = None
    logged: bool = True        # False for calls the gate does not judge at all
    #: Extra fields merged into this decision's event-ledger record: a read
    #: gate's ``waiver_reason``, ``bytes_estimate`` and ``job_id``, present
    #: only where the design calls for them. Routing decisions leave this
    #: empty; their own extra fields come from ``receipt`` instead.
    extra: dict[str, Any] = field(default_factory=dict)
    #: Other decisions ``record_event`` must also log as their own ledger
    #: line. A single call can name more than one gated-shape read (two
    #: files in one Claude Read-tool batch is not possible, but Codex's
    #: shell heuristic can match several in one command); only one
    #: ``Decision`` is ever returned as the call's own outcome, and the rest
    #: would otherwise leave a ``digest_intent`` audit row (written per path,
    #: unconditionally) with no matching gate event, breaking the audit's
    #: own adds-up invariant. Populated only by ``judge_read``; every other
    #: constructor leaves it empty.
    also_log: tuple["Decision", ...] = ()

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


def enclosing_repos(path: str) -> list[str]:
    """EVERY ancestor of ``path`` that holds ``.git``, outermost last.

    ``repo_key`` returns the nearest one, which is the right answer for
    *keying* a receipt and the wrong answer for *judging* a call. An
    adversarial review found the gap and it was reachable end to end with
    gate-allowed commands only: a client denied at a routed repository root
    ran ``git init vendor``, which the shell heuristic did not read as a
    write, and ``vendor`` became its own receipt key. That key had no policy
    entry, so the automatic decision retained it, and the client then edited
    and overwrote files inside the very repository it had been routed away
    from, including files that already existed. Worse, a repository that
    already contains a submodule or a linked worktree has the same hole with
    no command at all.

    So judgment considers the whole chain. ``repo_key`` is deliberately left
    alone: it is also what ``record_decision`` and ``read_receipt`` key
    receipts by, and repointing it would re-key every receipt already on
    disk.
    """
    found: list[str] = []
    current = os.path.realpath(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            found.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return found
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
                    clock: Any = time.time, code: str | None = None,
                    considered: dict[str, Any] | None = None,
                    automatic: bool = False,
                    policy_fingerprint: str | None = None,
                    capacity_fingerprint: str | None = None) -> dict[str, Any]:
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
    if not math.isfinite(now):
        raise RoutingError("clock_invalid")
    valid_until = now + ttl_seconds
    lease_until = stage_record.get("lease_until")
    if isinstance(lease_until, (int, float)) and not isinstance(lease_until, bool):
        if not math.isfinite(float(lease_until)):
            raise RoutingError("execution_stage_binding_invalid")
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
    # A receipt written by the automatic policy carries the code it decided
    # under and the inputs it saw, so the decision can be re-derived rather
    # than taken on trust. A receipt written by an agent calling
    # routing_decide carries neither, and the absent ``automatic`` flag is
    # how an audit tells the two apart.
    if automatic:
        receipt["automatic"] = True
    if code is not None:
        receipt["code"] = code
    if considered is not None:
        receipt["considered"] = considered
    if policy_fingerprint is not None:
        receipt["policy_fingerprint"] = policy_fingerprint
    if capacity_fingerprint is not None:
        receipt["capacity_fingerprint"] = capacity_fingerprint
    # The audit line first: a receipt that exists is always accounted for,
    # while an audit line without a receipt is only a decision that failed
    # to take effect.
    store.append_ledger(os.path.join(receipt_dir(state_root), AUDIT_LEDGER),
                        {"event": "routing_decided", **receipt})
    store.atomic_write_json(receipt_path(state_root, repo_root), receipt)
    return receipt


def read_receipt(state_root: str, repo: str) -> dict[str, Any] | None:
    """The receipt for ``repo`` or None; ValueError for one that is not a receipt.

    Reads through ``store.read_json_atomic`` rather than an ``os.path.exists``
    check followed by a separate open. ``atomic_write_json`` replaces this
    file with ``os.replace``, which on Windows is not the clean swap it is on
    POSIX: a reader can arrive in the instant there is no file, or be refused
    for a sharing violation while the replace is in flight, and both surface
    as ``OSError`` rather than "no receipt yet". ``read_json_atomic`` retries
    across that window and only raises once a receipt is genuinely absent, at
    which point this function reports that the ordinary way, as ``None``,
    rather than letting an in-flight replace look like a caller-visible crash.
    A corrupt document still fails immediately: only ``OSError`` is retried.
    """
    path = receipt_path(state_root, repo)
    try:
        loaded = store.read_json_atomic(path)
    except OSError:
        return None
    valid_until = loaded.get("valid_until") if isinstance(loaded, dict) else None
    if not isinstance(loaded, dict) or loaded.get("version") != RECEIPT_VERSION \
            or not isinstance(valid_until, (int, float)) or isinstance(valid_until, bool) \
            or not math.isfinite(float(valid_until)) \
            or loaded.get("owner_route") not in ("claude", "codex", "local") \
            or loaded.get("repo") != os.path.realpath(repo) \
            or not isinstance(loaded.get("item_id"), str) or not isinstance(loaded.get("stage"), str) \
            or not isinstance(loaded.get("owner_id"), str) \
            or not isinstance(loaded.get("stage_revision"), int) or isinstance(loaded.get("stage_revision"), bool):
        raise ValueError("routing receipt is not one this gate wrote")
    return loaded


def _sqlite_uri_path(path: str) -> str:
    """A filesystem path as the path part of a SQLite ``file:`` URI.

    Percent first, and that ordering is the fix: SQLite percent-decodes the
    path, so a directory named ``App%20Data`` was rewritten and the open
    failed with "unable to open database file". Escaping percent after the
    question mark and hash would have mangled this function's own escapes.

    The consequence was fail-closed and unusable: ``stage_binding`` returned
    ``stage_db_unavailable``, which denies every gated call in both clients,
    and ``capacity_digest`` returned None.
    """
    return (os.path.realpath(path).replace("%", "%25")
            .replace("?", "%3F").replace("#", "%23"))


def capacity_digest(capacity_db: str, now: float) -> str | None:
    """The capacity fingerprint as the hook sees it, or None if unreadable.

    Read-only, like :func:`stage_binding`: the hook never writes the router's
    state. ``None`` on an unreadable table on purpose, and it means "do not
    re-decide over this": an unreadable router is an infrastructure failure
    that ``stage_binding`` turns into a deny, and a decision made without the
    ledger would be worse than the stale one.

    Takes no client, deliberately. See :func:`capacity_router.capacity_fingerprint`
    for the livelock that a client-relative version caused.
    """
    uri = "file:" + _sqlite_uri_path(capacity_db) + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM capacity")]
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return capacity_fingerprint(rows, now)


def stage_binding(capacity_db: str, receipt: dict[str, Any], now: float) -> str | None:
    """None when the stage router still shows the receipt's stage owned by
    the receipt's owner on its route with an unexpired lease; otherwise the
    code naming what changed. The revision is not compared: a renewal bumps
    it without changing who owns the stage. The database is opened
    read-only: the hook never writes the router's state. An unreadable
    database is ``stage_db_unavailable`` (a deny, fail closed)."""
    uri = "file:" + _sqlite_uri_path(capacity_db) + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return "stage_db_unavailable"
    try:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT state, owner_id, owner_route, revision, lease_until FROM stages "
                         "WHERE item_id=? AND stage=?", (receipt["item_id"], receipt["stage"])).fetchone()
    except sqlite3.Error:
        return "stage_db_unavailable"
    finally:
        db.close()
    if row is None:
        return "stage_not_found"
    if row["state"] != "owned":
        return "stage_not_owned"
    if row["owner_id"] != receipt["owner_id"] or row["owner_route"] != receipt["owner_route"]:
        return "stage_reassigned"
    lease = row["lease_until"]
    if not isinstance(lease, (int, float)) or not math.isfinite(float(lease)) or float(lease) <= now:
        return "stage_lease_expired"
    return None


def _digest_job_state(local_queue_root: str, job_id: str) -> "dict[str, Any] | None":
    """A digest job's own ``status``/``error``, read directly from the local
    queue's database, read-only, the same way :func:`stage_binding` reads the
    stage router's. The hook must never construct ``LocalQueue`` itself:
    that class's constructor creates the queue's directory and tables as a
    side effect, which a call that only judges a ``Read`` must not do.

    None when the database or the job cannot be read; the read gate treats
    that as "not yet confirmed complete" rather than as evidence either way
    (see the read gate's own judgment), the same fail-closed posture
    ``stage_db_unavailable`` already carries for the write gate.
    """
    if not job_id:
        return None
    database = os.path.join(local_queue_root, "localq.sqlite3")
    uri = "file:" + _sqlite_uri_path(database) + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT status, error FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if row is None:
        return None
    return {"status": row["status"], "error": row["error"]}


#: SQLite keeps its journal beside the database; replacing one is replacing the store.
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def protected_paths(state_root: str, config_path: str | None, home: str,
                    capacity_db: str | None = None,
                    local_queue_root: str | None = None) -> tuple[str, ...]:
    """The gate's own state, the stage router's database (with its SQLite
    sidecars), and the files that install or disable the hook. An editing
    tool aimed under any of these is refused whatever repository they are
    in, so a client cannot write itself a receipt, replace the database the
    live check trusts, or unhook itself with a covered tool.

    ``local_queue_root``, when given, adds the local queue's own database
    (``localq.sqlite3``), the automatic intake's routing database
    (``routing.sqlite3``), and the service heartbeat (``runtime-state.json``)
    the read gate's readiness check trusts. Named as specific files, exactly
    as ``capacity_db`` is, not the whole directory: a client that could
    forge or age the heartbeat could force ``local_first``'s readiness check
    to waive on demand and read a gated file without ever digesting it,
    which is the mirror image of the escape a forged capacity row would
    have been for the write gate.
    """
    paths = [state_root]
    if config_path:
        paths.append(config_path)
    if capacity_db:
        paths.append(capacity_db)
        paths.extend(capacity_db + suffix for suffix in _SQLITE_SIDECARS)
    if local_queue_root:
        for name in ("localq.sqlite3", "routing.sqlite3"):
            full = os.path.join(local_queue_root, name)
            paths.append(full)
            paths.extend(full + suffix for suffix in _SQLITE_SIDECARS)
        paths.append(os.path.join(local_queue_root, "runtime-state.json"))
    paths.extend(install_paths(home).values())
    return tuple(sorted({os.path.realpath(path) for path in paths}))


def _under(path: str, root: str) -> bool:
    r"""Whether ``path`` is ``root`` or inside it.

    Both sides go through ``os.path.normcase``, which is a no-op on POSIX and
    on Windows lowercases and turns ``/`` into ``\``. Windows paths are
    case-insensitive and accept either separator, so without it a protected
    path named in a different case, or with forward slashes, compared unequal
    to the same path and the protected-path rule did not fire. The comparison
    stays exact on POSIX, where ``/etc/Passwd`` really is a different file
    from ``/etc/passwd``.

    Both sides also go through ``os.path.realpath``. Only ``path`` did, and
    that asymmetry was an undocumented precondition on every caller: a root
    that had not been resolved compared unequal to the same directory reached
    through a symlink, or, on Windows, through a short 8.3 name, which is the
    form ``tempfile.gettempdir`` can return. ``protected_paths`` happened to
    resolve its roots, so production was correct by coincidence rather than
    by construction. A predicate this one is the wrong place for a
    precondition nobody states.
    """
    real = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


#: Commands that act on a directory as a whole, so naming an ancestor of a
#: protected path reaches the protected path.
_TREE_VERBS = re.compile(
    r"(^|[\s;&|('\"])(rm|mv|cp|rsync|rmdir|shred|ln|chmod|chown|dd|truncate|tar|unzip|zip|"
    r"git\s+clean|git\s+worktree|find"
    # The Windows half, for the same reason it was added to the write
    # patterns: naming an ancestor of a protected path reaches the protected
    # path, and `rd /s` does that just as `rm -r` does.
    r"|(?i:del|erase|rd|move|copy|xcopy|robocopy|ren|rename|mklink|attrib|icacls|"
    r"takeown|Remove-Item|Copy-Item|Move-Item|Rename-Item|Set-Acl))(\s|$)")


def _reaches(path: str, root: str, *, through_ancestors: bool) -> bool:
    """Whether a write at ``path`` touches ``root``: the path is the root or
    below it, or (for commands that act on whole trees) the path is a
    directory above the root."""
    if _under(path, root):
        return True
    return through_ancestors and _under(root, os.path.realpath(path))


#: Programs whose command argument is another command to read.
#:
#: The POSIX names were the whole list, and on Windows that left three
#: ordinary spellings unread, each of them a way past the protected-path rule.
#: Reproduced with ntpath in place: ``bash.exe -c "rm -rf C:/Users/me/
#: .agent-bridge"`` was ALLOWED where the identical ``bash -c`` was refused,
#: because the basename still carried ``.exe``; ``powershell -Command "..."``
#: and ``cmd /c "..."`` were allowed because neither the program nor the flag
#: was recognised at all. Claude Code's Bash tool on Windows runs through Git
#: for Windows, so these are the spellings that actually occur there.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish",
                     "cmd", "powershell", "pwsh"})
#: Extensions Windows appends to an executable, stripped before matching the
#: name above. PATHEXT holds more; these are the ones a shell is spelled with.
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
#: ``sh -c`` and the Windows equivalents: ``cmd /c`` and ``/k``, PowerShell's
#: ``-Command`` and ``-EncodedCommand``. Case-insensitive, because Windows
#: flags are. ``-File`` is deliberately absent: it names a script to run, not
#: a command to read, so recursing into it would be reading a path as a
#: command.
_SHELL_COMMAND_FLAGS = re.compile(
    r"^(?:-[a-zA-Z]*c[a-zA-Z]*|[-/](?i:c|k|command|encodedcommand))$")


def _program_name(word: str) -> str:
    """The basename of ``word`` with any Windows executable suffix removed."""
    name = os.path.basename(word)
    lowered = name.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _shell_words(command: str) -> list[str]:
    """Split a command the way its shell would, keeping backslashes on
    Windows. Non-POSIX ``shlex`` does not protect a space inside a quote
    that opens mid-word (``--flag='a b'``), so both platforms use the POSIX
    lexer; on Windows the backslash is a path separator, not an escape."""
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if os.name == "nt":
        lexer.escape = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _command_paths(command: str, cwd: str) -> list[str]:
    """Every operand of a shell command, resolved against ``cwd``: absolute
    and relative paths alike (a bare ``build`` is ``cwd/build``), the value
    of ``--flag=path``, the parts of ``a:b``, and the target of ``>out``.
    Quoting is undone where the shell would, so a quoted path with spaces
    stays one path; only the string handed to ``sh -c`` (or another shell's
    ``-c``) is read as a nested command. A flag without a value and the
    shell operators are dropped; a flag's inline value (``--git-dir=path``,
    ``--directory=path``) is kept. Command names resolve under ``cwd`` too, which
    changes nothing for the repository they are in. A best-effort reading,
    used to bind a write to the repositories it names and to refuse writes
    aimed at protected locations."""
    words = _shell_words(command)
    found: list[str] = []
    previous: list[str] = []
    for word in words:
        raw = word
        word = word.strip("'\"")
        nested = (len(previous) >= 2 and _SHELL_COMMAND_FLAGS.match(previous[-1])
                  and _program_name(previous[-2]).lower() in _SHELLS)
        # A shell's own command flag names nothing. ``/c`` looks like a path
        # on Windows and ``-c`` does not look like one anywhere, so only the
        # Windows spelling caused trouble, and it caused plenty: translated as
        # a drive it became ``C:\``, an ancestor of every protected path, so
        # every ``cmd /c`` carrying a tree verb was refused.
        # ``bool(...)`` is load-bearing, and leaving it out cost the command
        # name out of every parsed command. ``A and B`` returns A itself when
        # A is falsy, A here was the empty ``previous`` list, and the very
        # next line appends to that same object: so this name *was*
        # ``previous``, and by the time it was tested it held one word and was
        # truthy. Every command's own program name was skipped, and 88 gate
        # tests and the 559-check suite all passed anyway.
        consumed_flag = bool(previous and _SHELL_COMMAND_FLAGS.match(word)
                             and _program_name(previous[-1]).lower() in _SHELLS)
        previous.append(raw)
        if nested:
            found.extend(_command_paths(word, cwd))
            continue
        if consumed_flag:
            continue
        word = word.lstrip("<>&|;")
        if not word or word in ("&&", "||", "|", ";", "&", ">", ">>", "<"):
            continue
        if word.startswith("-"):
            if "=" not in word:
                continue                                  # a bare flag names nothing
            word = word.split("=", 1)[1]                  # ``--flag=path`` names its value
        candidates = [word]
        if "=" in word:
            candidates.append(word.split("=", 1)[1])
        # ``a:b`` can be two operands on POSIX, so it is split there. Not on
        # Windows, where a colon is only ever a drive specification or an NTFS
        # stream name, and where splitting severed the drive letter out of any
        # path not at the start of the word. Reproduced: the nested command in
        # ``cmd /c "del C:/Users/me/.agent-bridge/routing/x.json"`` produced a
        # candidate with the drive gone, so the protected-path rule missed it.
        if os.name != "nt" and ":" in word and not re.match(r"^[A-Za-z]:[\\/]", word):
            candidates.extend(word.split(":"))
        for part in candidates:
            part = part.strip("'\"")
            if not part:
                continue
            expanded = _windows_drive_path(os.path.expanduser(part))
            # A forward slash always, not ``os.path.join``: this module's
            # documented contract for this function is a POSIX-style path
            # (see the docstring's own examples), and ``os.path.join`` on
            # Windows joins with a backslash instead, which any caller
            # comparing this output directly (rather than through ``_under``,
            # which normalizes separators itself) would read as a different
            # path than the one named.
            #
            # Already-rooted is judged by a leading separator, not
            # ``os.path.isabs``: ``ntpath.isabs("/tmp/x")`` is False on
            # Python 3.11 and True on 3.13 (a stdlib behavior change, not a
            # host difference), so a Windows CI job on one and not the other
            # is proof this must not gate on it. A leading ``/`` or ``\\`` is
            # what every candidate this function ever sees actually looks
            # like when it is already rooted; drive-letter paths take the
            # other branch below since ``os.path.isabs`` does agree on those
            # across versions.
            rooted = os.path.isabs(expanded) or expanded.startswith(("/", "\\"))
            found.append(expanded if rooted else f"{cwd.rstrip('/')}/{expanded}")
    return found


#: ``/c/Users/...`` and ``/cygdrive/c/Users/...``, the two ways a POSIX-style
#: shell on Windows spells a drive.
_MSYS_DRIVE = re.compile(r"^/{1,2}(?:cygdrive/)?([A-Za-z])(?=/)")


def _windows_drive_path(path: str) -> str:
    r"""``/c/Users/me`` as ``C:\Users\me``, on Windows only.

    Claude Code's Bash tool on Windows runs through Git for Windows, so this
    is the spelling that shell produces and accepts. ``ntpath.isabs`` calls
    ``/c/Users/me`` absolute, so the value was kept verbatim and later
    resolved against whatever the current drive happened to be, giving
    ``C:\c\Users\me``: a different directory, so the protected-path rule did
    not fire. Reproduced: ``rm -rf /c/Users/me/.agent-bridge`` was allowed
    where all three Windows spellings of the same directory were refused.

    A translation, not a validation. On POSIX ``/c/Users/me`` really is that
    path and comes back untouched.
    """
    if os.name != "nt":
        return path
    match = _MSYS_DRIVE.match(path)
    if match is None:
        return path
    remainder = path[match.end():]
    return match.group(1).upper() + ":\\" + remainder.lstrip("/").replace("/", "\\")


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


def _git_tag_writes(command: str) -> bool:
    for match in _GIT_TAG.finditer(command):
        rest = (match.group("rest") or "").strip()
        if not rest:
            continue                       # bare ``git tag`` lists
        words = rest.split()
        if any(word == flag or word.startswith(flag + "=") or (flag == "-n" and word.startswith("-n"))
               for word in words for flag in _GIT_TAG_READ_FLAGS):
            continue
        return True
    return False


def _formatter_writes(command: str) -> bool:
    return bool(_FORMATTERS.search(command)) and not _FORMATTER_REPORT_ONLY.search(command)


def shell_writes(command: str) -> bool:
    """Whether ``command`` looks like it writes. A heuristic; see the module docstring."""
    return (any(pattern.search(command) for pattern in _WRITE_PATTERNS)
            or _git_tag_writes(command) or _formatter_writes(command))


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


def infer_task_type(paths: list[str]) -> str:
    """What kind of work this call is, from what the call can actually show.

    Always ``"implementation"``. Stated as a function rather than a constant
    because the decision needs a task type and this is where it would be
    refined if a host ever gave the hook more than a tool name and a path.

    It is deliberately not ``"mechanical"``, ever. Mechanical work is what the
    local worker takes, and the local worker processes bounded inline text and
    returns a draft: it does not read files, run commands or edit anything
    (``localq.spool``). So routing a file edit to it would produce an intent
    nothing could satisfy. An earlier version of this function guessed
    "mechanical" when every target looked like a test file, which read well
    and was wrong for exactly that reason.

    The consequence, stated plainly rather than hidden: the gate compels a
    routing decision for implementation work, and automatic *local* routing
    happens at the local worker's own entry point (``work_route_local``,
    whose ``AutomaticIntake`` classifies and submits without anyone asking).
    Mechanical text work an assistant simply does in its own context produces
    no tool call, so no local mechanism can intercept it. That is a limit of
    the hook surface, not something an instruction file fixes.
    """
    return "implementation"


def _shell_read_paths(command: str, cwd: str) -> list[str]:
    """The files a whole-file-read command names, excluding the reader
    program's own name. ``_command_paths`` treats a command's own name as an
    operand too (see its docstring): harmless for the write gate's own
    purpose (a bogus "path" that fails the regular-file check downstream and
    changes nothing), but the read gate's event log and digest intent should
    name only the files actually read.

    A previous version excluded it by list position (dropping
    ``_command_paths()[0]``), on the assumption that word is always first.
    Shell redirection can place it elsewhere: ``< a.log cat`` names
    ``a.log`` before ``cat``, so dropping the first entry silently kept the
    program name and discarded the real read target -- a complete, silent
    read-gate bypass for that ordinary POSIX construction. Chaining
    (``cat a.log; cat b.log``) has the same root cause: only the first
    command's name ever fell at position zero, so every later command in
    the chain left its own name behind as a bogus extra "path". Found by
    resolved VALUE at the command-name position instead
    (``_shell_reader_words``, the same scan ``_shell_whole_file_read`` uses),
    and removed one matching occurrence per name so a file legitimately
    named after a reader (``cat cat``, reading a file called ``cat``) still
    comes back."""
    paths = _command_paths(command, cwd)
    for word in _shell_reader_words(command):
        resolved = _command_paths(word, cwd)
        if resolved and resolved[0] in paths:
            paths.remove(resolved[0])
    return paths


def _read_tool_paths(tool_input: Any, cwd: str) -> list[str]:
    """The file a ``Read``-kind tool call names. ``offset``/``limit``/``pages``
    are recorded by the caller for the event, not read here: the read gate
    judges the file's own on-disk size, never the requested range (design
    section 2.1)."""
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(path, str) or not path:
        return []
    return [path if os.path.isabs(path) else os.path.join(cwd, path)]


def classify(client: str, tool_name: str, tool_input: Any, cwd: str) -> tuple[str, list[str]]:
    """``("edit", paths)``, ``("read", paths)`` for a whole-file read,
    ``("shell", [cwd, *named paths])`` for a writing command, ``("shell_read",
    [])`` for one that neither writes nor reads a file whole, or
    ``("other", [])``."""
    if tool_name in EDIT_TOOLS[client]:
        return "edit", _edit_paths(client, tool_input, cwd)
    if tool_name in READ_TOOLS[client]:
        return "read", _read_tool_paths(tool_input, cwd)
    if tool_name in SHELL_TOOLS[client]:
        command = _command_text(tool_input)
        if shell_writes(command):
            # The command runs in cwd, and may name files elsewhere (git -C
            # /other commit, cd /other && ..., cp x /other/y). Every named
            # path counts as a place the command may write, so each
            # repository among them needs its own receipt.
            return "shell", [cwd, *_command_paths(command, cwd)]
        # The whole-file-read heuristic is Codex-only: Claude has its own
        # deterministic Read tool (matched above), and design section 2.1
        # states the two clients' read gates as separate mechanisms rather
        # than layering the shell heuristic under Claude's Bash tool too.
        if client == "codex" and _shell_whole_file_read(command):
            return "read", _shell_read_paths(command, cwd)
        return "shell_read", []
    return "other", []


#: Stage states that mean an automatic receipt has been overtaken by events
#: rather than that something is wrong. Under automatic routing each of these
#: is a reason to decide again; ``stage_db_unavailable`` is deliberately
#: absent, because a router we cannot read is an infrastructure failure and
#: must stay a deny rather than triggering a decision made without it.
_OVERTAKEN = frozenset({"stage_not_found", "stage_not_owned", "stage_reassigned",
                        "stage_lease_expired"})


def automatic_receipt_overtaken(receipt: dict[str, Any], now: float,
                                capacity_db: str | None, task_type: str,
                                policy_fingerprint: str | None = None,
                                capacity_fingerprint: str | None = None) -> bool:
    """Whether an automatic receipt should be replaced by a fresh decision.

    Five ways a recorded decision stops describing the call in front of it,
    all of them ordinary rather than exceptional:

    * the operator's routing policy has changed since it was decided, so the
      decision was made under rules that no longer apply;
    * the set of routes with eligible capacity has changed, so a decision
      that retained the work because the peer was unavailable is re-made now
      that it is, and one that routed to a peer is re-made when it goes away;
    * it was decided for a different kind of work (one receipt per
      repository, but a repository holds work of more than one kind);
    * it has expired;
    * the stage it points at is finished, reassigned or its lease lapsed,
      which is what happens after a normal ``stage_complete``.

    Before the stage case was handled, the first completed stage in a
    repository left every later edit denied with ``stage_not_owned`` and an
    instruction to claim a stage by hand, which is the opposite of automatic.
    Before the policy case was handled, classifying a repository took effect
    whenever its receipt happened to expire, up to four hours later, which
    made the operator's own document look inert. Capacity had the same
    four-hour lag for the same reason, and a receipt that says "the peer has
    no fresh capacity observation" is exactly the one that should stop being
    true the moment the peer appears.
    """
    if (policy_fingerprint is not None
            and receipt.get("policy_fingerprint") != policy_fingerprint):
        return True
    if (capacity_fingerprint is not None
            and receipt.get("capacity_fingerprint") != capacity_fingerprint):
        return True
    if str(receipt.get("stage", "")).split("#")[0] != task_type:
        return True
    valid_until = receipt.get("valid_until")
    if not isinstance(valid_until, (int, float)) or valid_until <= now:
        return True
    if capacity_db is None:
        return False
    return stage_binding(capacity_db, receipt, now) in _OVERTAKEN


# ------------------------------------------------------------- read judgment


def _same_path(a: str, b: str) -> bool:
    """Whether two already-resolved path strings name the same file, the
    same ``os.path.normcase`` treatment ``_under`` applies: a no-op on
    POSIX, and on Windows a case fold plus separator normalization. Without
    it, a digest receipt written for a path spelled in one case (or with the
    other slash direction) compared unequal to the identical file read back
    in another case -- never a bypass (the mismatch reads as "no receipt
    yet", so the strict-side outcome is only ever an extra, needless
    digest), but avoidable churn on Windows, the platform this helps.
    ``normcase`` is keyed on the OS family, not the actual volume, so it
    stays a no-op on macOS's own default case-insensitive volume too, same
    as every other POSIX one; closing that residual would need a real
    filesystem probe this project's other case-insensitivity fixes have not
    reached for either."""
    return os.path.normcase(a) == os.path.normcase(b)


def _judge_read_path(path: str, client: str, *, policy: "autoroute.Policy",
                     state_root: str, local_queue_root: str,
                     worker_executable: str, protected: tuple[str, ...],
                     clock: Any) -> "Decision | None":
    """One file named by a gated-shape read: the design's steps 1 (repository
    membership already checked by the caller; here from step 3 on) through 9.
    None means the fast path allowed it with nothing logged; a ``Decision``
    means it was judged and must be logged, allow or deny.
    """
    real_path = os.path.realpath(path)
    # A protected path can never be a valid work_digest_file target (it
    # refuses one by the same rule), so compelling a digest for one here
    # would be a deny nothing could ever satisfy. Checked before repo_key,
    # not after: state_root itself may sit inside a repository (the write
    # gate's own protected-path tests construct exactly that layout).
    if any(_under(real_path, root) for root in protected) or _under(real_path, state_root):
        return None
    repo_root = repo_key(real_path)
    if repo_root is None:
        return None
    if not policy.local_first.enabled:
        return None
    repo_policy = policy.for_repo(repo_root)
    if not repo_policy.mechanical_ok or repo_policy.classification not in autoroute.LOCAL_CLASSIFICATIONS:
        return None
    globs = localfirst.effective_globs(repo_policy, policy.local_first)
    if not localfirst.matches_any(real_path, repo_root, globs):
        return None
    try:
        info = os.stat(real_path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size < policy.local_first.read_gate_min_bytes:
        return None

    # Gated-shape from here on: every branch below is logged.
    size, mtime_ns = info.st_size, info.st_mtime_ns
    now = float(clock())
    readiness = localfirst.readiness(
        policy=policy, state_root=state_root, local_queue_root=local_queue_root,
        worker_executable=worker_executable, window_bytes=size, clock=clock)
    if not readiness.ready:
        return Decision(
            "allow", "local_first_waived",
            f"delegation-first gate: {real_path} is a mechanical artifact but the local lane "
            f"is not ready ({readiness.reason}); read allowed uncompelled",
            (repo_root,), logged=True,
            extra={"waiver_reason": readiness.code, "bytes_estimate": size})

    receipt = localfirst.read_digest_receipt(state_root, real_path)
    matches_current = (receipt is not None and isinstance(receipt.get("path"), str)
                       and _same_path(receipt["path"], real_path)
                       and receipt.get("size") == size and receipt.get("mtime_ns") == mtime_ns)
    if matches_current:
        job_id = str(receipt.get("job_id") or "")
        state = _digest_job_state(local_queue_root, job_id)
        if state is None:
            return Decision(
                "deny", "local_digest_pending",
                f"delegation-first gate: {real_path} was submitted for a local digest "
                f"(job {job_id}) but its outcome could not be confirmed. Call work_result "
                f"with job_id={job_id!r} and retry the read once it completes.",
                (repo_root,), logged=True, extra={"job_id": job_id, "bytes_estimate": size})
        job_status = str(state.get("status") or "")
        if job_status == "complete":
            return Decision(
                "allow", "local_digest_present",
                f"a local digest of {real_path} has already completed (job {job_id})",
                (repo_root,), logged=True, extra={"job_id": job_id, "bytes_estimate": size})
        if job_status in ("failed", "unknown", "cancelled", "expired"):
            return Decision(
                "allow", "local_first_waived",
                f"delegation-first gate: the local digest of {real_path} ended {job_status} "
                f"(job {job_id}); read allowed uncompelled", (repo_root,), logged=True,
                extra={"waiver_reason": f"digest_{job_status}", "job_id": job_id,
                       "bytes_estimate": size})
        error = str(state.get("error") or "")
        if job_status == "queued" and error.startswith("deferred:"):
            deferred_reason = error[len("deferred:"):]
            return Decision(
                "allow", "local_first_waived",
                f"delegation-first gate: the local digest of {real_path} is deferred "
                f"({deferred_reason}); read allowed uncompelled", (repo_root,), logged=True,
                extra={"waiver_reason": f"digest_deferred:{deferred_reason}", "job_id": job_id,
                       "bytes_estimate": size})
        # queued (not deferred) or running: the digest is in flight.
        return Decision(
            "deny", "local_digest_pending",
            f"delegation-first gate: {real_path} is being digested (job {job_id}, "
            f"{job_status}). Call work_result with job_id={job_id!r} and retry the read "
            f"once it completes. Exact tools (grep, rg, tail -n) are allowed now.",
            (repo_root,), logged=True, extra={"job_id": job_id, "bytes_estimate": size})

    if (receipt is not None and isinstance(receipt.get("path"), str)
            and _same_path(receipt["path"], real_path)
            and isinstance(receipt.get("created_at"), (int, float))
            and not isinstance(receipt.get("created_at"), bool)
            and now - receipt["created_at"] <= policy.local_first.digest_grace_seconds):
        return Decision(
            "allow", "local_first_waived",
            f"delegation-first gate: {real_path} changed since its last digest, within the "
            f"{policy.local_first.digest_grace_seconds}s grace window; read allowed uncompelled",
            (repo_root,), logged=True,
            extra={"waiver_reason": "recent_digest_changed_file", "bytes_estimate": size})

    matched_glob = next((glob for glob in globs if localfirst.matches_any(real_path, repo_root, (glob,))),
                        globs[0])
    readiness_info = {"ready": readiness.ready,
                      "calibrated_median_s": readiness.considered.get("calibrated_median_s"),
                      "budget_s": readiness.considered.get("budget_s")}
    localfirst.write_digest_intent(
        state_root, path=real_path, repo=repo_root, size=size, mtime_ns=mtime_ns,
        matched_glob=matched_glob, classification=repo_policy.classification, client=client,
        readiness_info=readiness_info, digest_grace_seconds=policy.local_first.digest_grace_seconds,
        clock=clock)
    return Decision(
        "deny", "local_digest_required",
        f"delegation-first gate: {real_path} is a mechanical artifact ({size:,} bytes, "
        f"matches {matched_glob}) in a repository the operator marked mechanical_ok, and "
        f"the local lane is ready ({readiness.reason}). Call work_digest_file with "
        f"path={real_path!r}, task_type one of {'|'.join(localfirst.DIGEST_TASK_TYPES)}, then "
        f"work_result on the returned job_id; this read is allowed once the digest completes. "
        f"Exact tools (grep, rg, tail -n) are allowed now.",
        (repo_root,), logged=True, extra={"bytes_estimate": size, "matched_glob": matched_glob})


def judge_read(client: str, paths: list[str], cwd: str, *, state_root: str,
              local_queue_root: str, worker_executable: str,
              protected: tuple[str, ...] = (), clock: Any = time.time) -> Decision:
    """Allow or deny one whole-file read across every path it names.

    Each path is judged independently (design section 2.1); the first that
    denies wins the call, since the tool call as given would otherwise
    surface content from a file that has not cleared the gate. A path the
    fast path allows contributes nothing (``None``); when every path does,
    the whole call is fast-path allowed and unlogged, matching the design's
    "no ledger write" rule for the ordinary case.

    When more than one path is judged (not fast-path-allowed), only one
    ``Decision`` is returned as the call's own outcome, but every one of them
    is logged: the rest ride along on the returned decision's ``also_log``,
    which ``record_event`` also writes a ledger line for. Each judged path
    already left its own ``digest_intent`` audit-ledger row regardless (a
    per-path side effect inside ``_judge_read_path``, written independent of
    which Decision this function returns); before ``also_log`` existed, only
    the first path's outcome ever became a gate event, so a call naming two
    files that both required a digest wrote two intents but only one event,
    and the audit's own adds-up invariant broke the moment the second file's
    intent expired with nothing to count it against.

    An unreadable or malformed routing policy is a deny, the same as an
    edit -- but only once at least one path is inside a repository: outside
    every repository a read is unconditionally allowed (step 1), whatever
    state the policy is in.
    """
    in_repo = [(path, repo_key(os.path.realpath(path))) for path in paths]
    in_repo = [(path, repo) for path, repo in in_repo if repo is not None]
    if not in_repo:
        return Decision("allow", "outside_repository",
                        "no repository holds any target this read names", logged=False)
    try:
        policy = autoroute.load_policy(state_root)
    except autoroute.PolicyError as exc:
        repos = tuple(sorted({repo for _, repo in in_repo}))
        return Decision(
            "deny", "gate_auto_decision_failed",
            f"delegation-first gate: the routing policy could not be read "
            f"({type(exc).__name__}); a gated-shape read cannot be judged until it can "
            f"[policy_unreadable]", repos, logged=True)
    decisions = []
    for path, _repo in in_repo:
        outcome = _judge_read_path(
            path, client, policy=policy, state_root=state_root,
            local_queue_root=local_queue_root, worker_executable=worker_executable,
            protected=protected, clock=clock)
        if outcome is not None:
            decisions.append(outcome)
    if not decisions:
        return Decision("allow", "not_gated_shape",
                        "no read target matched the mechanical-artifact shape", logged=False)
    primary_index = next((i for i, decision in enumerate(decisions) if not decision.allowed), 0)
    primary = decisions[primary_index]
    others = tuple(decisions[:primary_index] + decisions[primary_index + 1:])
    return replace(primary, also_log=primary.also_log + others) if others else primary


def _judge_write(client: str, kind: str, paths: list[str], tool_input: Any, cwd: str, *,
                 state_root: str, clock: Any, protected: tuple[str, ...],
                 capacity_db: str | None, decide: Any) -> Decision:
    """The write/edit-side judgment for an ``"edit"`` or ``"shell"``
    classification: protected paths, then routing receipts. Split out of
    ``judge`` so a Codex shell command that both writes and reads a file
    whole (``cat a.log > b.log``) can be judged on both counts -- see
    ``judge``'s own overlay of ``judge_read`` for a whole-file read hiding
    behind a write-shaped command."""
    if kind == "edit":
        hit = [path for path in paths if any(_under(path, root) for root in protected)]
    else:
        command = _command_text(tool_input)
        named = _command_paths(command, cwd)
        whole_trees = bool(_TREE_VERBS.search(command))
        hit = [root for root in protected
               if any(_reaches(path, root, through_ancestors=whole_trees) for path in named)]
    if hit:
        return Decision("deny", "gate_state_protected",
                        "delegation-first gate: the target is the gate's own state or a hook file; "
                        "nothing may write them through this client", tuple(sorted(set(hit))))
    # Every enclosing repository, so a nested .git cannot shadow an outer
    # routed decision. See enclosing_repos for the escape this closes.
    repos = tuple(sorted({repo for path in paths for repo in enclosing_repos(path)}))
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
        task_type = infer_task_type(paths)
        if (receipt is not None and decide is not None and receipt.get("automatic")
                and automatic_receipt_overtaken(
                    receipt, now, capacity_db, task_type,
                    autoroute.policy_fingerprint(state_root),
                    None if capacity_db is None
                    else capacity_digest(capacity_db, now))):
            receipt = None
        if receipt is None and decide is not None:
            # No receipt yet: make the decision now rather than refusing and
            # asking the agent to make it. This is the automatic part.
            try:
                decide(repo, task_type)
            except Exception as exc:  # noqa: BLE001  fail closed, name the class
                # AutoDecisionError carries a fixed reason code this codebase
                # wrote, so it is safe to repeat. Any other class's text is
                # unvetted and is left out, the same rule ``errors.py`` applies
                # to every caller-visible field.
                named = getattr(exc, "args", ()) and type(exc).__name__ == "AutoDecisionError"
                detail = f"{type(exc).__name__}: {exc}" if named else type(exc).__name__
                return Decision(
                    "deny", "gate_auto_decision_failed",
                    f"delegation-first gate: the routing decision for {repo} could not be "
                    f"created ({detail}); nothing is implemented until it can be", repos)
            try:
                receipt = read_receipt(state_root, repo)
            except (OSError, ValueError) as exc:
                return Decision("deny", "gate_state_unavailable",
                                f"delegation-first gate: routing state could not be read "
                                f"({type(exc).__name__}); nothing is implemented until it can",
                                repos)
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
            route = receipt["owner_route"]
            call = "work_route_local" if route == "local" else "execution_dispatch"
            automatic = ((" The routing decision was made automatically: "
                          + str(receipt.get("code", "")) + ".")
                         if receipt.get("automatic") else "")
            return Decision(
                "deny", "routed_elsewhere",
                f"delegation-first gate: stage {receipt.get('item_id')}/{receipt.get('stage')} "
                f"in {repo} is routed to {route}; this client does not implement it."
                f"{automatic} Reason: {receipt.get('reason')}. Call {call} with "
                f"item_id={receipt.get('item_id')!r}, stage={receipt.get('stage')!r}, "
                f"owner_id={receipt.get('owner_id')!r}, "
                f"stage_revision={receipt.get('stage_revision')} and your brief; the stage is "
                f"already claimed, so no stage_register or stage_claim is needed.",
                repos, receipt)
        if capacity_db is not None:
            stale = stage_binding(capacity_db, receipt, now)
            if stale is not None:
                return Decision(
                    "deny", stale,
                    f"delegation-first gate: the routing receipt for {repo} names stage "
                    f"{receipt.get('item_id')}/{receipt.get('stage')}, but the stage router no longer "
                    f"shows it owned by {receipt.get('owner_id')} on route {receipt.get('owner_route')} "
                    f"({stale}). Claim or renew the stage, then call routing_decide again.", repos, receipt)
    receipt = read_receipt(state_root, repos[0])
    return Decision("allow", "routing_receipt_valid",
                    f"stage {receipt.get('item_id')}/{receipt.get('stage')} is owned by this route",
                    repos, receipt)


def judge(client: str, tool_name: str, tool_input: Any, cwd: str, *,
          state_root: str, clock: Any = time.time, protected: tuple[str, ...] = (),
          capacity_db: str | None = None,
          local_queue_root: str = "", worker_executable: str = "",
          decide: Any = None) -> Decision:
    """Allow or deny one tool call. Fails closed when the gate's own state
    cannot be read. ``protected`` paths refuse the editing tools outright;
    with ``capacity_db`` every allow also requires the receipt's stage to be
    owned right now (:func:`stage_binding`).

    ``decide`` makes the gate automatic. When a repository has no receipt and
    ``decide`` is supplied, it is called with ``(repo, task_type)`` and must
    create the decision (see :mod:`autodecide`): compute the route from the
    operator's policy, establish the ownership it implies, and write the
    receipt. The call is then judged against that receipt like any other, so
    work the policy retains proceeds with no friction and work it routes
    elsewhere is refused here and owed to the route the receipt names.
    Without ``decide`` the gate behaves as it did: a missing receipt is a
    deny telling the agent which calls to make.

    ``local_queue_root``/``worker_executable`` are needed only for a
    ``kind == "read"`` call, or a Codex ``"shell"`` call that also reads a
    file whole (:func:`judge_read` either way); every other kind ignores
    them, so a caller that never enables local-first can leave them at their
    empty-string default.
    """
    if client not in CLIENTS:
        return Decision("deny", "client_invalid", "gate configured with an unknown client")
    kind, paths = classify(client, tool_name, tool_input, cwd)
    if kind == "other":
        return Decision("allow", "not_gated", "tool is not an implementation write", logged=False)
    if kind == "shell_read":
        return Decision("allow", "shell_read_only_heuristic",
                        "command does not look like it writes (heuristic)", logged=False)
    if kind == "read":
        return judge_read(client, paths, cwd, state_root=state_root,
                          local_queue_root=local_queue_root,
                          worker_executable=worker_executable,
                          protected=protected, clock=clock)
    write_decision = _judge_write(client, kind, paths, tool_input, cwd, state_root=state_root,
                                  clock=clock, protected=protected, capacity_db=capacity_db,
                                  decide=decide)
    # classify's own contract (tested, unchanged) picks a command's write
    # shape over its read shape: shell_writes is checked before
    # _shell_whole_file_read, so a Codex command that both writes and reads
    # a file whole (``cat a.log > b.log``, ``cat a.log | tee b.log``) never
    # reached judge_read at all -- the read gate's entire purpose defeated
    # for any ordinary command whose text happens to also carry a
    # write-shaped suffix. Checked independently here, only once the write
    # side would otherwise allow: a command already blocked for a write
    # reason gains nothing from also being judged as a read, and judge_read
    # has the side effect of recording a fresh digest intent, which a call
    # going nowhere should not trigger.
    if kind == "shell" and client == "codex" and write_decision.allowed:
        command = _command_text(tool_input)
        if _shell_whole_file_read(command):
            read_decision = judge_read(
                client, _shell_read_paths(command, cwd), cwd, state_root=state_root,
                local_queue_root=local_queue_root, worker_executable=worker_executable,
                protected=protected, clock=clock)
            # A deny always wins over the write-side allow. An allow only
            # replaces it when there is something worth logging (a
            # gated-shape outcome): an unlogged fast-path allow (outside any
            # repository, not gated-shape) has nothing the audit needs, and
            # returning it here would discard routing_receipt_valid's own
            # receipt-bearing record for no reason.
            if not read_decision.allowed or read_decision.logged:
                return read_decision
    return write_decision


def hook_output(decision: Decision) -> dict[str, Any]:
    """The PreToolUse wire shape both hosts read (``hookSpecificOutput``)."""
    if decision.allowed:
        return {}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"{decision.reason} [{decision.code}]",
    }}


#: Fields this function sets itself; a ``Decision.extra`` value under one of
#: these keys is dropped rather than silently overwriting it. Nothing in
#: this codebase populates a colliding key today (a read decision's own
#: ``extra`` keys -- ``waiver_reason``, ``bytes_estimate``, ``job_id``,
#: ``matched_glob`` -- are all distinct from these), but ``extra`` is a
#: plain dict a future ``Decision`` construction could populate with
#: anything, and this ledger is the audit's own source of truth.
_RESERVED_EVENT_FIELDS = frozenset({
    "at", "client", "tool", "permission", "code", "repos",
    "item_id", "stage", "owner_route",
})


def record_event(state_root: str, client: str, tool_name: str, decision: Decision,
                 clock: Any = time.time) -> None:
    """Append one ledger line for ``decision``, then one more for each entry
    in its ``also_log`` -- every path a single gated-shape read judged, not
    just the one call site treats as its overall outcome (``judge_read``'s
    own docstring)."""
    for entry in (decision, *decision.also_log):
        record = {"at": float(clock()), "client": client, "tool": tool_name,
                  "permission": entry.permission, "code": entry.code,
                  "repos": list(entry.repos)}
        if entry.receipt:
            record["item_id"] = entry.receipt.get("item_id")
            record["stage"] = entry.receipt.get("stage")
            record["owner_route"] = entry.receipt.get("owner_route")
        if entry.extra:
            record.update({key: value for key, value in entry.extra.items()
                           if key not in _RESERVED_EVENT_FIELDS})
        store.append_ledger(os.path.join(receipt_dir(state_root), EVENT_LEDGER), record)


def automatic_decider(client: str, state_root: str, capacity_db: str) -> Any:
    """The callable :func:`judge` uses to create a decision that does not exist.

    Imported lazily so ``gate`` stays importable without the stage router and
    so the two modules do not import each other at module scope.
    """
    def decide(repo: str, task_type: str) -> Any:
        from .autodecide import ensure_decision

        return ensure_decision(client=client, repo=repo, state_root=state_root,
                               capacity_db=capacity_db, task_type=task_type)
    return decide


#: Phase 5's own ledger (design section 2.9): separate from ``EVENT_LEDGER``
#: because these rows are not permission decisions -- there is no
#: ``permission``/``code`` here, only a measurement -- and mixing the two
#: shapes into one file would make every existing ``EVENT_LEDGER`` reader
#: (``audit.py``'s bypass and local_first accounting) responsible for
#: skipping rows it was never written to expect.
INLINE_LEDGER = "inline-measurement.jsonl"


def _response_byte_length(tool_response: Any) -> int:
    """Bytes of a Bash call's ``stdout``/``stderr`` combined -- both reach the
    caller's context, so both count toward what "the largest mechanical
    stream" (design section 2.9) actually put there. Never raises: a
    response shape this project has not seen (a future host version, a
    malformed payload) yields 0 rather than crashing a hook that must
    still print `{}` and exit 0 either way."""
    if not isinstance(tool_response, dict):
        return 0
    total = 0
    for key in ("stdout", "stderr"):
        value = tool_response.get(key)
        if isinstance(value, str):
            total += len(value.encode("utf-8", "replace"))
    return total


def record_inline_measurement(state_root: str, client: str, tool_name: str, tool_input: Any,
                              tool_response: Any, clock: Any = time.time) -> None:
    """Append one :data:`INLINE_LEDGER` line when this call's command looks
    like a test or build runner (``_TEST_BUILD_RUNNERS``), Phase 5's own
    measurement: never a decision, and this function has no way to express
    one -- ``run_post_hook`` calls it after the tool has already run and
    always answers the host with ``{}`` regardless of what happens here.

    Claude only, matching :data:`POST_MATCHERS`: a Codex call would never
    reach this (Phase 5 installs no PostToolUse entry for it), but the
    client check stays explicit here too rather than trusting the
    installer alone to keep it out.
    """
    if client != "claude" or tool_name != "Bash":
        return
    command = _command_text(tool_input)
    matched = _shell_reader_words(command, _TEST_BUILD_RUNNERS)
    if not matched:
        return
    record = {"at": float(clock()), "client": client, "matched_runner": _program_name(matched[0]).lower(),
              "bytes": _response_byte_length(tool_response)}
    store.append_ledger(os.path.join(receipt_dir(state_root), INLINE_LEDGER), record)


def run_post_hook(client: str, state_root: str, payload: dict[str, Any],
                  clock: Any = time.time) -> None:
    """The whole of Phase 5's PostToolUse handling: measure, never decide.

    Never raises. A PostToolUse hook fires after the tool call already
    succeeded and cannot undo it (docs/verified-cli-behaviour.md); there is
    nothing for an exception here to protect against, and the only wrong
    move is to let one reach the host as an unhandled error for a call the
    host itself already completed. ``main`` always prints ``{}`` after
    calling this, whether it returns normally or this catches something.
    """
    try:
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str):
            return
        record_inline_measurement(state_root, client, tool_name, payload.get("tool_input"),
                                  payload.get("tool_response"), clock)
    except Exception:  # noqa: BLE001  measurement only; never surfaces an error
        pass


def state_root_from_config(config_path: str) -> str:
    return gate_paths_from_config(config_path)[0]


def gate_paths_from_config(config_path: str) -> tuple[str, str, str, str]:
    """``(state_root, capacity_db, local_queue_root, worker_executable)`` from
    the private orchestration configuration. ``local_queue_root`` defaults to
    ``<state_root>/local-queue`` when the config omits it, matching
    ``config/orchestration.example.json``, so an existing config written
    before the local-first read gate existed keeps working.
    ``worker_executable`` defaults to the empty string when absent, the same
    reasoning: a config predating this feature (or a test's minimal one) has
    no worker to name, and an empty string is exactly what
    ``localfirst.readiness`` already reads as "no local worker configured"
    (``worker_not_configured``), never a crash.
    """
    loaded = store.read_json(config_path)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("state_root"), str):
        raise ValueError("orchestration config has no state_root")
    if not isinstance(loaded.get("capacity_db"), str):
        raise ValueError("orchestration config has no capacity_db")
    local_queue_root = loaded.get("local_queue_root")
    if local_queue_root is None:
        local_queue_root = os.path.join(loaded["state_root"], "local-queue")
    elif not isinstance(local_queue_root, str):
        raise ValueError("orchestration config local_queue_root must be a string")
    worker_executable = loaded.get("worker_executable")
    if worker_executable is None:
        worker_executable = ""
    elif not isinstance(worker_executable, str):
        raise ValueError("orchestration config worker_executable must be a string")
    return loaded["state_root"], loaded["capacity_db"], local_queue_root, worker_executable


def _hook_input() -> str:
    r"""The PreToolUse payload, decoded as UTF-8 whatever the locale says.

    This was ``sys.stdin.read()``, which decodes with the locale encoding. On
    Windows that is the ANSI code page, and both hosts emit raw UTF-8: Node's
    ``JSON.stringify`` and Rust's ``serde_json`` do not escape non-ASCII. So a
    repository whose path contained any non-ASCII character arrived as
    mojibake, ``enclosing_repos`` found no ``.git`` above the mangled path, and
    :func:`judge` returned ``allow`` with ``outside_repository``. **The gate
    printed an empty object and the edit proceeded ungated.**

    Measured, not inferred. The same payload naming a repository with an
    e-acute in its path is denied ``routed_elsewhere`` under a UTF-8 stdin and
    allowed under ``cp1252``. That is a silent, total bypass for every user
    whose name or project path is not pure ASCII, which is most of the world.

    Reading bytes and naming the encoding fixes it here, in the gate, rather
    than in a launcher: a fix in the launcher would not protect a host that
    invokes the module directly, and the launchers set ``PYTHONUTF8`` as well
    for everything else in the process.

    Strict decoding on purpose. A payload that is not valid UTF-8 is not
    something to guess at; it raises, and hook mode turns that into a deny.
    A leading byte-order mark is tolerated, because a BOM is not a
    disagreement about the encoding, only about announcing it.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:                    # a replaced stdin, as in a test
        return sys.stdin.read()
    raw = buffer.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8")


def run_hook(client: str, state_root: str, payload: dict[str, Any], *,
             clock: Any = time.time, capacity_db: str | None = None,
             protected: tuple[str, ...] = (),
             local_queue_root: str = "", worker_executable: str = "",
             decide: Any = None) -> Decision:
    tool_name = payload.get("tool_name")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    if not isinstance(tool_name, str):
        tool_name = "unknown"
        decision = Decision("deny", "hook_input_invalid", "delegation-first gate: hook input has no tool_name")
    else:
        decision = judge(client, tool_name, payload.get("tool_input"), cwd,
                         state_root=state_root, clock=clock, protected=protected,
                         capacity_db=capacity_db, local_queue_root=local_queue_root,
                         worker_executable=worker_executable, decide=decide)
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


#: Characters that make a Windows command line need quoting.
#:
#: Space is the one that matters, because an ordinary Windows home is
#: ``C:/Users/First Last`` with backslashes. The rest are here because the
#: first version of this set had only the obvious ones, and a comma, a
#: semicolon, an equals sign, a percent and an exclamation mark are every
#: bit as significant to ``cmd.exe`` while being perfectly legal in a
#: Windows directory name: NTFS forbids only < > : " / \ | ? * . A path
#: like ``C:/dev/a=b/hook.cmd`` came back unquoted, and ``cmd.exe`` then
#: truncates the program name at the delimiter, so the hook never runs.
#: Percent is variable expansion and exclamation is delayed expansion.
_CMD_NEEDS_QUOTES = ' \t"&|<>^(),;=%!'


def quote_for_host_shell(value: str) -> str:
    r"""Quote one argument for the shell the host will run this command with.

    ``shlex.quote`` is POSIX quoting and was being used on both platforms.
    It wraps a value containing a space in **single** quotes, and
    ``cmd.exe`` does not treat single quotes as quoting at all: it would
    look for a program literally named ``'C:\Users\First``. So on any
    Windows account whose home contains a space, which is the ordinary
    case, the installed hook command was malformed, the hook never ran, and
    a hook that never runs is a gate that never gates, silently.

    Windows filenames cannot contain ``"``, so wrapping in double quotes is
    sufficient for the paths this builds. It does not attempt general
    ``cmd.exe`` escaping, and it is not a substitute for running the
    launcher on a live Windows host, which has still not happened.
    """
    if os.name != "nt":
        return shlex.quote(value)
    if value and not any(char in value for char in _CMD_NEEDS_QUOTES):
        return value
    return '"' + value.replace('"', "") + '"'


def hook_command(root: str, client: str, config_path: str, *, event: str = "PreToolUse") -> str:
    launcher = os.path.join(os.path.realpath(root), "bin", HOOK_NAME)
    if os.name == "nt":
        launcher += ".cmd"
    return (f"{quote_for_host_shell(launcher)} --client {client} "
            f"--config {quote_for_host_shell(os.path.realpath(config_path))} --event {event}")


def hook_entry(root: str, client: str, config_path: str, *, event: str = "PreToolUse") -> dict[str, Any]:
    matcher = MATCHERS[client] if event == "PreToolUse" else POST_MATCHERS[client]
    return {"matcher": matcher,
            "hooks": [{"type": "command", "command": hook_command(root, client, config_path, event=event),
                       "timeout": 10}]}


def _is_ours(entry: Any) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(hook, dict) and HOOK_NAME in str(hook.get("command", ""))
        for hook in entry.get("hooks", []) or [])


def hooks_file_update(path: str, entry: dict[str, Any], previous: dict[str, Any] | None,
                      *, event: str = "PreToolUse", remove: bool = False,
                      base: bytes | None = None) -> bytes | None:
    """The bytes ``path`` (a Claude ``settings.json`` or Codex ``hooks.json``)
    should hold with our ``event`` entry present (or, with ``remove``,
    absent). None when nothing changes. Refuses to touch an entry of ours
    that someone edited, and never touches anyone else's entry.

    ``event`` generalizes this beyond ``PreToolUse``: Phase 5 (design
    section 2.9) adds a ``PostToolUse`` entry to the same file, tracked
    entirely independently of the ``PreToolUse`` one under its own key.

    ``base``, when given, is used as this file's current bytes instead of
    reading ``path`` from disk. ``plan_install`` passes the first call's
    returned bytes as the second call's ``base`` when one install pass
    changes both a Pre and a Post entry in the same file, so the second
    call builds on the first call's in-memory result; without this, the
    second call would re-read the untouched file from disk and its
    returned bytes would silently drop the first call's change when both
    land in the same ``updates`` dict entry. ``None`` (every existing call
    site) keeps today's read-from-disk behaviour exactly.
    """
    if base is not None:
        loaded = json.loads(base.decode("utf-8")) if base.strip() else {}
    elif os.path.exists(path):
        loaded = store.read_json(path)
    else:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must be a JSON object")
    raw = loaded
    hooks = raw.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} has a non-object hooks setting")
    ours_list = hooks.get(event, [])
    if not isinstance(ours_list, list):
        raise ValueError(f"{path} has a non-array hooks.{event}")
    ours = [item for item in ours_list if _is_ours(item)]
    if len(ours) > 1:
        raise ValueError(f"{path} holds more than one {HOOK_NAME} {event} entry; resolve by hand")
    current = ours[0] if ours else None
    if remove:
        if current is None:
            return None
        if current != previous and current != entry:
            raise ValueError(f"{path} {HOOK_NAME} {event} entry was edited; preserve it for review")
        new_list = [item for item in ours_list if not _is_ours(item)]
    else:
        if current == entry:
            return None
        if current is not None and current != previous:
            raise ValueError(f"{path} {HOOK_NAME} {event} entry was edited; preserve it for review")
        new_list = [item for item in ours_list if not _is_ours(item)] + [entry]
    new_hooks = {**hooks, event: new_list}
    if not new_list:
        new_hooks.pop(event)
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
            text = handle.read().decode("utf-8-sig")
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
    features = parsed.get("features")
    if features is not None and not isinstance(features, dict):
        raise ValueError(f"{path} sets features to something other than a table")
    if isinstance(features, dict) and "hooks" in features:
        if features["hooks"] is True:
            return None
        raise ValueError(f"{path} sets features.hooks to something other than true; enable hooks explicitly first")
    if begin in text:
        raise ValueError(f"{path} holds the agent-bridge hooks block but hooks are not enabled; repair by hand")
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
                 *, remove: bool = False, include_post: bool = False
                 ) -> tuple[dict[str, bytes], dict[str, bytes | None], dict[str, Any]]:
    """``include_post`` additionally installs Phase 5's PostToolUse entry
    (design section 2.9) for every client in ``clients`` that
    :data:`POST_MATCHERS` covers (``claude`` only today). It is otherwise
    left alone -- a plain re-install run without ``include_post`` neither
    adds nor silently removes an already-opted-in Post entry, since that
    flag means "install/enable", not "this is the complete desired state".
    ``remove`` removes whatever is actually recorded for each client,
    Post entry included, regardless of ``include_post``: uninstalling the
    gate means taking out everything this tool put there.
    """
    from ..onboard import _bytes
    paths = install_paths(home)
    originals = {path: _bytes(path) for path in paths.values()}
    receipt_raw = originals[paths["receipt"]]
    receipt = json.loads(receipt_raw) if receipt_raw else {}
    if not isinstance(receipt, dict) or (receipt and receipt.get("version") != 1):
        raise ValueError("unrecognized gate installation receipt; preserve it for review")
    previous = receipt.get("entries", {}) if isinstance(receipt.get("entries"), dict) else {}
    post_previous = receipt.get("post_entries", {}) if isinstance(receipt.get("post_entries"), dict) else {}
    post_clients = tuple(client for client in clients if include_post and client in POST_MATCHERS)
    updates: dict[str, bytes] = {}
    for client in clients:
        if client not in CLIENTS:
            raise ValueError("client_invalid")
        entry = hook_entry(root, client, config_path)
        target = paths["claude_settings"] if client == "claude" else paths["codex_hooks"]
        content = hooks_file_update(target, entry, previous.get(client), remove=remove)
        touches_post = (remove and client in post_previous) or (not remove and client in post_clients)
        if touches_post:
            post_entry = hook_entry(root, client, config_path, event="PostToolUse")
            content = hooks_file_update(target, post_entry, post_previous.get(client),
                                        event="PostToolUse", remove=remove, base=content)
        if content is not None:
            updates[target] = content
        if client == "codex":
            flag = codex_hooks_flag_update(paths["codex_toml"], remove=remove)
            if flag is not None:
                updates[paths["codex_toml"]] = flag
    # Entries for clients this run does not touch are carried forward, so a
    # one-client install or removal does not forget what the other holds.
    entries = {client: value for client, value in previous.items() if client not in clients}
    if not remove:
        entries.update({client: hook_entry(root, client, config_path) for client in clients})
    # post_entries starts from every recorded entry, not just the ones this
    # run leaves alone: a client this run targets but does not touch Post
    # for (a plain re-install without include_post) must keep its recorded
    # Post entry exactly as-is, matching the file on disk, which this run
    # never changed either.
    post_entries = dict(post_previous)
    for client in clients:
        if not ((remove and client in post_previous) or (not remove and client in post_clients)):
            continue
        if remove:
            post_entries.pop(client, None)
        else:
            post_entries[client] = hook_entry(root, client, config_path, event="PostToolUse")
    if entries or post_entries:
        receipt_body: dict[str, Any] = {"version": 1, "entries": entries,
                                        "config": os.path.realpath(config_path)}
        if post_entries:
            receipt_body["post_entries"] = post_entries
        new_receipt = (json.dumps(receipt_body, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if receipt_raw != new_receipt:
            updates[paths["receipt"]] = new_receipt
    return updates, originals, paths


def _entries_remaining(receipt_raw: bytes | None, removed: tuple[str, ...]) -> bool:
    """Whether the installation receipt still records a client (a Pre or a
    Post entry) after ``removed`` leave it."""
    if not receipt_raw:
        return False
    try:
        loaded = json.loads(receipt_raw)
    except ValueError:
        return False
    if not isinstance(loaded, dict):
        return False
    for key in ("entries", "post_entries"):
        entries = loaded.get(key)
        if isinstance(entries, dict) and any(client not in removed for client in entries):
            return True
    return False


def install(home: str, root: str, config_path: str, clients: tuple[str, ...], *,
            apply: bool, remove: bool = False, include_post: bool = False) -> dict[str, Any]:
    from ..onboard import _commit_updates
    updates, originals, paths = plan_install(home, root, config_path, clients,
                                             remove=remove, include_post=include_post)
    post_clients = tuple(client for client in clients if include_post and client in POST_MATCHERS)
    report: dict[str, Any] = {"planned_files": sorted(updates), "applied": False,
                              "remove": remove, "clients": list(clients),
                              "post_clients": list(post_clients)}
    if apply:
        # claude_settings, codex_hooks and codex_toml are pre-existing files
        # Claude Code/Codex own and already had inherited access (e.g. SYSTEM,
        # Administrators on Windows) to; only the gate's own receipt is this
        # project's file, which keeps the owner-only lockdown.
        host_owned = frozenset({paths["claude_settings"], paths["codex_hooks"], paths["codex_toml"]})
        report["backups"] = _commit_updates(updates, originals, shared_paths=host_owned)
        if remove and os.path.exists(paths["receipt"]) and not _entries_remaining(originals[paths["receipt"]], clients):
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
    "shell commands that write in a way the text heuristic does not recognise, including one that "
    "reaches the gate's own state or the hook files without naming their paths",
    "installation on Windows: the gate's own .cmd launcher is written and has never been run. "
    "The gate's logic is exercised on Windows by CI; this launcher is not, and neither is the "
    "hook being invoked by Claude Code or Codex rather than as a subprocess",
    "mechanical text work an assistant performs in its own context: it produces no tool "
    "call, so no local mechanism intercepts it. The local worker's automatic intake "
    "covers work an assistant sends it, not work it never sends",
    "the brief itself: a PreToolUse payload names a tool and some paths, not the task, so "
    "automatic routing compels the dispatch but the brief's words are the assistant's",
    "mechanical text already in the assistant's own context: no Read or shell call names a "
    "file, so the read gate never sees it, the same limit infer_task_type already states "
    "for the write gate",
    "inline test and build output the assistant never captures to a file: the largest "
    "mechanical stream, and nothing here can compel a digest of text that was never read "
    "from disk",
    "files under read_gate_min_bytes, and shell reads through a spelling the whole-file-read "
    "heuristic does not recognise (Codex only; Claude's Read tool is matched by name, not text)",
    "on Codex the read gate is a text heuristic over the shell command, the same strength "
    "limit the write gate's own heuristic states; on Claude it is the Read tool, "
    "deterministic (see read_gate in this report)",
    "inline output measurement (Phase 5) on Codex: Phase 0 found no codex CLI available to "
    "confirm whether Codex exposes a PostToolUse hook at all, so nothing is installed there "
    "and the column stays not countable rather than assumed either way",
    "inline output measurement (Phase 5) for a test or build runner invoked through a "
    "general-purpose wrapper (npm, yarn, pnpm, make, cargo, go, mvn, gradle, dotnet): only a "
    "runner matched by its own program name is counted, the same narrow by-name matching the "
    "read gate's own whole-file-read heuristic already uses, because a wrapper's name alone "
    "does not say whether this particular call was a test, a build, or something else entirely",
]


# ------------------------------------------------------------------ report


def _codex_hook_key_path(hooks_path: str) -> str:
    """The path string Codex itself uses in a trust-state key, not ours.

    Traced from codex-rs (utils/home-dir/src/lib.rs::find_codex_home and
    hooks/src/engine/discovery.rs's key_source): Codex canonicalizes CODEX_HOME
    (std::fs::canonicalize -- symlinks resolved, Windows on-disk casing) only
    when the CODEX_HOME environment variable is set; the default `~/.codex`
    (dirs::home_dir() + ".codex") gets no resolution at all. Applying
    os.path.realpath() unconditionally, as this function used to, canonicalizes
    a path Codex itself never canonicalizes in the common no-CODEX_HOME-set
    case, so the two sides can name the same file with different strings and
    a real trust decision never shows as recorded."""
    if os.environ.get("CODEX_HOME"):
        return os.path.realpath(hooks_path)
    return os.path.abspath(hooks_path)


def codex_trust_state(codex_toml: str, hooks_path: str) -> str:
    """``recorded``, ``needs_review`` or ``not_installed`` for our Codex entry.

    ``recorded`` means Codex has written a trusted_hash for our entry's
    position in hooks.json. Codex computes and checks that hash itself;
    this report does not recompute it, so ``recorded`` says trust was given
    to some definition at this position, not that it matches the current
    one. Codex shows "review required" again when it does not."""
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
        parsed = tomllib.loads(handle.read().decode("utf-8-sig"))
    state = parsed.get("hooks", {}) if isinstance(parsed.get("hooks"), dict) else {}
    key = f"{_codex_hook_key_path(hooks_path)}:pre_tool_use:{index}:0"
    entry = state.get("state", {}).get(key) if isinstance(state.get("state"), dict) else None
    if isinstance(entry, dict) and entry.get("trusted_hash"):
        return "recorded"
    return "needs_review"


def _automatic_routing_state(paths: dict[str, str]) -> dict[str, Any]:
    """Whether each installed client decides automatically or refuses instead.

    Read from the installed hook command, because that is what actually runs;
    a flag recorded anywhere else would describe an intention.
    """
    state: dict[str, Any] = {}
    for client, path in (("claude", paths["claude_settings"]),
                         ("codex", paths["codex_hooks"])):
        if not os.path.exists(path):
            state[client] = "not_installed"
            continue
        try:
            loaded = store.read_json(path)
            pre = loaded.get("hooks", {}).get("PreToolUse", [])
        except (OSError, ValueError, AttributeError):
            state[client] = "unreadable"
            continue
        entry = next((item for item in pre if _is_ours(item)), None)
        if entry is None:
            state[client] = "not_installed"
            continue
        command = " ".join(str(hook.get("command", "")) for hook in entry.get("hooks", [])
                           if isinstance(hook, dict))
        state[client] = "off" if "--no-automatic-routing" in command else "on"
    return state


def _policy_state(state_root: str) -> dict[str, Any]:
    """The operator's routing policy, summarised. Never its repository names."""
    from .autoroute import load_policy, policy_path

    path = policy_path(state_root)
    if not os.path.exists(path):
        return {"path": path, "present": False,
                "consequence": "every repository is retained; nothing is dispatched"}
    try:
        policy = load_policy(state_root)
    except Exception as exc:  # noqa: BLE001  a report never crashes
        return {"path": path, "present": True, "readable": False,
                "error": type(exc).__name__,
                "consequence": "the gate denies every gated call until it can be read"}
    return {"path": path, "present": True, "readable": True,
            "classified_repositories": len(policy.repos),
            "default_allows_dispatch": bool(policy.default.allowed_routes)}


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
    installed_post = {}
    for client, path in (("claude", paths["claude_settings"]), ("codex", paths["codex_hooks"])):
        present = False
        post_present = False
        if os.path.exists(path):
            try:
                loaded = store.read_json(path)
                hooks = loaded.get("hooks", {})
                present = any(_is_ours(item) for item in hooks.get("PreToolUse", []))
                post_present = any(_is_ours(item) for item in hooks.get("PostToolUse", []))
            except (OSError, ValueError, AttributeError):
                present = False
                post_present = False
        installed[client] = present
        installed_post[client] = post_present
    return {
        "state_root": state_root,
        "installed": installed,
        "automatic_routing": _automatic_routing_state(paths),
        "routing_policy": _policy_state(state_root),
        "codex_trust": codex_trust_state(paths["codex_toml"], paths["codex_hooks"]),
        "receipts": receipts,
        "recent_events": recent,
        "denials_in_window": sum(1 for event in recent if event.get("permission") == "deny"),
        # Claude's read gate is its Read tool, matched by name: deterministic,
        # the same strength its editing tools already have. Codex has no
        # such tool; its read gate is the whole-file-read shell heuristic
        # (design section 2.1), the same strength its write heuristic
        # already is. Static, not read from installation state: it is a
        # fact about the mechanism, not about whether this host has it
        # installed (``installed`` above already answers that).
        "read_gate": {"claude": "deterministic", "codex": "heuristic"},
        # Phase 5 (design section 2.9), the same static-vs-installed split
        # as read_gate above: whether this host has the PostToolUse entry
        # is installed_post, not this. Codex is "not_countable" -- not
        # "heuristic" -- because Phase 0 could not confirm the event
        # exists there at all, unlike the read gate's Codex heuristic,
        # which is a real, if imprecise, mechanism.
        "inline_measurement": {"claude": "counted", "codex": "not_countable"},
        "installed_post": installed_post,
        "not_covered": NOT_COVERED,
    }


# -------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=HOOK_NAME, description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    parser.add_argument("--client", choices=CLIENTS)
    parser.add_argument("--config", help="private orchestration config (state_root is read from it)")
    parser.add_argument("--state-root", help="alternative to --config")
    parser.add_argument("--no-automatic-routing", action="store_true",
                        help="do not create the routing decision; refuse a call that has no "
                             "receipt instead. The pre-automatic behaviour, kept for an "
                             "operator who wants every route claimed explicitly.")
    parser.add_argument("--event", choices=("PreToolUse", "PostToolUse"), default="PreToolUse",
                        help="which hook event this invocation answers. PostToolUse (Phase 5, "
                             "inline output measurement) only measures a matched Bash call's "
                             "response and never denies; absent, as on every hook installed "
                             "before Phase 5 existed, this defaults to PreToolUse.")
    ins = sub.add_parser("install", help="register the PreToolUse hook in Claude Code and/or Codex")
    ins.add_argument("--home"); ins.add_argument("--root", required=True); ins.add_argument("--config", required=True)
    ins.add_argument("--clients", default="claude,codex"); ins.add_argument("--apply", action="store_true")
    ins.add_argument("--remove", action="store_true")
    ins.add_argument("--include-post", action="store_true",
                     help="also register Phase 5's PostToolUse inline-measurement entry for "
                          "every listed client that supports it (claude only; see "
                          "POST_MATCHERS). Off by default, matching this feature's own "
                          "off-by-default design; a plain re-install without this flag leaves "
                          "an already-registered Post entry exactly as it is rather than "
                          "removing it.")
    rep = sub.add_parser("report", help="receipts, recent gate events, installation and trust state")
    rep.add_argument("--home"); rep.add_argument("--config"); rep.add_argument("--state-root")
    rep.add_argument("--events", type=int, default=20)
    aud = sub.add_parser("audit", help="eligible, routed, retained, bypassed, failed, and why")
    aud.add_argument("--home"); aud.add_argument("--config"); aud.add_argument("--state-root")
    aud.add_argument("--since-hours", type=float, default=24.0,
                     help="window to account for; 0 for everything on record")
    aud.add_argument("--json", action="store_true", help="the full record rather than the summary")
    words = sys.argv[1:] if argv is None else argv
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None) or any(word in ("install", "report", "audit", "-h", "--help") for word in words):
            raise
        # Hook mode with unusable arguments: the host must still read a deny.
        sys.stdout.write(json.dumps(hook_output(Decision(
            "deny", "gate_error", "delegation-first gate: the hook was started with arguments it "
            "cannot use; nothing is implemented until the installation is repaired")), sort_keys=True) + "\n")
        return 0
    home = os.path.expanduser("~")
    if args.command == "install":
        clients = tuple(part.strip() for part in args.clients.split(",") if part.strip())
        result = install(args.home or home, args.root, args.config, clients,
                         apply=args.apply, remove=args.remove, include_post=args.include_post)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "report":
        state_root = args.state_root or state_root_from_config(args.config)
        print(json.dumps(report(args.home or home, state_root, events=args.events),
                         indent=2, sort_keys=True))
        return 0
    if args.command == "audit":
        from .audit import render, report as audit_report

        state_root = args.state_root or state_root_from_config(args.config)
        document = audit_report(state_root, home=args.home or home,
                                config_path=args.config,
                                since_hours=args.since_hours)
        print(json.dumps(document, indent=2, sort_keys=True) if args.json
              else render(document), end="" if not args.json else "\n")
        return 0
    if args.event == "PostToolUse":
        # Phase 5 (design section 2.9): measurement only, never a decision.
        # A wholly separate branch from Pre's hook mode below rather than a
        # shared path with an early return, because none of Pre's setup
        # (capacity_db, protected_paths, the automatic decider) is a Post
        # concern, and Pre's own except clause below builds a
        # PreToolUse-shaped deny that would be meaningless here. Always
        # prints {} and exits 0: a PostToolUse hook fires after the tool
        # call already succeeded and cannot undo it, so there is nothing
        # an error here should ever turn into for the host to act on.
        try:
            if not args.client:
                raise ValueError("--client is required in hook mode")
            state_root = args.state_root or state_root_from_config(args.config or "")
            payload = json.loads(_hook_input() or "{}")
            if isinstance(payload, dict):
                run_post_hook(args.client, state_root, payload)
        except Exception:  # noqa: BLE001  measurement only; never surfaces an error
            pass
        sys.stdout.write(json.dumps({}) + "\n")
        return 0
    # Hook mode: judge the call described on stdin. Always exit 0; the
    # decision travels in the JSON so the host applies it, and a deny is
    # a deny whatever went wrong on the way to it.
    state_root = None
    try:
        if not args.client:
            raise ValueError("--client is required in hook mode")
        if args.state_root:
            state_root = args.state_root
            capacity_db = os.path.join(args.state_root, "capacity.sqlite3")
            local_queue_root = os.path.join(args.state_root, "local-queue")
            worker_executable = ""
        else:
            state_root, capacity_db, local_queue_root, worker_executable = \
                gate_paths_from_config(args.config or "")
        protected = protected_paths(state_root, args.config, home, capacity_db,
                                    local_queue_root)
        payload = json.loads(_hook_input() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        decision = run_hook(args.client, state_root, payload, capacity_db=capacity_db,
                            protected=protected, local_queue_root=local_queue_root,
                            worker_executable=worker_executable,
                            decide=None if args.no_automatic_routing else
                            automatic_decider(args.client, state_root, capacity_db))
    except Exception as exc:  # noqa: BLE001  fail closed, name only the class
        decision = Decision("deny", "gate_error",
                            f"delegation-first gate: could not judge this call ({type(exc).__name__}); "
                            f"nothing is implemented until the gate can")
        if state_root:
            try:
                record_event(state_root, args.client or "unknown", "unknown", decision)
            except Exception:  # noqa: BLE001  the deny stands whether or not it could be logged
                pass
    sys.stdout.write(json.dumps(hook_output(decision), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
