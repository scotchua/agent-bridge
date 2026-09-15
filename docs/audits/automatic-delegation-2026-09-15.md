# Automatic delegation: what was repaired, what was proven, what was not

15 September 2026. Raw captured output:
[`automatic-delegation-2026-09-15.log`](automatic-delegation-2026-09-15.log).

## The question this answers

Delegation was *available*: the orchestration MCP could register a stage,
claim it, record a routing receipt and queue a bounded implementation job, and
the delegation-first gate refused an edit without a receipt. But the decision
to delegate was optional and the assistant made it. A user still had to ask
for delegation, and an assistant that wanted to keep the work asked for its
own route and got it.

Three things were wrong. All three are addressed, and the evidence for each is
separated from the parts that remain unproven.

## 1. Both execution lanes could only run on one macOS layout

`claude_task.py` and `codex_task.py` each carried two constants:

* `GIT_BIN = "/Library/Developer/CommandLineTools/usr/bin/git"`, the
  standalone Command Line Tools path;
* `_assert_macos()`, refusing any host that is not Darwin with
  `/usr/bin/sandbox-exec` present.

Reproduced on this host before the repair:

```text
old GIT_BIN            : /Library/Developer/CommandLineTools/usr/bin/git
exists on this host    : False
FileNotFoundError: [Errno 2] No such file or directory:
  '/Library/Developer/CommandLineTools/usr/bin/git'
```

Inside the harness that `FileNotFoundError` became
`runner.RunResult(spawn_failed=True)`, and then
`TaskError("command spawn failed")`. That string is the whole defect: it names
no program, no path and no cause, and an operator reading a receipt containing
it cannot tell it from a failed login or a killed process.

The platform assert made it worse by hiding the rest. It ran first, so on
Linux the lane refused with `"Claude execution lane requires supported macOS
sandbox-exec"` before doing anything, even though only the *verification* step
needs confinement. Generation runs under the provider's own tool allowlist or
sandbox, and patch capture and re-application are git.

**Audit answer for the other lanes.** `codex_task.py:72` and `:222` were the
same two lines, so Claude-to-Codex failed identically. Local-model dispatch
never touches these harnesses and was unaffected.

### The repair

`src/agent_bridge/execution/hostenv.py`:

* `resolve_git()` tries a per-platform candidate list, then `PATH`, and its
  refusal names every path it tried (`git_unavailable`).
* `confinement(classification)` returns the backend this host will actually
  apply, or refuses by name. The refusal codes are
  `verification_confinement_unavailable` and
  `verification_confinement_insufficient`.
* `_spawn_detail()` in each lane distinguishes a missing file, a directory and
  a non-executable file, and names the program.

After the repair, the same failure names itself:

```text
claude -> could not start '/Library/Developer/CommandLineTools/usr/bin/git': no such file
codex  -> could not start '/Library/Developer/CommandLineTools/usr/bin/git': no such file
```

### The confinement boundary, stated exactly

| Backend | Network | Reads | Writes | Classifications | Verified |
| --- | --- | --- | --- | --- | --- |
| `macos-sandbox-exec` | denied | confined | confined | synthetic, public, internal_nonclient | yes, on macOS |
| `linux-netns-synthetic-only` | denied | **not confined** | **not confined** | synthetic only | not independently verified |
| anything else | n/a | n/a | n/a | none | refuses by name |

On Linux the write boundary is the disposable worktree plus the harness's own
post-run content snapshot of the source repository, not the kernel. That
snapshot really does catch an escape: a stand-in provider told to write above
its worktree produced

```text
ok: False  error: TaskError  detail: source repository integrity changed during task
```

and the job failed, with the written file still present. That is a detection
control, not a prevention control, which is exactly why this backend carries
invented material only. Requesting anything else on such a host is refused:

```text
the linux-netns-synthetic-only backend confines no reads, so it carries only
synthetic material; 'internal_nonclient' needs a host with full confinement
[verification_confinement_insufficient]
```

