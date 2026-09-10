# Build your own Claude/Codex consultation bridge

A local MCP server that lets a Codex task ask Claude for an independent second
opinion, and a Claude Code session do the same with Codex. Consultation only:
it cannot edit files, run commands, or merge code.

## How to use this document

Everything below the line marked **PASTE FROM HERE** is a build brief. Paste it
into Claude Code or Codex, in an empty directory, and let it build. It is
written for an AI assistant to execute, not for a human to follow by hand.

Read the two sections above that line first. They will save you money.

Once it is built, [consulting-a-peer.md](consulting-a-peer.md) covers getting findings
out of it instead of agreement.

## Read this before you start

**The traps in this are version-specific.** Every measured fact below was
verified against `claude 2.1.229` and `codex-cli 0.147.0` on macOS with Python
3.14. If your versions differ, the facts may not hold, and the brief tells your
assistant to re-verify rather than trust them.

**Both CLIs need their own login, and this surprises people.** An interactive
Claude Code session being signed in does **not** mean the `claude` CLI can
authenticate a subprocess. Check before building:

```
claude auth status
```

If that says `"loggedIn": false`, run `claude auth login`. Same idea on the
Codex side, against whatever `CODEX_HOME` the bridge will use.

**What a fresh build gets you, and what it does not.** The reference
implementation this brief is derived from is about 4,000 lines with 2,500 lines
of tests, and it went through four adversarial review rounds that produced 40
findings, including two separate cases where two correct-looking fixes defeated
each other. A first build from this brief will be a working v1 with the known
traps pre-avoided. It will not be as hardened. If you can get a copy of a
working implementation and port it instead, do that: the concurrency section
below describes weeks of findings in a few paragraphs, and reading a paragraph
is not the same as having found the bug.

**Cost.** The build itself is assistant time. Verifying it live costs real model
calls: roughly 40 consultations at a few seconds to a minute each. Budget a few
dollars on the Claude side and whatever your Codex plan charges.

---

# PASTE FROM HERE

Build a local, two-way consultation bridge between Claude Code and Codex, as a
single stdio MCP server with a required `--caller codex|claude` argument.

Work in the current directory. Keep all runtime state outside the repository, in
`~/.agent-bridge`. Do not modify my MCP configuration, my `~/.codex/config.toml`,
or any credential file. At the end, print the installation commands and stop.

## What it does

- `--caller codex` exposes only tools for consulting Claude: `claude_start`,
  `claude_continue`, `claude_poll`, `claude_read`, `claude_close`.
- `--caller claude` exposes only the symmetric `codex_*` tools.
- Neither caller can consult its own model. The tools for that must not exist in
  its session, which is stronger than refusing at call time.

Version 1 is consultation only. It must not edit repositories, execute
peer-proposed shell commands, merge code, or delegate recursively.

Do not use `claude mcp serve` as the backend: that exposes Claude Code's own
tools to an MCP client, it does not ask the Claude model a question. Invoke
`claude -p` for Claude and `codex exec` for Codex.

## Architecture

A neutral local broker, not one model driving the other.

```
top-level agent -> its MCP client -> agent-bridge-mcp --caller X
                -> isolated peer CLI invocation -> validated response back
```

`start` and `continue` return a `job_id` promptly. A detached worker per
consultation writes status atomically, so the MCP process can restart
mid-consultation and a later `poll` still resolves. `poll` returns a closed
status enum. `read` returns the validated response plus provenance. `close`
blocks further turns without erasing the audit record.

Use fixed argument arrays only. Never a shell command string, and pass
`shell=False` explicitly at every call site so a later edit cannot flip it by
omission. Write a test that greps your own source for `shell=True`, `os.system`
and `os.popen`.

Owner-only state: directories `0700`, files `0600`, process umask `077`. Note
that `os.makedirs(mode=...)` applies the mode to the leaf only and is masked by
the caller's umask, so create each component explicitly and enforce the mode.

## Verify these before writing code, and fail closed if they changed

Run `claude --help`, `codex exec --help` and `codex exec resume --help` and
confirm each flag exists. Put executable paths, allowed versions, timeouts,
concurrency, retention and the Claude budget in one versioned config file.
Re-check the executable and its version on every job.

These were measured, and four of them contradict what you would reasonably
assume:

1. **`codex exec resume` supports neither `-s/--sandbox` nor `-C/--cd`.** Set
   the sandbox with `-c sandbox_mode="read-only"`, which resume does accept, and
   set the working root with the subprocess `cwd`.
2. **Codex's session identifier is `thread_id`, on the `thread.started` JSONL
   event.** There is no `session_id`. `codex exec resume <thread_id>` takes it.
   Never use `--last`.
