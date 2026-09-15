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

`capacity_observe` accepts only time-bounded observations. Missing, stale or
unavailable capacity never grants a stage claim. Capacity remains advisory:
it does not expand task authority, permitted data routes, or review rules.

The local Ollama executor runs in the server's background thread while either
app is connected. A local result is an untrusted draft until its supervising
assistant checks it and records `work_feedback`.

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
control characters. A command outside that policy is refused by
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