`tests/test_hostenv.py` guards against re-hardcoding: it fails if either lane
reintroduces `GIT_BIN`, names `CommandLineTools`, defines or calls
`_assert_macos`, names the sandbox binary directly, or restores
`TaskError("command spawn failed")`.

## 2. The decision was optional and assistant-steered

### What changed

* `orchestration/autoroute.py` is a pure function. Four inputs decide a route,
  in this order: privacy and eligibility, task type, hardware load, capacity.
  Privacy is first so capacity can never override it. `ROUTES` is
  `("claude", "codex", "local")`: a paid route is absent from the vocabulary,
  not disabled by configuration, and `parse_policy` refuses one that names a
  route outside it.
* `orchestration/autodecide.py` holds the side effects: read the operator's
  policy, ask the router what capacity is fresh, decide, establish the stage
  ownership the route implies, write the receipt, and write a dispatch intent
  when the route is not the caller's own.
* The `PreToolUse` hook calls it when a repository has no valid receipt. So a
  decision exists before implementation with no user sentence and no assistant
  tool call.

### Policy is the operator's, and it starts inert

A repository with no entry in `<state_root>/routing/routing-policy.json` is
retained and never dispatched. `onboard apply` writes a scaffold with no
repositories in it and never rewrites an existing one. Installing this feature
therefore changes nothing about where work runs until somebody classifies
something. An unreadable policy is a deny, never a permissive default.

### Capacity evidence is first-hand only

When a decision retains work, the hook records one capacity observation for
its own route, sourced `gate-hook:client-present`, fresh for 15 minutes: a
client that just made a tool call is demonstrably running. It never records
one for the peer or the local model. Verified:

```text
capacity after one claude hook call:
{
  "claude": {
    "fresh_until": 1789485031.806452,
    "observed_at": 1789484131.806452,
    "source": "gate-hook:client-present",
    "status": "available"
  }
}
codex present: False  local present: False
```

### Five defects found while building this

Each was a live failure during this work, not a hypothetical, and each has a
named regression test in `tests/test_automatic_gate.py`. The last two were
found by walking the workflow exactly as `INSTALL.md` documents it, which is
worth noting on its own: reading the prose back against the running system
found what reading the code did not.

1. **A completed stage bricked the repository.** After a normal
   `stage_complete`, every later edit denied with `stage_not_owned` and told
   the assistant to claim a stage by hand, which is the opposite of automatic.
   Stages now carry a generation (`implementation`, `implementation#2`).
2. **One receipt per repository answered for work of a different kind.** A
   single decision governed every later call in that repository whatever it
   was about. An automatic receipt is now re-decided when the task type
   differs.
3. **An overtaken receipt denied instead of re-deciding.** Expiry, and a stage
   that is finished, reassigned or lease-lapsed, are ordinary events.
   `automatic_receipt_overtaken` treats them as a reason to decide again. An
   **unreadable** stage router is deliberately excluded and still fails closed
   with `stage_db_unavailable`: never decide without the router.
4. **Editing the policy changed nothing until a receipt aged out.**
   Classifying a repository and then editing it was still allowed, because
   the retained receipt was valid, for the same task type, and its stage was
   still owned. The change would have taken effect up to four hours later.
   Every automatic receipt now records a fingerprint of the policy it was
   decided under.
5. **A receipt named a route its decision did not choose, and the gate
   allowed the edit.** Exposed immediately by fixing defect 4. The stage
   router never reassigns an owned stage, so when the policy moved work to
   the peer the old stage was still owned on the old route; `_own_stage`
   returned that record and the receipt was written naming the old route
   while the decision said the new one. A decision to delegate had silently
   become a decision to retain, which is the single failure this whole
   mechanism exists to prevent. `stage_name` now skips a stage owned on a
   route the decision did not choose (completing one the decider itself
   holds), and `ensure_decision` checks the invariant rather than assuming
   it: a route it cannot establish as live owner is a deny.

   Verified across five consecutive policy changes, each acted on by the
   very next call, each receipt consistent with its own decision:

   ```text
   1. unclassified            route=claude code=retained_repo_unclassified
   2. both providers eligible route=codex  code=routed_peer_implementation
   3. codex withdrawn         route=claude code=retained_is_the_policy
   4. codex re-added          route=codex  code=routed_peer_implementation
   5. client_derived          route=claude code=retained_classification_ineligible
   ```

