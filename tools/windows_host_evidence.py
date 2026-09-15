#!/usr/bin/env python3
"""Collect the Windows evidence nothing in this repository currently produces.

Run this on a real Windows host. It writes one JSON record naming every check,
what it established, and what it could not. Standard library only, Python
3.11+, and it never touches the assistant configuration of the account running
it: everything happens under a temporary home that is deleted afterwards.

Why it exists
-------------
CI runs the whole offline suite plus ``test_delegation_gate``,
``test_automatic_gate``, ``test_delegation_audit`` and ``test_hostenv`` on
Windows runners, so the gate's *logic* is already exercised there. Three things
are not:

* **the gate's own ``bin/agent-bridge-gate-hook.cmd`` launcher**, which has
  never been executed on any host. The Windows smoke test in CI runs
  ``agent-bridge-windows-setup.cmd``, a different launcher;
* **the installed command string being one ``cmd.exe`` can actually run.** That
  string is built by ``gate.hook_command``, which until recently quoted paths
  with ``shlex.quote``: POSIX single quotes, which ``cmd.exe`` does not treat as
  quoting at all. On an ordinary Windows home containing a space the installed
  hook would have been unrunnable and silent;
* **the two execution lanes and the end-to-end workflow**, which CI skips on
  Windows outright.

The one rule that makes this script worth running
-------------------------------------------------
``gate`` hook mode **always exits 0**, and it turns *any* internal failure into
a deny carrying the code ``gate_error``: an unreadable config, a mangled
argument list, an import failure. So "a deny happened" is exactly what a
broken launcher produces, and a check that asserts only that would pass most
loudly when the thing under test is most broken.

Every deny assertion here therefore pins the *specific* code and rejects
``gate_error`` by name, and every deny check is paired with an allow check
through the same path. See :func:`expect_deny`.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = 1
REPO = Path(__file__).resolve().parents[1]

#: The deny code the gate emits when anything at all went wrong. Never an
#: acceptable pass for a check about routing or protected paths.
GATE_ERROR = "gate_error"

#: A minimal PreToolUse payload for each client, using the editing tool that
#: client actually has. Codex has no "Edit" tool, and driving it with one
#: classifies the call as not gated and allows it: two tests in this
#: repository did exactly that and passed while exercising nothing.
EDIT_TOOL = {"claude": "Edit", "codex": "apply_patch"}


class CheckError(AssertionError):
    """A check failed. The message is the evidence."""


class Skip(Exception):
    """A check cannot run here, for a reason worth recording."""


CHECKS: list[tuple[str, str, object]] = []


def check(identifier: str, title: str):
    def register(function):
        CHECKS.append((identifier, title, function))
        return function
    return register


# ---------------------------------------------------------------- assertions


def parse_hook_output(completed: subprocess.CompletedProcess, where: str) -> dict:
    """The hook's JSON, or a failure naming what came back instead.

    Hook mode always exits 0 and always prints JSON, so anything else is a
    launcher or environment problem rather than a decision.
    """
    if completed.returncode != 0:
        raise CheckError(
            f"{where}: exit {completed.returncode}, which hook mode never returns. "
            f"stdout={completed.stdout!r} stderr={completed.stderr[-2000:]!r}")
    text = completed.stdout.strip()
    if not text:
        raise CheckError(f"{where}: no output at all. stderr={completed.stderr[-2000:]!r}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CheckError(f"{where}: output is not JSON ({exc}). "
                         f"stdout={completed.stdout[:2000]!r} "
                         f"stderr={completed.stderr[-2000:]!r}") from None


def expect_allow(completed: subprocess.CompletedProcess, where: str) -> dict:
    """An allow is exactly ``{}``. Empty output is not an allow."""
    payload = parse_hook_output(completed, where)
    if payload != {}:
        raise CheckError(f"{where}: expected an allow ({{}}), got {json.dumps(payload)[:800]}")
    return payload


def expect_deny(completed: subprocess.CompletedProcess, code: str, where: str) -> str:
    """A deny carrying exactly ``code``, and never ``gate_error``.

    This is the whole reason the script is trustworthy. ``gate_error`` is what
    the hook emits when it could not judge the call: a mangled argument list
    from a quoting defect, an unreadable config, a failed import. Accepting it
    would mean every check here passes precisely when the launcher is broken.
    """
    payload = parse_hook_output(completed, where)
    output = payload.get("hookSpecificOutput")
    if not isinstance(output, dict):
        raise CheckError(f"{where}: expected a deny, got {json.dumps(payload)[:800]}")
    reason = str(output.get("permissionDecisionReason", ""))
    if output.get("permissionDecision") != "deny":
        raise CheckError(f"{where}: permissionDecision is not deny: {json.dumps(output)[:800]}")
    actual = reason.rsplit("[", 1)[-1].rstrip("]") if "[" in reason else "(no code)"
    if actual == GATE_ERROR and code != GATE_ERROR:
        raise CheckError(
            f"{where}: the gate could not judge the call at all ({GATE_ERROR}), which is "
            f"what a broken launcher or an unreadable config produces. This is a real "
            f"failure, not a deny. reason={reason!r}")
    if actual != code:
        raise CheckError(f"{where}: expected deny [{code}], got [{actual}]. reason={reason!r}")
    return reason


# ------------------------------------------------------------------- fixture


class Fixture:
    """An isolated home, orchestration config, and classified git repository.

    Nothing here reads or writes the real account's configuration.
    ``onboard._paths`` derives every path from the ``home`` it is given
    whenever that home is not the account's own, and the environment handed to
    every child process redirects ``HOME``, ``USERPROFILE``, ``APPDATA`` and
    ``CODEX_HOME`` as well, so both routes lead to the temporary tree.
    """

    def __init__(self, base: Path, *, spaced: bool = False, repo_copy: bool = False):
        self.base = base
        name = "a home with spaces" if spaced else "home"
        self.home = base / name
        self.state = self.home / ".agent-bridge" / "orchestration" / "state"
        (self.state / "routing").mkdir(parents=True)
        self.db = self.state / "capacity.sqlite3"
        config_dir = base / ("config dir with spaces" if spaced else "config")
        config_dir.mkdir(parents=True, exist_ok=True)
        self.config = config_dir / "orchestration.json"
        self.config.write_text(json.dumps(
            {"state_root": str(self.state), "capacity_db": str(self.db)}), encoding="utf-8")
        self.repo = base / ("a repo with spaces" if spaced else "repo")
        self.repo.mkdir(parents=True)
        run(["git", "init", "-q", str(self.repo)], where="git init")
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.root = REPO
        if repo_copy:
            # The launcher resolves its own directory with %~dp0. A repository
            # whose own path contains a space is the case that exercises it.
            self.root = base / "a checkout with spaces"
            self.root.mkdir(parents=True)
            shutil.copytree(REPO / "bin", self.root / "bin")
            shutil.copytree(REPO / "src", self.root / "src",
                            ignore=shutil.ignore_patterns("__pycache__"))
        self.classify()

    def classify(self, *, declared=("codex",), classification="internal_nonclient",
                 allowed=("claude", "codex")) -> None:
        """The operator's policy. Claude is then routed to Codex, so a claude
        edit is denied with ``routed_elsewhere`` and a codex edit is allowed:
        one deny and one allow through the same machinery."""
        document = {"version": 1, "declared_available": list(declared),
                    "repos": {str(self.repo): {"classification": classification,
                                               "allowed_routes": list(allowed)}}}
        (self.state / "routing" / "routing-policy.json").write_text(
            json.dumps(document), encoding="utf-8")

    def retain_everything(self) -> None:
        (self.state / "routing" / "routing-policy.json").write_text(
            json.dumps({"version": 1, "repos": {}}), encoding="utf-8")

    def env(self, *, for_launcher: bool = False) -> dict[str, str]:
        """The child environment. Isolated, and for a launcher, bare.

        ``for_launcher`` is the important half, and leaving it out made every
        launcher check here worthless. The environment seeded ``PYTHONPATH``
        and ``PYTHONDONTWRITEBYTECODE``, which are **the two variables the
        ``.cmd`` exists to set**. So all eight launcher checks would have
        passed on the harness's coat-tails even if ``%~dp0``, the ``..``
        segment or the quoted ``set`` produced nothing usable: the gate would
        have imported through the inherited path instead. That is the same
        shape as the two tests that drove the Codex hook with the wrong tool
        name and passed while reaching none of the code they named.

        So a launcher invocation gets an environment with those two removed,
        and with a hostile ``PYTHONPATH`` in their place: if the launcher does
        not set its own, the gate cannot import at all and the check fails
        loudly instead of passing quietly.
        """
        environment = dict(os.environ)
        environment.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "CODEX_HOME": str(self.home / ".codex"),
            "PYTHONPATH": str(self.root / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        environment.pop("CLAUDE_CONFIG_DIR", None)
        if for_launcher:
            hostile = self.base / "not the source tree"
            hostile.mkdir(exist_ok=True)
            environment["PYTHONPATH"] = str(hostile)
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment.pop("PYTHONUTF8", None)
            environment.pop("PYTHONIOENCODING", None)
        return environment

    def payload(self, client: str, *, relative: str = "app.py") -> str:
        tool = EDIT_TOOL[client]
        if tool == "apply_patch":
            tool_input = {"input": "*** Begin Patch\n*** Update File: "
                                   f"{relative}\n@@\n-x = 1\n+x = 2\n*** End Patch\n"}
        else:
            tool_input = {"file_path": str(self.repo / relative)}
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                           "tool_input": tool_input, "cwd": str(self.repo)})

    def shell_payload(self, client: str, command: str) -> str:
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                           "tool_input": {"command": command}, "cwd": str(self.repo)})

    # -- the three ways the hook can be invoked ---------------------------

    def launcher(self) -> Path:
        name = "agent-bridge-gate-hook" + (".cmd" if os.name == "nt" else "")
        return self.root / "bin" / name

    def via_launcher(self, client: str, payload: str, *, cwd: Path | None = None,
                     extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        """Through the launcher script, which is the whole point of this file."""
        argv = [str(self.launcher()), "--client", client, "--config", str(self.config), *extra]
        return run(argv, stdin=payload, env=self.env(for_launcher=True),
                   cwd=str(cwd or self.repo), where="launcher", shell_on_windows=True)

    def via_module(self, client: str, payload: str, *, cwd: Path | None = None
                   ) -> subprocess.CompletedProcess:
        """Through the Python module, which is what every existing test does."""
        argv = [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
                "--client", client, "--config", str(self.config)]
        return run(argv, stdin=payload, env=self.env(),
                   cwd=str(cwd or self.repo), where="module")

    def via_installed_command(self, command: str, payload: str,
                              *, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Through the exact command string written into the host's config.

        Handed to the platform's own shell rather than split by this script,
        because the question is whether *the host's shell* can run what the
        installer wrote.
        """
        # Explicitly ``cmd.exe /d /s /c``, which is what a Node host uses, and
        # not Python's ``shell=True``. That formats ``%COMSPEC% /c "..."``
        # without ``/d``, so an AutoRun value under
        # HKCU\Software\Microsoft\Command Processor would run first and land
        # its output on the hook's stdout, and without ``/s``, so cmd applies
        # its more complicated quote-stripping rules. Testing a different
        # invocation from the one the host uses would answer a different
        # question.
        #
        # Built as one pre-formatted string, not an argv list: ``command`` is
        # already quoted (it is the exact text ``gate.hook_command`` wrote to
        # the host's config), and passing a list through ``subprocess.run``
        # with ``shell=False`` makes Python re-quote every element with its
        # own ``list2cmdline`` on the way to ``CreateProcess``. That escapes
        # ``command``'s own embedded quotes with backslashes and wraps the
        # whole thing in a second, outer quote pair, which is not the command
        # line a real host ever sends and which ``cmd.exe /s`` cannot run
        # (measured: exit 1, "is not recognized as an internal or external
        # command"). A string, by contrast, reaches ``CreateProcess``
        # unchanged.
        if os.name == "nt":
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            argv = f'{comspec} /d /s /c "{command}"'
            return run(argv, stdin=payload, env=self.env(for_launcher=True),
                       cwd=str(cwd or self.repo), where="installed command")
        return run(command, stdin=payload, env=self.env(for_launcher=True),
                   cwd=str(cwd or self.repo), where="installed command", shell=True)


