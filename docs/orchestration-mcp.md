# Orchestration and local-worker MCP

This is an additive server. It does not replace or widen `agent-bridge-mcp`.
The existing bridge remains consultation-only and exposes only the opposite
peer's consultation tools.

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
Set the template's Python path, account short name, Claude binary directory,
private configuration path and private log paths for the target Mac. Keep the
filled-in plist out of the repository. Load it in the logged-in user's GUI
launchd domain, not as root or a system daemon.

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
