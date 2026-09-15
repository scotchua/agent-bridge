# What four rounds of adversarial review found

This bridge was reviewed by having each model attack the other's work, four
rounds, 40 findings. This is the write-up, kept because the findings transfer
even if the code does not.

Two things are worth knowing before the list.

**Individually correct fixes broke each other, twice.** Both times the result
was worse than either original bug, and both times every test passed. That is
the failure mode this document exists to warn about.

**Five tests were asserting bugs as correct behaviour.** Found by the reviewer,
not by me. A suite that certifies the defect is worse than no suite, because it
converts "we have not checked" into "we have checked and it is fine".

## The design was wrong in four ways before any of this

Measured against the real CLIs, contradicting what the original design assumed:

1. `codex exec resume` supports neither a sandbox flag nor a working-directory
   flag, so both have to be supplied another way.
2. Codex's session identifier is a thread id on a specific event, not the
   session field the design named.
3. Claude's result envelope reports success even on a failed run. A different
   field is the only trustworthy signal. Keying on the obvious one silently
   accepts failures as good answers.
4. Prompts go on standard input. Passing them as an argument as well silently
   duplicates the question.

Detail in [verified-cli-behaviour.md](verified-cli-behaviour.md).

## The one that cost real money

The two models enforce output schemas differently. One accepts a schema with
optional fields; the other routes it through a stricter validator that requires
every field to be listed as required. So a single shared schema worked perfectly
in one direction and failed **one hundred percent** of calls in the other.

Thirteen live calls were spent discovering something a five-line static check
would have caught. That check now exists and runs before any live call.

The general lesson: when two systems must both accept the same artefact, test
the artefact against both statically, before spending anything on a round trip.

## The concurrency findings, in the order they were found

Each round closed the previous gap and revealed the next one. All five turned out
to be windows in a single lifecycle.

| Round | The gap |
|---|---|
| 1 | A worker could invoke a model without holding the conversation it was speaking for |
| 2 | The fix protected the polling path and left the admission path unguarded |
| 3 | The uncertainty test counted how many processes were killed, and the safety record was written after the destructive action rather than before |
| 4 | Evidence of an in-flight call was recorded after starting the call, so a crash in between left an invisible orphan |
| 5 | Evidence was discarded when the local process ended, before the outcome had been durably saved |

Patching them one at a time converged slowly. What ended it was taking the
reviewer's own framing: instead of closing a fifth window, make the evidence
last for the **whole** operation, from before anything starts until after the
result is durably recorded. Asked afterwards whether a sixth window existed, the
reviewer could not name one.

The transferable version: **if you are fixing a sequence of ever-narrower
windows, you are enumerating instances of a class you have not named yet.** Name
the class.

## Where two correct fixes collided

Round 2 established that a finished record must never be overwritten, so a stale
process could not replace a real error with a wrong one. Correct.

Round 3 added a rule that a worker must publish its process id and then confirm
it still owned the conversation before calling a model. Also correct.

Together they were broken. If a job was marked finished while it waited, the
worker's attempt to publish its id was silently ignored, because finished
records are immutable. No id was published, the ownership check still passed
because the claim was intact, and a second worker then saw a finished job and
took the conversation. Two workers, one model session.

The fix is small: check that the write you depended on actually happened. The
lesson is not small. **A fix that makes something unwritable can silently break
any later fix that depends on writing it**, and neither change looks wrong on
its own.

## A defect no test suite would have found

Both models were asked only to name a failure mode and a minimum fix. Both
answered well, and both filled in a "disagreements" field arguing against
exponential backoff, which nobody had proposed.

The cause was two of my own decisions interacting. The instructions told the
model that "a consultation that only agrees is worthless", and the response
format made the disagreement field mandatory. Between them, the model was
cornered into producing a disagreement whether or not one existed.

For a tool whose entire purpose is honest second opinions, a manufactured
objection is the worst possible output: it is exactly what someone would act on.

The fix scopes disagreement to a position the caller actually stated, names the
empty case, forbids arguing against a position nobody took, and says plainly
that agreeing is a legitimate answer. Verified in both directions with two
prompts: one that states no position, which must now produce an empty list, and
one that states a wrong position and asks for confirmation, which must still
produce a real disagreement.

That second test is the important one. **Over-suppression would have produced
empty lists too, and would have looked like a success while destroying the point
of the tool.**

This survived three review rounds and 308 automated tests. It was only visible
when a real model answered a real question and a person read the answer
critically.

## The tests that were certifying bugs

All found by the reviewer.

- One required that a process which had *lost* a conversation could still modify
  it. That was the bug, written down as the expected result.
- One checked that bad output was quarantined, and passed because unrelated
  bytes happened to be present. The actual bad content was never saved.
- One proved a dangerous path was refused, using an input that was rejected
  earlier for a different reason, so the dangerous path was never exercised.
- Two asserted behaviour that a later fix deliberately reversed, and had not been
  updated.

What helps: assert on the **reason** something happened, not only the outcome.
One patch in this project silently failed to apply, leaving safe behaviour with a
misleading explanation. Only a reason-string assertion caught it.

## The external review, and the bug the whole suite was blind to

A fifth round, by an outside reviewer reading the published repository rather
than by either model. It found one genuine high-severity defect that four
adversarial rounds and 367 tests had all missed.

**The worker never loaded the machine-local config.** Configuration is layered:
committed defaults, then a gitignored local file holding your pinned CLI paths
and versions. That layering happens only when no explicit config path is given,
deliberately, so tests stay deterministic. But the broker handed the worker its
own config path, which in a normal install *is* the default path. So the worker
re-loaded the committed defaults and silently lost the overlay.

The consequences were exactly backwards from the guarantee in the README. The
worker saw no pinned executable, so it fell back to searching the PATH, which
is the very situation the setup command exists to prevent. And it saw an empty
version allow-list, so its per-job version check verified nothing. The binary
that ran could differ from the binary admission had validated.

**Why no test caught it:** every test sandbox passes an explicit config path, so
the layered path was never exercised end to end. The suite was thorough about
behaviour and blind to a code path it structurally never entered. A test suite
cannot find a bug in a branch it never takes, and 392 green checks say nothing
about the branch nobody wrote a test for.

The fix takes the reviewer's suggestion, which improves auditability as well as
correctness: each job snapshots the *merged* configuration into its own
directory, and the worker is pointed at that snapshot. The job then provably
runs the exact configuration it was admitted under, and the snapshot is hashed
into provenance rather than being a path whose contents may since have changed.

The same round also found a corrective retry that, in one narrow case, would
open a fresh peer session whose entire prompt was "your previous reply did not
satisfy the contract", about a conversation that peer had never seen; a
retention setting that implied a rotation which never happened; an error
category taxonomy with an unstated remainder, now enforced at import; a
contamination refusal that named no file, so a stray ~/AGENTS.md would fail
every consultation with a message identifying nothing; and an admission scan
whose cost grew with retention rather than with concurrency.

The transferable lesson is the first one. **Ask which code paths your tests
structurally cannot reach**, and treat those as unreviewed no matter how many
tests pass.

## Things accepted rather than fixed

Named as accepted risks, not solved problems. The reviewer confirmed they do not
change its final verdict.

- The consulted Codex process can read the filesystem. Its sandbox restricts
  writing. The instruction not to read is a rule, not a boundary.
- Codex's own built-in skills are present in the isolated directory and could not
  be disabled in the version this was built against. Inventoried per job, not
  proven inert.
- A process that deliberately detaches itself survives being killed.
- A conversation interrupted mid-call is held indefinitely until a person looks
  at it. There is no timeout by design, because a timeout would convert a known
  unknown into a silent assumption.

## Method notes

The fourth round was conducted **through the bridge itself**, one continued
conversation, seven consultations. It found the collision that three earlier
rounds and 308 tests had missed.

Rounds worked better when the request named the specific claims to attack and
gave the reviewer permission to say a section was clean. Asking "review this"
produces findings whether or not any exist; asking "here is the claim, here is
the code, try to falsify it, and tell me plainly if you cannot" produces
verdicts. Two of the most useful answers received were "correct" and "I cannot
identify one".

## Independently reported, outside the four rounds

Three defects in commit `80effae` were reported independently by **Charlie**,
and reproduced against the baseline before any of them was fixed. The repository
has no release-notes file with a contributor section, so the credit is recorded
here.

All three share one shape: something that looked like a check was not checking
what its name claimed.