### Two limits, stated rather than dressed up

* **The gate cannot write the brief.** A `PreToolUse` payload names a tool and
  some paths, not the task. So the gate writes a dispatch intent with the
  route and the full stage binding, refuses the edit, and names the single
  call that is owed. The assistant then calls `execution_dispatch` with those
  identifiers and its own brief: no `stage_register`, no `stage_claim`. The
  user is never the messenger and the assistant cannot edit instead, but the
  brief's words are the assistant's.
* **A file edit is never routed to a local model.** The local worker processes
  bounded inline text and returns a draft; it does not read files, run
  commands or edit anything. An earlier version of `infer_task_type` guessed
  "mechanical" when every target looked like a test file. It read well and was
  wrong: it produced an intent nothing could satisfy. Automatic local routing
  happens at `work_route_local`, whose intake classifies and submits without
  anyone asking. Mechanical work an assistant simply performs in its own
  context produces no tool call, so no local mechanism intercepts it. That is
  a limit of the hook surface and an instruction file does not fix it.

## 3. There was no accounting

`orchestration/audit.py`, reachable as
`bin/agent-bridge-gate-hook audit --config <orchestration.json>`. Over the
complete workflow run:

```text
Delegation audit, last 0.0 hours

  classified repositories: 1
  decisions: 1 (1 automatic, 0 agent-requested)
  eligible to delegate: 1
  routed away:          1 {'codex': 1}
  retained:             0

  bypasses the gate can see:
    routed but never dispatched: 0
    clients without the hook:    none
    writes aimed at the gate:    0
  a zero above means none this mechanism can see, not none.

  gate failures: 0
  execution_queue: {'complete': 1}

  hook (claude): installed
  hook (codex): installed
    the hook is installed but Codex has not trusted it, so it does not run
    and every Codex call is un-gated
  codex hook trust: needs_review
```

The bypass column is split on purpose. *Observed* is what the gate counted:
work routed away and never dispatched, clients the hook is not installed for,
and writes aimed at the gate's own state. *Not countable* enumerates the
surfaces no local mechanism can see, so a zero in the observed column is never
read as an absence. Failure rows print each job's own `error_detail` and
`harness_error_detail`, because an exception class name is not evidence.

## What was live-tested

`tests/test_automatic_delegation_e2e.py`, 14 tests, all passing on this host.
Real components: the gate installer writing into an isolated
`~/.claude/settings.json` and `$CODEX_HOME/hooks.json`; the installed launcher
run as a subprocess; the orchestration MCP server as a subprocess over stdio;
the real `ExecutionQueue` and `SubprocessHarnessExecutor` running the real
`claude_task.py` and `codex_task.py`; real git worktrees, patches and
re-application; verification under this host's real confinement backend; the
real local lane including a real HTTP round trip to a model endpoint; and the
real audit over the records left behind.

Both directions, run directly:

| | Codex to Claude | Claude to Codex |
| --- | --- | --- |
| queue state | `complete` | `complete` |
| harness status | `complete` | `complete` |
| route | `claude-subscription-cli` | `codex-subscription-cli` |
| verification | `returncode: 0` | `returncode: 0` |
| patch | 157 bytes, unapplied | 157 bytes, unapplied |
| `source_integrity_match` | true | true |
| `fresh_worktree_patch_match` | true | true |
| apply/commit/push/merge permission | all false | all false |

The source repository is byte-identical to its seed after both lanes:
`'def add(a, b):\n    return a - b\n'`.

