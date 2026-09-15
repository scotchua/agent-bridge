# Delegation-first gate

The gate makes the routing decision come first. Neither Claude Code nor the
Codex CLI can edit a repository through its editing tools until an
orchestration stage for that repository has been registered, claimed, and
recorded as a durable routing receipt naming the route that owns it. A
client whose route does not own the stage is told to dispatch instead of
editing. This is enforced by the host's own hook mechanism: the tool call is
refused by Claude Code or Codex when the hook says deny, not by a sentence
in an instruction file.

## What it covers, and what it cannot

Covered, with the hook installed and trusted:

* Claude Code editing tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`)
  and `Bash` commands that look like they write.
* Codex CLI `apply_patch` and shell tools (`local_shell`, `shell`,
  `shell_command`, `exec_command`) whose command looks like it writes.

Not covered, stated plainly:

* A Claude Desktop chat, the Codex desktop app, and Codex on the web expose
  no tool hook. They cannot be intercepted by this or any local mechanism.
  The strongest enforceable entry paths are the two CLIs, so that is where
  the gate lives.
* A person editing files in a terminal or editor.
* Claude Code started with `--safe-mode` or with `disableAllHooks`; Codex
  started with `--dangerously-bypass-hook-trust`.
* A Codex hook that has not been trusted once in Codex's `/hooks` view
  shows "New hook - review required" and does not run. The `report`
  subcommand says whether trust has been recorded.
* Shell commands that write in a way the text heuristic does not recognise.
  The heuristic (redirection, `tee`, `sed -i`, `rm`/`mv`/`cp`/`touch`/...,
  writing `git` subcommands, package installs, inline interpreters,
  here-documents, formatters) catches the ordinary ways a shell writes and
  can be evaded by an agent that means to. The editing tools are the
  deterministic part.
* Windows installation of the hook entries is written (`.cmd` launcher) but
  has not been run live; do not claim it works there until it has.

Everything the gate does is logged to `<state_root>/routing/gate-events.jsonl`
so an evasion or an unexpected allow is visible after the fact.

## How a stage becomes editable

1. `stage_register` and `stage_claim` through the agent-orchestration MCP,
   as today. The claim returns `owner_id`, `owner_route`, `revision`, and
   `lease_until`.
2. `routing_decide` with `item_id`, `stage`, `owner_id`, `stage_revision`,
   the repository path, and a short reason. The server re-checks the binding
   exactly as `execution_dispatch` does (stage owned, by this owner, at this
   revision) and writes `<state_root>/routing/<sha256(repo)[:32]>.json`:

   ```json
   {"version": 1, "repo": "/abs/repo", "item_id": "...", "stage": "...",
    "owner_id": "...", "owner_route": "claude", "stage_revision": 3,
    "decision": "self", "reason": "...", "caller": "claude",
    "decided_at": 1757900000.0, "valid_until": 1757914400.0}
   ```

   `valid_until` is the earlier of now plus `ttl_seconds` (default four
   hours, at most eight) and the stage lease. `decision` is `self` when the
   caller's route owns the stage, `peer` when the other provider does, and
   `local` for the local model. Every receipt is also appended to
   `<state_root>/routing/audit.jsonl`.
3. Edits in that repository from the owning route are allowed
   (`routing_receipt_valid`). Edits from the other route are denied
   (`routed_elsewhere`) with the instruction to use `execution_dispatch`.
   An expired receipt denies (`routing_receipt_expired`) and says to
   `stage_renew` then `routing_decide` again. No receipt denies
   (`no_routing_receipt`) and names the three calls to make.

Targets outside any git repository are not gated (`outside_repository`). A
tool call touching files in two repositories needs a receipt for each. If
the gate's own state cannot be read, the gate denies
(`gate_state_unavailable`): fail closed, never open.

## Install

From a checkout, with the private orchestration configuration you already
use for the MCP server:

```bash
./bin/agent-bridge-gate-hook install --root . --config ~/.agent-bridge/orchestration/orchestration.json
```

That prints the plan. Add `--apply` to write it. The installer:

* appends one `PreToolUse` entry to `~/.claude/settings.json` and one to
  `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`), preserving every
  other entry;
* sets `hooks = true` under `[features]` in Codex's `config.toml` inside a
  managed block, unless it is already true. A file that sets it to `false`
  is refused: turning hooks on is the operator's decision;
* records what it wrote in `~/.agent-bridge/onboarding/gate-installation.json`
  and refuses to overwrite an entry that has been edited since.

Then, once, start Codex and accept the new hook in `/hooks`. Until then
Codex shows "New hook - review required" and the gate does not run there.
`--clients claude` or `--clients codex` installs one side only. `--remove`
with `--apply` takes out only our entries and the managed block.

## Report

```bash
./bin/agent-bridge-gate-hook report --config ~/.agent-bridge/orchestration/orchestration.json
```

prints, as JSON: which clients have the hook entry, Codex trust state
(`trusted`, `needs_review`, `not_installed`), every receipt with `valid` or
`expired`, the most recent gate events with their decision codes, the count
of denials in that window, and the not-covered list above. The report is a
record of what the gate saw; it does not observe the surfaces it cannot hook.

## Hook wire

The hook reads the host's PreToolUse JSON on stdin (`tool_name`,
`tool_input`, `cwd`) and prints one JSON object: `{}` for allow, or

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny", "permissionDecisionReason": "..."}}
```

for deny. It always exits 0 so the host reads the decision rather than
treating a crash as a blocking error of unknown cause; an internal failure
is reported as a deny with code `gate_error` and no traceback. Its state root
comes from `--config` (the orchestration configuration's `state_root`) or
`--state-root`.