1. **A failing verification exited zero.** Both harness CLIs print their
   receipt and `return 0` whenever `run_task` returns normally, and `run_task`
   returns normally when the generated patch applied cleanly but the
   verification commands failed. The receipt said `verification_failed`; the
   process said success; the execution queue keyed on the exit code alone and
   recorded the job `complete`. Fixed on both sides: the CLIs now exit nonzero
   with `ok:false` unless the receipt status is `complete`, and the queue now
   requires the harness's own verdict as well as a zero exit
   (`outcome_is_success`). Either check alone is insufficient, so both are
   applied.

2. **The opt-in verification drained the production queue.**
   `delegation_verify` built an `ExecutionQueue` on the *configured* queue root
   and called `run_once` in a loop. `run_once` claims the oldest queued job in
   that root, not the job just submitted, so verifying the delegation opt-in
   would execute whatever real work the user had waiting, make a live provider
   call for it, and consume its receipt. Each synthetic direction now gets a
   temporary queue root that is created and destroyed inside the call. The
   local-model check had the same shape (`service.once()` runs whatever is
   queued) and got the same treatment.

3. **Source-integrity comparison could not see content changes.** Integrity was
   `git status --porcelain=v2` text before and after. For a modified file that
   output carries the HEAD and index hashes, never the worktree content, and
   untracked files appear by name only. So a task that overwrote a file which
   was *already* dirty, or rewrote an untracked one, left the status output
   byte-identical and passed. The snapshot now hashes content, mode and link
   targets for every path git reports as not clean. It is bounded, and exceeding
   the bound is a refusal to run rather than a smaller snapshot, because a
   snapshot that skipped files would report integrity it never checked.

The regression tests mutate an already-modified file and assert that the status
text is unchanged before asserting the snapshot differs, so the test would fail
if the fixture ever stopped exercising the actual gap.

## The nine blockers, after the three above were fixed

Codex reviewed the fixed branch and refused the merge again, with nine further
findings. They are recorded here because most of them are the same class of
error as Charlie's: a check whose name promised more than it did.

1. **Two executors, two contracts.** `ExecutionQueue` required
   `returncode == 0`, `harness_ok`, and `harness_status == "complete"`.
   `WindowsWslExecutor` returned none of those and used `status="completed"`.
   Every Windows job would have been judged by keys it never set. There is now
   one vocabulary (`complete`, `verification_failed`, `failed`, `aborted`), one
   validator (`validate_outcome`) applied in `run_once`, and a real
   queue-to-executor regression proving success becomes `complete` and a
   semantic failure becomes `failed`. A contract violation is now a failed job
   with a named error, not a state decided by which keys happened to exist.

2. **The content snapshot could block or be swapped.** It now refuses every
   non-regular file, opens with `O_NOFOLLOW|O_NONBLOCK`, `fstat`s the
   descriptor it actually got, counts bytes per chunk against a per-file and a
   cumulative cap, and re-checks identity at the end. Growth, replacement and
   FIFOs are refusals. Testing this established that git never enumerates
   FIFOs or sockets as untracked at all, so the real exposure was a swap
   between enumeration and open, which is exactly what the descriptor rechecks
   cover.

3. **The Windows executor translated a request nobody submits.** Queue entries
   carry provider, repo, brief, base and verify_argv, not tool/args/stdin. The
   executor now packs the admitted workspace deterministically into the bounded
   archive protocol and translates the real stored request. The tool-shaped
   entry point survives only for live boundary verification.

4. **The provider session had no host-side source.** `windows_auth` enrols one
   token per provider into DPAPI under the current user, with recorded consent,
   plain-language enrolment state, and revocation. Nothing reads `~/.claude`,
   `~/.codex` or any credential directory; nothing writes a token to disk in
   the clear; there is no fallback when OS protection is unavailable, because
   the only thing to fall back to is a plaintext token behind a file mode.

5. **There was no driver.** `windows_provision_driver` walks observe, consent,
   elevate, restart-and-resume, WSL install/update, provenance-verified image
   install, live boundary verification, machine-bound evidence, and per-user
   worker activation. `delegation_verify` now selects `WindowsWslExecutor` on
   Windows, so verification exercises the executor production uses.

6. **ACLs were enforced too late.** Queue root and job directory are now
   verified owner-only at creation and at submission, before any request
   content is written.

7. **The artifact workflow was unreleasable and did not say so.** It still is,
   and now says so in code: `RELEASE_TRUST_ANCHORS` is empty, so
   `verify_manifest_trust` refuses every manifest and `release_blockers()`
   returns the reason. Downloads are pinned against values committed here
   beforehand rather than a checksum file fetched from the same origin.

8. **A ruleset canary is not a reachability proof.** Reading back nftables
   rules shows the rules loaded. It shows nothing about whether a destination
   is reachable. A second canary now attempts a TCP connect to each protected
   range and to the guest's own default gateway, and a refused connection
   counts as reachable. Public provider egress is documented as deliberately
   allowed.

9. **The evidence gate could be written unprotected.** ACL enforcement on the
   record is mandatory, the write is atomic and never truncates the previous
   record, and every load verifies owner-only ACLs, regular-file status, no
   reparse ancestors and stable identity before the record is trusted.

## The credential store the execution lane was sharing

Integrated from `f1c731a` rather than cherry-picked, and hardened. The lane
had no `CLAUDE_CONFIG_DIR` and signed in against `~/.claude`, the store the
desktop app and interactive sessions also refresh; a concurrent invalid-grant
cleanup there blanks the tokens and the lane reports a lost login.

Setting a different directory is not sufficient, so `claude_config` allows
exactly one: `~/.agent-bridge/claude-home`. It refuses the shared store by
name, refuses any link, alias or reparse path that resolves to it, refuses an
arbitrary configured directory, and enforces then verifies owner-only
permissions on the directory and its contents before any authentication uses
it. `claude_config_dir_ready` is reported separately from `execution_complete`,
because the files ship with the checkout and the login does not.

The selector is never exposed to verification subprocesses: it is a path to a
live subscription session, and project code under test has no use for one.

## The seven release blockers, from the round after the nine

The external reviewer came back and the review was not clean. Seven findings,
and the common thread in five of them is the same shape: a check that looked
like a check and proved nothing.

### A version string was standing in for authentication

`observe_provider_lane` opened the provider lane on the strength of running
`claude --version` and `codex --version`. Both print without a session. The
gate that decides whether a long-lived subscription token can be handed into an
ephemeral guest was satisfied by a string that prints on a machine where no
token works at all.

The replacement is an authenticated turn. `guest_runner` gained a third request
mode, `auth_probe`, running one fixed minimal prompt through the exact guest,
runtime and auth-capsule path a real job uses, and requiring provider-specific
semantic success: for Claude a JSON result object with `subtype: success`,
`is_error: false`, a `usage` block and a fixed sentinel; for Codex the same
sentinel in the `--output-last-message` file. `API_KEY_ENV_KEYS` is checked
after the capsule contributes, so a probe that would have passed on a billed
API key is refused as `api_key_present` rather than counted as subscription
auth. The response is a verdict token with `stdout` and `stderr`
unconditionally empty: classification happens inside the guest, so no provider
output is ever recorded.

The lane needs two observations, not one. Portability is the authenticated
turn. Refresh behaviour is the same probe with a deliberately worthless capsule,
which must come back **rejected** rather than merely failing. That second gate
is what makes a version-only false positive structurally impossible: a code path
that says yes to anything has to reject a token it just accepted, and cannot.

Regressions cover rejected tokens, nonexistent tokens, version-only output, an
empty response, `is_error: true`, a missing sentinel, a missing `usage` block,
an API key in the environment, and the absence of captured bytes.

### The lane had no caller, and could not have had one

`record_boundary` stored `ProviderLane` closed, `observe_provider_lane` was
never invoked by anything, and `verified_executor` could not bootstrap itself:
the executor refuses provider jobs until the lane is open, and the lane could
only be opened by running a provider through the executor. Written down plainly
it is a deadlock, and it had been sitting behind a function nobody called.

The knot is cut by moving the observation out of the executor and into the
driver, as a rung of its own. `STAGE_PROVIDER_ENROLMENT` sits between
`STAGE_BOUNDARY_VERIFICATION` and `STAGE_READY`, so the probe runs inside a
guest whose containment was verified live moments earlier, with a present user
who consents to it in its own right. What justifies handing a session in before
the lane is open is not trust, it is that the boundary was just proven and the
probe is one fixed no-op.

