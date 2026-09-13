# agent-bridge

![tests](https://github.com/scotchua/agent-bridge/actions/workflows/tests.yml/badge.svg)

> **Experimental:** agent-bridge is ready for early testers, but its interfaces
> and setup may change as real-world feedback arrives. Review the privacy and
> platform limits below before using it. This early release is intended to find
> problems on fresh installations; it does not depend on a tester-count gate.

Connect Claude and Codex so you can say **“ask the other assistant”** without
copying messages between them. Either assistant can coordinate the work, ask
its teammate for help, and bring the answer back into your conversation.
An optional existing local Ollama model can handle routine text tasks.

The bridge runs on your computer. Claude and Codex consultations still go to
their providers through your own accounts.

## Why you might want this

Claude and Codex can contribute different approaches to the same problem.
This gives them a direct way to compare plans, investigate a bug, review a
change, or challenge an answer before you rely on it.

You stay in the conversation with whichever assistant you started with. That
assistant chooses the relevant context, consults the other through a tool, and
uses the reply to continue your task. Follow-up questions can continue the same
consultation. You do not have to act as the messenger.

For example:

- “Ask Claude to review this design before you implement it.”
- “Work with Codex to figure out why this Windows test fails.”
- “Have the local model classify these notes, then review its results.”

The bridge records what was actually sent and returned, so “the other assistant
agreed” can be checked against the exchange.

## Getting started

Use **Claude Code or Codex with access to local files and programs**, and give
it this request:

> Set up https://github.com/scotchua/agent-bridge on this computer. Read its
> AGENTS.md and docs/SETUP-WITH-AN-AGENT.md. Ask me about privacy restrictions
> and whether I want to connect an existing local model. Carry out setup and
> verification, preserve my current settings, and explain any remaining
> limitations. Ask before live tests that use my provider allowance or money.

A website-only chat cannot install the bridge on your computer.
If the repository is still private, request access before starting setup.

[The guided setup](docs/SETUP-WITH-AN-AGENT.md) takes the assistant through:

1. Checking Python 3.11+, Git, and both providers' CLI installations and logins.
   You complete any interactive account login yourself.
2. Asking which apps to connect, whether consultations should work in both
   directions, what each peer may receive, and whether to add a local model.
3. Showing a plan and staging the configuration before activation.
4. Running offline tests and, with authorization, live provider checks.
5. Installing the selected connections and shared collaboration instructions,
   preserving unrelated settings and keeping backups.
6. Reloading the selected apps as needed and verifying a question and follow-up.

The setup supports macOS and native Windows; Linux/WSL details are in
[INSTALL.md](INSTALL.md). In WSL, keep the checkout, Python, CLIs and state on
the Linux side. Claude Desktop can receive the MCP connection, but loading the
shared instructions into its chats/projects requires a separate verified step.

The live verification makes calls to **both providers**, even if you choose to
expose only one direction afterward. It consumes allowance or incurs charges
under your accounts. Activation requires a complete check tied to the chosen
configuration and installed CLI versions.

For manual setup and troubleshooting, see [INSTALL.md](INSTALL.md). For the
portable guided commands, see [SETUP-WITH-AN-AGENT.md](docs/SETUP-WITH-AN-AGENT.md).

## What you get

| Feature | What it provides |
| --- | --- |
| Two-way consultation | Codex can ask Claude; Claude can ask Codex. Either direction can be omitted. |
| Follow-up conversations | The coordinating assistant can continue a peer consultation without asking you to relay messages. |
| Shared instructions | A generated collaboration and model-effort policy, with managed pointers for the selected Codex and Claude Code installations. |
| Privacy choices | Baseline, strict, or custom eligibility rules for each receiving peer. |
| Optional local worker | Bounded summarization, extraction, classification, checklists and log triage through an existing Ollama model. |
| Exchange records | Prompts, replies, job status and available model/version/effort provenance. |
| Guided installation and removal | Staged settings, verification, backups, conflict checks and an uninstall preview. |

Under the hood, each assistant connects to a local MCP server. The bridge
launches the other provider's CLI, records its response, and returns it through
the tool. Codex gets `claude_start`, `claude_continue`, `claude_poll`,
`claude_read` and `claude_close`; Claude gets the matching `codex_*` tools.
The bridge does not expose self-consultation tools to either caller.

Jobs run separately from the initiating editor session and persist progress to
disk. The assistant can collect a completed reply after an editor restart.

## Privacy choices

Setup asks what may be sent to each receiving peer:

| Choice | Admitted material |
| --- | --- |
| Baseline | Public material, invented examples, and your own non-client internal work. |
| Strict | Public material and invented examples only. |
| Custom | A narrower list for each peer; synthetic examples remain necessary for full setup verification. |

Client-derived/confidential material and secrets are outside this bridge's
supported use. Removing names does not automatically make client-derived
material eligible.

**The bridge checks the supplied classification, not the contents of the text.**
Disallowed labels are rejected before dispatch, but it does not detect a secret
inside a request incorrectly labelled `public`, or automatically redact it.
The coordinating assistant must select appropriate context before sending it.

The repo does not copy the author's credentials, private policies, account
permissions or provider agreements. Each provider processes consultations under
your own account's terms. Local bridge records are separate from any records
retained by the providers.

See [Data retention](docs/DATA-RETENTION.md) for what is stored locally, how the
30-day defaults work, and what cleanup and uninstall leave behind.

## Optional local model

If you already have a model installed in Ollama, setup can connect it using its
exact model name and an explicit local endpoint. It asks separately whether the
worker may receive non-client internal text.

The worker accepts bounded inline text tasks. It does not read files, run shell
commands, download models or provide a cloud fallback. Other local runtimes
need a separately tested adapter.

**Local inference does not make the surrounding conversation private.** Text
passed by Claude or Codex, and results returned to it, are already visible in
that cloud assistant's conversation. The worker checks for supported local
model metadata and rejects recognized cloud routes, but it is not a network
sandbox around an arbitrary Ollama server.

## What it will not do

- **Synchronize all your chats or give either assistant the other's memory.**
  The coordinator sends selected context; a follow-up retains that consultation.
- **Turn a consulted peer into an unrestricted remote worker.** Peer calls are
  designed for consultation. The coordinating assistant can implement the
  suggestions using its own tools and your authorization.
- **Automatically grant equal permissions in every app.** Accounts, host tools
  and platform permissions remain separate. Setup reproduces the supported
  connection and shared guidance, not the author's entire environment.
- **Guarantee lower subscription usage or automatically balance allowances.**
  Consultations and live checks use capacity too. The author's separate capacity
  collectors and workload-routing integrations are not included in this setup.
- **Make agreement proof of correctness.** Peer replies are evidence to assess,
  not instructions to obey or an automatic approval to publish.

## Honest limits

- **Filesystem read isolation is incomplete on the Codex peer.** It is instructed
  not to inspect files and runs with a write sandbox, but reads are not fully
  confined. File content read this way can reach the provider and appear in
  replies, regardless of the request label. Built-in assets can remain available. Privacy labels do not repair
  this boundary; do not use this setup where enforced read confinement is required.
  The Claude peer runs with tools and customizations disabled.
- **Model and effort settings are separate from your interactive session.**
  Choose peer settings deliberately using [INSTALL.md](INSTALL.md). Records
  distinguish requested settings from observed information where available;
  unavailable information remains unknown. Shared guidance is not a guarantee
  that every host will enforce a model choice.
- **CLI updates can invalidate verification.** Setup pins versions, and version
  drift requires revalidation. A passing test is evidence for the configuration
  tested, not every future CLI release.
- **Stopping a local process does not prove a provider stopped processing.**
  Process-tree cleanup is best effort. An interrupted call can leave a
  conversation held as indeterminate for explicit resolution; see the admin
  commands in [INSTALL.md](INSTALL.md).
- **Offline CI is not a live installation test.** It uses stand-in provider
  programs. Your accounts, login, selected models and optional Ollama service
  still need verification on your computer.

## Checking it yourself

CI runs the offline tests on **macOS, Linux and Windows**, with Python **3.11
and 3.13**. The badge above links to current results. Windows fixes have also
been exercised in an ordinary interactive Windows 11 VM account.

From the repository, using your Python command (`python3` on many Macs,
`python` or `py -3` on Windows):

```text
python tests/test_suite.py
python -m unittest discover -s tests -p "test_onboard.py"
python -m unittest discover -s tests -p "test_local_worker.py"
```

These checks require no provider credentials or real model calls. Run Windows
checks as your ordinary user: elevated/service accounts can hide permission
problems. Symlink tests may be skipped when the account cannot create symlinks;
read the skip reasons rather than expecting a fixed passing-test count.

Live provider checks are a separate setup step. Local-worker unit tests use
stand-in HTTP services and do not prove that your chosen Ollama model works.
The Python implementation uses the standard library; no third-party Python
packages are required.

## Removing it

Ask your assistant to follow the [removal instructions](docs/SETUP-WITH-AN-AGENT.md#removing-the-setup).
Uninstall previews changes first and removes only registrations and instruction
blocks it can still identify as its own. Edited entries are preserved and
reported. Consultation history, provider logins, shared instructions and bridge
configuration are retained separately.

## Repository guide

| Path | Purpose |
| --- | --- |
| `AGENTS.md` / `CLAUDE.md` | Entry point for an assistant working with this repo. |
| `docs/SETUP-WITH-AN-AGENT.md` | Guided installation, verification and removal. |
| `docs/DATA-RETENTION.md` | Local storage, cleanup and provider-history boundaries. |
| `setup_bridge.py` | Portable launcher for onboarding and bridge commands. |
| `examples/onboarding-answers.json` | Example setup-answer schema, not preapproved choices. |
| `src/agent_bridge/` | Broker, MCP servers, onboarding and local worker. |
| `config/broker.json` | Machine-neutral defaults. |
| `tests/` | Offline tests, including onboarding and local-worker coverage. |
| `canaries/run_canaries.py` | Live checks required before activation. |
| `bin/` | Setup, MCP and administration wrappers. |

Bridge state defaults to `~/.agent-bridge`. Keep setup answers, live results
and consultation records in private locations outside the checkout.

For the engineering background, see the historical
[CLI measurements](docs/verified-cli-behaviour.md),
[review history](docs/REVIEW-HISTORY.md), and
[build-your-own brief](docs/BUILD-YOUR-OWN.md). Use the guided setup above to
install the current implementation.

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Please report
security issues through the private path in [SECURITY.md](SECURITY.md), not in a
public issue.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
