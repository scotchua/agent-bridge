# Windows delegation

Automatic execution delegation on Windows runs each job inside an ephemeral
WSL2 guest built from a pinned image, with no host mounts and no interop. This
document describes the setup path, the containment boundary, and the blockers
that are open today.

**Nothing in this document has been validated on a live Windows host.** Every
test in this repository runs on the development machine against injected
platform seams. Read it as a description of what the code does, not as evidence
that it works end to end.

## WSL is not something the user installs

WSL is an implementation detail. A person setting up Agent Bridge should not
have to know it exists, find the right Microsoft Store entry, or paste commands
into a terminal. Setup is a ladder of stages that the installer walks, asking
only for the decisions it may not make on the user's behalf.

The stages, in order (`agent_bridge.orchestration.windows_wsl_provision`):

| Stage | Who does it | Admin | Restarts |
| --- | --- | --- | --- |
| `unsupported_platform` | nobody, delegation is off | no | no |
| `windows_too_old` | **the user**, by updating Windows | **yes** | **yes** |
| `firmware_virtualization` | **the user**, in BIOS/UEFI | no | **yes** |
| `windows_features` | installer, elevated once | **yes** | no |
| `reboot_required` | installer, with consent | **yes** | **yes** |
| `wsl_kernel` | installer, elevated | **yes** | no |
| `wsl_update` | installer, elevated | **yes** | no |
| `guest_image` | installer | no | no |
| `boundary_verification` | installer | no | no |
| `ready` | delegation may be enabled | | |

`current_stage` returns the first unsatisfied rung, and every field of
`ProvisionState` fails closed: `None` is treated exactly like `False`. Nothing
is inferred from something else being true.

### The boundaries that are real

* **Administrator.** Enabling `VirtualMachinePlatform` and
  `Microsoft-Windows-Subsystem-Linux`, and installing or updating the WSL
  kernel, are machine-wide changes. Nothing elevates silently. A stage that
  needs elevation says so, and `require_consent` refuses to run it without an
  explicit admin consent from the user. Everything after the kernel is per-user
  and needs no elevation at all.
* **Restart.** Enabling those features does not take effect until the machine
  restarts. Restarting is a second, separate consent, because the cost of a
  surprise restart is measured in the user's unsaved work. Before restarting,
  provisioning writes a durable resume record and registers a per-user logon
  task (`AgentBridgeSetupResume`, `schtasks /SC ONLOGON`, no `/RU`, no `/RL`)
  so setup continues at the right rung afterwards rather than starting over or
  appearing finished.
* **Firmware.** Virtualization in BIOS/UEFI is not something software can
  toggle. It is reported as a stage only the user can perform, with no pretence
  otherwise.

### Resume

Enabling the Windows features ends the user's session, so setup has to survive
a reboot. That is a state machine with an owner, not a note left in a file.

* **Create.** `write_resume_record` goes through `windows_privacy`: an atomic
  protected replace, owner-only, with the directory verified first. There is no
  `secure=None` path and no window where the record exists unprotected, because
  a truncate-then-protect order is a window in which anyone can write the stage
  setup will resume at.
* **Pin.** A read hands back the record *and* its identity, and the next write
  is pinned to that identity. A record replaced between the read and the write
  fails as `replace_identity_changed` rather than being silently overwritten.
* **Register.** `schedule_resume` writes the record first and registers the
  logon task second. A task with no record is recoverable, because setup
  re-observes and continues. A record with no task is not, because nothing ever
  runs again. If `schtasks /Create` fails, the record is removed rather than
  left as a promise nothing will keep.
* **Called.** `driver.step` calls `schedule_resume` before it takes a rebooting
  stage. It used to write a bare record and register nothing, which produced
  exactly the unrecoverable case above: a machine that went down holding a note
  describing where it was, and nothing that would ever read the note. A stage
  that reboots with no `resume_command` in the context is refused outright as
  `resume_command_missing`, before the restart command runs. Staying where the
  machine is is recoverable; rebooting without a way back is not.
* **Own.** `schtasks /F` replaces whatever holds a name. `resume_ownership_blocker`
  compares the registered task's actual command against the one setup would
  register, and refuses as `resume_task_not_ours` if they differ, so setup
  cannot delete somebody else's task that happens to share the name.
  `register_resume_argv` validates the command it is given and raises rather
  than registering arbitrary command authority under a scheduled task.
* **Count.** `next_attempt` increments per stage, `should_stop_retrying` gives up
  after `MAX_STAGE_ATTEMPTS` (3), and `schedule_resume` refuses to schedule
  attempt four at all. A stage that cannot succeed does not loop at every logon.
* **Continue.** After login, `resume` reads and validates the record and reports
  the recorded stage, so the driver continues from there rather than starting
  over. A malformed or unprotected record is not read as "nothing to resume": it
  is reported, because "setup never started" and "setup's state was tampered
  with" are different facts.