The evidence then has to survive being written. `record_provider_lane` amends
the machine-bound record as a read-modify-write pinned with `expect_identity`,
and `_reload_lane` reads it back off disk and compares before anything is
enabled: a lane that does not survive the round trip reports
`provider_lane_not_durable` or `provider_lane_readback_mismatch`. Nothing is
enabled on the strength of a value still in memory.

### Resume was a note, not a state machine

The record could be written with `secure=None`, truncated before it was
protected, and left behind when the task registration failed. The task itself
could be created over whatever already held the name, and the command it ran
was not validated.

It is now owned end to end: protected atomic create with the directory verified
first and no unprotected window, identity-pinned updates, record written before
task so the unrecoverable ordering never occurs, ownership proven by comparing
the registered command before `schtasks /F` takes a name, bounded attempts
(`MAX_STAGE_ATTEMPTS`, 3) that refuse to schedule a fourth, continuation from
the recorded stage after login, and removal of both record and task only at a
terminal condition (`ready`, or stopped for a reason a reboot cannot change),
reported as `still_provisioning` otherwise. A malformed record is reported
rather than read as "nothing to resume", because tampered-with and
never-started are different facts.

### A digest match was being accepted as trust

`verify_manifest_trust` would return trusted for an anchor whose
`manifest_sha256` matched, with an empty signature field, on the reasoning that
the standard library has no signature primitive and an unchecked signature would
be worse. Both halves of that were true and the conclusion was still wrong: the
digest proves the manifest is the one the anchor names, and says nothing about
who wrote the anchor.

`orchestration/signing.py` is the answer: Ed25519 per RFC 8032 §6 in pure
standard library, verification only, signing confined to the tests because
nothing shipped has any business holding a release key. Non-canonical `S` is
rejected rather than accepted as malleable, and it is validated against the
RFC's own vectors. A missing signature, a missing public key, an invalid
signature and a raising verifier are four distinct named refusals, and the old
`signature_unverified` outcome is gone. "The standard library cannot do this" is
a claim worth checking before it becomes a design.

### The checked file was not the read file

`windows_auth` and `windows_evidence` verified a path and then reopened it,
which makes the verification decorative: between the two calls the name can
point somewhere else.

`read_private_file` now opens once and does everything through that descriptor,
including a second `fstat` compared to the first so a file that moved underneath
fails as `file_changed_while_reading`, with `GetFinalPathNameByHandleW` on
Windows rather than trusting the name. Writes take `expect_identity` so a
read-modify-write cannot discard somebody else's write. `enrol` rolls the secret
back when the record write fails, because two files for one logical change with
no atomic replace across paths is a half-applied state waiting to happen.

The self-inflicted version of this bug is worth recording: the first
implementation called `enforce_owner_only_file` on the read path, which on POSIX
`fchmod`s to `0600`. It would have silently repaired a world-readable evidence
record and then accepted it, erasing the evidence that anyone could have read
it. A test caught it. Reads verify and never repair; enforcement is a write-path
concern.

On zeroization the honest statement is now in the code rather than implied
around it. Python cannot guarantee a plaintext token is erased from process
memory. `MEMORY_LIFETIME_NOTE` says so. What is bounded is lifetime and spread,
and that is what is claimed.

### Junctions are not symlinks

Host workspace packing relied on `O_NOFOLLOW`, which does not exist on Windows,
and on `is_symlink()`, which is false for a directory junction. Both of the
mechanisms an attacker would actually use on the target platform were
unguarded.

Packing now refuses any entry with a nonzero `st_reparse_tag` as well as
anything `is_symlink()` reports, checks the repository root the same way, binds
each file's approving `lstat` to the `fstat` of the descriptor it will read by
`(st_dev, st_ino)`, and requires every resolved candidate to stay under the
resolved root. Refusals are counted in the result rather than passing silently.

The simulation boundary is stated in the test file rather than left for a reader
to discover: the junction tests prove the packer refuses the signal Windows
would give, not that Windows gives it.

### Keep the probes, measure them

The two network canaries are the slow ones, and the reviewer's point was to keep
them rather than optimise them away before anyone has seen them run.
`_run_canaries` now times each canary individually and records the duration
before the verdict is known, so the egress probes' cost appears separately in the
receipt. Whether they are worth it becomes a question with a measurement behind
it, answered after live evidence and not before.

### What is still not true

The lane is connected end to end in code: probe, observation, atomic
machine-bound persistence, re-read from disk, gated rung, activation. It has
never been run against a real subscription on a real Windows host. The image
step still refuses by design, because `RELEASE_TRUST_ANCHORS` is empty and both
shipped recipes are unfilled stubs. Neither of those is a test failure, and
neither is fixed by another round of review.

## Independent public-release review

Charlie independently reported three defects in public commit `80effae`: a
failed harness verification could be recorded as successful, synthetic
verification could consume unrelated queued work, and source-integrity checks
could miss changes to files that were already dirty. Each report was
reproduced and fixed with a regression test. The queue now requires both a
zero process exit and a `complete` harness receipt; all synthetic checks use
disposable queues; and dirty-file contents are included in bounded integrity
snapshots.

Public commit `4287429` carries those fixes. This Windows branch had reached
the same three conclusions independently, from a different direction, so
integrating it was mostly a matter of confirming that, and the confirmation is
`tests/test_public_hotfix.py`: the public regression suite, copied here
unmodified, passing against this tree's own implementations. Where the two
differ, this tree is the stricter one and public main's tests still hold:

* The harness gate here adds a closed `HARNESS_STATUSES` vocabulary and an
  `OUTCOME_REQUIRED_KEYS` validation, so an executor that omits the verdict
  fields is refused rather than defaulting to failure.
* The content snapshot here re-checks the descriptor's type after opening,
  sets the descriptor blocking after an `O_NONBLOCK` open, and folds the byte
  count into the digest.
* Verification isolation here is a named `_verification_queue_root` rather
  than an inline `mkdtemp`, and the Windows lane uses the same
  `select_executor` production uses, so verification cannot pass against an
  executor nobody runs.

One thing did not merge, and it is the one that mattered: public main refuses
automatic delegation on any platform that is not macOS, and this branch is the
Windows lane. Deleting the refusal to make room for the work would have been
the wrong direction, so the refusal stayed and became computed.
`onboard.delegation_platform_blocker` asks whether this machine carries a
boundary-verification record. No machine does, and none can until a signed
release manifest exists and a real Windows host passes verification, so the
observable behaviour is identical to public main's: refused, everywhere but
macOS. The difference is that the gate now states a fact about the machine
instead of a platform name somebody would have to remember to delete, and it
opens when the lane is genuinely proven rather than when someone edits a
string.

## The eleven blockers, from the round after the seven

Not clean again. Two of these are the kind that make a feature structurally
impossible rather than merely weak, and one of them is a forgery.

### A verifier that accepted a signature nobody signed

`signing.verify` accepted `A = R = <identity point>, S = 0`. With `A` the
identity the verification equation collapses to `[S]B == R`; the message never
enters the arithmetic, so one 64-byte constant verifies against every message,
with no private key. The order-2 point does the same. This is the small-order
forgery, and the module was checked against the RFC's vectors and passed all of
them while accepting it, which is a useful demonstration that passing the
published positive vectors is not coverage.

The instruction was to prefer a vetted platform verifier. That was measured
rather than assumed, and the measurement went the other way: OpenSSL, via
`cryptography` 50.0.0, accepts the same forgery and the order-2 variant. Both
were run. The library would have inherited the bug, and it is not present in
the guest image or a stock Windows Python anyway, so the fallback path would be
a trust check an attacker removes by uninstalling a package.

The fix is `_is_small_order`, testing `[8]P == identity`, applied to the public
key and the commitment. The whole torsion subgroup rather than the two
currently exploitable points, because the exploitable set depends on the shape
of the equation and the equation is easier to change than this file is to
re-audit. `VERIFICATION_COVERAGE` now states the coverage in words instead of
letting the presence of an RFC number imply an audit, and a test asserts that
the note does not claim more than is proven.

### A provider lane that could not be opened

`_execute_auth_probe` deleted the Codex last-message file before
`_codex_probe_verdict` read it. A correctly authenticated Codex session could
only ever return `no_sentinel`. The lane was not weak, it was shut.

It survived because the tests covered `_codex_probe_verdict` and not
`_execute_auth_probe`. Helper-only coverage proves the helper; the ordering bug
lived in the caller. The answer is now read through a protected descriptor and
the verdict computed from bytes rather than a pathname, so the cleanup cannot
be reordered back in front of the read, and the tests drive the full
`_execute_auth_probe` lifecycle through a capsule double.