def run(argv, *, stdin: str | None = None, env: dict[str, str] | None = None,
        cwd: str | None = None, where: str = "command", shell: bool = False,
        shell_on_windows: bool = False, timeout: int = 180
        ) -> subprocess.CompletedProcess:
    """One subprocess, with its output captured as text.

    ``shell_on_windows`` exists because a ``.cmd`` file is not directly
    executable: ``CreateProcess`` cannot run it, ``cmd.exe`` has to. That is
    also how a host runs it, so it is the honest way to invoke it here.
    """
    use_shell = shell or (shell_on_windows and os.name == "nt")
    if use_shell and not isinstance(argv, str):
        argv = subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(
            __import__("shlex").quote(part) for part in argv)
    try:
        return subprocess.run(
            argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, env=env, cwd=cwd, shell=use_shell, check=False)
    except OSError as exc:
        raise CheckError(f"{where}: could not start {argv!r} ({type(exc).__name__}: {exc})") from None


# -------------------------------------------------------------------- checks
#
# Every deny check below names its code. Every deny check is paired with an
# allow through the same path, because a deny alone is also what a broken
# launcher produces.


@check("launcher-report", "the gate's .cmd launcher runs at all and emits JSON")
def _launcher_report(fx: Fixture) -> dict:
    """First contact. Nothing has ever executed this file on any host."""
    if not fx.launcher().exists():
        raise CheckError(f"no launcher at {fx.launcher()}")
    completed = run([str(fx.launcher()), "report", "--home", str(fx.home),
                     "--config", str(fx.config)],
                    env=fx.env(for_launcher=True), cwd=str(REPO),
                    where="launcher report", shell_on_windows=True)
    if completed.returncode != 0:
        raise CheckError(f"launcher report: exit {completed.returncode} "
                         f"stderr={completed.stderr[-2000:]!r}")
    document = json.loads(completed.stdout)
    expected = ("automatic_routing", "codex_trust", "installed", "receipts",
                "recent_events", "routing_policy", "state_root")
    missing = [key for key in expected if key not in document]
    if missing:
        raise CheckError(f"report is missing {missing}: {sorted(document)}")
    if document["state_root"] != str(fx.state):
        raise CheckError(f"report read the wrong state root: {document['state_root']!r} "
                         f"is not {str(fx.state)!r}; the launcher may have mangled "
                         f"--config")
    return {"launcher": str(fx.launcher()), "state_root": document["state_root"],
            "report_keys": sorted(document)}


