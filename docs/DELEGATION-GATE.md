# Delegation-first gate

The gate makes the routing decision come first. Neither Claude Code nor the
Codex CLI can edit a repository through its editing tools until a durable
routing receipt names the route that owns the work. A client whose route
does not own it is told to dispatch instead of editing. This is enforced by
the host's own hook mechanism: the tool call is refused by Claude Code or
Codex when the hook says deny, not by a sentence in an instruction file.

**The decision is made automatically.** By default the hook computes the
route itself when a repository has no valid receipt: it reads the operator's
routing policy, asks the stage router what capacity is fresh, reads this
host's load, claims the stage the route implies, and writes the receipt. No
user sentence and no assistant tool call is involved. Work the policy
retains is then allowed with no friction; work it routes elsewhere is
refused here and owed to the route the receipt names.

That is the difference from the first version of this gate, which refused an
un-decided call and printed instructions telling the assistant to make three
MCP calls. The refusal was enforcement, but the decision was optional and
the assistant named its own `allowed_routes` when it made one. An assistant
that wanted to keep the work asked for its own route and got it.

## What it covers, and what it cannot

Covered, with the hook installed and trusted:

* Claude Code editing tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`)
  and `Bash` commands that look like they write.
* Codex CLI `apply_patch` and its shell tool, which Codex names `Bash` in
  hook input (the internal names `local_shell`, `shell`, `shell_command`,
  `exec_command` are matched too), when the command looks like it writes.
  A writing shell command that names a path in another repository
  (`git -C /other commit`, `cd /other && ...`, `cp x /other/y`) needs that
  repository's receipt as well as the working directory's.
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
  package installs, inline interpreters, here-documents, archive creation
  and extraction, `find -delete`/`-exec rm`, formatters that are not run in
  their report-only mode) catches the ordinary ways a shell
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

Every decision on a gated call is logged to
`<state_root>/routing/gate-events.jsonl`: each deny, each allow by receipt,
each allow outside a repository, and an internal failure once the state root
is known. Calls the gate classifies as reads or as non-editing tools are not
logged, so a shell write the heuristic misses leaves no trace here; the log
shows what the gate judged, not what it did not see.

## Automatic routing

### What decides

`orchestration/autoroute.py` is a pure function of what the host can observe
and what the operator configured. Four things decide a route, in this order,
and the order is the point:

1. **Privacy and eligibility.** A repository the operator has not classified
   is retained, always. `client_derived` is refused outright. Capacity is
   checked last precisely so it can never override this.
2. **Task type.** Implementation work goes to a provider or stays. It is
   never sent to a local model, for the reason under "What it cannot do".
3. **Hardware load.** One-minute load average per core, against a ceiling
   (`max_local_load_ratio`, default 0.75). A host that exposes no load
   average defers rather than assuming the machine is idle.
4. **Capacity.** A peer route needs a fresh, available observation in the
   stage router. A stale observation never makes a route eligible, so a
   machine nobody has reported capacity for retains everything.

Anything that survives all four is dispatched. Anything that does not is
retained by the assistant that asked, which is itself a decision with a
reason, recorded like any other. `ROUTES` is `("claude", "codex", "local")`:
a paid API route is not absent by configuration, it is absent from the
vocabulary, and `parse_policy` refuses a policy that names one.

### The operator's policy

`<state_root>/routing/routing-policy.json`, owner-only, protected from every
covered tool by the same rule that protects the receipts:

```json
{"version": 1,
 "prefer": ["codex", "claude", "local"],
 "max_local_load_ratio": 0.75,
 "repos": {
   "/abs/path/to/repo": {"classification": "internal_nonclient",
                         "allowed_routes": ["claude", "codex"],
                         "mechanical_ok": false}
 }}