* **Remove.** `finish_resume` deletes the task and the record only at a terminal
  condition: delegation may be enabled, or setup has stopped for a reason a
  further reboot cannot change. It refuses as `still_provisioning` otherwise. If
  the record is cleared but the task removal failed, that is reported rather
  than assumed.

The record deliberately **cannot** assert `boundary_verified`: a file on disk is
not evidence that the containment boundary held, and letting a record claim it
would turn the one gate that matters into something an attacker could write.

Every durable record goes through `sanitize_detail`, so reason codes never carry
OS error text or pathnames.

## The gate

`delegation_may_be_enabled(state)` is true only at the `ready` stage. Onboarding
reports the stage but can never satisfy the last rung: it has no way to run a
boundary verification, so it reports the boundary as not verified and delegation
stays off. Fail closed, always.

Onboarding output (`onboard.plan`, `onboard.status`, and the apply report)
carries two separate things that must not be confused:

* `windows_preflight` is read-only local evidence. `prerequisites_ready` enables
  nothing and authorizes nothing.
* the `windows_setup` block of the onboarding report is the ladder: current stage, the next step in plain language,
  what is still ahead, whether an administrator or a restart is coming, and the
  evidence behind each conclusion.

## Containment

Each job gets a fresh distribution imported from the pinned rootfs, run, and
unregistered. Within that:

* **No host mounts.** `/etc/wsl.conf` disables automount and interop, and the
  guest proves it: the `host-mount-absent` canary checks marker paths and
  `/proc/mounts` for host filesystem types, and `wsl-interop-absent` checks
  `binfmt_misc`, including renamed entries.
* **Pinned everything.** `wsl-conf-sha256` and `guest-runner-sha256` hash the
  files on disk, and `pinned-versions` verifies each tool's sha256 against
  `/etc/agent-bridge/versions.json` before reporting its version.
* **Network: public egress allowed, private egress blocked, and the blocking
  is tested rather than asserted.** The guest has outbound internet access on
  purpose. The provider CLIs authenticate and run against endpoints served
  from CDNs whose addresses no allowlist can enumerate correctly, so public
  provider egress is permitted and is not treated as a containment failure.

  Two canaries cover the part that is blocked, and they prove different
  amounts. `network-egress-policy` applies an nftables output-chain policy
  dropping RFC1918, link-local and CGNAT ranges in both address families,
  reads the ruleset back out of the kernel, and compares it to a pinned
  sha256. That is evidence the rules are loaded, and nothing more: a ruleset
  can be present and still not take effect, and matching text is not a
  reachability result. `network-egress-unreachable` then opens a real TCP
  connection attempt to one address in each protected range and to this
  guest's own default gateway, read from the kernel routing table rather than
  guessed. A destination that answers fails the canary, and so does one that
  refuses: a RST means the packet reached something that replied, which is
  precisely what the drops exist to prevent. A guest with no default route is
  refused rather than passed, because a canary that cannot find the host it is
  supposed to fail to reach has not tested anything.

  What this establishes is bounded, and is stated as bounded: those
  destinations were unreachable at that moment. It is not proof that every
  private address is unreachable, because a sample is not a proof. What it
  does rule out is the failure the ruleset check cannot see, where the table
  is installed but bypassed by a second interface, an unexpected route, or a
  flush after the read.

  This page previously said the guest had no network. That was wrong, and the
  claim is not made anywhere now, because an unenforced claim is worse than an
  acknowledged gap.

All seven canaries must pass, in order, before any work runs. The two network
canaries run last, so a cheaper structural proof fails first.

* **Verification commands run with no public egress at all.** The paragraph
  above is about the provider phase, which needs the internet. Verification
  commands are a different thing: they execute repository code, chosen by the
  brief, and the POSIX lane already runs them under `(deny network*)`. The
  Windows lane now matches. Before the first verification command,
  `enforce_verification_egress` installs a second nftables table
  (`VERIFY_EGRESS_TABLE`) whose output chain is `policy drop` with loopback
  accepted, reads the ruleset back out of the kernel and compares it to
  `verify_egress_ruleset()`, then opens a TCP attempt to each of
  `PUBLIC_EGRESS_PROBE_TARGETS`. A target that answers fails the job as
  `verification_egress_public_reachable`; a table that will not apply or will
  not read back fails it too. The job is **aborted** on any of those, not run
  with a warning attached.

  Two tables rather than one edit, because nftables runs every chain
  registered on a hook: the job policy stays as it is and the deny table is
  composed on top, so restoring provider egress after verification is removing
  one table rather than reconstructing the other. The receipt carries the
  `egress` field for each verification record, so the policy that was actually
  in force is recorded per command rather than claimed once for the job.

  `tests/test_verification_egress.py` includes the exfiltration-negative
  contract: a verification command that attempts to reach a public address
  must fail, and the test asserts that the abort happens before any
  verification command runs rather than after the first one.
