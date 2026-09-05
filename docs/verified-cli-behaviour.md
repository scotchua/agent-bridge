# What was measured, and what is still assumed

Every claim here was verified by running the real CLIs, not inferred from
documentation. Where something is an assumption it says so. If you are porting
this, re-verify against your own versions: several of these facts are
version-specific and at least one of them is the difference between a working
bridge and one that fails every call in one direction.

Measured locally on 2026-08-23 against the pinned executables. Anything not
listed as measured is an assumption, and the ones that matter are named at the
bottom. The broker re-verifies executable presence and version on every job and
fails closed on drift.

## Pinned executables

Paths are machine-specific and are pinned by `bin/agent-bridge-setup` into
`config/local.json`, which is gitignored. The versions below are the ones every
measurement in this document was taken against.

| Peer | Path | Observed version |
|---|---|---|
| Claude | whatever `agent-bridge-setup` pinned | `2.1.229 (Claude Code)` |
| Codex | whatever `agent-bridge-setup` pinned | `codex-cli 0.147.0` |

A second Claude install exists at `claude`, on
`$PATH`, which reported `2.1.235` during this build and auto-updated mid-session
from `2.1.234`. The bridge pins by absolute path **and** asserts the version, so
whichever install is first on `$PATH` cannot silently become the peer.

## Re-verifying a new peer version

Never replace the active pin before measuring its replacement. Setup first
writes a non-activating, complete effective candidate; the canary runner loads
that artifact verbatim and writes mandatory durable evidence; setup promotes
only evidence whose hash, observed versions, control counts, and `PASS` verdict
match the candidate:

```
./bin/agent-bridge-setup --candidate ~/.agent-bridge/candidates/effective.json
./canaries/run_canaries.py \
  --config ~/.agent-bridge/candidates/effective.json \
  --direction both --out ~/.agent-bridge/canary-results/latest.json
./bin/agent-bridge-setup --promote ~/.agent-bridge/candidates/effective.json \
  --results ~/.agent-bridge/canary-results/latest.json
```

Run any additional version-specific probes for claims in this document before
the promotion step. `config/local.json` is an overlay fragment, not a complete
config; when supplied explicitly it is merged with committed defaults. A setup
candidate is already complete and is therefore consumed without re-layering.

## Corrections to the original build brief

The brief asserted four things that do not hold. All four are handled.

1. **`codex exec resume` supports neither `-s/--sandbox` nor `-C/--cd`.**
   Measured by reading `codex exec resume --help`. The sandbox is therefore set
   with `-c sandbox_mode="read-only"`, which resume does accept under
   `--strict-config`, and the working root is set by the subprocess `cwd`.
   Verified: both argv shapes parse against an unauthenticated isolated home,
   failing on auth rather than on argument parsing.

2. **Codex's session identifier is `thread_id`, not `session_id`.** It arrives
   on the `thread.started` JSONL event, measured directly:
   `{"type":"thread.started","thread_id":"01a0308f-..."}`. `codex exec resume`
   takes that thread id; a bad one fails with `no rollout found for thread id`.

3. **Claude's `subtype` field says `success` even when the run failed.** On an
   auth failure the envelope carried `subtype: "success"`, `is_error: true`,
   `terminal_reason: "api_error"`. The backend therefore treats `is_error` as
   the only trustworthy success signal. This is the single most dangerous
   measured fact in this document: keying on `subtype` would have accepted
   failed runs as good ones.

4. **Prompt delivery is stdin, not an argument.** `codex exec` reads stdin when
   no prompt argument is given; passing both makes Codex append stdin as a
   separate `<stdin>` block, duplicating the question. `codex exec resume`
   needs an explicit `-` to read the prompt from stdin.

## Other measured facts

- **`claude --session-id <uuid>` is honoured exactly.** The returned
  `session_id` equalled the one sent. This is what makes continuation by
  explicit ID real rather than hopeful. `--last`-style behaviour is never used
  on either side.
- **`--safe-mode` disables CLAUDE.md, skills, plugins, hooks, MCP servers,
  commands and agents, while auth, model selection and permissions keep
  working.** Read from its own help text. Layered with `--strict-mcp-config`
  plus an empty `--mcp-config`, `--tools ""` and `--setting-sources ''`.
