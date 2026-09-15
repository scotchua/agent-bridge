# Install

For the easier conversational path, start with
[Set this up with Claude or Codex](docs/SETUP-WITH-AN-AGENT.md). The steps below
remain the lower-level verification and troubleshooting reference.

The guided installer covers consultation and its optional local worker. It can
also, on explicit opt-in, enable "automatic delegation": the durable stage
router and bounded cross-provider implementation lane described in
[Orchestration and local-worker MCP](docs/orchestration-mcp.md). This stays off
unless you say yes during `onboard questionnaire` or set
`automatic_delegation.enabled: true` in your answers file; every existing
answers file without that key keeps working and stays disabled. See
[Automatic delegation](#automatic-delegation-optional-opt-in) below. Its
persistent execution-worker service is currently verified on macOS only.

Written for someone who is comfortable in a terminal but is not a developer. If
any step fails, the failure message is meant to tell you what to do; if it does
not, that is a bug worth reporting.

## What you need first

- **macOS, Linux, or Windows.** WSL remains an alternative on Windows; see
  [Windows](#windows) for the differences between native Windows and WSL.
- **Python 3.11 or newer.** Check with `python3 --version`. No packages to
  install: this uses only the standard library on purpose, so there is nothing
  to keep updated and nothing new to trust.
- **The Claude Code CLI**, signed in.
- **The Codex CLI**, signed in.

Both CLIs are paid products on their own subscriptions. This tool does not
change what they cost; it just lets them talk to each other.

## Windows

Native Windows is supported using built-in Windows facilities only: byte-range
locks, Job Objects for process trees, and `icacls` for state ACLs. Startup
fails closed unless the ACL proves that no principal other than the owner,
SYSTEM, and Administrators has any access. Separately, the POSIX guarantee is
that state directories have mode exactly `0700` and files exactly `0600`.
Unlike the POSIX implementation, Windows uses bounded pipe-reader threads and
immediate Job Object termination (no graceful console signal). A process
deliberately escaping its Job Object is outside the process-tree guarantee,
just as a POSIX process deliberately starting a new session escapes its
original group.

Use PowerShell and run the commands below with `python` where they say
`python3`. Install the native Windows builds of the Claude and Codex CLIs.

**What has actually been run on Windows, and what has not.** The offline suite
has been run by hand on Windows 11 with CPython 3.12 as an ordinary interactive
user, and CI runs it on Windows with CPython 3.11 and 3.13. An ordinary Windows
account without symlink privileges skips the checks that require them.
Use the summary printed by your checkout as the authority: every
executed check must pass, and only explicitly explained skips are acceptable.
This covers the process-tree, locking and ACL code directly with stand-in CLIs.

The full live native-Windows setup remains unverified. Steps 2, 4, 5 and 6 need
both vendors' CLIs installed and signed in, which the test machine did not
have. If you walk the whole install on Windows, watch CLI discovery and pinning
in step 2 and use the `.cmd` launchers shown in step 5. Please report what you
find either way.

**Automatic delegation is not available on Windows.** Its persistent execution
worker is verified on macOS only. `onboard questionnaire` does not offer it
here and `onboard plan` and `apply` refuse it, because no Windows machine
carries the boundary-verification record the gate requires. Everything below
describes provisioning work in progress, not a feature you can turn on.

**You do not need to install WSL.** Automatic delegation on Windows runs jobs in
an ephemeral WSL2 guest, but that is an implementation detail: setup detects
what is missing, enables the Windows features, installs or updates WSL, installs
the pinned guest image, and verifies the containment boundary, asking separately
before anything that needs an administrator and before anything that restarts
the machine, and resuming itself after a restart. `onboard plan` and
`onboard status` report the current stage under `windows_setup`. Delegation
stays off until every stage is satisfied.

The command that does it is `bin\agent-bridge-windows-setup` (equivalently
`python setup_bridge.py windows-setup`). `plan` shows where you are, `step`
does the single next thing and takes the consent flags, `status` shows the
resume record and the logon task, and `resume` is what the logon task runs for
you after a restart. Consents are separate flags and none of them survives a
reboot: a resumed stage that needs one waits for you. It has not been run on a
live Windows host.

`validate` reads the machine and prints one JSON report, writing nothing:

```text
bin\agent-bridge-windows-setup validate --report validation.json
```

Every check is named, says what it proves and what it does not, and a check
that could not run is reported as blocked rather than skipped, so the verdict
is `ready` only when all of them passed. Two runs on an unchanged machine
produce the same checks, so you can diff one against the last. Attach the
report when you tell somebody the lane works; it is the only form of that
claim anybody can check.

Delegation on Windows does not run Claude Code or Codex jobs yet. The mechanism
is there: the guest is handed a memory-only session capsule for the job's
lifetime, never an API key and never a copy of your credential store. What has
not been observed on a real machine is whether a session minted on your host is
accepted from inside the guest, and how one that expires mid-job behaves. Until
a verification run on your own machine records both, provider jobs are refused
by name. See [docs/WINDOWS-DELEGATION.md](docs/WINDOWS-DELEGATION.md) for the
full boundary, the setup ladder and what is still open, and
[docs/WINDOWS-ROOTFS.md](docs/WINDOWS-ROOTFS.md) for building the guest image.

The guest blocks the host filesystem completely and blocks every route to your
host and local network, but it does have outbound internet access, because the
provider CLIs need it. None of it has been validated on a live Windows host.

WSL is also supported and provides the POSIX implementation. To use it:

**1. Install WSL.** Open PowerShell as Administrator and run:

```
wsl --install
```

Reboot when it asks. On first launch it will ask you to create a Linux username
and password. That password is only for Linux and is separate from your Windows
login. Microsoft's own guide is at
<https://learn.microsoft.com/windows/wsl/install>.

**2. Open the Linux terminal.** Search your Start menu for "Ubuntu", or run
`wsl` from PowerShell. Everything from here happens in that window, not in
PowerShell or Command Prompt.

**3. Install the two CLIs INSIDE Linux.** This is the step people get wrong.
A Claude or Codex CLI installed on Windows is not visible to the Linux side.
Install both from inside the Ubuntu window, following each vendor's Linux
instructions, and sign in from there too. When you sign in, the browser window
that opens is your normal Windows browser, which is expected.

**4. Keep everything on the Linux side of the filesystem.** Do not put this
tool, or its state directory, under `/mnt/c` or any other `/mnt/` path. Those
are your Windows drives seen from Linux, and they do not keep Linux file
permissions: the tool would ask for owner-only and silently get
world-readable. Consultation history would not be private.

The tool checks this at startup and refuses rather than storing your history
somewhere it cannot protect. If you see a message about the state directory not
keeping owner-only permissions, this is why. Your Linux home directory, which
is where you start, is the right place, and the default `~/agent-bridge` and
`~/.agent-bridge` are already correct.

Then continue with step 1 below, in the Ubuntu window. The `/mnt/` filesystem
warning applies to WSL only, not a native Windows installation.

## 1. Get the code

```
git clone https://github.com/scotchua/agent-bridge.git ~/agent-bridge
cd ~/agent-bridge
```

On WSL, `~` is your Linux home directory, which is correct. Do not substitute a
`/mnt/c/...` path here.

## 2. Find and pin your CLIs

```
./bin/agent-bridge-setup
```

This looks for both CLIs, tells you which versions it found and whether each one
is signed in, and shows you what it would pin. It writes nothing yet.

It is normal to have more than one Claude install. Setup prefers one that is
signed in, because a second copy that has never been logged in will fail every
single call, and the error looks like a bug in this tool rather than a login
problem.

If it reports something is not signed in:

```
<the path it printed> auth login
```

and for Codex, signing in to the **isolated** home this tool uses:

```
CODEX_HOME=~/.agent-bridge/codex-home <the path it printed> login
```

Then, when both are signed in, stage a complete effective configuration:

```
./bin/agent-bridge-setup --candidate ~/.agent-bridge/candidates/effective.json
```

This does not change the active `config/local.json`. The candidate is one
complete, validated effective config: committed defaults plus the current local
overlay and the proposed executable/version pins. Re-run this after either CLI
updates, because the active pin will refuse the new version until its candidate
has passed verification.

## 3. Check it works without spending anything

```
python3 tests/test_suite.py
```

Expect `passed: N  failed: 0`. This uses stand-in programs pretending to be the
two CLIs, so it makes no network calls and costs nothing. Some checks may report
`skipped` if your system restricts `ps`; that is fine.

On native Windows, run it as yourself rather than elevated. Expect every
executed check to pass. Symlink checks may be skipped when an ordinary account
cannot create symlinks unless Developer Mode is on; read the printed skip
reasons rather than expecting a fixed count.

## 4. Check it works for real

```
./canaries/run_canaries.py \
  --config ~/.agent-bridge/candidates/effective.json \
  --direction both \
  --out ~/.agent-bridge/canary-results/latest.json
```

This makes multiple real calls to both models and consumes allowance or incurs
charges under your own provider accounts. Obtain authorization before running it. It tries one cheap call first
and stops early if a CLI is not usable, rather than running the whole matrix
into a wall.

Read the four numbers it prints: how many replies were valid, how many worked
first time, how long they took, and whether every follow-up landed on the
intended conversation. One success proves nothing, which is why it runs forty.

The result file records the effective-config hash, configured and observed
versions, requested and executed controls, and the final verdict. It must be on
durable storage, not under a temporary directory. Skipping the timeout canary
records `INCOMPLETE`, never `PASS`.

Only promote the candidate after reviewing a `PASS` result:

```
./bin/agent-bridge-setup \
  --promote ~/.agent-bridge/candidates/effective.json \
  --results ~/.agent-bridge/canary-results/latest.json
```

Promotion rechecks the candidate hash, both configured/observed version
bindings, and the control counts. It then atomically writes only the derived
overlay fragment to `config/local.json`; a missing, incomplete, failed, or
version-mismatched result is refused.

## 4b. Decide what each side is allowed to receive

Before you turn it on, read the **Privacy choices** section of the README. The short
version: this sends your text to two different companies under two different
agreements, and it was built assuming both of your plans protect data equally.
If yours do not, you can allow one side less than the other by adding this to
`config/local.json`:

```json
{
  "peers": {
    "codex": { "allowed_source_classifications": ["public"] }
  }
}
```

Substitute whichever peer is on the weaker plan. Doing nothing keeps both sides
equal, which is the right default only if your plans really are equal.

## 4c. Decide how hard each side should think

Optional, and worth thirty seconds. Neither peer inherits a reasoning setting
from your own configuration: the Codex peer deliberately ignores your personal
config file, and the Claude peer is started fresh. Left alone, both run at their
own default.

If you are using this for review work, where the whole point is catching what
you missed, higher effort is usually worth the extra cost and time. Add to
`config/local.json`:

```json
{
  "peers": {
    "claude": { "reasoning_effort": "xhigh" },
    "codex":  { "reasoning_effort": "xhigh" }
  }
}
```

Accepted values are `low`, `medium`, `high`, `xhigh` and `max`. They can differ
per side. Whatever you choose, including choosing nothing, is recorded on every
consultation in the ledger, so you can always tell later how hard the model was
asked to think about an answer you relied on.

## 5. Turn it on

Only after step 4 passes. These two commands change your MCP configuration.

```
codex mcp add claude-peer -- ~/agent-bridge/bin/agent-bridge-mcp --caller codex
```

```
claude mcp add --scope user codex-peer -- ~/agent-bridge/bin/agent-bridge-mcp --caller claude
```

Two things about the first command. Run it with your **normal** Codex setup, not
with `CODEX_HOME` set to the isolated directory: pointing it at the isolated home
would hand the consulted model this very bridge, and it would be able to consult
back in a loop. The tool refuses to run at all if it detects that, but the
simplest protection is not to create it.

On **Windows (PowerShell)**, use the `.cmd` launcher instead, because a `/bin/sh` script
cannot be executed there:

```
codex mcp add claude-peer -- "$env:USERPROFILE\agent-bridge\bin\agent-bridge-mcp.cmd" --caller codex
```

```
claude mcp add --scope user codex-peer -- "$env:USERPROFILE\agent-bridge\bin\agent-bridge-mcp.cmd" --caller claude
```

**Restart the Codex desktop app** after adding it, or the new tools will not
appear in a new task.

## 6. Try it

In a new Codex task:

> Use claude_start to ask Claude: "A job queue retries failed jobs with a fixed
> one second backoff, forever. Name the single worst failure mode and the minimum
> change that fixes it." Set source_classification to synthetic. Then poll and
> read, and show me Claude's response verbatim.

In a new Claude Code session, the same thing with `codex_start`.

You get a job id back immediately, then wait somewhere between fifteen seconds
and a minute or so.

## Turning it off

```
codex mcp remove claude-peer
```

```
claude mcp remove --scope user codex-peer
```

Restart the Codex app. Your history stays in `~/.agent-bridge` until you delete
it, and `~/.agent-bridge/ledger/exchanges.jsonl` is the record of every
consultation you ever ran.

## Automatic delegation (optional, opt-in)

The default bridge above installs consultation only: either assistant can ask
its peer a question and get an answer back. Automatic delegation is a
separate, advanced opt-in that adds durable stage ownership, a queue for
bounded cross-provider implementation jobs, and automatic local routing for
eligible non-client mechanical text. It never applies, commits, pushes, or
merges on its own, and it never silently falls back from local processing to
a cloud model or a paid API.

Both bounded implementation harnesses (Claude and Codex) already ship in this
checkout; nothing extra needs installing for either direction. The Codex
execution harness signs in through the same isolated `CODEX_HOME`
(`~/.agent-bridge/codex-home`) documented above for consultation, so the
`codex login` you already ran covers this too. It never copies credentials
and never falls back to your default `~/.codex` home.

**The Claude execution lane needs its own login, once.** It uses one dedicated
configuration directory, `~/.agent-bridge/claude-home`, and refuses to run
against anything else. That is not a preference: the default `~/.claude` store
is the one the desktop app and interactive sessions refresh, and a concurrent
invalid-grant cleanup there signs the lane out. The lane refuses the shared
store by name, refuses any link or alias that resolves to it, refuses any
other directory, and refuses a directory other accounts can read.

```
mkdir -p ~/.agent-bridge/claude-home && chmod 700 ~/.agent-bridge/claude-home
CLAUDE_CONFIG_DIR=~/.agent-bridge/claude-home claude /login
```

Sign in there with the same subscription. Never copy credentials into it: a
copied session is one the provider cannot see you revoke. Onboarding writes
`claude_config_dir` into the private config and reports
`claude_config_dir_ready` separately from `execution_complete`, because the
harness files ship with this checkout and the login does not.

**Turn it on:**

```
python setup_bridge.py onboard questionnaire --answers /absolute/private/answers.json
```

Answer yes to "Enable automatic delegation?" (or set
`"automatic_delegation": {"enabled": true}` directly in the answers file).
Optionally name an already-built, compliant private local-worker executable
with `local_worker_executable` if you want the local-model lane checked too;
omit it and that lane is honestly reported `not_configured` rather than
guessed at.

**Verify before applying.** This is a live check against your own signed-in
CLIs; it never applies a patch, commits, pushes, merges, downloads a model, or
enables paid fallback, and it only ever uses synthetic, disposable content:

```
./bin/agent-bridge-orchestration-verify \
  --config ~/.agent-bridge/orchestration/orchestration.json \
  --callers codex,claude \
  --out ~/.agent-bridge/orchestration/delegation-verify.json
```

**Apply with the evidence:**

```
python setup_bridge.py onboard apply \
  --answers /absolute/private/answers.json \
  --candidate /absolute/private/candidate.json \
  --results /absolute/private/canaries.json \
  --delegation-results ~/.agent-bridge/orchestration/delegation-verify.json
```

Apply refuses to enable automatic delegation without valid, matching evidence,
and refuses outright if nothing required was actually proven. Otherwise the
completion report says exactly `"Automatic delegation: enabled"`,
`"...: partial"`, or `"...: blocked"`, matching what the evidence showed. A
`"partial"` result commonly means the local-model lane was not configured, or
one direction's live provider CLI is not signed in or not authorized on this
machine (see the honest limitations in the README about the two harnesses'
different confinement).

**On macOS**, apply stages a private, owner-only LaunchAgent template for the
execution worker but does not silently load it. Load it into the login GUI
domain yourself, never as root:

```
python setup_bridge.py onboard activate-launch-agent --apply
```

Without `--apply` this only prints the exact command it would run. Loading is
idempotent: an already-active agent is reported as such and never re-loaded.

**Check, disable, or remove it:**

```
python setup_bridge.py onboard uninstall --answers /absolute/private/answers.json --delegation-only
python setup_bridge.py onboard uninstall --answers /absolute/private/answers.json --delegation-only --apply
python setup_bridge.py onboard deactivate-launch-agent --apply
```

Uninstall removes only the orchestration MCP registrations and LaunchAgent
file it can still identify as its own, leaving the ordinary consultation
bridge installed and untouched. The private orchestration configuration and
its receipt are retained for inspection, matching how consultation's shared
instructions and `config/local.json` are retained.

**Windows and Linux** support the same registration and private-config
generation, but do not install a continuously-running execution-worker
service; that has only been independently verified on macOS. `onboard plan`
reports this boundary for your platform before you apply anything. Windows can
register the worker as a per-user logon task in your own Task Scheduler
namespace, with no administrator rights and no other account, and you can remove
it yourself; that path has not been exercised on a live Windows host.

## Delegation-first gate (optional, after automatic delegation)

With automatic delegation on, a PreToolUse hook can make the routing decision
mandatory: Claude Code and the Codex CLI refuse to edit a repository until a
stage for it is claimed and `routing_decide` has written a receipt, and the
client whose route does not own the stage is told to dispatch instead.
Install with `./bin/agent-bridge-gate-hook install --root . --config <orchestration.json> --apply`,
then accept the hook once in Codex's `/hooks`. What it covers and what it
cannot (desktop and web chats have no hook surface) is in
[docs/DELEGATION-GATE.md](docs/DELEGATION-GATE.md).

## Day to day

```
./bin/agent-bridge-admin status
```

Shows how many consultations have run and warns you if any conversation is
being held for review. `ledger --tail 20` shows recent activity, and `cleanup`
removes old material but is a dry run until you add `--apply`.
