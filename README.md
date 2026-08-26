# agent-bridge

![tests](https://github.com/scotchua/agent-bridge/actions/workflows/tests.yml/badge.svg)

A local bridge that lets Claude and Codex consult each other for a second
opinion, and keeps a record of every exchange.

Everything runs on your own machine. Nothing is hosted, there is nothing to sign
up for, and the only network calls are the ones the two CLIs already make on
your behalf.

## Why you might want this

If you use both Claude and Codex, you have probably noticed they disagree, and
that the disagreement is often the useful part. Asking one to check the other
normally means copying text between two windows and losing the thread.

This makes that a tool call. A Codex task can ask Claude a bounded question and
continue that conversation across several turns. A Claude Code session can do
the same with Codex. The reply comes back as structured data with a record of
which model said it, on what version, at what time.

It was built for reviewing work before it goes out: designs, scripts, a piece of
logic you are not sure about. The reason it keeps a ledger is that "the other
model agreed" is only worth something if you can go back and see what was
actually asked.

## What it will not do

Version 1 is consultation only. It cannot edit your files, run commands, merge
anything, or start consultations on its own. A consulted model can suggest you
ask again; it cannot do it. Only you can.

It also refuses to carry sensitive material. Every request must be labelled
`internal`, `synthetic`, or `public`, and anything labelled as client-derived or
confidential is rejected outright. That refusal is not a substitute for your own
judgement, for the reason in [Honest limits](#honest-limits).

## Getting started

See **[INSTALL.md](INSTALL.md)**. Short version:

```
./bin/agent-bridge-setup          # find and check your CLIs
python3 tests/test_suite.py       # offline, costs nothing
./canaries/run_canaries.py --direction both   # live, costs a few dollars
```

Then two `mcp add` commands, which INSTALL.md spells out.

## What you get

With `--caller codex`, a Codex task sees `claude_start`, `claude_continue`,
`claude_poll`, `claude_read`, `claude_close`. With `--caller claude`, a Claude
session sees the matching `codex_*` tools. Neither side can consult its own
model: those tools simply do not exist in that session, which is a stronger
guarantee than refusing when asked.

`start` and `continue` hand back a job id straight away, so nothing blocks while
a model thinks. The work happens in a separate process that writes its progress
to disk, so you can restart your editor mid-consultation and still collect the
answer.

## Honest limits

Read these before you trust it with anything that matters.

- **Your two subscriptions may not have the same data protections.** This was
  built assuming they do. If yours differ, the weaker plan governs whatever you
  sent in that direction, and you can restrict what each side is allowed to
  receive. See [Data policy](#data-policy-two-vendors-two-accounts-two-sets-of-terms).
- **The consulted model is told not to read your filesystem. On the Codex side
  that is a rule, not a wall.** Its sandbox blocks writing, not reading. So treat
  the question you send as the boundary: assume anything in the prompt could be
  read, and put nothing in it you would not want read. The
  `internal / synthetic / public` label is there to make you think about that
  every time, not to enforce it for you.
- **The consulted model still has its own built-in skills.** The Codex side runs
  with your configuration and rule files disabled, but Codex ships skills of its
  own inside the isolated directory this tool uses, and no setting existed to
  turn those off in the version this was built against. They are inventoried on
  every job so you can see what was there. Nobody has proven they are inert.
- **A consulted model's answer is one opinion, not a verdict.** The tool labels
  it as data and never treats it as an instruction. Neither should you. If it
  matters, the person signing it is still the person signing it.
- **Killing a runaway process is best effort.** A process that deliberately
  detaches itself can survive.
- **If a consultation is interrupted mid-call, that conversation is held.**
  Stopping a local program does not prove the model on the other end stopped, so
  rather than guess, the conversation waits for you to look at it. There is no
  timeout, on purpose. `agent-bridge-admin status` tells you when one is waiting.

## One setting worth making deliberately

Neither peer inherits a reasoning-effort setting from your own configuration.
The Codex peer runs with your personal config ignored, which is the isolation
working correctly, and the Claude peer is started fresh. So unless you set one,
both run at their own default rather than at whatever you have chosen for
yourself elsewhere.

That is a reasonable default and a poor accident. For review work, set
`reasoning_effort` per peer in `config/local.json`; see
[INSTALL.md](INSTALL.md). Whatever you pick, including nothing, is recorded on
every consultation, so the ledger can always answer how hard the model was
asked to think about an answer you relied on.

## Data policy: two vendors, two accounts, two sets of terms

This is the assumption most worth checking before you use it for anything real.

### The technical shape

Every consultation sends the prompt you composed to one of two **different
companies**, through that company's own CLI, under your own account with them,
governed by whatever plan you are on with them. The bridge adds no hosting of
its own: there is no server in the middle, and nothing is stored anywhere except
on your machine. But it does not change, improve, or unify what either company
does with the text once it arrives.

**This was built by someone whose Claude and Codex subscriptions have
comparable data protections.** That is a fact about the author's accounts, not a
property of this software. If your two plans differ, the software will happily
send the same sentence to both, and the weaker plan governs what happens to the
copy it received.

The important consequence is that **your exposure is per direction, not an
average of the two.** If one side retains prompts for longer, or trains on
inputs where the other does not, then anything you send *in that direction* gets
that treatment. Sending it once is the exposure. There is no netting.

### What to check, on each plan separately

Ask the same five questions of both vendors, for the specific plan you are on,
because answers commonly differ between consumer, professional, team and
enterprise tiers of the same product:

1. Are my inputs used to train or improve models?
2. How long is prompt and output content retained, and can I turn retention off?
3. Who at the vendor can access content, and under what circumstances, for
   example abuse review?
4. Where is content processed and stored, geographically?
5. Does a business or enterprise agreement change any of the above, and would
   that agreement cover the CLI specifically rather than only the web product?

Answers change. Check them yourself, on your own plan, rather than trusting a
summary in a README, including this one.

### Hardening one side

If one side is weaker, you have three options, roughly in order of how much they
cost you:

**Narrow what the weaker peer may receive.** Each peer can be allowed a shorter
list than the other, in `config/local.json`:

```json
{
  "peers": {
    "codex": { "allowed_source_classifications": ["public"] }
  }
}
```

That peer then refuses anything not on its list, and the refusal is enforced
before any request leaves your machine. The tool description that peer's caller
sees also advertises only the narrower list, so the calling model is told what
it may send rather than discovering it by being refused.

**Run only one direction.** The two directions are separate MCP servers. Skip
the `mcp add` for the one you do not want, and that direction does not exist.

**Upgrade the weaker plan**, if the vendor offers a tier with terms you are
satisfied with.

### What this means in plain language

If you are an accountant, here is the whole thing without the jargon.

This tool makes it very easy to send a question to two different AI companies.
Easy enough that you will stop thinking about it, which is exactly the risk. The
convenience is real and so is the exposure it creates.

**Treat the question you type as a document you are handing to an outside firm.**
Because that is what it is. You are handing it to two outside firms, and each one
does what its own contract with you says, not what the other one does.

Three practical rules:

- **If you would not paste it into that company's public chat window, do not
  send it through this.** The bridge is a nicer interface to the same act.
- **Client-identifying information does not go in, ever.** Not names, not
  account numbers, not enough surrounding detail to identify someone. If you
  need a second opinion on a client situation, describe the *structure* of the
  problem with the identifying facts removed. That is the same discipline you
  would use asking a colleague at a conference.
- **The label is a speed bump, not a lock.** Marking something `internal` does
  not protect it. It exists to make you pause for one second and think about
  what you are about to send. It refuses obvious mistakes; it cannot read your
  prompt and tell you that paragraph three names a client.

And the part people skip: **your obligations to clients do not change because a
tool made something convenient.** Confidentiality rules, engagement letters, and
any consent requirements that apply to disclosing client information apply
exactly as they did before. A tool being on your own machine does not make the
data local, because the whole point of it is to send the text somewhere else.

None of this is legal advice, and it is not a substitute for reading your own
vendor agreements or asking someone qualified about your own obligations.

## How it was built, and why that is in the repo

This started as a design that turned out to be wrong in four specific ways, all
documented in [docs/verified-cli-behaviour.md](docs/verified-cli-behaviour.md).
It then went through four rounds of adversarial review, where each model was
asked to attack the other's work. That produced 40 findings, including two
separate cases where two individually correct fixes broke each other, and five
tests that had been quietly asserting a bug was correct behaviour.

[docs/REVIEW-HISTORY.md](docs/REVIEW-HISTORY.md) is the write-up. It is in here
because the findings are more useful than the code: if you build something like
this yourself, that document is the part worth reading first.

[docs/BUILD-YOUR-OWN.md](docs/BUILD-YOUR-OWN.md) is a brief you can paste into
Claude or Codex to have it build an equivalent from scratch, with the traps
already listed. Porting this repository is the better option if you can.

## Layout

| Path | What |
|---|---|
| `bin/agent-bridge-setup` | find, check and pin your two CLIs |
| `bin/agent-bridge-mcp` | the MCP server, needs `--caller codex` or `--caller claude` |
| `bin/agent-bridge-admin` | `status`, `ledger`, `reporting`, `cleanup`, `indeterminate`, `resolve` |
| `config/broker.json` | committed defaults, machine-neutral |
| `config/local.json` | your machine, written by setup, never committed |
| `schema/` | the response contract both models must satisfy |
| `src/agent_bridge/` | the implementation, standard library only |
| `tests/test_suite.py` | full offline suite against stand-in CLIs |
| `canaries/run_canaries.py` | live verification |

State lives in `~/.agent-bridge`, outside this repository. On POSIX systems,
state directories have mode exactly `0700` and files exactly `0600`. On
Windows, no principal other than the owner, SYSTEM, and Administrators has any
access. No consultation content is ever written into the repo.

## Checking it yourself

Every push runs the offline test suite on macOS and Linux, on two Python
versions, via the badge at the top. That run uses stand-in programs in place of
the two CLIs, so it needs no credentials and costs nothing. The live
verification is deliberately not automated: it spends real model calls, so it
stays a decision a person makes.

## Requirements

**macOS, Linux, and Windows.** Python 3.11+, the Claude Code CLI, the Codex
CLI. No third-party Python packages, deliberately: the audit surface is this
repository and nothing else.

**Windows passes its own test suite**, 478 tests, zero failures, zero skips,
verified by hand in a Windows 11 VM on CPython 3.12 rather than only in CI.
That took finding several defects that no amount of POSIX testing could have
surfaced, because the POSIX idiom and the Windows behaviour differ silently:
`os.kill(pid, 0)` is a liveness probe on POSIX and a console interrupt on
Windows, and a job-liveness check that only looked at the leader process
reported a surviving descendant as contained.

Verified on one configuration. An English-language `icacls`, a domain-joined
machine, or a redirected profile are not covered by that run, and the ACL
parser is the part most likely to need work on them. It fails closed, so an
unrecognised ACL refuses the run rather than assuming privacy.
[WSL](INSTALL.md#windows-use-wsl) remains supported and uses the POSIX path.

A peer on Windows is also terminated immediately, with no graceful stage,
because no console signal can be aimed at one process tree without risking
delivery to the bridge itself. That difference is recorded in each job.

**Windows uses a separate implementation of the same safety interface.** Its
locks are mandatory byte-range locks rather than POSIX advisory `flock` locks.
It contains peers in a Win32 Job Object and terminates that job, with no
polite stage at all, for the reason given above. Whether a tree is still alive
is answered by asking the job which processes it contains, not by inspecting
the leader. A process deliberately
escaping that job is outside the tree guarantee, comparable to a POSIX child
starting a new session. Pipe output is drained with bounded reader threads
because Windows `select` does not support anonymous pipes.

State privacy on Windows guarantees that no principal other than the owner,
SYSTEM, and Administrators has any access; it does not claim POSIX mode-bit
semantics. The bridge applies that ACL with the built-in `icacls` tool and
reads it back; if the ACL cannot be verified, it refuses to claim or use
owner-only permissions. WSL remains supported and uses the POSIX behavior;
[INSTALL.md](INSTALL.md#windows) explains its filesystem caveat.

Built and measured against `claude 2.1.229` and `codex-cli 0.147.0` on macOS.
Several documented behaviours are version-specific, which is why setup pins your
versions and jobs refuse to run when they drift.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