@check("launcher-allow", "the launcher forwards stdin and returns an allow")
def _launcher_allow(fx: Fixture) -> dict:
    """Also the stdin test: the payload only reaches the gate through it."""
    fx.retain_everything()
    completed = fx.via_launcher("claude", fx.payload("claude"))
    expect_allow(completed, "launcher allow")
    return {"stdin_reached_the_gate": True}


@check("launcher-deny", "the launcher returns a routed_elsewhere deny, not gate_error")
def _launcher_deny(fx: Fixture) -> dict:
    """The paired half. ``gate_error`` here would mean the launcher mangled
    something on the way, which is the failure this whole file exists to
    detect, so :func:`expect_deny` refuses it by name."""
    fx.classify()
    reason = expect_deny(fx.via_launcher("claude", fx.payload("claude")),
                         "routed_elsewhere", "launcher deny")
    return {"reason": reason[:400]}


@check("launcher-chosen-route", "the route the decision chose may still edit")
def _launcher_chosen_route(fx: Fixture) -> dict:
    """Through apply_patch, the tool Codex actually has. With "Edit" this
    would be allowed without the receipt being read at all."""
    fx.classify()
    expect_deny(fx.via_launcher("claude", fx.payload("claude")),
                "routed_elsewhere", "launcher deny before codex")
    expect_allow(fx.via_launcher("codex", fx.payload("codex")), "launcher codex allow")
    return {"claude": "denied", "codex": "allowed"}


@check("launcher-exit-zero", "the launcher exits 0 on a deny")
def _launcher_exit_zero(fx: Fixture) -> dict:
    """A non-zero exit would read to the host as a failed hook rather than a
    refusal, and the host's behaviour then is not something this project
    controls."""
    fx.classify()
    completed = fx.via_launcher("claude", fx.payload("claude"))
    if completed.returncode != 0:
        raise CheckError(f"deny exited {completed.returncode}")
    return {"returncode": 0}


@check("launcher-foreign-cwd", "the launcher works from a directory outside the repo")
def _launcher_foreign_cwd(fx: Fixture) -> dict:
    """A host runs the hook with the working directory set to the project,
    which is not where the launcher lives."""
    fx.classify()
    elsewhere = fx.base / "some other place"
    elsewhere.mkdir(exist_ok=True)
    expect_deny(fx.via_launcher("claude", fx.payload("claude"), cwd=elsewhere),
                "routed_elsewhere", "launcher from a foreign cwd")
    return {"cwd": str(elsewhere)}


@check("launcher-strict-posture", "--no-automatic-routing reaches the gate through the launcher")
def _launcher_strict(fx: Fixture) -> dict:
    """An extra argument after the config path, which is where an argument
    forwarding defect would show up."""
    fx.retain_everything()
    receipt = fx.state / "routing"
    for stale in receipt.glob("*.json"):
        if stale.name != "routing-policy.json":
            stale.unlink()
    expect_deny(fx.via_launcher("claude", fx.payload("claude"),
                                extra=("--no-automatic-routing",)),
                "no_routing_receipt", "launcher strict posture")
    return {"forwarded": "--no-automatic-routing"}