### A driver with no caller

`windows_provision_driver.step` wrote a bare resume record and never called
`schedule_resume`, so a rebooting stage registered no logon task. Nothing would
ever have read the record. And `windows_provision_driver` had no production
entrypoint at all, which is why that was never noticed: the reboot path could
not run, because nothing ran the driver.

`agent_bridge.windows_setup` is the command, reachable as
`bin\agent-bridge-windows-setup`, `setup_bridge.py windows-setup` or
`python -m`. `step` now calls `schedule_resume`, and a rebooting stage with no
resume command is refused as `resume_command_missing` before the restart rather
than taken with no way back. Before scheduling, setup installs an owner-only,
self-contained `agent-bridge-resume.pyz` below the runtime root. The task runs
that stable copy directly, so it depends on neither the repository checkout nor
a `PYTHONPATH` surviving the restart. Consents do not survive one either: a
resumed stage that needs one stops for a person, because an unattended task
elevating on a consent given before the reboot is a standing grant nobody
re-affirmed.

### Rollback against a pathname

Enrolment's rollback re-opened a name and wrote to it. Two concurrent
enrolments could leave the secret from one beside the record from the other,
and a rollback could overwrite an enrolment newer than the one it was undoing.

Three things now hold it: an advisory exclusive lock on an open handle over the
auth root, held across both writes and released by the kernel if the process
dies; the previous ciphertext captured through a descriptor-bound verified
read; and `expect_identity` on both the forward write and the rollback, so a
rollback whose pin no longer matches writes nothing and surfaces as
`enrolment_rollback_incomplete`. The lock is the ordinary defence and the pin
is the independent one, because a writer that never took the lock is exactly
what a lock cannot see.

### Bounds applied after the damage

`unpack_workspace` called `getmembers()`, which walks the entire archive's
metadata into memory before any limit applies, and then extracted. `capture_diff`
ran git with `stdout=PIPE` and checked the size afterwards, so an oversized
diff was already in memory before anything objected.

Both now bound before rather than after. Unpacking streams and claims every
budget ahead of each write: member count, per-member and cumulative bytes,
expansion ratio, path length and depth, case-folded duplicates, and a refusal
of every link, device and special file. Files are written `O_EXCL | O_NOFOLLOW`
with the measured byte count beating the header's claim. The diff streams
through `_bounded_git`, reusing the job runner's bounded reader and
process-group teardown, with `diff_too_large` and `diff_timed_out` as named
outcomes.

The expansion ratio is worth recording as a design correction rather than a
choice. 100 refused a legitimate 290:1 lockfile. 1000 is above deflate's
measured single-stream ceiling of roughly 1028:1, which makes it decorative.
500 sits between two measurements, both of which are asserted in the tests.

### Verification ran with the internet

The POSIX lane runs verification commands under `(deny network*)`. The Windows
lane ran them with the provider's public egress still open. Verification
commands execute repository code chosen by the brief, which is the one place in
the job where arbitrary code runs with something worth exfiltrating nearby.

`enforce_verification_egress` now installs a second nftables table with a
`policy drop` output chain, reads the ruleset back out of the kernel, compares
it to the pinned expectation, and probes public targets. Any failure aborts the
job rather than warning. Two tables rather than editing one, because nftables
runs every chain on a hook, so restoring provider egress afterwards is removing
a table rather than reconstructing one. The provider phase keeps its egress on
purpose; the tests do not.

### Two facts about one pathname

The queue admitted a brief by hash, and translation read the brief by name.
Between those, the name could point somewhere else. `read_brief` now takes the
admitted digest, opens once, `fstat`s for type and size, reads from the
descriptor, `fstat`s again to catch a swap, and compares the digest in constant
time. There is no second unchecked read of the name.

`request['base']` was ignored outright. A receipt recording a diff without
saying what it was a diff against cannot be checked. `resolve_base` resolves it
through git, requires `HEAD` to match, refuses by name (`base_missing`,
`base_invalid`, `base_unresolvable`, `base_mismatch`), and the resolved SHA is
recorded on the outcome.

### Three budgets that disagreed

The runtime allowed 1 MiB by default and 4 MiB hard for input, the packer
permitted 32 MiB compressed before base64 expansion, and the response limits
exceeded the outer cap. Three layers, three answers, and the smallest one is
the one that actually applies, which means the other two were describing a
system that does not exist.

There is now one derived budget in `guest_runner`, with the arithmetic written
out, and `windows_wsl_runtime` asserts against it at import so a change to one
layer that the others do not follow fails to load rather than failing in
production. The caps are measured rather than maximal, and the tests exercise
just below and just above each cross-layer boundary.

### What is still not true, after eleven more

The production command exists and is tested end to end as a command, with the
OS supplied. It has not been run on Windows. No image has been imported, no
canary has run in a real guest, no provider lane has been observed against a
real subscription, and `RELEASE_TRUST_ANCHORS` is still empty, so the image
step still refuses by design. The hardened verifier is stronger than the
library alternative that was measured against it, and it is still not an
audited implementation, which is stated in the module rather than left to be
inferred.

## The consolidation round

A round with no external reviewer. The work was to make the change reviewable,
close the gaps that could be closed without a Windows machine, and be exact
about the ones that cannot be.

### Public main's platform refusal, kept and made true

Public main `4287429` refuses automatic delegation on any platform but macOS.
This branch is the Windows lane, so the obvious integration was to delete the
refusal to make room for the work. That would have been weakening a trust
check to accommodate a feature, which is the move this project keeps refusing
elsewhere.

The refusal stayed. What changed is what it is derived from: no longer a
hard-coded platform name, but whether this machine carries a boundary
verification record. Today no machine can carry one, so the observable
behaviour is identical to public main, refused everywhere but macOS. The
difference is that the gate now states a fact about the machine, and opens
when the lane is genuinely proven rather than when somebody edits a constant.

The gate reads the durable record and spawns nothing. An earlier draft routed
it through the preflight sweep, which meant onboarding ran a subprocess ladder
in order to decide whether to ask a question.

`tests/test_public_hotfix.py` is public main's own regression file, copied
unmodified. It passes against this tree, which is the evidence that the
integration preserved the fixes rather than the claim that it did.

### One definition of success, and a test that keeps it that way

`outcome_is_success` existed and most paths called it. `delegation._direction_status`
did not: it judged a direction by `returncode == 0`, which is a fact about a
process, and the evidence row it read carried no harness verdict at all. So a
supplied `--delegation-results` file could enable a direction on the strength
of a zero exit, and no audit of that file could have caught it, because the
information needed to catch it was not in the file.

The row now carries `harness_ok`, `harness_status` and `harness_verdict` end
to end, the gate calls the one function, and `VERIFICATION_PROFILE` is
`automatic-delegation-v2` so evidence written under the weaker rule is refused
by name rather than graded against a rule it was never produced under.

`tests/test_success_gate_unity.py` asserts the property rather than the
instances: it reads the source of every execution module and requires that
only `outcome_is_success` compares an outcome's exit code to zero. One
exemption is listed by name with its reason, because an unexplained exemption
is how a re-derived rule gets back in.

### Three Windows paths that are not the file they spell

Found by writing the tests, not by review. `_validate_windows_host_path`
accepted all three:

* `C:\agent-bridge\NUL`, and the same for every DOS device name, in any
  directory, with or without an extension. Win32 resolves it as a device. A
  record written there is accepted and gone, which makes an unwritten record
  indistinguishable from a written one.
* `C:\agent-bridge\record.json:hidden`, an alternate data stream. Writing one
  leaves the visible file untouched, and a hash or an ACL taken on the base
  name describes something else.
* `C:\agent-bridge\record.json ` with a trailing space, or a trailing dot.
  Win32 strips both, so two strings that compare unequal are one file. Every
  identity check in this project is a string comparison somewhere.

All three are refused now, and the over-refusal was checked too: `NULL`,
`CONFIG`, `COM10` and `console` are ordinary names and still pass, because a
check people work around is not a check.

### Offline fixtures for a lane that had only been mocked

The guest provider lane was tested through a seam that returns a hand-written
`(returncode, stdout, stderr)` triple. That is the right way to test the
decisions and it is not a test of the lane: the triple never came from a
process, so the real subprocess path, the Codex answer file, and the child
environment were all outside it.

