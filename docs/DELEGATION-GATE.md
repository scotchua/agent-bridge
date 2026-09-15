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
* The gate's own state and the hook files. An editing tool, or a shell
  command the heuristic reads as a write, aimed under the state root, at
  the orchestration config, at the stage router's database (and its
  SQLite `-wal`, `-shm`, `-journal` sidecars), at `~/.claude/settings.json`,
  or at Codex's `hooks.json` or `config.toml` is denied
  (`gate_state_protected`) in any repository or none. A command that acts
  on a whole tree (`rm`, `mv`, `cp`, `rsync`, `tar`, and their kin) is also
  refused when it names a directory above a protected path. A client
  cannot write itself a receipt, replace the database the live check
  trusts, or unhook itself with a covered tool.
* Live ownership. On every allow the hook re-reads the stage router's
  database read-only and requires the receipt's stage to be owned, now, by
  the receipt's owner on the receipt's route with an unexpired lease
  (`stage_not_found`, `stage_not_owned`, `stage_reassigned`,
  `stage_lease_expired` deny; an unreadable database is
  `stage_db_unavailable`, a deny). A receipt is a pointer to ownership the
  router holds, not a token: a fabricated one must name a stage the router
  really assigned to this route, which is the delegation-first flow itself.

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
  writing `git` subcommands including a `git tag` that creates or deletes,
  package installs, inline interpreters, here-documents, formatters that
  are not run in their report-only mode) catches the ordinary ways a shell
  writes and can be evaded by an agent that means to. Reads, `--check` and
  `--diff` runs, `git tag --list` and `ruff check` pass as reads. The
  editing tools are the deterministic part. The protected-path rule for
  shell commands has the same limit: a write that reaches a protected file
  without naming its path is not seen.
* The `report` subcommand says whether Codex has recorded trust for our
  hook's position in `hooks.json` (`recorded`); it does not recompute the
  hash Codex checks, so `recorded` means trust was given at that position,
  not that it matches the current definition. Codex itself shows "review
  required" again when it does not.
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
   The audit line is appended before the receipt is written, so a receipt
   that exists is always accounted for. A receipt or lease with a
   non-finite time is refused on both sides.
3. Edits in that repository from the owning route are allowed
   (`routing_receipt_valid`) after the live ownership check above. A
   renewal does not invalidate the receipt; completing, releasing or
   reassigning the stage does. Edits from the other route are denied
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
(`recorded`, `needs_review`, `not_installed`), every receipt with `valid` or
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
treating a crash as a blocking error of unknown cause; an internal failure,
including unusable arguments, is reported as a deny with code `gate_error`
and no traceback. The reason text ends with the decision code in brackets,
`... [no_routing_receipt]`, so a denial can be read back to its rule. Its
state root and the stage router database come from `--config` (the
orchestration configuration's `state_root` and `capacity_db`); with
`--state-root` the database is `<state_root>/capacity.sqlite3`.