@check("installed-command-runs", "the command the installer writes is one the host's shell can run")
def _installed_command(fx: Fixture) -> dict:
    """The highest-value check in this file.

    ``gate.hook_command`` quoted both paths with ``shlex.quote`` until
    recently: POSIX single quotes, which ``cmd.exe`` does not treat as quoting.
    On an ordinary Windows home containing a space the installed hook was
    unrunnable, and because a hook that never runs is a gate that never gates,
    nothing would have said so.

    So this installs into a home whose path contains a space, reads the
    ``command`` string back out of the host's own configuration file, and hands
    it to the platform shell with a real payload. A quoting defect surfaces as
    ``gate_error`` or as a shell failure, and both are refused here.
    """
    fx.classify()
    installed = run([sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
                     "install", "--home", str(fx.home), "--root", str(fx.root),
                     "--config", str(fx.config), "--apply"],
                    env=fx.env(), cwd=str(fx.root), where="install")
    if installed.returncode != 0:
        raise CheckError(f"install: exit {installed.returncode} "
                         f"stderr={installed.stderr[-2000:]!r}")
    report = json.loads(installed.stdout)
    if not report.get("applied"):
        raise CheckError(f"install did not apply: {json.dumps(report)[:800]}")

    evidence: dict[str, object] = {"home": str(fx.home), "planned": report.get("planned_files")}
    commands = {}
    for client, relative in (("claude", Path(".claude") / "settings.json"),
                             ("codex", Path(".codex") / "hooks.json")):
        path = fx.home / relative
        if not path.exists():
            raise CheckError(f"{client}: {path} was not written")
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = document.get("hooks", {}).get("PreToolUse", [])
        found = [hook.get("command") for entry in entries
                 for hook in entry.get("hooks", [])
                 if "agent-bridge-gate-hook" in str(hook.get("command", ""))]
        if len(found) != 1:
            raise CheckError(f"{client}: expected one gate hook command, found {found}")
        commands[client] = found[0]
    evidence["commands"] = commands

    if os.name == "nt":
        for client, command in commands.items():
            if "'" in command:
                raise CheckError(
                    f"{client}: the installed command contains a single quote, which "
                    f"cmd.exe does not treat as quoting: {command}")
            if ".cmd" not in command:
                raise CheckError(f"{client}: no .cmd launcher in {command}")

    # Now the part that only a host can answer: can the shell run it?
    fx.classify()
    reason = expect_deny(fx.via_installed_command(commands["claude"], fx.payload("claude")),
                         "routed_elsewhere", "installed claude command")
    expect_allow(fx.via_installed_command(commands["codex"], fx.payload("codex")),
                 "installed codex command")
    evidence["claude_deny_reason"] = reason[:400]
    return evidence


@check("protected-state", "a shell write aimed at the gate's own state is refused")
def _protected_state(fx: Fixture) -> dict:
    """``gate_state_protected`` rather than ``routed_elsewhere``, so the check
    distinguishes the protected-path rule from the routing rule. Run with the
    repository retained, which removes routing as the explanation.

    The target is **quoted**, and the first version of this check was wrong
    not to be: this fixture's home contains spaces, and an unquoted path with
    spaces is not a command that would delete anything, so the gate is right
    not to see one. Quoting it also makes the check the interesting one, since
    a quoted path containing spaces is the ordinary Windows shape.
    """
    fx.retain_everything()
    policy = fx.state / "routing" / "routing-policy.json"
    verb = "del" if os.name == "nt" else "rm -f"
    quoted = f'"{policy}"'
    expect_deny(fx.via_launcher("claude", fx.shell_payload("claude", f"{verb} {quoted}")),
                "gate_state_protected", "protected state write")
    # And an ordinary read in the same repository is still allowed, so the
    # deny above is the rule firing rather than everything being refused.
    reader = "type" if os.name == "nt" else "cat"
    expect_allow(fx.via_launcher("claude", fx.shell_payload("claude", f"{reader} app.py")),
                 "ordinary read")
    # A write inside the repository is allowed too, so the deny is about the
    # target rather than about the verb.
    expect_allow(fx.via_launcher("claude", fx.shell_payload("claude", f"{verb} app.py")),
                 "a write inside a retained repository")
    return {"verb": verb, "target": str(policy), "quoted": True}


@check("protected-state-mixed-case", "the protected-path rule survives a re-spelled path")
def _protected_mixed_case(fx: Fixture) -> dict:
    """Windows paths are case-insensitive and take either separator, so the
    same protected file can be named several ways. On POSIX the differently
    cased path is a different file and the rule correctly does not fire, so
    this check asserts the platform's own semantics rather than one
    platform's."""
    fx.retain_everything()
    policy = fx.state / "routing" / "routing-policy.json"
    # The re-spelled component has to be at or above the protected root. The
    # first version of this check upper-cased "routing", a segment *below* the
    # state root, so the path was still plainly under the root on every
    # platform and the check proved nothing. The protected root here is the
    # state root, so that is the component to re-spell.
    respelled = str(fx.state.parent / fx.state.name.upper()
                    / "routing" / "routing-policy.json").replace("\\", "/")
    completed = fx.via_launcher("claude", fx.shell_payload(
        "claude", ("del " if os.name == "nt" else "rm -f ") + f'"{respelled}"'))
    if os.name == "nt":
        expect_deny(completed, "gate_state_protected", "re-spelled protected path")
        return {"respelled": respelled, "protected_root": str(fx.state),
                "expected": "gate_state_protected"}
    payload = parse_hook_output(completed, "re-spelled protected path")
    code = str(payload.get("hookSpecificOutput", {}).get(
        "permissionDecisionReason", "")).rsplit("[", 1)[-1].rstrip("]")
    if code == "gate_state_protected":
        raise CheckError("POSIX treated a differently cased path as the same file")
    return {"respelled": respelled, "protected_root": str(fx.state),
            "posix_code": code or "allowed"}