Local routing: `work_route_local` from both callers, decision `local`, reason
`eligible_mechanical_work`, `fallback: "none"`, an immutable routing receipt,
one real HTTP request to the model endpoint, and a draft returned as a draft.
`client_derived` material is refused (`classification_refused`) and work
needing judgment is refused (`task_requires_cloud_or_human_judgment`), neither
with any cloud fallback.

Restart recovery: a job interrupted while running, and a job whose claim was
taken before its receipt said running, both recover to `blocked` with
`interrupted_requires_reconciliation` and are not picked up again. A job that
was only queued survives and still runs. A routing decision survives into the
next hook process, which matters because every tool call is a new process.

A failure records its own detail rather than a class name:

```text
harness_status:  failed
harness_verdict: read_failure
error_detail:    Claude is not authenticated through a supported claude.ai subscription
```

## What was NOT proven, and must not be claimed

* **No provider account was contacted and no allowance was spent.** The
  provider CLIs and the local model are stand-in executables
  (`tests/fakes/fake_exec_claude.py`, `fake_exec_codex.py`,
  `fake_local_worker.py`). Their `--version` reports `fake-exec-*` so no
  receipt built on them can be read as a provider run. This proves the
  dispatch machinery, the enforcement, the receipts and the recovery. It does
  not prove any provider's login, model behaviour, or that a real CLI emits
  the envelope shape the harness parses on a version nobody has measured here.
* **macOS is unverified by this run.** Everything here ran on Linux
  (`Linux 6.18.44 x86_64`, Python 3.11.15, git 2.43.0). The
  `macos-sandbox-exec` backend is unchanged and remains the only
  independently verified confinement, but no macOS execution happened in this
  work. The existing macOS LaunchAgent evidence is untouched and equally
  unrefreshed.
* **Windows is unverified and is not offered.** The `.cmd` launcher for the
  hook is written and has still not been run under either host. `hostenv`
  serves no Windows confinement backend at all: the Windows lane runs
  verification inside the WSL guest, a different mechanism with its own
  separate evidence that this work did not touch. Do not present Windows
  automatic delegation as installed, verified or available.
* **The Linux confinement backend is not independently reviewed.** It denies
  network access and nothing else. It is restricted to synthetic material for
  that reason and reports its own limits in every receipt.
* **Some checks do not pass in this container, and none of them is a
  regression.** Each was verified by running the same module against a
  pristine `git worktree` of `origin/main` and comparing the failure sets:

  | Module | Here | On pristine main here | CI on main |
  | --- | --- | --- | --- |
  | `tests/test_suite.py` | 559 pass, 2 fail | same 2 | green |
  | `test_provider_lane_fixtures` | 19 fail, 2 error | identical set | green |
  | `test_guest_runner` | 1 error | identical | green |

  The two main-suite failures (`R5: an unenumerable ancestor fails closed`,
  `LC: an unwritable marker refuses the run rather than spawning blind`) rely
  on permissions uid 0 ignores; the README already warns against running the
  checks from an elevated account. `test_guest_runner` needs the
  `agent-bridge-job` account that exists only inside the provisioned WSL
  guest rootfs. `test_provider_lane_fixtures` fails in its guest auth-probe
  fixtures for the same environment reason. All three pass in CI on
  `a6c59ed`, so they are artifacts of this container.

  **The main suite's two orphan-process checks are intermittent here, and
  the first attribution of them in this document was wrong.** They were
  called load-induced on the strength of two clean runs. Three further runs
  produced 2, 4 and 3 total failures, so the checks are not reliably clean in
  this container and two passes did not support that conclusion:

  ```text
  solo run 1   passed: 559   failed: 2     (baseline only)
  solo run 2   passed: 557   failed: 4     (both orphan checks)
  solo run 3   passed: 558   failed: 3     (one orphan check)
  ```

  The mechanism below is one of at least two, and naming it as the whole
  story was the third wrong attribution of this check. See the review round
  for the correction: a single sample taken half a second after the group
  kill is not a measurement of whether the group survived, and the survivor
  a later run reported was not a zombie at all. `group_survivors` calls
  `process_group_members`, which
  enumerates with `ps -A -o pid=,pgid=` and counts **any** process in the
  group, and the check runs 0.5 seconds after the group kill. A SIGKILLed
  grandchild is a zombie until reaped, and reaping in this container lands
  either side of that window:

  ```text
  t+ 0.1s  members in group 9205: [('9206', 'Z')]
  t+ 0.5s  members in group 9205: [('9206', 'Z')]   <- the check samples here
  t+ 1.0s  members in group 9205: []
  ```

  So the check asks "did a live process survive the group kill" and measures
  "does `ps` still list anything", which are not the same question. A zombie
  holds nothing and can do nothing. The 0.5s sleep is in
  `tests/test_suite.py` and the enumeration is in `platform/posix.py`;
  `runner.py`, `broker.py`, `platform/` and `backends/` carry no change on
  this branch, which is a reason to expect no difference rather than evidence
  of none, so attribution also rests on running the same suite alternately
  against this branch and a pristine worktree of `origin/main` on the same
  machine.

  **Resolved in the review round below.** `group_survivors`, the test helper,
  now excludes zombies, so the check measures the question it asks.
  `platform.process_group_members` keeps the conflation deliberately: there,
  counting a zombie as alive holds a job for reconciliation slightly longer
  than necessary, which is the safe direction, while skipping zombies would
  make it readier to release a stage, which is not. A test wants the true
  answer; a liveness gate wants the conservative one.