`tests/fakes/fake_guest_claude.py` and `fake_guest_codex.py` are provider CLIs
that really run, emitting the shapes each provider actually produces. The lane
now runs end to end against them with no runner seam at all. The mode is
passed on argv rather than in the environment, because the guest runner
rebuilds the child environment from a fixed allowlist and would have stripped
it, which would have left every test silently running the default.

The fixture that matters most is the Codex mode that writes no answer file
while the sentinel is still in the stream. A stdout-scraping probe passes
there. This one must not, and now there is a test that says so.

### A validation runner that cannot flatter a machine

`agent-bridge-windows-setup validate` runs a fixed, ordered suite and prints
one JSON object. The verdict is computed from the suite rather than from the
rows, so a report missing a row entirely is not ready either. Timings and
timestamps live in a separate envelope the verdict is not computed from, so
two runs on an unchanged machine produce byte-identical checks and a diff of
two reports is a diff of what changed about the machine.

Three ways a suite like this stops being trustworthy are closed by
construction. A check with no runner is `blocked`, never skipped silently. A
check that raises or returns the wrong shape is a failure, and the exception
text is discarded rather than carried into a report people paste into issues.
And `blocked` and `skipped` are not passes, so a report full of skips cannot
be mistaken for a machine that worked.

It writes nothing. The durable record stays with `windows_evidence`, which
refuses to be written anywhere but on Windows and binds itself to the machine
and the artefacts. A green report is something a person reads; keeping the two
apart is what stops a green report from enabling delegation on its own.

### A signed image fixture, and a signing procedure with no key in it

Every link of the image trust chain had a test. The chain did not.
`tools/make_test_image.py` builds a complete one offline in about a second:
rootfs tarball, manifest, canonical bytes, digest, signature, anchor, verified
through the project's own verifier. Two builds are byte-identical, and
`--check` proves that rather than asserting it.

Its key is derived from a constant published in that file, and is therefore
worthless. That is the only safe way to ship a signed fixture: one signed by a
key that had to stay secret would either commit the secret or stop working.
The release tool refuses that key by name, and a test asserts it never appears
among the shipped anchors.

`tools/release_signing.py` never reads, writes, derives or accepts a private
key. The shipped package cannot sign at all. The tool emits the exact
canonical bytes to sign, the signature is produced elsewhere by whoever holds
the key, and the tool verifies it before printing an anchor. It refuses a file
that is a key by name or by content, and its `scan` subcommand checks the
whole working tree and runs in CI.

### What is still not true, after this round

No step of this has run on Windows. No image has been imported, no canary has
run in a real guest, no provider lane has been observed against a real
subscription, and no release has been signed, so `RELEASE_TRUST_ANCHORS` is
still empty and the image step still refuses by design.

The validation runner has never produced a `ready` verdict, and cannot, on any
machine in this project's reach. Its `platform` check fails first everywhere
else, by design.

The Windows path, locking, line-ending and process tests assert real Win32
semantics, and three of them are skipped off Windows because they need the
real namespace to say anything. The rest are pure and run on every runner,
which is the point: a Windows-only test that runs on one runner is a test that
stops being read.

## The slicing round

Consolidating the change into reviewable commits meant running the whole
suite at each one. That is not a formality here. It found five places where a
test landed in an earlier commit than the code it exercises, and one real
defect that only a rerun could have caught.

### Five commits that could not have passed

The slices were ordered by the module import graph, which is the right
ordering for reading and the wrong one for a test that spans two modules.
Every one of these was invisible at the tip, because the tip has everything:

- The guest harness taught `guest_runner` two new canary names while the host
  side of the agreement, `windows_wsl_runtime.CANARY_ORDER`, learned them six
  commits later. The conformance test that asserts the two lists are equal
  failed in between. The 74-line runtime hunk moved back to the harness
  commit, where it already belonged: it imports `guest_runner` and asserts
  against that module's transport constants.
- `QueuePrivacyAtCreationTests` tested the queue's use of the privacy module
  from the commit that introduced the module, not the commit that wired it in.
- `tests/test_windows_auth.py` imported `windows_delegation` one commit before
  it existed. That commit did not fail, it failed to *collect*: the suite
  reported one error and ran nothing at all, which is the failure mode most
  likely to be waved through.
- Two methods of `OutcomeTests` called `execution_queue.validate_outcome` from
  the runtime commit, a commit before the gate that defines it.
- Twenty-four onboarding tests asserted an `onboard.py` shape that arrives
  with the platform-gate commit.

Nothing about the change moved: the tree at the tip is byte-identical before
and after the reordering. What moved is which commit carries which line, so
that a reviewer reading one commit sees a set of tests that pass against the
code in front of them.

### A hundred tests that were not running

Seven test files had grown a class or a function below their
`if __name__ == "__main__":` block. Under pytest the module is imported whole
and every class runs, so CI was green and stayed green. Running one of those
files directly executed `unittest.main()` at the point it was written and
exited before the later definitions existed: 26 of 45 in `test_auth_probe`,
10 of 20 in `test_signing`, 28 of 41 in `test_windows_privacy`, 46 of 67 in
`test_windows_provision_driver`, 45 of 57 in `test_windows_setup`, 127 of 152
in `test_windows_wsl_runtime`.

`tests/test_suite_hygiene.py` parses every test file and fails if anything is
defined after the block. It has to be a source check. At runtime the classes
that would be skipped have either already been collected or the process has
already gone, so nothing executing can observe the problem.

### A rejection marker that matched three digits

One test failed intermittently across the slice runs and passed on every
rerun, which is the shape of a problem that gets recorded as "flaky" and
forgotten. Capturing the failure name rather than the count gave it away:

```
AssertionError: 'auth_probe_rejected' != 'auth_probe_failed'
```

`AUTH_REJECTION_MARKERS` carried a bare `"401"`, substring-matched against the
whole of stdout and stderr. Claude Code's result envelope carries a random
`session_id`, and a UUID contains those three digits about once in a hundred
and forty. On a live machine a request id, a duration in milliseconds, a token
count, a cost or a line number in a traceback does the same thing.

Neither verdict opens the lane, so this was never a trust hole. It is a
correctness one, and it matters most exactly where the runbook says the
verdict is the whole result an operator gets: a spurious `auth_probe_rejected`
sends someone to re-authenticate a session that was never refused, and the
real fault goes unlooked-for.

Status codes now match through `AUTH_REJECTION_PATTERNS`, which require the
number to be presented as a status code: followed by the word that names it,
or introduced by one of `http`, `status`, `code` or `error` within ten
characters on the same line. The gap admits digits because a real status line
reads `HTTP/1.1 401`. `403 Forbidden` was always a phrase and is unchanged,
and a test now asserts that no marker in the list is a bare number.

The regression test is the exact envelope that failed, with the session id
pinned at a value carrying the digits. Beside it is the property that envelope
is an instance of: ten thousand random session ids, none of which may change a
verdict. A fixed envelope proves one id is handled; the property proves the id
cannot be what decides.

### What is still not true, after this round

Every slice now passes the full suite on this machine, and the tip passes it.
None of that is evidence about Windows. The five orderings were found by
running the suite, the hundred skipped tests were found by moving a block, and
the rejection marker was found by a rerun; all three are the kind of thing
that is only ever found by running something, which is the argument for the
runbook rather than a substitute for it.

## Round five: the automatic delegation work, 2026-09-15

Three defects found by running the thing rather than by reading it. Recorded
here because the pattern transfers: each one passed every test that existed,
and each one was found within minutes of driving the real workflow end to end.

41. **Both execution lanes could only run on one macOS layout, and the only
    tests that would have caught it never ran anywhere it was broken.**
    `GIT_BIN` was the standalone Command Line Tools path and `_assert_macos()`
    refused every other host, so the first git call spawned a file that does
    not exist and the lane reported `TaskError: command spawn failed`. CI ran
    `test_claude_task.py` and `test_codex_task.py` on macOS only, precisely
    because they could not pass elsewhere. A platform-conditional test step is
    a place defects go to live. Both modules now run on macOS and Linux and
    assert what the *selected* confinement backend claims rather than macOS
    semantics everywhere.

42. **A completed stage bricked its repository.** The automatic decision used
    one stage name per repository, so the first ordinary `stage_complete` left
    every later edit denied with `stage_not_owned` and an instruction to claim
    a stage by hand. The gate was working exactly as written and the result
    was the opposite of the feature. Stages carry a generation now.