@check("repo-keying-is-stable", "one repository does not key to two receipts")
def _repo_keying(fx: Fixture) -> dict:
    """If two spellings of one repository produced two receipt keys, an
    assistant routed away could re-spell the path and be judged against a
    fresh, absent receipt. The check compares the keys the gate derives, not
    the gate's answer, so it cannot be satisfied by an unrelated deny."""
    sys.path.insert(0, str(fx.root / "src"))
    from agent_bridge.orchestration import autodecide, gate  # noqa: PLC0415

    spellings = [str(fx.repo), str(fx.repo).replace("\\", "/")]
    if os.name == "nt":
        drive, rest = os.path.splitdrive(str(fx.repo))
        spellings.append(drive.lower() + rest)
        spellings.append(drive.upper() + rest)
        spellings.append(str(fx.repo).upper())
    names = {spelling: gate.receipt_name(spelling) for spelling in spellings}
    items = {spelling: autodecide.item_id_for(spelling) for spelling in spellings}
    if len(set(names.values())) != 1 or len(set(items.values())) != 1:
        raise CheckError("one repository keyed to more than one receipt or item: "
                         + json.dumps({"receipt_names": names, "item_ids": items}, indent=2))
    return {"spellings": spellings, "receipt_name": next(iter(set(names.values()))),
            "item_id": next(iter(set(items.values())))}


@check("launcher-stdin-is-utf8", "a non-ASCII repository path is judged, not silently allowed")
def _launcher_utf8(fx: Fixture) -> dict:
    """The worst defect this project has had, verified on the host it bit.

    Hook mode read its payload with ``sys.stdin.read()``, which decodes using
    the locale encoding. On Windows that is the ANSI code page, and both hosts
    emit raw UTF-8. So a repository whose path held any non-ASCII character
    arrived as mojibake, no ``.git`` was found above the mangled path, and the
    gate returned ``allow`` with ``outside_repository``: a silent, total
    bypass for every user whose name or project path is not pure ASCII.

    The payload goes in as raw UTF-8 bytes, which is what a host writes, and
    the environment carries no ``PYTHONUTF8`` or ``PYTHONIOENCODING``, so this
    measures the default behaviour of this host rather than a forced one.
    """
    accented = fx.base / "caf\u00e9-\u9879\u76ee"
    accented.mkdir()
    run(["git", "init", "-q", str(accented)], where="git init accented")
    (accented / "app.py").write_text("x = 1\n", encoding="utf-8")
    document = {"version": 1, "declared_available": ["codex"],
                "repos": {str(accented): {"classification": "internal_nonclient",
                                          "allowed_routes": ["claude", "codex"]}}}
    (fx.state / "routing" / "routing-policy.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8")

    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                          "tool_input": {"file_path": str(accented / "app.py")},
                          "cwd": str(accented)}, ensure_ascii=False)
    argv = [str(fx.launcher()), "--client", "claude", "--config", str(fx.config)]
    completed = run(argv, stdin=payload, env=fx.env(for_launcher=True),
                    cwd=str(accented), where="launcher utf-8", shell_on_windows=True)
    reason = expect_deny(completed, "routed_elsewhere", "launcher utf-8")
    return {"repository": str(accented),
            "non_ascii": [c for c in accented.name if ord(c) > 127],
            "reason": reason[:300]}


@check("launcher-failure-is-a-deny", "a launcher that cannot start Python still denies")
def _launcher_fails_closed(fx: Fixture) -> dict:
    """The other fail-open, and on Windows it was the default case.

    The gate always exits 0 with its decision in the JSON, but that contract
    only starts once the interpreter is running. Both launchers used to exit
    with the interpreter's status and nothing on stdout when it could not
    start, and a host reads a hook that produced no decision as a
    non-blocking error and runs the tool anyway. On a stock Windows account
    with no Python the bare name ``python`` resolves to the Microsoft Store
    App Execution Alias stub, so this was not an edge case there.
    """
    environment = fx.env(for_launcher=True)
    environment["AGENT_BRIDGE_PYTHON"] = "definitely-not-an-interpreter"
    argv = [str(fx.launcher()), "--client", "claude", "--config", str(fx.config)]
    completed = run(argv, stdin=fx.payload("claude"), env=environment,
                    cwd=str(fx.repo), where="launcher with no interpreter",
                    shell_on_windows=True)
    if completed.returncode != 0:
        raise CheckError(
            f"a launch failure exited {completed.returncode} rather than 0, so a host "
            f"would read it as a failed hook and run the tool anyway. "
            f"stdout={completed.stdout!r} stderr={completed.stderr[-800:]!r}")
    reason = expect_deny(completed, "gate_launcher_failed", "launcher with no interpreter")
    return {"reason": reason[:300], "returncode": 0}


@check("quoting-survives-the-shell", "a quoted path reaches the program as one argument")
def _quoting_round_trip(fx: Fixture) -> dict:
    r"""Asserts each platform's own semantics, not one platform's everywhere.

    On Windows this is the delimiter matrix, because that is what the defect
    was: NTFS forbids only ``< > : " / \ | ? *``, so a comma, a semicolon, an
    equals sign, a percent and an exclamation mark are all legal in a
    directory name and all significant to ``cmd.exe``, and the first version
    of the quoted set had only the obvious ones. A path like
    ``C:\dev\a=b\hook.cmd`` came back bare and ``cmd.exe`` truncates the
    program name at the delimiter.

    On POSIX the matrix would be wrong: a comma needs no quoting in ``sh``,
    and asserting it did would be this project's recurring mistake pointed the
    other way. So POSIX gets the property that actually matters instead, and
    gets it by measurement: the quoted form, handed to the real shell, must
    come back as exactly one argument equal to the original.
    """
    sys.path.insert(0, str(fx.root / "src"))
    from agent_bridge.orchestration import gate  # noqa: PLC0415

    # A single quote belongs only in the POSIX matrix below: it is the shell's
    # own quoting character there, so it must round-trip, but it is not a
    # cmd.exe delimiter at all (measured: a bare, unquoted path containing one
    # runs on a real Windows host without error). Asserting it must be quoted
    # on Windows would be exactly the mistake the docstring above warns
    # against, pointed the other way: a property that holds on one platform
    # asserted as if it held on both.
    characters = (" ", "\t", ",", ";", "=", "%", "!", "&", "^", "(", ")")
    if os.name == "nt":
        results, bare = {}, []
        for character in characters:
            path = "C:\\dev\\a" + character + "b\\hook.cmd"
            quoted = gate.quote_for_host_shell(path)
            results[character] = quoted
            if quoted == path:
                bare.append(character)
        if bare:
            raise CheckError("left bare on Windows, so cmd.exe would break the "
                             f"command at the delimiter: {bare}")
        return {"platform": "nt", "quoted": results}

    # POSIX: measure the round trip through the real shell. A single quote is
    # tested only here: it is ``sh``'s own quoting character, so getting its
    # escaping wrong is exactly the kind of defect a round trip catches.
    round_trips = {}
    for character in (*characters, "'"):
        path = "/dev/a" + character + "b/hook"
        quoted = gate.quote_for_host_shell(path)
        completed = run(["sh", "-c", "printf '%s' " + quoted], where="sh round trip")
        if completed.returncode != 0 or completed.stdout != path:
            raise CheckError(
                f"quoting {path!r} as {quoted!r} did not survive sh: "
                f"exit {completed.returncode} stdout={completed.stdout!r}")
        round_trips[character] = quoted
    return {"platform": "posix", "round_tripped": round_trips}


