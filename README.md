# agent-bridge

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
| `bin/agent-bridge-admin` | `status`, `ledger`, `cleanup`, `indeterminate`, `resolve` |
| `config/broker.json` | committed defaults, machine-neutral |
| `config/local.json` | your machine, written by setup, never committed |
| `schema/` | the response contract both models must satisfy |
| `src/agent_bridge/` | the implementation, standard library only |
| `tests/test_suite.py` | full offline suite against stand-in CLIs |
| `canaries/run_canaries.py` | live verification |

State lives in `~/.agent-bridge`, outside this repository, readable only by you.
No consultation content is ever written into the repo.

## Requirements

Python 3.11+, the Claude Code CLI, the Codex CLI. No third-party Python
packages, deliberately: the audit surface is this repository and nothing else.

Built and measured against `claude 2.1.229` and `codex-cli 0.147.0` on macOS.
Several documented behaviours are version-specific, which is why setup pins your
versions and jobs refuse to run when they drift.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