* **Bounded I/O, from one budget.** One JSON request in on stdin, one JSON
  response out on stdout, one exchange per process, no framing. The sizes are
  no longer three sets of numbers that disagreed: `guest_runner` derives the
  whole budget and `windows_wsl_runtime` asserts against it at import, so a
  change to one layer that the others do not follow fails to load rather than
  failing in production.

  The numbers are chosen to be defensible, not enormous: brief 256 KiB,
  workspace 8192 members, 4 MiB per member, 32 MiB of content, 12 MiB of
  archive, expansion ratio 500, base64 16 MiB, request 1 MiB with a 17 MiB
  total, stdout 2 MiB, diff 4 MiB. Timeout 3600 s, 256 args, 64 KB per arg.

  The ratio bound is the one worth explaining. 100 refused a legitimate 290:1
  lockfile, and 1000 is above deflate's measured single-stream ceiling of
  roughly 1028:1, which makes it decorative. 500 is between the two, and
  `tests/test_workspace_unpacking.py` asserts both measurements rather than
  leaving the choice as an opinion.
* **Owner-only ACLs**, verified by reading them back with `icacls` rather than
  trusting a zero exit code.
* **Fail-closed cleanup.** Registration is a tri-state (`none`, `created`,
  `unproven`), proven by listing the distro name immediately before and after
  the import. A name is never unregistered unless this run positively created
  it. When cleanup is unproven, the job directory, install directory and owner
  record are **retained** so the stale reaper can retry; deleting them would
  make a leaked distribution permanently unreapable.

Receipts record hashes, lengths and reason codes. They never contain input,
output, commands, secrets or source.

* **The diff is streamed through its cap, not truncated after the fact.**
  `capture_diff` used to run git with `stdout=PIPE` and check the size
  afterwards, which means the whole diff was already in memory before anything
  decided it was too large. It now goes through `_bounded_git`, which reuses
  the same `_BoundedReader` pump threads and process-group teardown the job
  runner uses: the read stops at `MAX_DIFF_BYTES`, the process group is
  terminated so descendants die with it, and the outcomes are the named
  `diff_too_large` and `diff_timed_out` rather than a quietly shortened diff.

### The cost of the network canaries is measured, not hidden

The two network canaries are the slow ones. A connection attempt to an address
that drops the packet costs whatever the timeout is, and that is real wall-clock
time every job pays. The temptation is to drop them for speed before anyone has
seen them run.

They stay. `_run_canaries` times each canary individually and records the
duration under `canary_timing_key(name)` *before* the verdict is known, so the
receipt shows what the egress probes actually cost rather than folding them into
one total. That turns "are they worth it" into a question with a measurement
behind it, to be answered after live evidence exists and not before.

## Packing the host workspace

The archive handed to the guest is built on the host, from the repository, and
that is a boundary. If a directory in the tree can be swapped for a link to
somewhere else between the moment it is approved and the moment it is read, the
archive gets bytes from outside the repository.

`O_NOFOLLOW` is the usual answer and it does not exist on Windows, so it is not
relied on:

* **Reparse points, not just symlinks.** A Windows directory junction is not a
  symlink, and `is_symlink()` is false for one. `_is_link_like` refuses an entry
  whose `st_reparse_tag` is nonzero as well as one `is_symlink()` reports, so
  junctions are refused by the signal Windows actually gives. The repository
  root itself is checked the same way and refused if it is a link or a
  reparse point.
* **Handle-bound identity.** Each file is approved by an `lstat` and then opened,
  and the `(st_dev, st_ino)` of the approving `lstat` is compared against
  `fstat` of the descriptor that will be read. A path replaced between the two
  fails the comparison, and the bytes read are the bytes from the descriptor
  that passed.
* **Containment.** `_PackRoot.contains` resolves each candidate and requires it
  to stay under the resolved repository root, so a path that escapes by any
  route is refused rather than followed.
* **Counted, not silent.** Refusals increment `links_refused` in the result, so
  an archive that skipped something says so.

`tests/test_workspace_packing.py` covers symlink and junction swaps in both
positions, escape attempts, and the direct claim that no bytes from outside the
repository enter the archive. The simulation boundary is stated in that file: the
junction cases prove the packer refuses the signal Windows would give, not that
Windows gives it. That second half needs the VM.

## Unpacking is bounded before anything is materialized

Packing is one direction. The guest unpacks what it is handed, and an archive
is attacker-shaped input the moment a brief can influence what goes into it.

`unpack_workspace` streams (`for member in archive`) and never calls
`getmembers()`, because `getmembers()` walks the whole archive's metadata into
memory before a single bound has been applied, which is the bomb the bounds
exist to stop. Every limit is claimed *before* the member is written:

* member count, cumulative uncompressed bytes, and per-member bytes;
* the compression ratio, measured against bytes actually consumed;
* path length and path depth, and the resolved destination staying under the
  workspace root;
* duplicates and case-folded collisions, so two members differing only in case
  cannot overwrite each other on a case-insensitive filesystem;
* every link, device, fifo and anything else that is not a regular file or a
  directory, refused outright rather than filtered later.

Files are written with `O_EXCL | O_NOFOLLOW` through a manual copy rather than
`TarFile.extract`, and the **measured** byte count beats the header's claim, so
a header that understates its payload does not buy extra bytes.

## Files that are checked are the files that are read

`windows_auth` and `windows_evidence` used to verify a path and then reopen it
to read. Between the two calls the name can point somewhere else, which makes
the check decorative.

`windows_privacy.read_private_file` opens the path once and does everything
through that descriptor: `fstat` for the type and size bound
(`MAX_PRIVATE_FILE_BYTES`, 1 MB), the ACL check against the descriptor, the read
itself, then a second `fstat` compared to the first, refusing as
`file_changed_while_reading` if the file moved underneath. On Windows the
descriptor is resolved to a name with `GetFinalPathNameByHandleW` rather than by
trusting the name it was given.

Reads verify and never repair. An earlier version called
`enforce_owner_only_file`, which on POSIX would `fchmod` a world-readable
evidence record to `0600` and then accept it, silently fixing the fact that
anyone could have read it. Enforcement belongs on the write path only.

Writes carry the other half. `atomic_private_write` takes an `expect_identity`,
so a read-modify-write is pinned to the record it read and fails as
`replace_identity_changed` rather than discarding somebody else's write.

`enrol` writes two files for one logical change and there is no atomic replace
across two paths, so it rolls back. The rollback used to be best effort against
a pathname, which meant two enrolments racing could leave the secret from one
beside the record from the other, and a rollback could overwrite an enrolment
newer than the one it was undoing.

Three things now hold it together:

* **A lock on the auth root.** `windows_privacy.exclusive_lock` takes an
  advisory exclusive lock on an open handle (`fcntl.flock`, or
  `msvcrt.locking` on Windows), held across both writes. It is a handle lock
  on purpose: the kernel releases it if the process dies, so a crash mid-enrol
  leaves no stale lock state to clear. Contention is reported as
  `enrolment_busy`; a platform with neither primitive is refused as
  `lock_unsupported` rather than proceeding unlocked.
* **Descriptor-bound reads.** The previous ciphertext is read through
  `read_private_file`, so the bytes captured for rollback are the bytes that
  were verified, not whatever the name resolved to a moment later.
* **Identity-pinned writes.** Both the forward write and the rollback pass
  `expect_identity`. A rollback whose pin no longer matches writes nothing and
  returns false, which surfaces as `enrolment_rollback_incomplete`. It cannot
  overwrite a newer enrolment, and it cannot fail silently.

The lock is the ordinary defence and the identity pin is the independent one,
because a writer that never took the lock is exactly the case a lock cannot
see.

**On zeroization, plainly: Python cannot guarantee that a plaintext token is
erased from process memory.** `MEMORY_LIFETIME_NOTE` says so in code. Releasing a
reference is not wiping, and the garbage collector or the OS pager may have
copied the bytes already. What is actually bounded is lifetime and spread: the
plaintext exists only for the duration of one request, is never written to a
file, never appears on an argv, never reaches a verification subprocess, and is
redacted out of every outcome. Nothing here claims that deleting something
erases memory.

## The brief that was admitted is the brief that is read

The queue admits a brief by hash. Translation then had to read it off disk, and
for a moment those were two different facts about the same pathname.

`read_brief(path, expected_sha256=...)` closes it. The file is opened once,
`fstat`ed for type and size, read from that descriptor, `fstat`ed again to
catch a swap mid-read (`changed_while_reading`), and the digest of the bytes
read is compared in constant time against the digest the queue admitted. A
mismatch is `brief_changed_after_admission`, not a silent substitution. A brief
that is a symlink fails as `brief_invalid/not_a_regular_file` rather than being
followed, and `translate` refuses a request whose `brief_sha256` is not 64
characters as `brief_unverified` rather than reading an unchecked pathname.
There is no second read of the name anywhere in the path.

## `base` means something

`request['base']` used to be ignored. A receipt that recorded a diff without
saying what it was a diff against is a receipt that cannot be checked, and
silently ignoring a field the caller set is worse than rejecting it.

`resolve_base(repo, base)` resolves the requested base through git and requires
`HEAD` to equal it. The named refusals are `base_missing` (the repository has
no such commit), `base_invalid` (not a shape git will resolve),
`base_unresolvable` (git could not answer) and `base_mismatch` (the worktree is
not on the base that was asked for). The resolved SHA is packaged and recorded
on the outcome as `base_sha`, so the exact commit plus the dirty overlay the
packer captured is what the receipt proves, matching the POSIX lane.