@check("claude-config-dir-observation", "whether install_paths honours CLAUDE_CONFIG_DIR")
def _claude_config_dir(fx: Fixture) -> dict:
    """Recorded, not asserted, because it rests on a fact I cannot check here.

    ``gate.install_paths`` computes Claude's settings path as
    ``<home>/.claude/settings.json`` and never consults ``CLAUDE_CONFIG_DIR``,
    while the rest of this repository treats that variable as where Claude
    Code's configuration lives, and ``INSTALL.md`` tells the user to set it.
    If Claude Code reads ``settings.json`` from there, then on an account that
    sets it the hook is written to a file the host does not read while the
    install report and ``gate report`` both say "installed", which is a
    fail-open.

    Whether Claude Code does read it is a fact about Claude Code, not about
    this repository, so this check states the mismatch and leaves the
    conclusion to somebody who can observe the host. It does not fail.
    """
    sys.path.insert(0, str(fx.root / "src"))
    from agent_bridge.orchestration import gate  # noqa: PLC0415

    import inspect
    source = inspect.getsource(gate.install_paths)
    paths = gate.install_paths(str(fx.home))
    return {
        "install_paths_reads_claude_config_dir": "CLAUDE_CONFIG_DIR" in source,
        "claude_settings_path": paths["claude_settings"],
        "claude_config_dir_in_this_environment": os.environ.get("CLAUDE_CONFIG_DIR"),
        "open_question": ("does Claude Code read settings.json from CLAUDE_CONFIG_DIR? "
                          "If it does, an account that sets it gets a hook written "
                          "where the host does not look, reported as installed."),
    }


@check("hostenv-git", "git resolves on this host and the resolved path runs")
def _hostenv_git(fx: Fixture) -> dict:
    """``GIT_CANDIDATES["Windows"]`` is empty, so resolution falls straight
    through to PATH. Nothing has ever checked what that returns on Windows, or
    that the returned path is usable by subprocess rather than merely present.
    """
    sys.path.insert(0, str(fx.root / "src"))
    from agent_bridge.execution import hostenv  # noqa: PLC0415

    resolved = hostenv.resolve_git()
    completed = run([str(resolved), "--version"], where="resolved git")
    if completed.returncode != 0:
        raise CheckError(f"resolved git {resolved} does not run: "
                         f"exit {completed.returncode} {completed.stderr[-500:]!r}")
    return {"git": str(resolved), "version": completed.stdout.strip()}


@check("hostenv-no-confinement", "Windows is refused for verification, by name")
def _hostenv_confinement(fx: Fixture) -> dict:
    """The rule in AGENTS.md is that a backend which cannot deny the network
    and confine reads is not offered outside ``synthetic``. On Windows there
    is no backend at all, so every classification must be refused with a code
    rather than silently degrading."""
    sys.path.insert(0, str(fx.root / "src"))
    from agent_bridge.execution import hostenv  # noqa: PLC0415

    seen = {}
    for classification in ("synthetic", "public", "internal_nonclient"):
        try:
            backend = hostenv.confinement(classification, system=platform.system())
        except hostenv.HostCapabilityError as exc:
            seen[classification] = {"refused": True, "code": exc.code}
            continue
        seen[classification] = {"refused": False, "backend": backend.name,
                                "denies_network": backend.denies_network,
                                "confines_reads": backend.confines_reads,
                                "confines_writes": backend.confines_writes}
    if os.name == "nt":
        unrefused = [name for name, value in seen.items() if not value["refused"]]
        if unrefused:
            raise CheckError(f"Windows offered a verification backend for {unrefused}: "
                             + json.dumps(seen))
    return {"platform": platform.system(), "by_classification": seen}


def _unittest_module(fx: Fixture, module: str, *, where: str) -> dict:
    """Run one test module and return its counts, so a skip is a skip.

    CI never runs the execution-lane modules on Windows. A later change could
    turn their named skips into errors and nothing would notice, which is the
    thing worth checking: not that they pass, but that they decline cleanly.
    """
    program = (
        "import json, sys, unittest\n"
        "sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')\n"
        f"import {module} as m\n"
        "r = unittest.TextTestRunner(verbosity=0, stream=open(__import__('os').devnull, 'w'))"
        ".run(unittest.defaultTestLoader.loadTestsFromModule(m))\n"
        "print(json.dumps({'ran': r.testsRun, 'failures': len(r.failures),\n"
        "                  'errors': len(r.errors), 'skipped': len(r.skipped),\n"
        "                  'skip_reasons': sorted({str(reason) for _, reason in r.skipped})}))\n")
    completed = run([sys.executable, "-c", program], env=fx.env(), cwd=str(fx.root),
                    where=where, timeout=900)
    tail = completed.stdout.strip().splitlines()
    if not tail:
        raise CheckError(f"{where}: no result. stderr={completed.stderr[-2000:]!r}")
    return json.loads(tail[-1])