3. **Claude's result envelope reports `subtype: "success"` even when the run
   failed.** `is_error` is the only trustworthy success signal. This is the most
   dangerous fact here: keying on `subtype` accepts failed runs as good ones.
4. **Prompts go on stdin.** `codex exec` reads stdin when given no prompt
   argument; passing both duplicates the question as a separate `<stdin>` block.
   `codex exec resume` needs an explicit `-` to read the prompt from stdin.

Also measured:

- `claude --session-id <uuid>` is honoured exactly, which is what makes
  continuation by explicit ID real. For continuation use `--resume <that uuid>`.
- Claude's structured output arrives in a dedicated `structured_output` key on
  the envelope. Read that first; fall back to parsing `result` as a JSON string.
- Codex's final message is best read from `-o/--output-last-message <file>`, a
  documented file channel. Do not guess which JSONL event carries terminal text.
- `--safe-mode` disables CLAUDE.md, skills, plugins, hooks, MCP servers,
  commands and agents while auth, model selection and permissions keep working.
  Do not use `--bare`: its auth becomes API-key-only, which breaks a
  subscription peer.
- A fresh `CODEX_HOME` directory must exist before the run or Codex errors.

## Peer isolation

Claude peer: `--safe-mode`, plus `--strict-mcp-config` with
`--mcp-config '{"mcpServers":{}}'`, plus `--tools ""`, plus
`--setting-sources ''`, plus `--max-budget-usd`. The empty strict MCP config is
what stops the peer loading this bridge and calling back into it.

Codex peer: `--ignore-user-config`, `--ignore-rules`, `--strict-config`,
`--skip-git-repo-check`, sandbox read-only, in a dedicated empty working
directory under a dedicated `CODEX_HOME`. `--ignore-user-config` blocks
`$CODEX_HOME/config.toml`, which is where `codex mcp add` writes and therefore
the one recursion vector that matters. Additionally refuse to run if that file
ever appears in the isolated home.

State the limits honestly in code comments, in the tool descriptions, and in the
prompt you send:

- The read-only sandbox restricts **writes, not reads**. It is not a filesystem
  read boundary. Do not claim it is one.
- The isolated Codex home still accumulates Codex's own system skills and a
  plugin cache. No feature flag existed at 0.147.0 to disable them. Record an
  inventory on every job rather than asserting they are inert.
- Measured and worth knowing: an ancestor `.codex/config.toml` is **not** read
  by `codex exec` at 0.147.0. Verify this yourself with a bogus key under
  `--strict-config`, with a positive control.
- Codex discovers `AGENTS.md` by walking **upward** from its working directory.
  Walk to the filesystem root checking for `AGENTS.md`, `CLAUDE.md`, `.rules`
  and `.codexrules`, and fail closed on any ancestor you cannot enumerate.

## The response contract, and the one thing that will waste your money

Use one versioned JSON Schema for both peers, enforce it with Claude's
`--json-schema` and Codex's `--output-schema`, and then validate it again in the
broker. The broker's copy is the source of truth; never trust the peer complied.

**Codex routes `--output-schema` through OpenAI structured outputs, which
requires every key in `properties` to also appear in `required`, at every object
level, plus `additionalProperties: false`.** Claude's `--json-schema` accepts
optional properties happily. So a schema with optional fields works perfectly in
one direction and fails 100% of calls in the other, with
`invalid_json_schema`. Make every field required and express absence as an empty
array or an empty string.

Write a static test that walks your shipped schema and asserts every object
lists all its properties as required. Do this before spending a single live
call: the reference implementation lost 13 live calls discovering a statically
checkable fact.

Shape:

```json
{
  "contract_version": "1",
  "status": "answer | needs_context | refusal",
  "summary": "concise answer",
  "analysis": ["substantive point"],
  "disagreements": ["disagreement with a position the caller actually stated"],
  "risks": [{"severity": "high|medium|low", "issue": "...", "mitigation": "..."}],
  "questions": ["information needed from the caller"],
  "confidence": "high | medium | low"
}
```

Add length and item caps. Cap the number of validation violations you collect
too: a bounded payload with a million tiny invalid array items will otherwise
turn a few megabytes of input into far more memory than the input itself.

## The prompt, and a trap that survives every test suite

The caller composes its own bounded question. Send nothing else: no transcript,
no working tree, no environment, no memories, no nearby files. Tell the peer it
has no repository content **supplied** to it and that filesystem inspection is
**prohibited**, framed as a rule it must follow rather than a capability it
lacks, because on the Codex side that is the truth.

Now the trap. Do not write anything like "a consultation that only agrees is
worthless." Combined with a required `disagreements` field, that pressures the
peer into filling it whether or not a disagreement exists. Observed live: asked
only to name a failure mode and a fix, both peers argued against exponential
backoff, which nobody had proposed. For an adversarial-review tool a fabricated
objection is worse than none, because the caller may act on it.