43. **A plausible-sounding signal produced an unsatisfiable instruction.**
    `infer_task_type` guessed "mechanical" when every target path looked like
    a test file, and routed such edits to the local model. It read well. But
    the local worker processes inline text and returns a draft; it does not
    edit files, so the dispatch intent named a call that could never be made.
    A signal that is cheap to compute is not the same as a signal that means
    something.

44. **Editing the policy looked like it did nothing, and fixing that exposed
    the real bug.** A receipt decided under one policy stayed authoritative
    until it expired, so classifying a repository took effect up to four
    hours later. Recording the policy's fingerprint in each receipt fixed
    that and immediately surfaced worse: the stage router never reassigns an
    owned stage, so when the route changed the old stage was still owned on
    the old route, the receipt was written naming *that* route while the
    decision said the new one, and the gate allowed the edit. A decision to
    delegate had silently become a decision to retain.

    Two lessons, both familiar from earlier rounds. Individually correct
    fixes interact: this one only became visible because another fix started
    exercising a path that had never run. And the invariant that mattered
    ("a receipt never names a route its decision did not choose") was true by
    construction right up until it was not, which is exactly the kind of
    thing to assert rather than reason about. It is asserted now.

45. **Walking the documentation found what reading the code did not.** Both
    of the above came from running the commands `INSTALL.md` tells a user to
    type, in order, against a fresh state, and looking at what actually came
    back. The unit tests were green throughout. A test suite checks the cases
    somebody thought of; the documented workflow is the case the user will
    actually hit.

46. **I called an intermittent failure "load-induced" on two clean runs.**
    Two main-suite checks about orphan processes (`codex->claude` and
    `claude->codex: no orphan processes survive in the killed groups`) failed
    during this work. Two consecutive runs came back clean, so I recorded
    them as load-induced and moved on. Three further runs then produced 2, 4
    and 3 total failures: the checks are **intermittent in this container**,
    not reliably clean, and "two runs passed" did not support the conclusion
    I drew from it.

    Attribution rests on a branch-versus-main comparison instead, which is
    the thing that actually answers the question: the same suite alternated
    between this branch and a pristine worktree of `origin/main`, four rounds
    each, on the same machine. `runner.py`, `broker.py`, `platform/` and
    `backends/` are untouched by this branch, which is a reason to expect no
    difference, not evidence of none.

    The lesson is the one this document already makes about tests that
    certify a defect, pointed the other way: a green run is not evidence that
    a flaky check is benign, and two of them are not evidence either. Count
    the runs before naming a cause.

## Round six: an adversarial review of the automatic delegation work

Commit `58e4d8c` and the four that followed it were reviewed adversarially
and rejected before merge, with two critical defects and four high-impact
gaps. The review's evidence was independent: 544 core tests passed with five
environmental skips and 118 focused gate, routing and audit tests passed, and
none of those passing tests covered any of the defects. That sentence is the
whole round in miniature, so it goes first.

47. **Routing could be escaped one directory down.** Judgment keyed on the
    nearest ancestor holding `.git`. An assistant routed away from a
    repository could therefore run `git init src` and edit freely inside the
    repository it had just created, because the nested root keyed to a
    different receipt and had none. Judgment considers every enclosing
    repository now, and creating a repository counts as a write.

    The escape needed no exotic mechanism and no privileged call. It is the
    kind of thing a test written from the inside does not think to try,
    which is what adversarial review is for.

48. **Confinement that confined nothing much.** Linux verification denied
    the network and then let generated code write anywhere the account
    could reach. The receipt said `sandbox` and the source repository was
    checked afterwards, so the evidence looked complete while proving
    nothing about the rest of the machine. It now runs inside a mount
    namespace and, more to the point, **refuses to run at all** unless a
    write outside the worktree, a write to `$HOME` and a remount of `/` all
    actually fail on this host at that moment.

    Building that boundary produced four defects of its own, each of which
    left a boundary that passed its own tests while restricting nothing:
    sealing `/` before binding the worktree made the worktree unwritable
    (the kernel refuses to remount a bind read-write over a read-only
    source); an inherited working directory left a handle on the sealed
    mount, so relative writes failed and absolute ones succeeded; retaining
    `CAP_SYS_ADMIN` let the payload remount `/` read-write and get zero back;
    and `os.execv` does not search `PATH`, so the verification command never
    ran. The self-check exists because of the third one.

49. **Capacity was evidence in name only.** The MCP tool `capacity_observe`
    let either assistant name the route, the availability, the source string
    and a freshness window of any length. So "a fresh observation from an
    authorized source" meant whatever the model typed, for a route it knew
    nothing about, lasting years, and there was no collector to compare it
    against. The tool is gone; capacity has two writers, neither on the wire;
    a `trusted` column set by the writer rather than read from the row
    decides what routes work, and defaults to untrusted so an installed
    ledger's existing rows stop counting on upgrade.

    The general shape is worth naming: a field that only a trustworthy
    caller would fill in honestly is not a security property, it is a
    convention. The fix was not to validate the field harder. It was to
    remove the caller.

50. **Two fixes for staleness, and the second one taught the first a lesson.**
    Finding 44 recorded the policy fingerprint so an operator's edit took
    effect at once. Capacity had exactly the same four-hour lag and nobody
    noticed, because the first fix looked like it had settled the category.
    A receipt now records a capacity digest too.

    The first attempt at that digest re-decided on every single call, because
    the hook rewrites its own presence row every time it runs, so a digest
    over the table changed every time. The digest is route names only, minus
    the asking client's own presence row: eight re-decisions became three.
    A fingerprint has to cover exactly what the decision depended on, and a
    self-observation is not something the decision depended on.

51. **Withdrawing the operator's declaration did not withdraw it.** The
    replacement for `capacity_observe` is a list in the operator's own
    policy file, replayed into the ledger with a short freshness so that
    deleting a route takes effect promptly. A test asked whether it actually
    did, and it did not: the row the previous replay wrote stayed eligible
    until it aged out, so a deletion meant nothing for fifteen minutes. The
    withdrawal is replayed too, matched on its own source so it cannot
    remove a peer's real first-hand presence.

    I had written the fifteen-minute freshness *as* the withdrawal
    mechanism in the docstring before checking that it was one.

52. **Cleanup keyed on the repository answered for the wrong job.** A
    dispatch intent names a route, item, stage, owner and revision, and
    retiring it was keyed on the repository alone. An assistant holding any
    other owned stage in that repository could dispatch that instead and the
    intent for the stage it was actually refused would be recorded as met.
    The audit's "routed but never dispatched" column is the one thing that
    catches a routing nobody honoured, so a cleanup that clears more than it
    dispatched is the single bug that column cannot survive. Every field of
    the binding is compared now, and a mismatch is recorded rather than
    silent.

53. **Independence was enforced in one direction only.** A review of the
    asking client's own work was correctly sent to the peer. A review of the
    *peer's* work fell through to the ordinary branch, where the peer was
    allowed and had capacity, and went straight back to its author. Half a
    guard reads like a whole one.

54. **Two failing checks were about the account, not the code.** Two suite
    checks prove the bridge fails closed when a directory cannot be listed
    or written, and both create that condition with `chmod`. Running as uid
    0, the condition cannot be created at all: the restriction is a no-op and
    the check reports a failure that has nothing to do with the code, on the
    same line a real regression would use. They measure whether mode bits
    bind this account and skip by name when they do not.

55. **My fix for the staleness gap livelocked the two clients, and its own
    regression test could not have caught it.** Finding 50's digest
    subtracted the asking client's own presence row, on the reasoning that a
    client's own presence is not news to itself. That reasoning is fine and
    the conclusion was wrong, because **a receipt is one shared
    per-repository artifact**. Claude and Codex therefore computed different
    digests from the identical ledger, each found the other's receipt
    overtaken, each re-decided it to route the work to the other, and both
    ended up permanently denied, each holding an instruction to dispatch to
    the other. Worse than the four-hour staleness it replaced.

    The general rule, which I did not have before this: anything compared
    against a shared artifact has to be computed identically by everyone who
    compares it. Route names only already solved the churn the subtraction
    was for, so the subtraction was buying nothing and costing everything.

    The part worth dwelling on is how it survived. The test written to guard
    exactly this drove the Codex hook with `tool="Edit"`. Codex has no `Edit`
    tool, so the gate classified the call as not gated and allowed it without
    reading the receipt. **The test passed while exercising nothing**, and it
    was the only test standing between this defect and a merge. A test that
    asserts the right thing about the wrong call is not a weaker test than
    none; it is worse, because it reports coverage.

    Found by walking `INSTALL.md` by hand again, which is now three for three
    on finding what the suites did not (findings 44, 45 and this one).

