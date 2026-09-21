# Orchestration and local-worker MCP

This is an additive server. It does not replace or widen `agent-bridge-mcp`.
The existing bridge remains consultation-only and exposes only the opposite
peer's consultation tools.

**Guided path.** `setup_bridge.py onboard` can now do everything below for you
as an explicit opt-in ("automatic delegation"): generate the private config,
register the caller-bound server in both app configurations, stage or install
the macOS LaunchAgent, and gate all of it on the synthetic verification
described here. See [INSTALL.md](../INSTALL.md#automatic-delegation-optional-opt-in).
The rest of this document is the manual/reference path and still applies
verbatim if you prefer to wire it up by hand, or need to troubleshoot what the
guided path generated.

The standalone `agent-bridge-orchestration` server gives either caller the
same durable work controls:

- route eligible mechanical, non-client text to the local worker;
- read local status and completed drafts, then record usefulness feedback;
- register, claim, renew, complete and inspect capacity-routed stages; and
- dispatch bounded implementation to the opposite provider and read its
  unapplied result; and
- read a content-free aggregate status report.

The server never silently falls back from local processing to a cloud model.
Client-derived and confidential material remains outside this inline route.
Caller identity comes from `--caller`; it is not accepted from tool input.
Expired stage leases require reconciliation outside this MCP surface, so a
caller cannot seize a stage merely because time passed.

## Configuration preview

Copy `config/orchestration.example.json` to a private location and replace all
placeholder paths with absolute paths. This repository does not install that
file or edit Claude Desktop or Codex configuration automatically.

The two app entries use the same private configuration and differ only in
caller identity:

```text
bin/agent-bridge-orchestration --caller claude --config /absolute/private/orchestration.json
bin/agent-bridge-orchestration --caller codex --config /absolute/private/orchestration.json
```

**There is no tool for declaring capacity.** There was one, `capacity_observe`,
and it was the wrong shape: the assistant chose the route, the availability,
the source string and the freshness window, so "a fresh observation from an
authorized source" meant whatever the model typed, for as long as it liked.

Capacity has exactly two writers now, neither of them on the wire:

 • the gate hook records that the client calling it is running. First-hand,
   its own route only, fifteen minutes.
 • `declared_available` in `routing-policy.json` lists the routes installed
   on this machine. That is a standing operator declaration, not a health
   check, and it is recorded as one: the ledger shows the source, and
   removing a route from the list withdraws it on the next decision.

Missing, stale, untrusted or unavailable capacity never grants a stage claim,
and no observation may claim a window longer than the routing lease. Capacity
remains advisory: it does not expand task authority, permitted data routes, or
review rules.

The local Ollama executor runs in the server's background thread while either
app is connected. A local result is an untrusted draft until its supervising
assistant checks it and records `work_feedback`.

### Checkpoints and production provenance

Every `purpose=work` intake requires an append-only checkpoint bound to caller,
task type, classification, UTF-8 byte count, nonblank-line count and risk
flags. `work_route_local` consumes a supplied `work_checkpoint` id or creates
one automatically for the exact unit it already received. Its assistant-facing
schema does not expose `purpose` or caller identity, so a caller cannot relabel
production as a calibration test or impersonate the other assistant.

`work_checkpoint_no_eligible_unit` records the honest case where a task has no
plausible mechanical unit. `work_checkpoint_audit` returns only aggregate
counts: total, eligible, dispatched, no-unit and refusals by a closed reason
set. It exposes no document text, task id or content hash. This is auditable
instruction/tool enforcement for desktop chats; only supported CLI hook
surfaces can be hard-gated before a read or edit.

### Certified Gemma local backend

The default remains `private_worker` for compatibility. An operator may select
`local_backend: "gemma_certified"` using the shape in
`config/orchestration.gemma.example.json` on a POSIX host. Native Windows
selection currently refuses because verified nested process-tree termination
is not yet available for this adapter. This route is intentionally narrow:

- only `summarize` is admitted;
- the model digest, delegate source and receipt-validator source are pinned;
- the installed delegate's `local-delegate/v2` receipt is validated against
  the exact invocation, input, output, options and parent queue job;
- timeout and cancellation terminate the adapter process group on POSIX; and
- refusal or failure has no Qwen, Apple or cloud fallback.

Configuration is server-owned. No request can select a backend or model. The
receipt directory must already exist as a private ordinary directory, and all
configured files must be absolute, existing, regular and non-symlink paths.
Run the synthetic calibration after changing the backend and before relying on
it for production.

Cross-provider implementation is different. The MCP processes only submit jobs
and read durable status. They never start Claude or Codex. This matters on macOS
because a desktop app's MCP sandbox may be unable to use the logged-in user's
Keychain even when the provider CLI is correctly signed in.

Run the execution consumer as the logged-in user, outside both desktop apps:

```text
bin/agent-bridge-execution-worker --config /absolute/private/orchestration.json
```

For an explicit foreground check, `--once` processes at most one queued job and
exits. It is safe to run on an empty queue. A queue-wide advisory lock prevents
a foreground check and a background worker from running together. On restart,
any job whose provider send may have started is marked `blocked` for manual
reconciliation and is never automatically sent again.

For continuous macOS operation, copy
`config/com.agent-bridge.execution-worker.plist.example`, replace every
placeholder with an absolute path, keep the resulting file private, and load it
as a per-user LaunchAgent. The template stores no credentials. Provider sign-in
continues to use the CLIs' existing Keychain-backed subscription sessions.
Set the template's Python path, account short name, Claude and Codex binary
directories (the same directory twice if both CLIs live together), private
configuration path and private log paths for the target Mac. A CLI's
directory missing from `PATH` here means that provider's harness cannot
discover its executable at run time (`codex_task.py` and `claude_task.py`
both fall back to a bare `shutil.which()` lookup when no pinned executable
is passed), and the job fails closed with `Codex executable unavailable` or
`Claude executable unavailable` rather than silently searching elsewhere.
Keep the filled-in plist out of the repository. Load it in the logged-in
user's GUI launchd domain, not as root or a system daemon.

Before relying on the lane, submit a synthetic job from each configured caller.
Confirm the terminal receipt identifies the opposite provider, source integrity
matches, disposable worktrees were removed, and all permission-to-land fields
remain false. A passing offline test alone does not prove subscription login or
Keychain visibility.

The macOS LaunchAgent is the only persistent execution-worker service currently
live-tested. The orchestration MCP has a Windows launcher, but continuous
Windows and Linux execution-worker service installation remains unverified.
The bundled resource sampler is also macOS-specific; without a separately
reviewed platform sampler, local jobs on Windows or Linux defer rather than
assuming the machine has safe spare capacity.

Both bounded implementation harnesses ship in `src/agent_bridge/execution/`:
`claude_task.py` and `codex_task.py`. `SubprocessHarnessExecutor` validates
both harness paths before either direction can be constructed, so a fresh,
supported macOS checkout with both provider CLIs signed in can exercise
Codex-to-Claude and Claude-to-Codex bounded execution without installing
anything outside this repository. `agent_bridge.orchestration.delegation.harness_availability()`
reports the bundled files present; it still cannot and does not report a live
pass, since that needs this machine's own signed-in CLIs.

Verification commands are checked once, at admission, against the policy
both harnesses enforce (`src/agent_bridge/execution/verify_policy.py`): a
fixed set of programs named without a path (`git`, `pytest`, `python`,
`python3`, `npm`, `pnpm`, `yarn`, `cargo`, `go`), Python limited to
`-m pytest` or `-m unittest`, git limited to `diff` and `status`, and no
NUL, carriage return or line feed in any argument (other characters are
passed through as the harness receives them). A command outside that policy is refused by
`execution_dispatch` as `verify_argv_rejected: <reason>` before a job exists;
the same command reaching a harness is refused with the same words. The
Claude lane requires at least one command (`claude_verification_required`);
the Codex lane permits none.

Receipts carry the reason for a failure, not only its class. A harness that
refuses a job prints `{"ok": false, "error": "<class>", "error_detail":
"<fixed text>"}`, and the queue copies both fields into `receipt.harness`
(printable characters only, at most 512) with `harness_status: failed` and
`harness_verdict: read_failure`. A failure before the harness runs (a brief
that changed after admission, an unavailable executable, a missing Claude
configuration directory) records `error` and `error_detail` at the top of the
receipt. A receipt that says only `"error": "TaskError"` is a defect, not a
diagnosis.

`routing_decide` turns a proven stage binding into a durable routing receipt
for one repository (`<state_root>/routing/<hash>.json`, mirrored to
`routing/audit.jsonl`). It requires the same binding proof as
`execution_dispatch`: the stage is owned, by the named `owner_id`, at the
named `stage_revision`. The receipt is what the delegation-first gate reads:
a PreToolUse hook in Claude Code and the Codex CLI that refuses editing tools
and writing shell commands in a repository with no fresh receipt, or one whose
`owner_route` is the other provider. Install, coverage, and the plainly stated
surfaces it cannot intercept are in [DELEGATION-GATE.md](DELEGATION-GATE.md).

**`routing_decide` is the manual path.** By default the hook does not wait to
be called: with no valid receipt it computes the route itself from the
operator's `routing-policy.json`, the routes with fresh capacity in this
database, and the host's load average per core, then registers, claims and
records the decision before the edit is judged. So a decision always exists
before implementation, it was computed rather than requested, and a receipt
written that way carries `automatic: true`, its decision `code`, and the
`considered` inputs so it can be re-derived. `routing_decide` remains
available for an assistant that wants to record a decision of its own, and
the audit counts the two separately.

When the automatic decision routes work away from the asking client, the hook
also writes a dispatch intent
(`<state_root>/routing/intents/<hash>.json`) carrying the route and the full
stage binding, and the deny message names the single call that is now owed.
`execution_dispatch` retires the intent when it accepts the job, which is
what keeps the audit's "routed but never dispatched" column meaningful.

The Codex harness's confinement is different from the Claude harness's, and
this is stated plainly rather than glossed over: Claude's lane trusts a tool
allowlist (`--tools Read,Grep,Glob,Edit,Write`, no shell) because Claude Code
has no native OS-level sandbox; the Codex lane instead runs `codex exec` under
Codex's own `-s workspace-write` sandbox, pinned with
`sandbox_workspace_write.network_access="false"`, and Codex keeps its own
shell tool inside that boundary. Neither lane claims filesystem-read
confinement; `-s workspace-write` restricts writes, not reads, exactly like
the consultation peer's documented limitation. The Codex harness additionally
walks every ancestor of its own disposable task-root directory (never the
worktree/repository itself) for `AGENTS.md`/`.rules`/`CLAUDE.md`/`.codexrules`
before every run, because `--ignore-user-config`/`--ignore-rules` do not stop
Codex from discovering `AGENTS.md` by walking upward from its working
directory. The isolated `CODEX_HOME` defaults to the same path
`config/broker.json` documents for consultation
(`~/.agent-bridge/codex-home`), so one `codex login` covers both lanes; the
harness never copies credentials into it and never falls back to the shared
desktop `~/.codex` home.

`bin/agent-bridge-orchestration-verify` runs the three synthetic checks this
document describes (Codex-to-Claude bounded execution, Claude-to-Codex bounded
execution, eligible work to the local model) against disposable, synthetic
content only, and writes a durable result the guided `onboard apply
--delegation-results` step validates before it will register anything. It
never applies, commits, pushes, merges, downloads a model, or enables paid
fallback, and it refuses to run against a result path inside a temporary
directory.
