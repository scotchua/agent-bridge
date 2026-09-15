# Windows host evidence

Three things about the delegation-first gate have never been exercised on a
real Windows host, and one of them has never been exercised anywhere.
`tools/windows_host_evidence.py` collects all of it in one run and writes a
JSON record.

## What is already covered, so this does not repeat it

CI runs, on Windows runners as well as Linux and macOS: the whole offline
suite, plus `test_delegation_gate`, `test_automatic_gate`,
`test_delegation_audit` and `test_hostenv`. So the gate's *logic* is already
Windows-tested. This script deliberately covers what that misses.

## Three defects this already found, before the script ever ran on Windows

Sweeping for what to check turned up three real defects, all now fixed and
all now checked by the script, so a Windows run verifies the fixes there:

 • **The gate allowed every edit, silently, for anyone whose path was not pure
   ASCII.** Hook mode decoded its payload with the locale encoding, which on
   Windows is the ANSI code page, while both hosts emit raw UTF-8. A
   repository path with a non-ASCII character arrived as mojibake, no `.git`
   was found above it, and the gate returned `allow` with
   `outside_repository`. Measured: denied under a UTF-8 stdin, allowed under
   `cp1252`.
 • **A launcher that could not start Python failed open**, exiting non-zero
   with no JSON, which a host reads as a non-blocking error before running the
   tool anyway. On a stock Windows account with no Python, the bare name
   `python` resolves to the Microsoft Store alias stub, so that was the
   ordinary path there.
 • **The quoted-character set for `cmd.exe` had only the obvious delimiters.**
   A comma, semicolon, equals sign, percent and exclamation mark are legal in
   a Windows directory name and all significant to `cmd.exe`.

## What it covers

 • **The gate's own `bin/agent-bridge-gate-hook.cmd` launcher**, which has
   never been executed on any host. The Windows smoke test in CI runs
   `agent-bridge-windows-setup.cmd`, a different launcher. Seven checks drive
   it: a report, an allow, a deny, the chosen route editing, exit code, a
   foreign working directory, and argument forwarding.
 • **The installed command string being one `cmd.exe` can actually run.** This
   is the highest-value single check. `gate.hook_command` quoted both paths
   with `shlex.quote` until recently: POSIX single quotes, which `cmd.exe`
   does not treat as quoting at all. On an ordinary Windows home containing a
   space the installed hook was unrunnable, and a hook that never runs is a
   gate that never gates. The check installs into a home whose path contains
   a space, reads the `command` string back out of the host's own
   configuration file, and hands it to the platform shell with a real payload.
 • **The execution lanes and the end-to-end workflow**, which CI skips on
   Windows entirely. They must decline by name, not error.
 • **Windows path semantics on a real filesystem**: the protected-path rule
   against a re-spelled path, and whether one repository can key to two
   receipts under two spellings.

## Running it

```
git clone https://github.com/scotchua/agent-bridge
cd agent-bridge
git checkout claude/tender-tesla-7881pp
python tools\windows_host_evidence.py --self-test
python tools\windows_host_evidence.py --report windows-host-evidence.json
```

Run `--self-test` first. It proves the script's own assertions can fail before
you trust a pass, and takes under a second. Then send back
`windows-host-evidence.json`.

`--list` prints the check ids. `--only <id>` runs one. `--keep-fixtures`
leaves each check's temporary tree in place.

A launcher check deliberately runs with `PYTHONPATH` pointing somewhere
useless and `PYTHONDONTWRITEBYTECODE` unset, because those are the two
variables the `.cmd` exists to set. The first version of this script seeded
them into every child, so every launcher check would have passed on the
harness's coat-tails even if the launcher produced nothing usable. Deleting
the launcher's own `PYTHONPATH` now fails three checks; before, it failed
none.

## What it will not touch, and will not spend

 • **Your assistant configuration.** Every check runs under its own temporary
   home, deleted afterwards. `onboard._paths` derives every path from the home
   it is given whenever that home is not the account's own, and the
   environment handed to every child process redirects `HOME`, `USERPROFILE`,
   `APPDATA`, `LOCALAPPDATA` and `CODEX_HOME` as well, so both routes lead to
   the temporary tree.
 • **Provider allowance.** No provider is contacted. The one check that looks
   at Claude Code or Codex runs `--version` and records what it finds.
 • **Administrator rights.** Nothing needs them.

Requirements: Python 3.11+ and `git` on `PATH`. Nothing else.

## The one rule that makes the record worth reading

Hook mode **always exits 0**, and it turns any internal failure into a deny
carrying the code `gate_error`: an unreadable config, a mangled argument list,
a failed import. So "a deny happened" is exactly what a broken launcher
produces, and a check asserting only that would pass most confidently when the
thing under test was most broken.

Every deny assertion here therefore pins the specific code and rejects
`gate_error` by name, and every deny is paired with an allow through the same
path. The `--self-test` mode proves that rejection works, and its result is
embedded in every record under `assertion_self_test`, so a reviewer does not
have to take it on trust.

This matters because the same defect shape has already shipped in this
repository twice: two tests drove the Codex hook with `tool_name: "Edit"`,
Codex has no `Edit` tool, so the gate classified the call as not gated and
allowed it, and both tests passed while reaching none of the code they named.
One of them was the only guard against the two clients livelocking each other.

## What a passing record still does not prove

 • **The hook being invoked by Claude Code or the Codex CLI themselves.** Every
   check drives the launcher or the module directly. That is the same
   interface a host uses, but not the same integration. Getting that evidence
   needs those CLIs signed in and would spend provider allowance, so it is a
   separate, separately authorized exercise.
 • **Verification confinement on Windows**, which does not exist. The lane
   checks establish that it is *refused by name*, not that it works.
 • **Anything about a provider account.**