56. **Two wrong causes for one intermittent check, and then a third.** The
    orphan-process checks were attributed to machine load (finding 46,
    corrected), then to zombie accounting (finding 46's correction, also
    incomplete). A run after the zombie filter landed reported a survivor
    that was not a zombie.

    The check samples once, half a second after the group kill, and asks
    whether a live process survived. SIGKILL is asynchronous, the kernel
    still has to run the exit path, and a parent still has to reap, so one
    sample at a fixed instant measures scheduling, not containment. It now
    polls until the groups drain or five seconds pass, and reports each
    survivor's actual process state, because a pid on its own does not say
    whether it is running, sleeping uninterruptibly, stopped or already dead,
    and those have different causes. A process that genuinely leaked stays
    forever, so the deadline costs nothing and the old single sample cost
    false failures.

    I have now been wrong about this check three times, each time from a
    message that printed pids and nothing else. The diagnostic was the fix
    that mattered.

57. **The Windows hook command could not have run on most Windows accounts.**
    `hook_command` quoted both paths with `shlex.quote`, which is POSIX
    quoting. It wraps a value containing a space in *single* quotes, and
    `cmd.exe` does not treat single quotes as quoting at all: it would look
    for a program literally named `'C:\Users\First`. So on any Windows
    account whose home contains a space, which is the ordinary shape of a
    Windows home, the installed command was malformed, the hook never ran,
    and nothing said so. A hook that never runs is a gate that never gates.

    Found by static reading while working the review's "live-test the Windows
    launcher and command quoting" item, which I cannot do from here. The
    quoting is fixed and tested as a function; the launcher still has not
    been run under either host on Windows, and the installer's `not_covered`
    list still says exactly that. A cross-platform string built with one
    platform's quoting rules is worth looking for wherever else it appears,
    and pulling that thread found three more of the same shape, all in the
    gate:

    * **the write heuristic listed only POSIX verbs**, so on Windows a
      `cmd.exe` call that wrote with a built-in (`del`, `move`, `ren`, `rd`)
      or a PowerShell cmdlet (`Remove-Item`, `Set-Content`, `Out-File`) read
      as a read;
    * **the tree-verb list had the same gap**, so a recursive delete of the
      directory holding the gate's own state read as touching only that
      directory;
    * **the protected-path comparison was case- and separator-sensitive**,
      and Windows paths are neither, so the same path named in a different
      case or with forward slashes compared unequal and the rule did not
      fire. Both sides go through `os.path.normcase` now, a no-op on POSIX.

    None of the four is verified on a live Windows host, and that is the
    honest summary of the Windows position: the code is now written for the
    platform instead of assuming the other one, and nobody has run it there.

58. **I duplicated three findings into two earlier rounds while writing
    them up.** `str.replace` on the heading "What is still not true, after
    this round", which appears once per round, inserted findings 55 to 57
    under the consolidation round and the slicing round as well as this
    one. Three rounds each claimed to have found the same three things.

    Caught by reading the file rather than by anything automated, which is
    the point: a document whose whole purpose is an accurate record of what
    was found when had silently become inaccurate, and nothing in the
    repository checks that. An anchor that is not unique is not an anchor.

59. **The protected-path predicate resolved one of its two arguments, and CI
    on two platforms said so.** `_under(path, root)` ran `realpath` on
    `path` and not on `root`. That is an undocumented precondition on every
    caller: a root reached through a symlink, or on Windows through a short
    8.3 name, is a different spelling of the same directory and compared
    unequal. `protected_paths` happens to resolve its roots, so production
    was correct by coincidence rather than by construction.

    It surfaced because the test I wrote for the case-folding fix passed an
    unresolved root. It passed on Linux, whose `/tmp` is neither a symlink
    nor a short name, and failed on both macOS runners, where `/var` is a
    symlink to `/private/var`, and both Windows runners, where
    `gettempdir` can return the 8.3 form. Both arguments are resolved now,
    so the predicate has no precondition to forget.

    Two things worth keeping. A predicate is the wrong place for a
    precondition nobody states; it will be correct until a caller is added.
    And the platform-split tests are better evidence than the mock they
    replaced: the Windows case-folding behaviour is now asserted **by the
    Windows runners**, which is the difference between a claim and a
    measurement, and it is the reviewer's standard applied to my own work.

60. **The gate allowed every edit, silently, for anyone whose path was not
    pure ASCII.** Hook mode read its payload with `sys.stdin.read()`, which
    decodes using the locale encoding. On Windows that is the ANSI code page,
    and both hosts emit raw UTF-8: Node's `JSON.stringify` and Rust's
    `serde_json` do not escape non-ASCII. So for a repository whose path held
    any non-ASCII character the payload arrived as mojibake,
    `enclosing_repos` found no `.git` above the mangled path, and `judge`
    returned `allow` with `outside_repository`. **The gate printed an empty
    object and the edit proceeded ungated.**

    Measured rather than reasoned: the same payload naming a repository with
    an e-acute is denied `routed_elsewhere` under a UTF-8 stdin and allowed
    under `cp1252`. This is the worst defect this project has had. It is not
    an edge case but a whole population: every user whose name or project
    path is not pure ASCII, which is most of the world.

    The fix reads bytes and names the encoding, in the gate rather than in a
    launcher, because a launcher fix would not protect a host that invokes
    the module directly. Non-UTF-8 bytes are a deny rather than a guess, and
    a byte-order mark is tolerated because a BOM is not a disagreement about
    the encoding, only about announcing it. The launchers set `PYTHONUTF8`
    too, for everything else in the process.

    The general rule: **a decoding default is a platform assumption**, and
    this codebase had already been caught four times by platform assumptions
    in cross-platform components. I had been looking for them in path
    handling and quoting. Encoding is the same class and I did not think of
    it until a sweep did.

61. **A launcher that could not start Python failed open, and on Windows that
    was the default case.** The gate always exits 0 and carries its decision
    in the JSON, but that contract only begins once the interpreter is
    running. Both launchers exited with the interpreter's status and nothing
    on stdout when it could not start, and a host reads a hook that produced
    no decision as a non-blocking error and runs the tool anyway. On a stock
    Windows account with no Python the bare name `python` resolves to the
    Microsoft Store App Execution Alias stub, so the fail-open was the
    ordinary path there, not an unlucky one.

    Both launchers now turn a launch failure into a deny and exit 0, while
    passing the operator subcommands' own exit status through untouched,
    because turning `report`'s failure into a fake hook decision would hide
    it. Writing that fix produced a smaller lesson of its own: `$?` after an
    `if` whose branches did not run is defined as zero, so the first version
    reported "launcher exit 0" for a failure that was exit 127.

62. **My quoted-character set for `cmd.exe` had only the obvious delimiters.**
    Finding 57 fixed the quoting and I chose the set by intuition: space,
    tab, quote, and the redirection and grouping characters. NTFS forbids
    only `< > : " / \ | ? *`, so a comma, a semicolon, an equals sign, a
    percent and an exclamation mark are all legal in a directory name and all
    significant to `cmd.exe`, which truncates the program name at the
    delimiter or expands a variable. `C:\dev\a=b\hook.cmd` came back bare.

    Worth noticing that this is a fix to a fix, found by asking a fresh
    reader to attack the same area rather than by re-reading it myself. The
    matrix is now asserted per platform: the delimiter set on Windows, and on
    POSIX a measured round trip through the real shell, because a comma needs
    no quoting in `sh` and asserting that it did would be the same mistake
    pointed the other way.

63. **The evidence collector I wrote to test the launcher tested the harness
    instead.** Its fixture seeded `PYTHONPATH` and `PYTHONDONTWRITEBYTECODE`
    into every child, including every launcher invocation, and those are
    precisely the two variables the `.cmd` exists to set. So all eight
    launcher checks would have passed even if `%~dp0`, the `..` segment or
    the quoted `set` produced nothing usable, because the gate would have
    imported through the inherited path. A launcher invocation now gets those
    removed and a hostile `PYTHONPATH` in their place, and deleting the
    launcher's own `export PYTHONPATH` now fails three checks where it
    previously failed none.

    It also ran the installed command string through Python's `shell=True`,
    which formats `%COMSPEC% /c` without `/d` or `/s`, where a Node host uses
    `/d /s /c`. Testing a different invocation from the one the host uses
    answers a different question.

    Third time in this session that the same defect shape has appeared in my
    own test code. The pattern is stable enough to name as a rule: **ask of
    every new test what its pass would look like if the mechanism were
    absent**, and if the answer is "the same", the test is decoration. The
    collector now carries an assertion self-test whose result is embedded in
    every record, so the next reader does not have to take the question on
    trust.