## Labels and approvals

Default admission is `public`, `synthetic` and `internal_nonclient`, which are
the queue's own labels (`execution_queue.ALLOWED_CLASSIFICATIONS`) rather than
a second vocabulary invented here. `client`,
`client_derived` and `confidential` require an explicit per-job approval, and
the executor refuses by name rather than guessing. This is unchanged by
anything on this page.

## Background activation

`agent_bridge.orchestration.windows_activation` registers the execution worker
as a per-user logon task (`AgentBridgeExecutionWorker`). No `/RU`, no
`/RL HIGHEST`, no `/S`: it lives in the calling user's own Task Scheduler
namespace and runs as them. A background worker needs nothing machine-wide, and
asking for it would mean a worker that outlives the user's session and can reach
other users' state.

It is reversible. `deactivate` removes exactly that task, the user can see and
delete it in Task Scheduler, and unlike activation it needs no consent flag:
turning something off is always allowed.

**A task name is not ownership.** `schtasks /Create /F` silently replaces
whatever holds the name and `/Delete /F` removes it, so both `activate` and
`deactivate` first query the task and compare the program it runs against the
one this installation manages (`proves_ownership`). Arguments may differ,
because upgrading the worker's flags is ordinary; the program may not. A task
that runs something else, or a registered task that will not say what it runs,
is left alone and reported as `task_not_owned_by_agent_bridge` or
`task_action_unreadable`. The resume task registered before a restart goes
through the same check and the same strict command contract.

The command must be an absolute **local** path, validated by the same contract
the runtime uses for host paths: no UNC, no device path, no traversal.
`ntpath.isabs` accepts `\\\\server\\share\\worker.exe`, and a logon task pointing
at a network share runs whatever that share serves at sign-in.

Registration refuses anything it cannot quote safely. `schtasks` takes its action
as a single string, so an argument containing a quote or a `%` is rejected rather
than escaped, and an action longer than 261 characters is refused rather than
silently truncated into a different command.

## The provider lane

**Provider jobs are refused today, but not because the handoff is impossible.**
That is a correction to what this page said before.

Both CLIs accept a subscription session without an API key and without a
persistent credential store inside the image:

* Claude Code reads `CLAUDE_CODE_OAUTH_TOKEN`, which the user mints on the host
  with `claude setup-token`, alongside a per-job `CLAUDE_CONFIG_DIR`.
* Codex CLI accepts `codex login --with-access-token`, with the token supplied
  on **stdin, never argv**, and a per-job `CODEX_HOME`.

So the guest receives a bounded, memory-only auth capsule: a private tmpfs
mounted at `/run/agent-bridge-auth` with `mode=0700,noexec,nosuid,nodev`,
unmounted in a `finally` block, and redacted out of every byte of captured
output. `ANTHROPIC_API_KEY` is never set. Nothing is copied from the host's
credential store.

Two things have **not** been observed on a live host, and both are properties
of the provider, not of this code:

* **Portability.** Whether a session minted on the host is actually accepted
  from inside an ephemeral guest with a different machine identity.
* **Refresh.** How a session that expires mid-job behaves when the capsule is
  discarded and nothing the CLI writes back is kept.

Until a verification record on the machine itself reports both, every `claude`
and `codex` job is refused by name as `provider_lane_unverified`. The text is
carried in code at `guest_runner.AUTH_UNPROVEN` and
`windows_delegation.PROVIDER_EXECUTION_BLOCKER`.

### How the lane is observed

`claude --version` is not authentication. A version string prints without a
session, so a check that accepted one would open the lane on a machine where no
token works at all. The observation is a real authenticated turn.

`guest_runner` gains a third request mode, `auth_probe`, beside `tool` and
`provider_job`. It runs one fixed, minimal, synthetic prompt and requires
provider-specific evidence that the model actually answered:

* **Claude.** `--output-format json`, and the result object must carry
  `subtype: success`, `is_error: false`, a `usage` dictionary, and the fixed
  sentinel `AUTH_PROBE_SENTINEL` in the text. `--tools ""` so the probe cannot
  do anything.
* **Codex.** `--output-last-message` into a file inside the job directory, run
  with the `read-only` sandbox, and the file must contain the same sentinel.

  This lane was structurally unopenable until recently: `_execute_auth_probe`
  deleted the last-message file before `_codex_probe_verdict` looked at it, so
  a correctly authenticated Codex session could only ever return
  `no_sentinel`. The answer is now read through a protected descriptor
  (`read_probe_message`: `O_NOFOLLOW`, `fstat` for a regular file, a 64 KiB
  cap, a bounded read) and the verdict is computed from those bytes; only then
  does `discard_probe_message` remove the file. The verdict function takes the
  bytes, not a pathname, so it is no longer possible to reintroduce the bug by
  reordering the cleanup. `tests/test_auth_probe.py` covers the full
  `_execute_auth_probe` success and failure lifecycle through a capsule
  double, not the helper alone, because helper-only coverage is what let the
  original defect through.

