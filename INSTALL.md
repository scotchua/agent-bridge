# Install

Written for someone who is comfortable in a terminal but is not a developer. If
any step fails, the failure message is meant to tell you what to do; if it does
not, that is a bug worth reporting.

## What you need first

- **macOS or Linux.** Windows is not supported directly. See
  [Windows: use WSL](#windows-use-wsl) below, which is a short detour and then
  these same instructions.
- **Python 3.11 or newer.** Check with `python3 --version`. No packages to
  install: this uses only the standard library on purpose, so there is nothing
  to keep updated and nothing new to trust.
- **The Claude Code CLI**, signed in.
- **The Codex CLI**, signed in.

Both CLIs are paid products on their own subscriptions. This tool does not
change what they cost; it just lets them talk to each other.

## Windows: use WSL

The tool refuses to start on Windows, because what it would be missing there
are the pieces that make its safety promises true rather than conveniences.
WSL is Microsoft's official way to run Linux inside Windows, and inside it
everything works normally. Half of this project's automated tests run on Linux
on every change, so this is the supported path and not a workaround.

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

Then continue with step 1 below, in the Ubuntu window.

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

Then, when both are signed in:

```
./bin/agent-bridge-setup --write
```

That writes `config/local.json`, which stays on your machine and is never
committed. Re-run this after either CLI updates, because the pinned version will
no longer match and jobs will refuse to run. That refusal is deliberate: these
tools change behaviour between versions, and this one is built against measured
behaviour rather than guesses.

## 3. Check it works without spending anything

```
python3 tests/test_suite.py
```

Expect `passed: N  failed: 0`. This uses stand-in programs pretending to be the
two CLIs, so it makes no network calls and costs nothing. Some checks may report
`skipped` if your system restricts `ps`; that is fine.

## 4. Check it works for real

```
./canaries/run_canaries.py --direction both --out /tmp/canary.json
```

This makes real calls to both models and costs real money: roughly 40
consultations, a few dollars on the Claude side. It tries one cheap call first
and stops early if a CLI is not usable, rather than running the whole matrix
into a wall.

Read the four numbers it prints: how many replies were valid, how many worked
first time, how long they took, and whether every follow-up landed on the
intended conversation. One success proves nothing, which is why it runs forty.

## 4b. Decide what each side is allowed to receive

Before you turn it on, read the **Data policy** section of the README. The short
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

## Day to day

```
./bin/agent-bridge-admin status
```

Shows how many consultations have run and warns you if any conversation is
being held for review. `ledger --tail 20` shows recent activity, and `cleanup`
removes old material but is a dry run until you add `--apply`.
