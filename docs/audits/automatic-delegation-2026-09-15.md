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

  The mechanism is understood and reproduces standalone, in code this branch
  does not touch. `group_survivors` calls `process_group_members`, which
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

  **Observation for the maintainer, not changed here.**
  `platform.process_group_members` is also used by production reconciliation.
  Returning zombies there means a reconciler can judge an already-dead group
  still alive and re-signal it. Harmless, but it is the same conflation, and
  changing production semantics is not this change's call to make.

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