64. **Four Windows spellings walked straight through the protected-path
    rule.** The rule reads a command's operands and refuses one that reaches
    the gate's own state. Every mechanism it used to do that was written for
    POSIX, so on Windows the ordinary spellings were invisible to it. All four
    reproduced with `ntpath` in place, all four now refused:

    * `cmd /c "del C:\Users\me\.agent-bridge\routing\x.json"` was
      **allowed**, because `cmd` was not a recognised shell, so the quoted
      command stayed one word, and the colon split then severed the drive
      letter out of it;
    * `bash.exe -c "..."` was **allowed** where the identical `bash -c "..."`
      was refused, because the basename still carried `.exe`;
    * `powershell -Command "..."` was **allowed**, because neither the program
      nor the flag was recognised at all;
    * `rm -rf /c/Users/me/.agent-bridge` was **allowed**, because
      `ntpath.isabs` calls that absolute, so it was kept verbatim and later
      resolved against the current drive as `C:\c\Users\me\...`, a
      different directory.

    The last is the one that matters most: **Claude Code's Bash tool on
    Windows runs through Git for Windows**, so `/c/...` is the spelling that
    shell actually produces. The gate covered `sh -c` and nothing else.

    Fixing it produced a false positive of exactly the kind that makes a rule
    worthless in the other direction. Translating a Git-Bash drive turned a
    bare `/c`, which is `cmd.exe`'s own command flag, into `C:\`, an
    ancestor of every protected path, so with a tree verb in the command
    every `cmd /c` was refused. Caught by the test that asks whether an
    unrelated write is *still allowed*, which is why that test exists: a rule
    that refuses everything is not a rule, and it fails safe, so nothing else
    would have complained.

65. **A percent in the database path denied every call in both clients.**
    SQLite percent-decodes a `file:` URI path, and the read-only URI escaped
    `?` and `#` but not `%`. With the database under a directory named
    `App%20Data` the open failed, `stage_binding` returned
    `stage_db_unavailable`, which is a deny on every gated call, and
    `capacity_digest` returned None. Fail-closed, and unusable. The ordering
    is the fix: `%` has to be escaped first, or the function mangles its own
    escapes.

66. **The sweep that found these was cheap and I should have run it earlier.**
    Findings 60 to 65 all came from asking five fresh readers to attack one
    dimension each of the Windows surface, in order to decide what a Windows
    VM should test. Between them they produced a silent total bypass for
    non-ASCII paths, two fail-opens, four bypasses of the protected-path
    rule, a fix to one of my own fixes, and two vacuity bugs in the collector
    I had just written to test all of it.

    I had been reading the same code myself for a long stretch and had found
    the last of my own defects some time before. The asymmetry is worth
    naming: **re-reading my own work has sharply diminishing returns, and
    handing one narrow dimension to a reader with no memory of writing it does
    not.** Several of the findings were demonstrated by execution in the
    report rather than argued, which is also what made them quick to confirm
    and impossible to wave away.

### Still open from that sweep, recorded rather than fixed

Nine further defects were reported with reasoning I found credible but have
not yet verified or fixed. They are listed here so they are not lost, in the
order I would take them:

 • **The execution lanes would not run on Windows at all.** `os.fchmod` in
   `_atomic_json` does not exist there, `os.set_blocking` is Unix-only
   through 3.11, `_hash_regular_file` omits `O_BINARY` so the descriptor is
   opened in text mode, and `_env()` passes none of `SYSTEMROOT`, `COMSPEC`
   or `PATHEXT`, which `runner.scrubbed_env` in this same repository keeps
   and explains why. Masked today only by ordering: `_require_confinement`
   refuses on Windows before the first receipt is written. CI skips both lane
   modules there, so nothing says so.
 • **`shutil.which` puts the current directory first on Windows**, and with
   `GIT_CANDIDATES["Windows"]` empty every Windows git resolution goes
   through that line, so a `git.cmd` in the working directory beats a real
   `git.exe` on PATH.
 • **The keying asymmetry.** `receipt_name`, `item_id_for` and the policy
   lookup hash or match `realpath` with no `normcase`, while `_under`
   normcases both sides. So the same module treats Windows as
   case-insensitive for refusing a write and case-sensitive for identifying a
   repository: two spellings of one repository produce two receipts, and only
   the spelling the operator typed matches their own policy entry. The
   collector's `repo-keying-is-stable` check will fail on Windows if this is
   right.
 • **`own_home` is plain string equality** between two Windows paths, so
   three ordinary spellings of one home flip the branch and `CODEX_HOME` is
   silently dropped, which writes the Codex hook where Codex never reads it
   while the report says installed.
 • **`codex_trust_state` compares an exact dict key** across a process
   boundary on a platform where one file has many spellings, so trust can
   read as `needs_review` forever and the audit then prints that the Codex
   half of the gate does not run.
 • **`read_receipt` uses `os.path.exists` then `read_json`**, where
   `store.py`'s own docstring says to use `read_json_atomic` on Windows
   because a reader can arrive mid-replace.
 • **Job directories and artefacts are protected with mode bits only**, which
   Windows ignores; this project already owns the ACL seam
   (`store.secure_mkdir`, `platform.enforce_owner_only_file`).
 • **`store.atomic_write_bytes` plus `os.replace`** may carry an
   inheritance-blocked owner-only DACL onto the user's own `settings.json`.
 • **BOM and mixed line endings** in an existing `config.toml` or
   `settings.json` abort the install with a message that names neither cause.

Each needs verifying before it is believed, which is the standard the rest of
this document is held to and the reason they are here rather than in the list
above.

### What is still not true, after this round

* **The two desktop products and Codex on the web cannot be intercepted.**
  They expose no hook surface. This is stated in the README, in
  `DELEGATION-GATE.md` and in the installer's `not_covered` list, and it is
  why the honest name for this feature is "automatic routing in Claude Code
  and the Codex CLI, with assistant-mediated dispatch". If that is not where
  someone works, this changes nothing for them.
* **The brief is the assistant's words.** A `PreToolUse` payload names a tool
  and some paths. The gate compels the dispatch and names the route, item,
  stage, owner and revision; it cannot write the task.
* **Mechanical work an assistant simply does in its own context is not
  intercepted,** because it produces no tool call. What is automatic at the
  local lane is the admission: classification, the privacy refusal, the
  submission and the absence of any paid fallback. The choice to use the lane
  is still the assistant's, and the end-to-end test that says so used to
  claim otherwise.
* **The Linux confinement confines writes and the network, not reads.** The
  receipt records `confines_reads: false`. It is offered for synthetic
  material only, and only after proving its own boundary on the host.
* **Generation is unconfined on every platform.** The provider CLI runs with
  the real `HOME` and its own Read and Write tools inside the generation
  worktree. That is the direct worktree-to-patch channel and confining
  verification does not touch it.
* **What has and has not run on macOS and Windows.** I said "nothing here
  has been live-tested on Windows or macOS" several times in this round, and
  that understated the evidence. Reading the workflow settles it:

  Run and passing on all three runners: the whole offline suite, and
  `test_delegation_gate`, `test_automatic_gate`, `test_delegation_audit` and
  `test_hostenv`. So the gate's own logic, including classification,
  routing, receipts, capacity, dispatch intents, the protected-path rule and
  the write heuristic, is exercised on real macOS and Windows hosts. The
  Windows case-folding behaviour above is asserted by the Windows runners
  rather than by patching `os.path`.

  Run on macOS and Linux, skipped on Windows: `test_claude_task`,
  `test_codex_task` and the end-to-end workflow. So the macOS
  `sandbox-exec` confinement is exercised on macOS, and neither execution
  lane nor the full workflow is exercised on Windows at all.

  Never run anywhere: **the gate hook's own `.cmd` launcher.** The Windows
  smoke test in CI runs `agent-bridge-windows-setup.cmd`, a different
  launcher. And on no platform has the hook been invoked by Claude Code or
  Codex themselves; every test drives the gate module as a subprocess, which
  is the same interface but not the same integration.

  Being imprecise about this cut both ways: it understated what CI proves
  and it blurred the one thing that genuinely has no coverage.