@check("lanes-decline-cleanly", "the execution lanes skip by name rather than erroring")
def _lanes_decline(fx: Fixture) -> dict:
    """CI skips these modules on Windows entirely, so this is the only place
    they are exercised there at all."""
    evidence = {}
    for module in ("test_claude_task", "test_codex_task"):
        counts = _unittest_module(fx, module, where=module)
        evidence[module] = counts
        if counts["errors"]:
            raise CheckError(f"{module} errored {counts['errors']} times on this host: "
                             + json.dumps(counts))
        if counts["failures"]:
            raise CheckError(f"{module} failed {counts['failures']} times on this host: "
                             + json.dumps(counts))
        if os.name == "nt" and not counts["skipped"]:
            raise CheckError(f"{module} skipped nothing on Windows, where there is no "
                             f"confinement backend: " + json.dumps(counts))
    return evidence


@check("workflow-declines-cleanly", "the end-to-end workflow declines by name on Windows")
def _e2e_declines(fx: Fixture) -> dict:
    """Also skipped by CI on Windows. It must refuse rather than pass
    vacuously, which is what its own docstring promises."""
    counts = _unittest_module(fx, "test_automatic_delegation_e2e", where="e2e")
    if counts["errors"] or counts["failures"]:
        raise CheckError("the end-to-end module did not decline cleanly: " + json.dumps(counts))
    return counts


@check("audit-runs", "report and audit both run through the launcher on this host")
def _audit_runs(fx: Fixture) -> dict:
    """The audit reads the stage router's SQLite database through a read-only
    URI built from a filesystem path, which is the kind of thing a drive
    letter and a backslash break."""
    fx.classify()
    expect_deny(fx.via_launcher("claude", fx.payload("claude")),
                "routed_elsewhere", "a decision before the audit")
    rendered = run([str(fx.launcher()), "audit", "--home", str(fx.home),
                    "--config", str(fx.config), "--since-hours", "0", "--json"],
                   env=fx.env(for_launcher=True), cwd=str(REPO), where="audit",
                   shell_on_windows=True)
    if rendered.returncode != 0:
        raise CheckError(f"audit: exit {rendered.returncode} "
                         f"stderr={rendered.stderr[-2000:]!r}")
    document = json.loads(rendered.stdout)
    if document["routed"]["count"] != 1:
        raise CheckError("the audit did not see the routed decision: "
                         + json.dumps(document.get("routed")))
    capacity = (document.get("stages") or {}).get("capacity") or {}
    if not capacity:
        raise CheckError("the audit read no capacity rows, so the read-only "
                         "SQLite URI probably did not open on this host")
    return {"routed": document["routed"], "capacity": capacity,
            "stages_available": (document.get("stages") or {}).get("available")}


@check("hosts-present", "which hosts exist on this machine, for the record")
def _hosts_present(fx: Fixture) -> dict:
    """Not a pass/fail of this project. It records what a follow-up run could
    attempt: the hook being invoked by Claude Code or Codex themselves has
    happened on no platform, and only a machine with those CLIs can change
    that. Version probes only, so nothing is sent to a provider.
    """
    found = {}
    for name in ("claude", "codex", "git", "python", "python3"):
        where = shutil.which(name)
        found[name] = {"path": where}
        if where and name in ("claude", "codex"):
            completed = run([where, "--version"], where=name, timeout=120)
            found[name]["version"] = (completed.stdout or completed.stderr).strip()[:200]
            found[name]["returncode"] = completed.returncode
    return found


# ----------------------------------------------------------------- self-test


def _fake(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def self_test() -> list[dict]:
    """Prove the assertions fail when they should, before trusting a pass.

    A collector that reports seventeen passes has told you nothing until you
    know it can report a failure. The one that matters is the third case
    below: hook mode turns *every* internal problem into a deny carrying
    ``gate_error`` and still exits 0, so "a deny happened" is exactly what a
    broken launcher produces. If ``expect_deny`` accepted that, every check in
    this file would pass most confidently when the gate was most broken. That
    is the shape of defect this project has already shipped twice.
    """
    deny = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "nope [routed_elsewhere]"}})
    error = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "could not judge this call [gate_error]"}})

    cases = [
        ("an allow is accepted", lambda: expect_allow(_fake("{}"), "t"), True),
        ("a deny with the right code is accepted",
         lambda: expect_deny(_fake(deny), "routed_elsewhere", "t"), True),
        ("gate_error is NOT accepted as a routing deny",
         lambda: expect_deny(_fake(error), "routed_elsewhere", "t"), False),
        ("the wrong deny code is not accepted",
         lambda: expect_deny(_fake(deny), "gate_state_protected", "t"), False),
        ("a deny is not an allow", lambda: expect_allow(_fake(deny), "t"), False),
        ("an allow is not a deny",
         lambda: expect_deny(_fake("{}"), "routed_elsewhere", "t"), False),
        ("empty output is not an allow", lambda: expect_allow(_fake(""), "t"), False),
        ("empty output is not a deny",
         lambda: expect_deny(_fake(""), "routed_elsewhere", "t"), False),
        ("non-JSON output is not an allow",
         lambda: expect_allow(_fake("The system cannot find the path specified."), "t"), False),
        ("a non-zero exit is never a decision",
         lambda: expect_allow(_fake("{}", returncode=1), "t"), False),
        ("a deny with no code is not a deny with a code",
         lambda: expect_deny(_fake(json.dumps({"hookSpecificOutput": {
             "permissionDecision": "deny", "permissionDecisionReason": "no code here"}})),
             "routed_elsewhere", "t"), False),
    ]

    results = []
    for title, thunk, should_pass in cases:
        try:
            thunk()
            raised = False
        except CheckError:
            raised = True
        ok = (not raised) if should_pass else raised
        results.append({"case": title, "expected": "accept" if should_pass else "reject",
                        "ok": ok})
        print(f"  {'PASS ' if ok else 'FAIL '} {title}", flush=True)
    return results