## Next steps for a real host

1. Sign both CLIs in, then run the same two lanes with the pinned executables
   on that machine. `bin/agent-bridge-orchestration-verify` is the authorised
   path; it spends allowance, so ask first.
2. On macOS, confirm `hostenv.confinement("internal_nonclient")` returns
   `macos-sandbox-exec` and that the verification profile still denies reads
   and outside writes (`tests/test_claude_task.py` asserts what the selected
   backend claims, so it checks the real thing there).
3. Classify one repository, make one edit in Claude Code, and read
   `gate-hook audit`. That is the whole workflow in three steps.
4. Do not publish a release, and do not make broad Windows claims, until 1 and
   2 have produced evidence on the hosts in question.

---

# The adversarial review round

The five commits above were reviewed adversarially and **rejected before
merge**: two critical defects and four high-impact gaps. The review's own
evidence is the part worth quoting: 544 core tests passed with five
environmental skips, 118 focused gate, routing and audit tests passed, and
none of those passing tests covered any of the defects.

The interactive work below was done on Linux, and no provider account was
contacted. It is not true, as an earlier draft of this section said, that
none of it is evidence about macOS or Windows: CI runs the gate suites
(`test_delegation_gate`, `test_automatic_gate`, `test_delegation_audit`,
`test_hostenv`) and the whole offline suite on macOS and Windows runners as
well, and the execution lanes and the end-to-end workflow on macOS. What has
no coverage on any platform is the gate hook's own `.cmd` launcher and the
hook being invoked by Claude Code or Codex rather than as a subprocess. The
per-platform split is in `REVIEW-HISTORY.md` under "What is still not true".

## Critical: routing could be escaped one directory down

Judgment keyed on the nearest ancestor holding `.git`. Reproduced end to end
against the fix:

```text
STEP 1  claude tries to Edit secret.py at the routed root        -> DENY [routed_elsewhere]
STEP 2  claude tries the escape command: git init src            -> DENY [routed_elsewhere]
STEP 3  nested .git exists: yes
STEP 4  claude tries to Edit a PRE-EXISTING file inside it       -> DENY [routed_elsewhere]
STEP 5  claude tries a shell write inside the nested repo        -> DENY [routed_elsewhere]
STEP 6  codex, which the decision chose, edits inside it         -> ALLOW
```

