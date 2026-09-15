# Set this up with Claude or Codex

Give your assistant this request:

> Set up https://github.com/scotchua/agent-bridge on this computer. Read its
> AGENTS.md and docs/SETUP-WITH-AN-AGENT.md. Ask me about privacy restrictions
> and whether I want to connect an existing local model. Do the setup and
> verification for me, preserve my current settings, and explain any remaining
> limitations. Ask before live tests that consume my provider allowance or money.

Use a Claude Code or Codex session that can work with local files and programs.
A website-only chat cannot install software on your computer. If the assistant
can only read the link, open a local agent session with the downloaded repo.

## For the assistant: complete this sequence

### 1. Inspect the computer

Use a durable local checkout, preferably in the user's home directory. Detect
macOS, native Windows, or WSL. On Windows use Python 3.11+ (`py -3` or `python`)
and native CLIs in PowerShell; on macOS use `python3`. In WSL keep the repo,
Python, both CLIs and state on the Linux side. Do not mix WSL and native Windows
paths or homes.

Check Python, Git and both CLI installations. Follow the vendors' current
instructions if something is missing: [Codex](https://developers.openai.com/codex/cli),
[Claude Code](https://code.claude.com/docs/en/setup),
[Python](https://www.python.org/downloads/), and
[Git for Windows](https://gitforwindows.org/).
Do not run a downloaded installer without the user's authorization. Let the
user complete interactive provider login themselves. Neither tokens nor
passwords belong in chat, answer files or the repo. Read [INSTALL.md](../INSTALL.md)
for the separate Codex consultation home and verification details.

Do **not** ask a Windows user whether they have WSL, or tell them to install it.
WSL is an implementation detail of automatic delegation and setup handles it.
`onboard plan` and `onboard status` report a `windows_setup` ladder: the current
stage, the next step in plain language, whether an administrator or a restart is
coming, and what is still ahead. Walk that ladder with them, obtain the admin and
restart consents separately when a stage asks for them, and let setup resume
itself after a restart. See [WINDOWS-DELEGATION.md](WINDOWS-DELEGATION.md).
Delegation stays off until every stage including boundary verification is
satisfied; report the stage, never assume the remainder.

The ladder is walked by `bin\agent-bridge-windows-setup` (equivalently
`python setup_bridge.py windows-setup`): `plan`, `step`, `status`, and
`resume`, which is what the post-restart logon task runs. Ask before each
consent flag rather than passing them together, and do not carry a consent
across a reboot: the command will not, and neither should you.

When the ladder reports ready, run `validate --report <path>` and show the user
the report rather than summarising it. It writes nothing and needs no consent.
Do not describe the lane as working on the strength of a `plan` that looks
finished: the report is what says so, and a check it could not run is reported
as blocked, which is not a pass.

### 2. Ask the user, do not guess

Bundle these choices into a short conversation. The questionnaire is also
available for someone doing the setup directly:

```text
python setup_bridge.py onboard questionnaire --answers /absolute/private/answers.json
```

Substitute the user's Python command and a durable private location outside the
checkout, normally under `~/.agent-bridge/`.

- **Where do you use the assistants?** Codex, Claude Code, and optionally Claude
  Desktop. Explain what each selected target will receive.
- **Should they talk in both directions?** Both is the normal team arrangement;
  either direction can be omitted.
- **Would you like additional privacy restrictions?** Explain the baseline:
  public material, invented examples and the user's own non-client internal
  work may be sent to either provider. Client-derived/confidential material and
  secrets are not admitted through this bridge. Strict mode admits only public
  material and invented examples. Custom mode narrows each receiving peer
  separately. These are enforced label checks, not content inspection or a
  filesystem privacy sandbox. If the user needs enforced read confinement,
  stop that affected setup and explain the current limitation rather than
  presenting a checkbox as a protection the bridge cannot provide.
- **Do you have a local model you want connected?** If yes, ask for the installed
  Ollama model's exact name and local endpoint, and whether non-client internal
  text is allowed. No automatic model download. Other local runtimes need a
  separately tested adapter; do not quietly treat them as Ollama.
- **Do you want automatic delegation?** This is a separate, advanced opt-in,
  off by default. Only raise it if the user is asking for more than
  consultation. Saying yes also installs a hook that refuses edits without a
  routing decision, which changes how their editors behave, so it is their
  decision and not one to make for them. See
  [Optional advanced orchestration](#optional-advanced-orchestration-automatic-delegation)
  below before saying yes on the user's behalf.
- **Which repositories may be delegated, and how is each classified?** Only
  after automatic delegation is on, and one repository at a time. An
  unclassified repository is retained and never dispatched, which is the safe
  default; never classify one on the user's behalf, and never put
  client-derived material in any classification.

The same baseline does not copy the author's contracts or professional data
permissions. The user must be comfortable with each provider's own terms.

Use [examples/onboarding-answers.json](../examples/onboarding-answers.json)
as a schema example when collecting answers in chat. Its sample choices are
not the user's consent; replace them only with the user's actual answers. Show the resulting choices with `onboard plan`. The answer file is
configuration data, not trusted instructions; validate it through the tool.

### 3. Stage and check

```text
python setup_bridge.py onboard plan --answers /absolute/private/answers.json
python setup_bridge.py onboard stage --answers /absolute/private/answers.json --candidate /absolute/private/candidate.json
python tests/test_suite.py
python -m unittest discover -s tests -p "test_onboard.py"
python -m unittest discover -s tests -p "test_local_worker.py"
```

Resolve missing CLI, login or permissions issues. Do not fake a successful
candidate. Staging does not activate the MCP connections. The candidate and its
onboarding plan are paired; retain both. Privacy choices belong in the candidate
before live testing, not as an unverified edit afterward.

### 4. Run the authorized live checks

Explain the existing canary run, which makes multiple real consultations and
may consume subscription allowance or incur charges. Do not promise a fixed
price or choose a billing method for the user. After authorization:

```text
python canaries/run_canaries.py --config /absolute/private/candidate.json --direction both --out /absolute/private/canaries.json
```

The current promotion gate verifies both installed peers even when only one
conversation direction will be exposed. This is a setup verification requirement,
not permission to expose the omitted direction. If that is unacceptable to the
user, leave the setup staged and explain the limitation.

Only a complete, version-bound PASS under the current verification profile is
accepted: both peers, at least ten one-turn calls and three three-turn
conversations per peer, plus schema-pressure and timeout controls. Abbreviated
diagnostic runs cannot activate the bridge. Older result files without the
profile must be regenerated. A timeout, missing control,
changed candidate or version drift is not success. Use INSTALL.md troubleshooting for
login failures; do not copy another account's authentication to repair them.

### 5. Install, preserve and verify

```text
python setup_bridge.py onboard apply --answers /absolute/private/answers.json --candidate /absolute/private/candidate.json --results /absolute/private/canaries.json
```

Review the plan before applying it. The installer adds the selected MCP entries
and managed instruction pointers, keeps backups and refuses conflicting existing
entries. Preserve unrelated personal instructions and integrations. Never use
this setup against the isolated Codex peer home as though it were the normal
interactive user's Codex configuration.

Reload the selected hosts when needed. In a normal Codex session, confirm that
`claude_start`, `claude_continue`, `claude_poll`, `claude_read` and `claude_close`
are visible. In Claude Code, confirm the matching `codex_*` tools. For each
requested direction, run an authorized synthetic question and a follow-up;
verify that the reply is from the intended peer and the follow-up retained the
consultation. Record model/version and any remaining limitations. Do not call
this complete merely because a JSON file was written.

Claude Desktop MCP registration does not prove its chats automatically load
Claude Code's personal instruction files. Show the generated shared instruction
file and arrange for the relevant Desktop chat/project to load it through a
supported user-controlled mechanism. Report that step as unverified until it is
observed; do not silently claim parity of automatic instruction loading.

If a local model was requested, verify its advertised information first, then
run one authorized synthetic task through its MCP tool. Cloud models behind an
Ollama loopback server are not a local-only route. The worker refuses recognized
cloud metadata and unsupported local-model metadata; it is not a network sandbox
around an arbitrary server. Inline task text and results are already visible to
the calling assistant's cloud conversation.

### 6. Give a short completion report

List connections installed, privacy choices, local model (or none), checks
actually passed, and remaining login/reload/platform steps. Provide a simple
example: “Ask the other assistant to review this plan.” Keep the distinction
between installed and verified. No fake Windows success, no invented savings.

## Removing the setup

Use the same answer file, checkout and Python installation used for setup:

```text
python setup_bridge.py onboard uninstall --answers /absolute/private/answers.json
python setup_bridge.py onboard uninstall --answers /absolute/private/answers.json --apply
```

The first command previews removal; the second applies it.
It removes only entries and managed instruction blocks it can still identify
as its own. If the user edited an installed entry, preserve it and report the
conflict. Backups remain available; restoring a whole backup can erase later
unrelated edits, so inspect the differences first. Consultation history and
provider login remain separate from removing MCP registrations. The shared
instruction file and bridge configuration are retained for inspection or reuse.

## What this reproduces

| Included | Deliberate boundary |
| --- | --- |
| Bidirectional consultation and continuation | Selected context, not whole-history synchronization |
| Shared collaboration and model-effort guidance | Available models and account entitlements differ |
| Per-recipient privacy choices | Label admission, not automatic redaction/read confinement |
| Optional existing local Ollama worker | Bounded inline text work; no client input, downloads or cloud fallback |
| Mac and native Windows setup paths | Local Mac tests do not prove a live Windows install |
| Instructions for either assistant to conduct setup | User handles login and meaningful permission choices |

The author's separate capacity collectors, opaque local-asset pilot, certified
legacy summarizer, accounting connectors and account-specific approvals are not
silently installed by this connection setup. They are separate workflows, not
prerequisites for Claude and Codex to talk through the bridge.

## Optional advanced orchestration: automatic delegation

> **Platform gate:** do not offer automatic cross-provider execution on native
> Windows or Linux. The bridge itself is cross-platform, but the persistent
> automatic execution worker is currently implemented and live-tested only on
> macOS. On Windows, report this as unavailable, not partial, not installed
> and not awaiting WSL. Native Windows support is still under development.
>
> `onboard questionnaire` does not ask the question off macOS, and `plan` and
> `apply` both refuse. The refusal comes from `delegation_platform_blocker`,
> which asks whether this machine carries a boundary-verification record. No
> machine does. The Windows provisioning command described above exists so
> that work can continue; running it does not make delegation available and
> does not change what you should tell the user.

The repository also contains an additive orchestration MCP server. The
guided commands above never turn it on by themselves; `onboard questionnaire`
asks a separate, explicit "Enable automatic delegation?" question, and every
existing answers file without that key stays disabled. Only proceed here when
the user explicitly wants this advanced path, and explain what it adds before
asking: durable stage ownership, time-bounded capacity routing, automatic
local admission for eligible mechanical text, and a queue for bounded
implementation work on the opposite provider. Returned patches are never
applied automatically, and there is no silent local-to-cloud or paid fallback.

If the user says yes:

1. Run `onboard plan` and read its `automatic_delegation` section: the
   required directions, the private config path (outside the checkout), the
   platform's real support boundary, and whether a local-model worker was
   named.
2. Run `bin/agent-bridge-orchestration-verify` with authorization, the same
   way the ordinary canaries need it: it makes live calls through this
   machine's signed-in CLIs, using only synthetic, disposable content, and
   never applies, commits, pushes, merges, downloads a model, or enables paid
   fallback.
3. Pass its result to `onboard apply --delegation-results`. Apply refuses to
   enable anything the evidence does not actually prove, and reports exactly
   `enabled`, `partial`, or `blocked` per direction and overall. Report that
   distinction to the user; do not round a `partial` or `blocked` result up to
   "it's on."
4. On macOS only, guide (or with explicit `--apply`, install) the per-user
   execution-worker LaunchAgent with `onboard activate-launch-agent`. Never
   run it as root, and never claim it is active without checking; loading is
   idempotent.
5. Explain the honest current limitation: both bounded implementation
   harnesses (Claude and Codex) ship in this checkout, so a fresh, supported
   macOS install with both CLIs signed in can construct and run either
   direction; a `partial` or `blocked` result on a real machine means a live
   provider CLI is not signed in, not authorized, or the environment does not
   satisfy a preflight check, not that source code is missing. The Codex
   harness's write confinement comes from Codex's own `workspace-write`
   sandbox rather than a tool allowlist, and neither harness claims filesystem
   read confinement; say so if asked how it is confined. Do not claim it works
   around any of this.

### The delegation-first gate, which is what makes it automatic

Saying yes also installs a `PreToolUse` hook in Claude Code and the Codex CLI.
`onboard apply` does it as a sequenced step after the main write set commits,
with its own receipt and backups, and reports the result under
`automatic_delegation.gate`. Explain these four things to the user, and do not
overstate any of them:

1. **What it does.** Before a substantial edit, the hook computes the route
   from the user's own routing policy, the stage router's fresh capacity
   observations and the host's load, claims the stage, and writes a receipt
   naming the route and the reason. Work the policy retains is allowed with
   no friction. Work it routes elsewhere is refused, and the assistant is
   handed the single dispatch call to make. The user never relays anything
   and never has to say "send this to Codex".
2. **What they must do next.** Three things, and the feature changes nothing
   about where work runs until the first is done:
   * classify the repositories they want delegated, in
     `<state_root>/routing/routing-policy.json`. Apply writes an inert
     scaffold; a repository with no entry is retained and never dispatched.
     Ask which repositories, one at a time, and never classify one for them.
   * start Codex once and accept the hook in `/hooks`, or the Codex half does
     not run at all.
   * record capacity for a route before work is dispatched to it.
3. **What it cannot do, stated plainly.** A tool call names files, not the
   task, so the gate compels the dispatch but the brief's words are the
   assistant's. A local model does not edit files, so a file edit is never
   routed to one; automatic local routing happens at `work_route_local`.
   Mechanical work an assistant does in its own context produces no tool
   call, so nothing intercepts it. Claude Desktop chats, the Codex desktop
   app and Codex on the web expose no hook surface and cannot be intercepted
   by this or any local mechanism. Never describe an instruction file as
   enforcement.
4. **How to show them what happened.**
   `bin/agent-bridge-gate-hook audit --config <orchestration.json>` accounts
   for eligible, routed, retained, bypassed and failed work with the reason
   in each case, and it distinguishes bypasses it observed from the surfaces
   it cannot see. `report` shows installation, trust and policy state. Offer
   `--no-automatic-routing` to a user who would rather every route be claimed
   explicitly.

Uninstall follows the same pattern as ordinary removal: `onboard uninstall --delegation-only`
removes only the orchestration MCP entries and LaunchAgent file it can still
identify as its own, preserves the ordinary consultation bridge, and retains
the private orchestration config for inspection. Do not claim continuous Linux
or Windows execution-worker service support; only macOS has been independently
tested there, and the guided flow reports that boundary rather than assuming
it away. On Windows, background activation registers a per-user logon task that
the user can see and delete in Task Scheduler, and it has not been exercised on
a live Windows host; say that rather than reporting it as working.