- **`--bare` was rejected as the isolation mechanism.** Its help states auth
  becomes strictly `ANTHROPIC_API_KEY` or `apiKeyHelper`, with OAuth and
  keychain never read, which would break a subscription peer.
- **Process-group kill works on a forked child.** A stub that ran
  `sleep 300 & sleep 300` left no survivors after SIGTERM to the group.
- **`os.makedirs(mode=...)` applies the mode to the leaf only**, and is masked
  by the caller's umask. Intermediate directories were being created `0755`
  under a lax umask. `store.secure_mkdir` now creates each component explicitly
  and enforces `0700`, verified under `umask 022`.
- **No `features.skills` or `features.plugins` flag exists at 0.147.0.**
  Searched the shipped binary's feature-flag strings; the ones present are
  `code_mode`, `multi_agent_v`, `token_budget`, `rollout_budget` and similar.
  There is no supported way to turn Codex's own skills off.
- **A fresh `CODEX_HOME` must exist before the run.** Codex errors with
  `Error finding codex home` otherwise. The backend creates it.

## Measured during the 2026-08-23 review round

- **An ancestor `.codex/config.toml` is NOT read by `codex exec`.** Planted a
  file containing `this_key_does_not_exist_anywhere = 42` in a parent
  directory's `.codex/`, ran from a child with `--ignore-user-config
  --strict-config`, and the run proceeded normally to its auth failure with no
  config error. Control: the same key in `$CODEX_HOME/config.toml` *without*
  `--ignore-user-config` produced a hard `unknown configuration field` error,
  which proves `--strict-config` does detect a config it actually reads. The
  review's suggestion that project-scoped config might be an unguarded MCP
  vector is therefore **not confirmed at 0.147.0**. Re-measure on a version
  bump; the canary suite is the place for it.
- **`AGENTS.md` above the old boundary was a real hole.** The workspace check
  stopped at `~/.agent-bridge`, while Codex discovers `AGENTS.md` by walking
  upward from its working directory. Fixed: the walk now goes to the filesystem
  root. Nothing in `~` or `/` currently trips it.
- **`proc.communicate()` buffers everything before any cap applies.** A peer
  emitting unbounded output could exhaust memory before the byte cap was
  reached. Fixed: reader threads enforce the cap while reading and kill the
  process group on breach. Measured against `yes` flooding stdout: capped at
  exactly the limit, killed in 0.03s.

## Measured during the second review round

- **`shutil.rmtree.avoids_symlink_attacks` is True on this platform**, so once
  a delete starts it walks with fd-relative calls. The exposure in
  `cleanup --apply` is therefore only the top-level path resolution, which is
  now checked twice with a symlink-component test immediately before deleting.
- **`/var` is a symlink to `/private/var` on macOS**, which broke a first
  attempt at symlink-component detection: a realpath'd root never string-
  prefixes a `/var`-spelled target, so the walk inspected nothing and returned
  a false negative. Path containment must compare realpaths at each step, and
  must never realpath the target itself, because that resolves the symlink
  being hunted.
- **A descendant that inherits a pipe and outlives the peer** previously
  blocked a reader thread until timeout, so a job that finished in
  milliseconds was reported as a timeout. Measured before: 5.12s and
  `timed_out: true`. After moving to a selector loop with a post-exit drain:
  0.62s, correct output, `timed_out: false`, and the condition recorded as
  `descendant_held_pipes`.

## Measured by the first authenticated canary run, 2026-08-23

The live run resolved the two standing assumptions and found one real defect.

- **The two peers enforce schemas differently, and contract v1 was invalid for
  Codex.** `codex exec --output-schema` routes the schema through OpenAI
  structured outputs, which requires every key in `properties` to appear in
  `required` at every object level. v1 left `analysis`, `disagreements`,
  `risks`, `questions` and `risks.items.mitigation` optional, so every Codex
  call failed with:

  ```
  invalid_json_schema: In context=('properties','risks','items'), 'required' is
  required to be supplied and to be an array including every key in properties.
  Missing 'mitigation'.
  ```

  Claude's `--json-schema` accepted the same document without complaint, which
  is why one direction scored 19/19 and the other 0/13. Contract v2 makes every
  field required and expresses absence as an empty array or empty string.
  Probed live: the all-required shape is accepted, and `minLength`, `maxLength`,
  `maxItems`, `$schema` and `title` all survive.
- **Claude's structured output arrives in a dedicated `structured_output` key
  on the result envelope.** Recorded as
  `extraction_path: envelope.structured_output` on all 20 successful Claude
  calls of the full matrix. It is the first channel the extractor tries, so
  none of the six fallbacks was ever used.

  Correction for the record: an earlier revision of this document stated the
  path was `envelope.result:json_string`. That was asserted before it had been
  read out of the run data and was simply wrong. The error-path envelope
  observed during the build carried no `structured_output` key, which is
  presumably where the wrong inference came from, but an error envelope says
  nothing about a successful one.
- **Codex's final message arrives on the documented `--output-last-message`
  file channel**, recorded as `output_last_message:json` on all 20 successful
  Codex calls. The decision to read that file rather than guess which JSONL
  event carries terminal text was correct.
- **Continuation by exact session id works in both directions.** 12 of 12
  continuations in the full matrix used the intended session, 6 per direction.
- **Measured latency, full matrix.** codex to claude p50 35.9s, p95 70.7s.
  claude to codex p50 16.2s, p95 25.3s. Both well inside their configured
  timeouts (300s and 420s), and the p95 on the slower direction is a reminder
  that a five-minute cap would have been uncomfortably close for a hosted
  bridge with any prompt larger than these.
- **Cost.** The 20 Claude-side calls of the full matrix reported $1.7696 total,
  against a configured `max_budget_usd` of 0.50 per call. Codex reports no cost
  figure through this interface.

## Prompt design defect found in live use, 2026-08-24

Both peers populated `disagreements` against a position the question never
raised. Asked only to name a failure mode and a minimum fix, Claude wrote
"None outright, but I'd push back if the asker intends exponential backoff
alone as the fix" and Codex asserted "I disagree that exponential backoff alone
fixes the core failure." Nobody had proposed exponential backoff.

Two things in this implementation caused it jointly:

- The preamble said "A consultation that only agrees is worthless," which is
  pressure to produce disagreement.
- Contract v2 makes `disagreements` a required key, so the peer must emit it.

Together they push a peer to fill the field whether or not a disagreement
exists. For a tool whose whole purpose is adversarial review, a fabricated
objection is worse than none: the caller may act on it, and it costs exactly
the reviewer attention the tool is supposed to earn.

Fixed on both sides. The preamble now scopes disagreement to a position the
caller actually stated, names the empty-list case, forbids arguing against a
position nobody took, and explicitly legitimises agreement. The schema carries
property-level descriptions defining each field's empty case, probed live for
acceptance before adoption.

Verified live, both directions, two prompts:

| Prompt | claude | codex |
|---|---|---|
| States no position (original question) | `disagreements: []` | `disagreements: []` |
| States a wrong position, asks for confirmation | 1 substantive disagreement | 1 substantive disagreement |

The second row is the one that matters. An over-suppressing fix would have
produced empty lists in both rows, and would have destroyed the anti-sycophancy
value while looking like a success. Both peers named the caller's stated claim
and refused to confirm it.

Caveat: n=2 per direction. This shows the fix works on the case that exposed
the defect and does not suppress genuine disagreement. It is not a measured
rate.

## Round 4, conducted over the live bridge, 2026-08-24

The first review round run through the bridge itself rather than by hand. Seven
consultations in one continued conversation. It found a defect class that three
prior rounds and 308 tests had missed: **the fixes from rounds 2 and 3 defeated
each other.**

Terminal immutability (round 2) made the ownership handshake's precondition
(round 3) silently unmeetable. A job reconciled to failed while queued turned
the worker's `running` write into a no-op, so no pid was published, the
handshake still passed because the claim was intact, and a rival then saw a
terminal job and took the slot. Both workers would have driven the same peer
session. Reproduced before fixing.

Then five successive passes each named a distinct window in the same lifecycle:

| Pass | Window |
|---|---|
| 1 | `running` write swallowed, so no pid was ever published |
| 2 | reaping was on the poll path, leaving admission unguarded |
| 3 | the uncertainty predicate was the kill count, and the hold was written after signalling |
| 4 | the marker was written after `Popen`, so a peer could exist with no evidence |
| 5 | the marker was deleted at peer exit, before the worker durably committed |

Patching them one at a time was converging slowly, so pass 5 took the reviewer's
own framing instead: the marker's lifetime now spans the whole attempt, from
before the peer exists until after the worker durably commits. That closes the
class rather than a sixth instance. Asked directly whether a sixth boundary
existed, the reviewer could not name one.

Verdict: the concurrency withhold was lifted, with two conditions, both since
satisfied. Marker retirement is gated on a confirmed conversation release rather
than merely ordered after it, and retention is bounded with retired markers
excluded from in-flight discovery.

### Watched, not characterised

- **A peer CLI intermittently omitting its own model and cost.** Reported by
  an outside reviewer against `claude 2.1.241` on three calls, with fresh
  versus resume ruled out by the third. The bridge records whether the CLI
  reported each field, so a null is attributable rather than ambiguous, but
  three calls characterise nothing and no amount of reasoning here will fix a
  field the CLI did not send.
  `agent-bridge-admin reporting` is the other half: it separates records
  written before the instrumentation from records that can actually answer the
  question, and breaks the rest down by CLI version and by resume. Below
  `--min-sample` it prints counts and says so, because a rate from a handful of
  calls is a number and not a finding.
  Entry condition: the usable sample reaching the threshold. The outcome is an
  upstream report, not a change here; this is the CLI's envelope, not ours.

### Tabled, not abandoned

Improvements the reviewer would prefer but does not require. Entry conditions
noted so they are picked up on a trigger rather than forgotten.

- **Crash-injection testing at lifecycle boundaries.** Currently the boundaries
  are tested by placing marker states directly; genuine fault injection would
  kill a worker at each point. Entry condition: any future change to the marker
  lifecycle, or the first real mid-call worker death observed in the ledger.
- **Monitoring aged indeterminate holds.** `status` warns that holds exist but
  does not track their age. Entry condition: the first hold that occurs in
  practice, or more than one hold outstanding at once.
- **Asserting on diagnostic reasons, not only outcomes.** This round produced a
  patch that silently failed to match its target; the behaviour stayed safe and
  only a reason-string assertion caught it. Entry condition: apply to new tests
  as written, rather than retrofitting.

## Assumptions still outstanding

These are assumptions, not measurements, because no authenticated peer run was
possible during this build. Each one is either belt-and-braces or is failed
closed rather than guessed around.

1. **RESOLVED by the live run.** Claude's structured output arrives as a JSON
   string in `envelope.result`. Original text follows for the record.
   **Where Claude puts structured output on a *successful* run.** Every live
   attempt failed at authentication, so only the error-path envelope was
   observed. The extractor tries, in order: `structured_output`,
   `structuredOutput`, `structured_result`, `output`, then `result` as an
   object, then `result` as a JSON string, then the first balanced JSON object
   embedded in `result`. The winning channel is recorded per attempt as
   `extraction_path`, so the first live canary will state which one is real.
   Every path fails closed rather than returning an unvalidated payload.
2. **Codex's terminal event names.** Fragments recovered from the binary
   include `item.added`, `item.completed`, `item.started` and `thread.failed`;
   `turn.started` was observed live. The backend does not depend on any of
   them: it reads the final message from `--output-last-message`, a documented
   file channel, and treats unknown event types as ignorable so a minor Codex
   version bump does not break the bridge.
3. **Whether Codex's system skills load during a peer run.** The review
   confirmed this remains open: the skills are present, this implementation
   does not disable them, and no live execution was available to test whether
   they activate. Treat them as potentially active. The isolated home
   holds 6 system skills (`review-agent`, `skill-creator`, `plugin-creator`,
   `skill-installer`, `openai-docs`, `imagegen`) and a 180-entry plugin
   marketplace cache, some plugins carrying `.mcp.json`. `--ignore-user-config`
   demonstrably stops `config.toml` from loading, which is where `codex mcp add`
   writes and therefore the one path by which this bridge could be handed back
   to its own peer; the broker additionally refuses to run if that file appears.
   Nothing here proves the system skills are inert. An inventory is recorded on
   every job.

## The read boundary

`-s read-only` restricts writes. It is not a filesystem read boundary and is
not claimed as one anywhere in this implementation. This is the same scoped
acceptance the existing `codex-bridge` carries, not a new or smaller risk. The
controls that actually apply are: client-derived content is refused, the
workspace is empty and per-conversation, no user config or `.rules` are loaded,
the environment is scrubbed of credentials, and the peer is instructed not to
inspect the filesystem. An instruction is not a control. Treat it as one layer,
not a boundary.