Judgment now considers every enclosing repository, and `git init`, `clone`
and `submodule` count as writes. Eight regression tests in
`tests/test_automatic_gate.py::TheRoutingEscapeAnAdversarialReviewFound`.

## Critical: Linux verification was not confined

It denied the network and let generated code write anywhere the account could
reach, so checking the source repository afterwards proved nothing about the
rest of the machine.

Verification now runs inside a mount namespace that binds the worktree and
scratch directory writable, seals every other mount point read-only, drops
all capabilities, and **refuses to run at all** unless a write outside the
worktree, a write to `$HOME` and a remount of `/` all actually fail on this
host at that moment. Measured escape matrix, every row as stated:

```text
inside worktree: relative write, absolute write, mkdir      OK
outside worktree; $HOME; clobber an outside file            REFUSED
the source repository; the job directory; /dev/shm          REFUSED
via /proc/1/root; via symlink; via hardlink                 REFUSED
remount / read-write                                        REFUSED (rc=7)
bind-mount over cwd                                         no effect
nested unshare (uid_map write EROFS 30, remount errno 1)    no effect
network                                                     DENIED
git status --porcelain / git diff --check                   rc=0
python3 -m unittest --help / python -m unittest --help      rc=0
```

Four defects surfaced while building it, each leaving a boundary that passed
its own tests and restricted nothing: sealing `/` before binding the worktree
(unwritable worktree), an inherited working directory (relative writes
EROFS, absolute writes fine), retained `CAP_SYS_ADMIN` (the payload could
remount `/` and get 0 back), and `os.execv` not searching `PATH` (the
verification command never ran). The self-check exists because of the third.

**Still not confined:** reads, on Linux, which the receipt records as
`confines_reads: false`, and **generation on every platform**, which runs
with the real `HOME` and the provider's own Read and Write tools inside the
generation worktree. That is the direct worktree-to-patch channel and this
change does not touch it.

## High: capacity was model-controlled

`capacity_observe` let either assistant name the route, availability, source
and a freshness window of any length. The tool is removed. Capacity has two
writers, neither on the wire: the hook's own first-hand presence, and
`declared_available` in the operator's policy file, replayed as a declaration
and withdrawn when the operator deletes a route. The ledger's `trusted`
column is set by the writing code path, never read from the row, defaults to
untrusted so an installed database's existing rows stop counting on upgrade,
and only a trusted row routes work. No window may exceed the routing lease.

Proven by `tests/test_capacity_router.py` (20 tests, including a legacy
database built without the column, whose pre-existing row grades `untrusted`
and blocks the stage) and `tests/test_automatic_gate.py` (declaration alone
routes; withdrawal stops it; withdrawal cannot erase a peer's real presence;
an untrusted row routes nothing).

**Not claimed:** the declaration is not a health check. Nothing in this tree
probes whether a declared peer is actually reachable, and the ledger and the
audit both print the source so the difference is visible rather than implied.

## High: decisions went stale for four hours

A receipt now records a digest of the routes with eligible capacity, so a
decision that retained work because the peer was unavailable is re-made the
moment the peer appears. Measured, with the policy untouched between the two
calls, so only the capacity digest can be what noticed.

The digest took two attempts, and the second mistake was worse than the
staleness it was fixing.

Attempt one digested the rows, and re-decided on every call, because the hook
rewrites its own presence row every time it runs: eight decisions where one
was correct. Attempt two fixed that by digesting route names only and
subtracting the asking client's own presence row. That made the digest
client-relative, and a receipt is one shared per-repository artifact, so
claude and codex computed different values from the identical ledger, each
found the other's receipt overtaken, and each re-decided it to route the work
to the other. Both were then permanently denied, each holding an instruction
to dispatch to the other. A livelock.

The digest is now route names only and identical for both clients. Measured
through the real hook, alternating the two clients four times with codex
declared available:

```text
 round 1   claude: DENY routed_elsewhere      codex: ALLOW
 round 2   claude: DENY routed_elsewhere      codex: ALLOW
 round 3   claude: DENY routed_elsewhere      codex: ALLOW
 round 4   claude: DENY routed_elsewhere      codex: ALLOW
 decisions recorded: 1
```

