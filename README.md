# agent-bridge

![tests](https://github.com/scotchua/agent-bridge/actions/workflows/tests.yml/badge.svg)

> **Experimental:** agent-bridge is ready for early testers, but its interfaces
> and setup may change as real-world feedback arrives. Review the privacy and
> platform limits below before using it. This early release is intended to find
> problems on fresh installations; it does not depend on a tester-count gate.

> **Windows support boundary:** the consultation bridge and guided setup work
> on native Windows. Automatic cross-provider execution currently does not:
> its persistent execution worker is macOS-only. Windows automatic delegation
> is under development and must not be presented as installed or verified.
> The provisioning command in this tree refuses to enable it, and that refusal
> is computed from the absence of a verification record on the machine, not
> from a platform name somebody has to remember to change later.

Connect Claude and Codex so you can say **“ask the other assistant”** without
copying messages between them. Either assistant can coordinate the work, ask
its teammate for help, and bring the answer back into your conversation.
An optional existing local Ollama model can handle routine text tasks.
An advanced, separately configured orchestration server can also assign durable
work stages and dispatch bounded implementation jobs between the two provider
CLIs without automatically applying their patches.

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

The consultation-bridge setup supports macOS and native Windows; Linux/WSL details are in
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
| Advanced orchestration | Durable stage ownership, capacity the operator declares or the hook observes first-hand, automatic admission for eligible mechanical work sent to a local model, and bounded cross-provider implementation jobs. |
| Delegation-first gate | The routing decision is made automatically before implementation, from your own policy, and no edit happens without a receipt naming the route. Includes an audit of what was eligible, routed, retained and bypassed. |
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

## Advanced orchestration

The repository includes an additive orchestration MCP server for users who want
more than consultation. It can:

- route eligible non-client mechanical text to the local worker without a cloud
  fallback;
- register and claim durable work stages so only one assistant owns a stage;
- select a route only from routes with fresh capacity, which the operator
  declares in the policy file or the gate hook observes first-hand; no tool
  lets an assistant declare a route available;
- queue a bounded implementation job for the other provider's subscription
  CLI;
- **compel a local digest of a large log or other mechanical artifact before
  a cloud model reads it whole**, in a repository the operator opts in and
  marks `mechanical_ok` -- off by default, and never for text an assistant
  never captured to a file; and
- **make the routing decision automatically, and refuse implementation
  without one.**

Cross-provider implementation runs in disposable Git worktrees and returns an
unapplied patch. It never grants permission to apply, commit, push or merge.
On macOS, a separate per-user execution worker runs outside the desktop-app MCP
sandboxes so the provider CLIs can use their existing subscription logins. A
crash after a possible provider send blocks the job for reconciliation instead
of silently sending it again.

**Guided, opt-in "automatic delegation."** The guided onboarding flow can now
turn this on for you if you explicitly ask for it; it stays off by default and
existing answer files keep working unchanged. Saying yes adds, on top of the
ordinary consultation bridge:

- a private orchestration configuration generated outside the checkout, with
  resolved absolute paths and owner-only permissions;
- the caller-bound orchestration MCP server registered alongside (not instead
  of) the consultation entries in Codex's and Claude's configurations; and
- on macOS, a guided or installed per-user execution-worker LaunchAgent,
  never as root, staged from a safe template with private logs and config.

Turning it on requires passing three synthetic, non-client verification
checks first (Codex-to-Claude bounded execution, Claude-to-Codex bounded
execution, and eligible work to a local model); apply refuses to enable it
without that evidence. The completion report says exactly
**"Automatic delegation: enabled"**, **"...: partial"**, or **"...: blocked"**
depending on what was actually proven, never more than that. See
[Orchestration and local-worker MCP](docs/orchestration-mcp.md) for the manual
reference path and current platform boundary.

Saying yes also installs the **delegation-first gate**, which is the part that
makes delegation automatic rather than available.

**What it is, named precisely: automatic routing in Claude Code and the Codex
CLI, with assistant-mediated dispatch.** Three qualifications carried in that
sentence, each of them load-bearing:

 • **Automatic routing.** The decision is computed and recorded before any
   substantial edit, and the edit is refused without it. That part is
   enforced by the host, not requested.
 • **In the two CLIs.** Claude Code and the Codex CLI expose a `PreToolUse`
   hook. Claude Desktop, the Codex desktop app and Codex on the web do not,
   so nothing local intercepts them. If those are where you work, this
   changes nothing for you.
 • **Assistant-mediated dispatch.** A tool call names files, not the task, so
   the gate can compel the dispatch and name the route, item, stage and
   owner, but the assistant writes the brief. We are not going to call a
   sentence in an instruction file enforcement.

The mechanism:

- Before any substantial edit, a `PreToolUse` hook computes the route from
  **your** routing policy, the routes with fresh capacity, and this host's
  load, claims the stage, and writes a durable receipt naming the route, the
  reason, and digests of the policy and capacity it decided under. Change
  either and the next call re-decides. No sentence from you and no tool call
  from the assistant is involved.
- Work your policy retains is then allowed with no friction. Work it routes
  elsewhere is refused, and the assistant is handed the one dispatch call to
  make. You never relay anything.
- `onboard apply` writes an **inert** policy: a repository you have not
  classified is retained and never dispatched. You choose what becomes
  eligible.
- `bin/agent-bridge-gate-hook audit` accounts for what was eligible, routed,
  retained, bypassed and failed, with the reason in each case.

**Two limits we state rather than dress up.** A tool call names files, not the
task, so the gate compels the dispatch but the brief's words are the
assistant's. And a Claude Desktop chat, the Codex desktop app and Codex on the
web expose no hook surface at all, so they cannot be intercepted by this or
any other local mechanism; the two CLIs are the strongest enforceable entry
path and that is where the gate lives. We do not describe an instruction file
as enforcement. Full list of what is and is not covered:
[docs/DELEGATION-GATE.md](docs/DELEGATION-GATE.md).

**Honest current limitation:** this checkout ships bounded implementation
harnesses for both directions (`src/agent_bridge/execution/claude_task.py`
and `codex_task.py`), so a fresh, supported macOS install with both CLIs
signed in can construct and run the execution lane in either direction
without installing anything beyond this repository. Passing verification
still needs this machine's own signed-in provider CLIs; it is not proven by
source code existing. Local routing and consultation are unaffected. The
execution worker's LaunchAgent has been live-tested on macOS only; continuous
service setup for Linux and Windows is not offered, only portable
registration and configuration.

## What it will not do

- **Synchronize all your chats or give either assistant the other's memory.**
  The coordinator sends selected context; a follow-up retains that consultation.
- **Turn a consulted peer into an unrestricted remote worker.** Peer calls are
  designed for consultation. The coordinating assistant can implement the
  suggestions using its own tools and your authorization.
- **Automatically grant equal permissions in every app.** Accounts, host tools
  and platform permissions remain separate. Setup reproduces the supported
  connection and shared guidance, not the author's entire environment.
- **Guarantee lower subscription usage or discover subscription allowances.**
  Consultations and live checks use capacity too. Advanced orchestration can use
  fresh observations supplied by an authorized source, but it does not scrape,
  infer or promise provider quotas.
- **Make agreement proof of correctness.** Peer replies are evidence to assess,
  not instructions to obey or an automatic approval to publish.

## Consultation budgets and outcomes

The peer integrations have no validated native wall-clock limit. The bridge
therefore terminates them externally: a POSIX process group receives SIGTERM
and then SIGKILL, and a Windows Job Object is terminated as a unit. On POSIX,
descendants that deliberately leave the process group remain outside this
guarantee. External termination also cannot prove that remote model work stopped.

`peers.<name>.timeout_seconds` is one whole-request budget, measured from the
start or continue call. Admission, queueing, worker preparation, every attempt,
validation, termination grace and leader reaping consume the same deadline;
retries do not reset it. The defaults are 300 seconds for Claude and 420 for
Codex, with `grace_seconds` set to 5. Each external invocation receives only
the remaining time. Grace is reserved inside it, capped at half the remaining
time, with up to 0.1 seconds reserved for reaping. A nonpositive or already
exhausted budget launches no peer. OS scheduling, locks and durable filesystem
writes can add latency, including receipt publication after cleanup; this is
not a real-time guarantee.

The caller prompt budgets are 32,000 characters for `start` and 16,000 for
`continue`. The generated corrective prompt has a 12,000-character budget.
Configure these with `limits.prompt_start_max_chars`,
`limits.prompt_continue_max_chars` and `limits.prompt_corrective_max_chars`.
Each is clamped below `limits.prompt_max_chars`, the 100,000-character hard
cap. Oversize caller input is refused before preflight or dispatch; the worker
also checks the framed prompt against the hard cap before a consultation
launch. A corrective prompt that cannot fit is never launched.

A timeout returns `peer_timeout` and is never automatically retried. Shorten
or decompose the question before trying again, or route work needing repository
context to the separate code-task path. A transient failure can repeat the
same prompt once within the remaining deadline. Authentication failure remains
`peer_auth_failure` and is never retried automatically. Malformed or schema-invalid
output gets at most one corrective retry, in the exact same session; recovery
stops if no session exists or the corrective prompt cannot fit. Only validated
output received within the deadline can complete a consultation successfully,
including a shorter follow-up after failure.

Timeout, authentication and structured-output failures expose a `receipt` in
both poll and read, also saved as
`receipt.json` and in provenance and the ledger. Receipt version `1` is an
additive broker envelope field: the peer response contract remains version `2`.
The distinct receipt outcomes are `timeout`, `auth_failure` and
`structured_output_exhausted`; the underlying error category remains available.
Each includes the caller prompt and effective config SHA-256 hashes, attempt
count and prompt hashes, elapsed stages (including execution and termination),
elapsed and allowed seconds, and a suggested `next_action`: respectively
`shorten`, `route_to_code_task` or `decompose`. All three actions are listed in
`allowed_next_actions`; they are suggestions, not approval or an automatic route.
Authentication must be restored before another peer call after an auth failure.
`unresolved_questions` holds the entire caller prompt as one verbatim string,
including whitespace and embedded questions. Unvalidated peer text stays in
quarantine and is never copied into a receipt. A preflight timeout returns its
receipt directly with zero consultation attempts.

Health or status visibility proves neither a valid authenticated session nor
approval to act on a peer's answer. Admin status explicitly reports session
validation as `not_checked` and `approval_granted` as false.

### Local redaction handoffs

The broker's closed start and continue argument sets accept the optional pair
`preparation_dir` (an absolute local directory) and `clearance_sha256` (the
SHA-256 of the exact `clearance.json` bytes). Existing requests without either
field retain their behavior. The published MCP `tools/list` input schemas
advertise this optional pair: both fields must appear together and cannot be
used with `label`. No response-contract or config-version change is needed for
the additive arguments.

Keep preparations outside broker state, in a directory with an opaque name.
Stage `output.txt`, `candidates.txt`, `receipt.json`, `certificate.json` and
`prompt.txt`, with their exact byte hashes in the `artifacts` object of
`preparation.json`. The manifest uses `redaction-handoff/v1`, retains the source
classification and binds the receipt's invocation, input, canonical input,
effective options, parent and attempt identities in `receipt_bindings`.
The receipt must be a complete `local-delegate/v2` redaction success, including
successful schema-constrained passes one and two. The certificate must bind an
eligible `local-delegate-certification/v2` route to the same model digest,
prompt, validator and effective options. Validation is copied locally; no
plugin checkout is imported at runtime.

For peer use, `prompt.txt` must contain the **fully assembled peer envelope**,
using `envelope.build_initial` or `envelope.build_continuation` with the current
response contract. A preparation originally assembled for `codex_task` must
be assembled and reviewed again for the peer. Human review remains mandatory
for the output, candidate review, preparation and exact assembled prompt.
After review, record a new `clearance.json` with exactly these fields:

```json
{
  "contract": "redaction-peer-handoff/v1",
  "human_reviewed": true,
  "preparation_sha256": "<SHA-256 of preparation.json bytes>",
  "output_sha256": "<SHA-256 of output.txt bytes>",
  "candidate_review_sha256": "<SHA-256 of candidates.txt bytes>",
  "prompt_sha256": "<SHA-256 of prompt.txt bytes>",
  "assembly": {
    "peer": "claude",
    "contract_version": "2",
    "contract_schema_sha256": "<SHA-256 of the configured schema file bytes>",
    "mode": "initial",
    "conversation_id": "<fresh canonical UUID chosen before review>",
    "peer_session_id": "<same UUID for initial Claude; null for initial Codex>"
  }
}
```

For an initial handoff, the broker creates the reviewed conversation UUID and
refuses reuse of an existing conversation. For a continuation, use mode
`continuation`, the existing conversation ID and its exact peer session ID,
with a separate preparation and clearance. Codex assigns its first session ID
after invocation, so its initial clearance binds the fresh broker conversation
and a null prior session. A `codex_task` clearance never authorizes a peer call.
Hashes detect changed bytes; hashes do not authenticate a person, prove that a
human reviewed them, or prove that redaction is semantically complete.

The request payload and source classification must exactly equal the
preparation. Client-derived material remains refused in both directions,
including with complete receipt and clearance; each peer's configured
allowance and the global refused list also apply. There is no bypass flag.
Handoff requests cannot include a free-form label. Only the redacted payload,
local preparation reference and hash bindings enter the request; raw input,
candidate text, identifier lists and entity maps are not copied into requests,
failure receipts or telemetry.

The worker re-reads the artifacts and pinned clearance after assembly and
immediately before every send, checking the actual UTF-8 prompt against the
cleared bytes. Both backend pipe writers preserve those UTF-8 bytes. A changed
payload, envelope, contract, candidate review, receipt, certificate, clearance
or session produces a deterministic `input_schema_invalid` refusal before the
affected send; classification gates use `source_classification_refused`.
Identical transient retries recheck the bindings. A different corrective
prompt cannot inherit clearance: the first cleared attempt may already have
been sent, but no corrective retry is sent. Review a new preparation and submit
a newly cleared continuation to recover.

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
- **The execution lanes now run on Linux, and that is not the same as
  verified everywhere.** Both harnesses used to hard-code one macOS layout
  (git at the standalone Command Line Tools path, `/usr/bin/sandbox-exec` as
  the only verification confinement), so on any other host the first git call
  spawned a file that does not exist and the lane reported only
  `TaskError: command spawn failed`. Git is now resolved per host and the
  verification confinement is a selected backend that names what it cannot
  do. macOS `sandbox-exec` remains the **only independently verified**
  backend and the only one that carries `public` or `internal_nonclient`
  material. The Linux backend denies network access and confines no reads, so
  it carries **synthetic material only**; on Linux the write boundary is the
  disposable worktree plus the harness's own source-integrity snapshot, not
  the kernel. A host with neither backend refuses by name rather than running
  verification unconfined.
- **Advanced orchestration is not yet a portable service installer.** Its core
  queue and routing logic is tested offline, while the persistent execution
  worker and subscription-backed implementation lane are currently verified on
  macOS only. Guided onboarding can register and configure it on any supported
  platform; only the continuously-running macOS LaunchAgent is offered as an
  installed service.
- **The Codex execution harness's write confinement is Codex's own sandbox,
  not this project's.** `codex_task.py` runs `codex exec -s workspace-write`
  in a disposable worktree, pinning network access off; unlike the Claude
  harness (a restricted tool allowlist, no shell), Codex retains its own shell
  tool inside that sandbox. Confinement is exactly what Codex documents for
  `workspace-write`, no more, and read confinement is not claimed for either
  harness. A bundled harness proves the lane can be constructed and its
  offline contract tested; it is not a substitute for the live synthetic
  verification this machine's signed-in CLIs still have to pass.
- **The read gate's two clients are not equally strong, and it does not
  compel everything.** Claude's `Read` tool is matched by name;
  deterministic. Codex has no such tool, so its reads are recognised from
  shell command text; a heuristic, the same strength its write gate already
  is. Neither sees mechanical text an assistant never captures to a file,
  which is the largest mechanical stream there is, or a shell reader the
  heuristic does not recognise. `gate report`'s `read_gate` field states
  each client's strength rather than leaving it implied.

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
| `docs/orchestration-mcp.md` | Manual advanced orchestration, local routing and external execution-worker setup. |
| `docs/DELEGATION-GATE.md` | Host-enforced delegation-first gate, automatic routing, the audit report, and their stated limits. |
| `docs/audits/automatic-delegation-2026-09-15.md` | What the automatic component was live-tested against, and what remains unproven. |
| `setup_bridge.py` | Portable launcher for onboarding and bridge commands. |
| `examples/onboarding-answers.json` | Example setup-answer schema, not preapproved choices. |
| `src/agent_bridge/` | Broker, MCP servers, onboarding and local worker. |
| `src/agent_bridge/orchestration/` | Durable stage routing, automatic route selection, the gate, the audit, and the cross-provider execution queue. |
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


## Connectivity and login health

Run `bin/agent-bridge-admin health` for both pinned executables, version drift,
credential context and local login visibility (`--json` for a timestamped report).
It does not log in, change pins, read credential contents or send inference.
A local signed-in result does not test network connectivity or token refresh.
On macOS, repeat a negative restricted-shell check in normal Terminal with
Keychain access before replacing login. See the [health runbook](docs/bridge-health.md)
and [September 6 investigation](docs/audits/connectivity-investigation-2026-09-06.md).