# --------------------------------------------------------------------- driver


#: Checks that need their own fixture shape. Everything else gets the plain
#: one; these get a home, config and checkout whose paths contain spaces,
#: because that is the case the quoting defect turned on.
SPACED = {"installed-command-runs", "launcher-report", "launcher-allow",
          "launcher-deny", "launcher-chosen-route", "launcher-exit-zero",
          "launcher-foreign-cwd", "launcher-strict-posture",
          "protected-state", "protected-state-mixed-case", "audit-runs",
          "launcher-stdin-is-utf8", "launcher-failure-is-a-deny"}
NEEDS_CHECKOUT_COPY = {"launcher-report", "launcher-allow", "launcher-deny",
                       "launcher-chosen-route", "launcher-exit-zero",
                       "launcher-foreign-cwd", "launcher-strict-posture",
                       "installed-command-runs", "protected-state",
                       "protected-state-mixed-case", "audit-runs",
                       "launcher-stdin-is-utf8", "launcher-failure-is-a-deny"}


def collect(only: tuple[str, ...] = (), keep: bool = False) -> dict:
    results = []
    for identifier, title, function in CHECKS:
        if only and identifier not in only:
            continue
        started = time.time()
        base = Path(tempfile.mkdtemp(prefix="ab-win-evidence-"))
        record = {"id": identifier, "title": title}
        try:
            if shutil.which("git") is None:
                raise Skip("no git on PATH; every check here needs a repository")
            fixture = Fixture(base, spaced=identifier in SPACED,
                              repo_copy=identifier in NEEDS_CHECKOUT_COPY)
            record["evidence"] = function(fixture)
            record["status"] = "pass"
        except Skip as exc:
            record["status"] = "skip"
            record["detail"] = str(exc)
        except CheckError as exc:
            record["status"] = "fail"
            record["detail"] = str(exc)
        except Exception as exc:  # noqa: BLE001  an unexpected failure is still a result
            import traceback
            record["status"] = "error"
            record["detail"] = f"{type(exc).__name__}: {exc}"
            record["traceback"] = traceback.format_exc()[-4000:]
        record["seconds"] = round(time.time() - started, 2)
        if keep:
            record["fixture"] = str(base)
        else:
            shutil.rmtree(base, ignore_errors=True)
        results.append(record)
        marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "error": "ERROR"}[record["status"]]
        print(f"  {marker:<5} {identifier:<34} {title}", flush=True)
        if record["status"] in ("fail", "error"):
            print(f"        {record['detail'][:1500]}", flush=True)
        elif record["status"] == "skip":
            print(f"        {record['detail']}", flush=True)
    summary = {state: sum(1 for r in results if r["status"] == state)
               for state in ("pass", "fail", "skip", "error")}
    return {
        "version": VERSION,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(REPO),
        "git_head": _git_head(),
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "version": platform.version(), "machine": platform.machine(),
            "os_name": os.name, "python": sys.version,
            "python_executable": sys.executable,
            "filesystem_encoding": sys.getfilesystemencoding(),
            "tempdir": tempfile.gettempdir(),
            "expanduser": os.path.expanduser("~"),
        },
        "results": results,
        "summary": summary,
        "assertion_self_test": _self_test_quietly(),
        "what_this_does_not_prove": [
            "the hook being invoked by Claude Code or the Codex CLI themselves. Every "
            "check here drives the launcher or the module directly, which is the same "
            "interface a host uses but not the same integration.",
            "anything about a provider account: no provider was contacted and no "
            "allowance was spent.",
            "verification confinement on Windows, which does not exist. The lane "
            "checks establish that it is refused by name, not that it works.",
        ],
    }


def _self_test_quietly() -> dict:
    """The assertion self-test, folded into every record.

    So the JSON a reviewer reads carries the proof that the checks beside it
    could have failed, rather than asking them to take it on trust.
    """
    import contextlib, io  # noqa: PLC0415

    with contextlib.redirect_stdout(io.StringIO()):
        results = self_test()
    return {"cases": len(results),
            "behaved_correctly": sum(1 for r in results if r["ok"]),
            "failed_cases": [r["case"] for r in results if not r["ok"]]}


def _git_head() -> str:
    completed = run(["git", "-C", str(REPO), "rev-parse", "HEAD"], where="git head")
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect Windows host evidence for the delegation-first gate.")
    parser.add_argument("--report", default="windows-host-evidence.json",
                        help="where to write the JSON record")
    parser.add_argument("--only", action="append", default=[],
                        help="run only this check id; repeatable")
    parser.add_argument("--list", action="store_true", help="list the check ids and exit")
    parser.add_argument("--keep-fixtures", action="store_true",
                        help="leave each check's temporary tree in place for inspection")
    parser.add_argument("--self-test", action="store_true",
                        help="prove this script's own assertions can fail, then exit")
    args = parser.parse_args(argv)

    if args.self_test:
        print("Proving the assertions reject what they should:")
        results = self_test()
        bad = [r for r in results if not r["ok"]]
        print()
        print(f"{len(results) - len(bad)}/{len(results)} self-test cases behaved correctly")
        return 1 if bad else 0

    if args.list:
        for identifier, title, _ in CHECKS:
            print(f"{identifier:<34} {title}")
        return 0

    print(f"agent-bridge Windows host evidence, {platform.system()} "
          f"{platform.release()}, Python {sys.version.split()[0]}")
    print(f"repository: {REPO}")
    print("Nothing below touches this account's assistant configuration: every check "
          "runs under its own temporary home.")
    print()
    document = collect(tuple(args.only), keep=args.keep_fixtures)
    Path(args.report).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    summary = document["summary"]
    print()
    print(f"pass {summary['pass']}   fail {summary['fail']}   "
          f"skip {summary['skip']}   error {summary['error']}")
    print(f"record written to {Path(args.report).resolve()}")
    return 1 if summary["fail"] or summary["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