```

**A repository with no entry is retained and never dispatched.** That is the
default, and it is deliberate: installing this feature must not begin sending
repositories nobody has classified to a provider. `onboard apply` writes an
inert scaffold with no repositories in it and never rewrites one that exists.

An unreadable or malformed policy is a deny (`gate_auto_decision_failed`
naming `policy_unreadable`), never a permissive default.

### What the receipt records

Beyond the fields the manual path writes, an automatic receipt carries
`automatic: true`, the decision `code`, and the `considered` block: the
classification, the allowed routes, the fresh routes, the load figure and the
task type the decision actually saw. So a decision can be re-derived rather
than taken on trust, and the audit can tell an automatic decision from one an
assistant asked for. The `considered` block holds no task content.

### Capacity evidence

When the decision retains the work, the hook records one capacity observation
for **its own route only**, sourced `gate-hook:client-present` and fresh for
15 minutes. A client that just made a tool call is demonstrably running, so
that is an observation rather than an assumption. The hook never records one
for the peer or the local model: those still need a real observation from an
authorized source, and without one the policy retains the work.

### Decisions are re-made when they are overtaken

An automatic receipt is replaced by a fresh decision, rather than refused,
when it was decided for a different kind of work, when it has expired, or
when the stage it points at is finished, reassigned or its lease has lapsed.
The third case is ordinary: it is what happens after a normal
`stage_complete`. Before that was handled, the first completed stage in a
repository left every later edit denied with `stage_not_owned` and an
instruction to claim a stage by hand, which is the opposite of automatic.
Stages therefore carry a generation (`implementation`, `implementation#2`),
so a completed decision is history rather than a wall.

An unreadable stage router is **not** treated as overtaken. That stays a deny
(`stage_db_unavailable`): fail closed, and never decide without the router.

### What it cannot do

* **It cannot write the brief.** A PreToolUse payload names a tool and some
  paths. It does not contain the task. So when the decision routes work to a
  peer, the gate writes a durable dispatch intent carrying the route, the
  repository and the full stage binding, and refuses the edit. The assistant
  then makes exactly one call, `execution_dispatch`, with those identifiers
  and its own brief. No `stage_register`, no `stage_claim`. The user is never
  the messenger and the assistant cannot edit instead, but the brief's words
  are the assistant's.
* **It cannot route a file edit to a local model.** The local worker
  processes bounded inline text and returns a draft; it does not read files,
  run commands or edit anything. An intent telling it to edit a file would be
  unmeetable. Automatic local routing therefore happens at the local worker's
  own entry point, `work_route_local`, whose intake classifies and submits
  without anyone asking for it.
* **It cannot see mechanical work an assistant just does in its own
  context.** That produces no tool call, so no local mechanism intercepts it.
  This is a limit of the hook surface. An instruction file does not fix it
  and we do not describe one as if it did.

### Turning it off

`--no-automatic-routing` on the hook command restores the earlier posture: a
repository with no receipt is refused outright and an assistant must claim a
route explicitly. Stricter, and more friction. The audit reports which
posture each installed client is running under.

## The audit

```bash
./bin/agent-bridge-gate-hook audit --config ~/.agent-bridge/orchestration/orchestration.json
```

Accounts for a window (default 24 hours, `--since-hours 0` for everything on
record). `--json` for the full record. It reports:

* **eligible**: decisions whose policy permitted a route other than the
  assistant that asked.
* **routed**: count, by route, by code, and every reason.
* **retained**: the same, including why each one stayed.
* **automatic share**: how many decisions the policy made against how many an
  assistant requested through `routing_decide`.
* **bypasses**, split in two on purpose:
  * *observed*: work routed away and never dispatched (an intent still
    `awaiting_brief`); clients the hook is not installed for, where every
    call is un-gated and leaves no record at all; and writes aimed at the
    gate's own state.
  * *not countable*: the surfaces no local mechanism can see, enumerated.
    A zero in the observed column means none this mechanism can see, not
    none, and the report says so in its own output.
* **failures**: gate failures separately from ordinary denials, plus every
  failed or blocked job in both queues **with its diagnostic detail**, not
  just an exception class name.

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