**How the livelock survived its own regression test.** The test written to
guard it drove the codex hook with `tool="Edit"`. Codex has no `Edit` tool,
so the gate classified the call as not gated and allowed it without reading
the receipt at all. The test passed while exercising nothing. The fixture now
picks each client's real editing tool, and the same walk that found the
livelock also found the test defect.

## High: the red head, and the hardcoded interpreter

CI was red on the reviewed head (Ubuntu 3.11, Ubuntu 3.13, Windows 3.13). The
cause was mine: `_require_confinement` ran before the verify-command check, so
runners with no usable namespace errored instead of skipping. Reordered, and
tests that need a backend skip by name. **CI is green on `6af1be4`**
(run 34992443320).

The end-to-end suite assumed a bare `python`, which cost the reviewer three
failures on macOS, where it has not existed since system Python 2 was
removed. It probes `python3` first, which is what the setup documentation
says, and skips by name on a host with neither.

Two suite checks that need a directory's mode bits to actually deny access
now measure whether mode bits bind this account and skip when they do not.
Running as uid 0, the restriction is a no-op and the check was reporting a
failure about the account on the line a real regression would use.

## High: what this is, named precisely

**Automatic routing in Claude Code and the Codex CLI, with
assistant-mediated dispatch.** All three qualifications are load-bearing, and
the README, `INSTALL.md` and `DELEGATION-GATE.md` now carry the phrase rather
than leaving it to be assembled from separate paragraphs.

The end-to-end test whose docstring said local work was routed "without
anyone asking for it" calls `work_route_local` itself. What is automatic at
the local lane is the *admission*: classification, the privacy refusal, the
submission, and the absence of any paid fallback. The choice of lane is the
assistant's, because the gate cannot route file edits to a worker that does
not edit files. The docstring says that now.

## The follow-ups the review allowed to come after merge, done here anyway

* **Dispatch-intent cleanup is bound to the exact job** (route, item, stage,
  owner, revision). Keyed on the repository alone, any owned stage in it
  could retire the intent for the stage that was actually refused, and the
  audit's "routed but never dispatched" column is the one thing that catches
  an unhonoured routing. A mismatch is recorded rather than silent.
* **A decision that retains the work retires the intent an earlier decision
  wrote,** with its reason, so the audit stops reporting a routing the policy
  has reversed.
* **Independence is enforced in both directions.** A review of the *peer's*
  work used to fall through to the ordinary branch and go straight back to
  its author.
* **A stage somebody else owns is not adopted.** A receipt naming a foreign
  owner points at a lease this decider cannot renew.
* **The routing policy is read once** for both the rules and the digest
  stamped in the receipt, so an operator's save cannot land between the two.
* **The delivered patch is re-read and compared** to what was generated
  before it is returned, which is the one check that does not depend on
  confinement being correct.
* **Windows instructions no longer contradict themselves.** "Automatic
  delegation is not available on Windows" sat three paragraphs above
  "automatic delegation on Windows runs jobs in an ephemeral WSL2 guest".
  The gate and the execution lane are named as separate components, the
  `.cmd` launcher is described as written and never run, and the WSL2 path is
  described as provisioning work in progress.

## Still outstanding, and stated rather than closed

1. **The Windows hook launcher has never been run under either host.** Not
   fixable from here.
2. **The hook has not been exercised through the real Codex and Claude Code
   hosts** in this round; the gate is driven as a subprocess with real
   `PreToolUse` payloads, which is the same interface but not the same
   integration.
3. **Shell-command interception is a heuristic over command text**, stated as
   such in the module docstring, the README and `DELEGATION-GATE.md`. It is
   not a security boundary. The editing tools are the deterministic part.
4. **Generation is unconfined.** See above.
5. **No release, and no broad Windows or macOS claim,** until evidence exists
   on those hosts.