The probe runs through the exact guest, runtime and auth-capsule path a real
job takes: the same tmpfs capsule, the same environment construction, the same
canaries. `API_KEY_ENV_KEYS` is checked **after** the capsule has contributed
its environment, so a probe that would have succeeded on a billed API key is
refused as `api_key_present` rather than counted as subscription auth.

The response crossing the boundary is a verdict token and nothing else.
`stdout` and `stderr` are unconditionally empty: the classification happens
inside the guest so no provider output, sentinel or otherwise, is ever
recorded. The verdicts are `authenticated`, `rejected`, `no_sentinel`,
`api_key_present`, `failed`, `timed_out`.

### Two gates, not one

`windows_provision_driver.observe_provider_lane` opens the lane only when both
observations land:

* **Portability**, the real authenticated turn above, with every canary passed
  and the sandbox torn down. Anything else records
  `portability_not_observed:<verdict>`.
* **Refresh behaviour**, the same probe with a deliberately worthless capsule of
  the right shape (`SYNTHETIC_EXPIRED_TOKEN`). It must come back **rejected**,
  not merely fail. A run that is accepted records
  `refresh_behaviour_not_observed:accepted`; one that times out or errors
  records `refresh_behaviour_inconclusive:<verdict>`, because "the network was
  bad" is not evidence about how expiry behaves.

The second gate is what makes a version-only false positive structurally
impossible. A code path that says yes to anything fails it by construction: it
would have to reject a token it just accepted.

Rejected and nonexistent tokens are covered by regression
(`tests/test_auth_probe.py`), along with version-only output, an empty
response, a success object with `is_error: true`, a missing sentinel, a
missing `usage` block, an API key in the environment, and the requirement that
the response carries no captured bytes.

### Wiring it into provisioning

The lane used to have no caller. `record_boundary` stored `ProviderLane` closed,
`observe_provider_lane` was never invoked, and `verified_executor` could not
bootstrap itself: the executor refuses until the lane is open, and the lane
could only be opened by running a provider through the executor.

The knot is cut by putting the observation in the driver rather than the
executor. `STAGE_PROVIDER_ENROLMENT` is a rung of its own, between
`STAGE_BOUNDARY_VERIFICATION` and `STAGE_READY`:

1. The boundary is verified live and recorded first. The probe therefore runs
   inside a guest whose containment has just been proven, not on trust.
2. `_provider_step` asks for consent in its own right (`PROVIDER_PROBE_CONSENT`),
   because the probe spends the user's allowance and uses their session.
3. `record_provider_lane` amends the existing machine-bound evidence file as a
   read-modify-write pinned with `expect_identity`, through the same atomic
   protected replace as every other record. It does not rewrite the boundary
   evidence and it cannot be applied to a file somebody swapped underneath it.
4. `_reload_lane` reads the record back off disk and compares it to what was
   just written. A lane that did not survive the round trip reports
   `provider_lane_not_durable` or `provider_lane_readback_mismatch`, and nothing
   is enabled on the strength of an in-memory value.
5. Only then does the ladder advance to `ready`, and only then does
   `load_verified_state` report `provider_lane_verified`, which is what
   `verified_executor` reads before it will build a provider executor.

So the lane is now connected end to end in code. It has still never been run
against a real subscription on a real Windows host, and nothing in this
worktree claims otherwise.

## Where the session comes from

`windows_auth` is the host half, and it never touches the user's own store.
Nothing reads `~/.claude`, `~/.codex`, or any credential directory: copying one
into a guest moves a long-lived session somewhere the user did not put it and
cannot see. The supported input is the provider's own non-interactive one, and
it is enrolled **once**, with explicit consent, into DPAPI under the current
Windows account.

* The ciphertext is useless to another account on the machine and useless on
  another machine. There is no fallback: a platform without OS protection
  refuses to enrol, because the only alternative is a plaintext token wearing a
  strict file mode, and a file mode is not encryption.
* The record stored beside it carries the provider, the capsule kind, when it
  was enrolled and the sentence that was consented to. It never carries the
  token.
* At job time `load_capsule` decrypts into memory, the capsule travels inside
  the same bounded request as the work, and it is dropped. Nothing in this
  project writes a returned capsule anywhere.
* `revoke` removes the local copy and returns text that says plainly that
  revoking at the provider is a separate act, because deleting a ciphertext
  does not cancel a token.
* `enrolment_state` renders a plain-language setup screen, and distinguishes
  "a session is saved" from "jobs can use it": being enrolled and having a
  verified lane are different facts, and a screen showing only the first would
  promise something that will refuse.