Instead: scope disagreement to a position the caller actually stated, require
naming that position, name the empty-list case explicitly, forbid arguing
against a position nobody took, and say plainly that agreeing is a legitimate
answer while agreeing because it is easier is not.

Then test both directions: a prompt that states no position should produce an
empty list, and a prompt that states a wrong position and asks for confirmation
should produce a real disagreement. Testing only the first would let
over-suppression look like success.

Return peer output as data, clearly labelled with the peer identity, and never
reinterpret it as instructions for the calling agent.

## Errors, retries and quarantine

Use a closed enum of error categories with constant hints. Do not build a
free-form "safe detail" field: it will eventually interpolate peer output.
Write a test that plants sentinel strings in a fake peer's stdout and stderr and
asserts they appear nowhere in any caller-visible response, while still being
retained on disk for a human.

Retry rules: never retry a policy refusal, invalid input, version mismatch,
authentication failure, or any deterministic gate failure. At most one retry for
a transient process failure. At most one **corrective** retry for a
schema-invalid response, and generate that corrective prompt entirely from a
closed error code plus schema metadata, never from quarantined peer text.
Record each attempt's prompt hash so "was an identical prompt retried" is a
checkable fact. Write a test that fails if it was.

When you report an exhausted retry, keep the substantive category and carry
exhaustion as a separate flag. Collapsing it to "retry exhausted" throws away
the actual reason and can make a hint claim something that did not happen.

Quarantine unusable output including the peer's own final-message channel, not
just stdout. On the Codex side stdout carries only JSONL events, so
quarantining stdout alone preserves everything except the offending text.

Classification order matters, and must be identical in both backends: timeout,
then output-cap breach, then auth, then nonzero exit, then session problems. A
cap breach is why you killed the peer, so the nonzero exit is a consequence, not
the cause. Getting this backwards makes a flooding peer look transient and gets
it retried.

## Process handling

Run each peer in its own process group and kill the whole group on timeout with
a grace period. State the limit honestly: a child that calls `setsid()` escapes
`killpg`, and this is not an OS-level containment mechanism.

Enforce output caps **while reading**, with a non-blocking selector loop rather
than `communicate()`, which buffers everything before any cap applies. Do not
use reader threads joined with a timeout: an expired join abandons a thread
holding a pipe, and across many jobs that accumulates.

If a process the peer spawned holds a pipe open after the peer exits, drain
briefly then stop. Do not wait for EOF, which lets an unrelated descendant
stretch a finished job into a reported timeout. But then treat the result as
**incomplete and fail closed**, because a truncated prefix can parse as one
valid JSON object when the whole stream would not have. Whether the stream ended
matters more than how long you waited.

## Concurrency, which is where the real difficulty is

This section compresses four review rounds. Take it seriously or expect to
rediscover it.

**Status writes.** Take a per-job lock, enforce an ordering (`queued` <
`running` < terminal), and make terminal documents immutable **including
same-status rewrites**. Allowing `failed` to overwrite `failed` lets a stale
reconciler replace a real peer failure with "worker died". Add a compare-and-set
on a sequence number for any caller that decided from an earlier read.

**Learning the worker's pid.** The broker cannot know it before spawning, so its
pid-recording write races the worker's own first write. Make pid recording a
separate operation that does not touch status.

**Conversation claims.** Do every admission check and the claim itself inside
one lock on the conversation record. Require exact ownership for every change on
release, not just for clearing the claim, and give the first turn a claim too so
there is no exception to reintroduce the hole. Have the claim carry its own
timestamp, so the window between claiming and writing the first status is
covered. A future timestamp must not be honoured: clamping it to "now" restarts
the grace window on every check, which is the unbounded lease it was meant to
prevent.

**Global admission.** Counting active jobs then launching is a check-then-act
across processes. Serialise it with a global lock held for the count and the
first status write, and never across the process spawn.

**The handshake that makes it correct.** After publishing its pid in the
`running` status, and before invoking the peer, the worker must verify under the
conversation lock that it still owns the slot. Order matters: once the pid is
visible a rival cannot steal the slot, and if a rival got there first the
handshake fails before any peer call.

**And verify the publication succeeded.** If the job was already reconciled to
terminal while it sat queued, that `running` write is a silent no-op, no pid is
published, the handshake still passes because the claim is intact, and a rival
then sees a terminal job and takes the slot. Two workers, one peer session.
Check the returned document says `running` with your own pid, and abandon
without any peer call if it does not. This is two correct fixes defeating each
other, and it is the single hardest thing here.