Every error is a fixed token. A message that quoted what it was given would
eventually quote a token.

## The driver

`windows_provision_driver` walks the whole ladder: observe, consent, enable the
Windows features, restart and resume, install or update WSL, install the image,
verify the boundary live, record the evidence, then activate the per-user
worker. One step per call, because each can require a decision, take minutes,
or end the user's session, and a loop would make the consent boundary depend on
how fast the previous command finished.

One of its steps refuses today rather than completes, and the refusal is the
absence of something that has not been produced: the **image step**, because no
release has published a signed manifest to anchor trust to
(`windows_rootfs.release_blockers()` says so by name).

The **provider lane** step no longer refuses by construction. It is wired, it
asks for consent, and on a machine with a verified boundary and an enrolled
session it will run the probe and record what it observed. It has not been run.

Nothing here has been run on a live Windows host.

## The command that walks it

A driver with no caller is a library describing an installation rather than one
that performs it, and that is what this was until `agent_bridge.windows_setup`
existed. The reboot machinery in particular could not have worked at all: the
logon task it registers has to point at a command, and there was no command to
point at.

```
bin\agent-bridge-windows-setup plan
bin\agent-bridge-windows-setup step --admin-consent
bin\agent-bridge-windows-setup step --admin-consent --reboot-consent
bin\agent-bridge-windows-setup status
```

Equivalently `python setup_bridge.py windows-setup <subcommand>`, or
`python -m agent_bridge.windows_setup`.

Five subcommands. `plan` is read-only. `step` does the single next thing.
`status` shows the resume record and the logon task. `resume` is what the logon
task runs after a restart. `validate` runs the deterministic check suite below
and prints a report. Exit codes are the step vocabulary, so a wrapper
branches without parsing: 0 ok, 1 failed, 2 blocked, 3 a consent is required, 4
bad arguments. One JSON object on stdout per invocation.

The module owns three things and delegates everything else:

* **Where the installation lives.** Runtime root, rootfs and manifest, from
  flags or the per-user default under `%LOCALAPPDATA%`. Per-user rather than
  machine-wide, because everything under it is an artefact of one person's
  sessions and the ACLs the privacy layer applies are per-user ACLs.
* **What the OS says right now.** Preflight, the two optional-feature states,
  and whether a restart is pending. All read-only and bounded, none of it
  inferred from a previous run of this process. A feature state that cannot be
  read is `None`, which the ladder treats as "not collected" rather than
  "disabled", so an unreadable state blocks instead of triggering an elevated
  enable of something already on. Reboot detection fails towards *pending*: the
  cost of that direction is one avoidable restart prompt, bounded by the stage
  attempt budget, and the cost of the other is enabling the virtual machine
  platform and then trying to start a sandbox that cannot work.
* **The argv the resume task will run.** The same interpreter, the repository's
  `setup_bridge.py`, `windows-setup resume`, the same runtime root. It names
  the launcher script rather than `-m` because a logon task inherits the user's
  environment and not the shell that started setup, so a `PYTHONPATH` would not
  survive the restart. It is validated through `windows_activation.build_action`
  when it is built, so an interpreter that cannot be named in a scheduled task
  is discovered before a reboot is offered rather than after one is taken.

Consents are four separate flags and there is deliberately no `--yes`. Each
covers a different surprise: machine-wide features, ending the session,
installing an image, spending a provider session. **None of them survives a
reboot.** A resumed stage that needs a consent stops and waits for a person to
run `step`, because an unattended logon task that could elevate on a consent
given before the restart is a standing grant nobody re-affirmed.

`resume` has exactly three outcomes and they are not collapsed into each other:
nothing to resume (the ordinary case, exit 0, nothing changed); still
provisioning (stages advanced until one needed a person; record and task stay
so the next logon continues); or terminal, which is either delegation may be
enabled or the stage has exhausted its attempts and another reboot will not
change that. Only in the terminal case are the record and the logon task
removed, and only through `finish_resume`, which re-checks the condition itself
rather than trusting the loop that called it. One invocation takes at most
`MAX_RESUME_STEPS` stages, because a resume that looped until something changed
would be indistinguishable from a resume that hung.

`tests/test_windows_setup.py` drives all of this through `main` with an argv:
reboot-required registering the ownership-proven task, the resume continuing at
the recorded stage, both terminal paths retiring the record and the task, the
refusal when there is no way back, the refusal when the task is not ours, and
the orphaned-task report. What it does not do is run on Windows.

## The validation report

`validate` exists because "it works on my machine" is not a form of evidence
anybody else can act on, and a screenshot is not either. It runs a fixed,
ordered list of named checks and prints one JSON object.

```
bin\agent-bridge-windows-setup validate --report validation.json
```

What makes it deterministic, and why each part is required:

* **Fixed order, no parallelism.** The checks are a tuple, run front to back. A
  check whose prerequisite failed is reported `blocked` rather than run, so a
  cascade produces one failure and a list of things that were never attempted,
  instead of twelve failures that all say the same thing.
* **No wall clock in the verdict.** Timings and the timestamp live in a
  separate `envelope` key that the verdict is not computed from, so two runs on
  an unchanged machine produce byte-identical `checks` and a diff of two
  reports is a diff of what changed about the machine.
* **A closed vocabulary.** `pass`, `fail`, `blocked`, `skipped`, and nothing
  else. A check that returns an unknown status, returns the wrong shape, or
  raises is a `fail` naming the check. Treating a misbehaving check as a pass
  would be the worst failure this command has.
* **`blocked` and `skipped` are not passes.** The verdict is `ready` only when
  every check in the suite passed, computed from the suite rather than from the
  rows, so a report that is missing a row entirely is not ready either. A suite
  that only listed the checks somebody had got around to writing would call a
  half-validated machine ready.
* **Bounded facts only.** A check may attach numbers, booleans and short
  tokens. A path, a username or a line of command output is refused rather than
  serialised, because "just this once" is how a host path ends up in a report
  somebody pastes into an issue. An exception's text is discarded entirely.

Every row carries `proves` and `does_not_prove`, so a person reading a green
run sees, next to each pass, the thing that pass does not establish. The report
also carries a `not_proven` list about itself: a subscription session surviving
the boundary, unreachability of the network (the egress check samples
destinations and reads the ruleset back, which is a different claim), and
anything about the image's contents beyond it being the reviewed one.

The four guest-side checks are four readings of **one** ephemeral job, not four
jobs. Four instances would be four imports and four teardowns, and their four
answers could disagree with each other.

`validate` writes nothing to the machine. The durable record stays with
`windows_evidence`, which refuses to be written anywhere but on Windows and
binds itself to the host and the artefacts. A report is something a person
reads and attaches; keeping the two apart is what stops a green report from
enabling delegation on its own.

Nothing in this suite has produced a `ready` verdict anywhere, and it cannot on
any machine in this project's reach: the `platform` check fails first
everywhere but Windows, by design.

## Evidence, not assumption

Readiness is not something a planner decides. `windows_evidence` holds a
`provision-evidence.json` in the runtime root, and
`execution_worker.select_executor` builds the executor through
`windows_delegation.verified_executor`, which reads it. A record is accepted
only if it:

* was written by `record_verification`, which refuses on anything that is not
  native Windows, so a test on a development machine cannot produce one;
* carries a host fingerprint matching the machine reading it, so a record
  copied from a working machine does not enable a fresh one;
* names the rootfs hash the manifest currently pins and the hash of the guest
  runner this process would ship, so replacing either invalidates it;
* lists every canary in `CANARY_ORDER` and says the boundary was verified.

Any failure produces the empty, fail-closed state and a named reason, surfaced
as `provisioning_unverified:<reason>`, so an operator is told which check
refused rather than that delegation is off for no stated reason. The provider
lane is a separate block inside the same record: a fully verified boundary does
not open it, and it opens only after the two-gate observation above is written,
re-read from disk and confirmed.

No such record has been produced on a live Windows host from this worktree.
Every test here exercises the refusal paths.

### No live validation

No job has been dispatched through this executor on a live Windows host from
this worktree, no image has been imported into WSL2, and no canary has run
inside a real guest. The provisioning argv, the activation argv and the runtime
lifecycle are reviewed and tested as data. Treat "installed" and "verified" as
separate claims, and do not report the second on the strength of the first.

## Configuration

`orchestration.json` accepts four optional Windows keys. The first three are
all-or-none; `windows_wsl_sidecar_path` is optional on top of them:

```json
{
  "windows_wsl_runtime_root": "C:\\Users\\sam\\AgentBridge\\wsl",
  "windows_wsl_rootfs_path": "C:\\Users\\sam\\AgentBridge\\agent-bridge-rootfs-amd64.tar",
  "windows_wsl_manifest_path": "C:\\Users\\sam\\AgentBridge\\agent-bridge-rootfs-amd64.manifest.json",
  "windows_wsl_sidecar_path": "C:\\Users\\sam\\AgentBridge\\agent-bridge-rootfs-amd64.sidecar.json"
}
```

These stay strings rather than `Path`, because the configuration must load and
validate on any platform and `Path("C:\\x")` off Windows is a relative path
named `C:\x`. They are validated by the same contract the runtime uses: absolute
native paths only, no UNC, no device paths, no traversal.

`execution_worker.select_executor` picks the WSL2 executor when `os.name` is
`nt` and the POSIX harness executor otherwise. See
[WINDOWS-ROOTFS.md](WINDOWS-ROOTFS.md) for building the image these paths point
at.