**Evidence spanning the whole attempt.** Write a durable marker **before** the
peer is spawned, enrich it with spawn identity after, transition it when the
peer exits, and retire it only after the worker has durably committed the
outcome and successfully released the claim. Gate retirement on the release
actually succeeding, not merely on running after it.

Marker **presence** blocks a conversation from being reassigned. Never use "how
many processes did I kill" as the predicate: a crashed attempt whose group
already exited leaves the remote call exactly as indeterminate as a live one.
Persist the hold **before** signalling anything, so a crash cannot destroy the
only evidence without recording the hold.

**Reaping.** Reap a dead predecessor's peer on the **admission** path, not only
when someone polls, because admission can displace a dead claim with no poll
ever happening. Validate spawn identity before signalling: a bare process-group
number can be recycled, so record the leader pid and its start time and check
both. If identity cannot be established, do **not** signal, keep the hold, and
tell the operator manual cleanup may be needed. An unreaped orphan is a bounded
nuisance; signalling an unrelated process group is not. Never signal your own
process group.

**Indeterminate conversations.** Killing a local process does not prove a remote
request was abandoned. If a worker died with a peer call in flight, hold the
conversation and require deliberate operator resolution rather than resuming it
as though the call failed. Give resolution a dry-run mode, and refuse to resolve
while an unverifiable process may still be alive unless the operator explicitly
acknowledges it. Surface held conversations in ordinary status output with a
loud warning, because a hold has no timeout by design and would otherwise sit
unnoticed.

## Provenance

Record per job: job and conversation id, caller and peer, source
classification, timestamps and duration, executable path and observed version,
requested and observed model, contract version and schema SHA-256, prompt and
response SHA-256, sanitized argv shape, exit code, terminal status, closed error
category, peer session id, and your implementation's Git commit **plus an
independent source hash and dirty-tree state**. Do not represent an uncommitted
file by the previous commit.

Append an immutable ledger line per exchange. Store payloads only where needed
for continuation, owner-only, with configurable retention and an explicit
cleanup command that is a dry run until `--apply`.

Any destructive cleanup must verify containment under the state root twice, and
must check for symlink components on the **unresolved** path. Resolving first
destroys the evidence: an in-state symlink then passes containment, resolves to a
legitimate path with no symlink components, and the delete lands on its target.

## Access control

Every `start` and `continue` requires a `source_classification`. Accept only
non-sensitive categories in v1 and refuse anything client-derived or
confidential outright, under every spelling you can think of. Reject unknown
fields. Apply explicit input and output caps.

## Tests

Build fake `claude` and `codex` executables so the whole broker is testable with
no live calls. Reproduce the real envelope shapes, including the `subtype` lie.
Cover: caller-specific tool exposure; the full cycle in both directions; exact
session-ID continuation with a proof that "last session" is never used; schema
enforcement both ways; input and output caps; classification refusal; auth
failure; version mismatch; nonzero exit; timeout with full process-group
cleanup and a zero-orphan assertion; malformed-output quarantine; no raw stderr
or peer output in any safe error; no shell execution anywhere; atomic writes and
restart-safe polling; commit, source-hash and dirty-state provenance; no
identical retry after a deterministic failure; the backend being unable to call
the bridge back; and permissions.

Then the concurrency cases from the section above, each one individually.

Two warnings from experience. First, if your orphan checks shell out to `ps`,
make them **skip** where `ps` is unavailable rather than aborting the suite, or
your results will not be reproducible in a sandbox. Second, and more important:
watch for tests that assert the bug as correct behaviour. In the reference
implementation an external reviewer found five such tests, including one that
required a non-owner to be able to mutate a conversation, and one whose
quarantine check passed on unrelated bytes. Assert on the diagnostic reason as
well as the outcome; a patch that silently fails to apply can leave safe
behaviour with a misleading explanation.

## Live verification

Only after the offline suite is green. Ten one-turn consultations per direction,
three three-turn conversations per direction, one deliberately
schema-hostile prompt per direction, and a timeout canary per direction against
a controlled stub rather than a real hang. Synthetic, non-sensitive prompts.

Probe reachability first and exit nonzero rather than reporting a pass on a
direction that was never exercised. Report contract-valid rate, first-attempt
rate, p50 and p95 latency, failures by category, orphan count, and whether every
continuation used the intended session id. Do not declare it reliable from one
successful call.

## Finally

Do not install anything. Print the exact commands I would run to authenticate
and to register both MCP servers, and stop for my approval. Note in that output
that `codex mcp add` writes to `$CODEX_HOME/config.toml` and must be run with
the **default** `CODEX_HOME`, never the isolated one, and that Codex Desktop
must be restarted before the tools appear in a new task.
