# Manifest-bound release install

`src/agent_bridge/orchestration/release_install.py` installs one
operator-approved release into one existing task folder under
`~/Claude/team-state/tasks/<item>`. The delegation gate recognises exactly one
canonical command for it. Other recognised literal launches are denied
(`release_install_unresolved`); quoted prose is routed normally. Opaque Python
bodies, renamed copies and dynamic shell spellings keep ordinary shell
routing, whose heuristic can be evaded. Approval security does not depend on
that heuristic.

The installer is standard-library Python only. It imports nothing from
`agent_bridge` or from the task, and it runs under `env -i` with `-I -S -B`,
so dependency repositories on `PYTHONPATH` are neither imported nor treated as
targets. The module imports on every platform because the gate loads it
everywhere. `install`, `accept` and `authorize` refuse with `unsupported_platform` anywhere
but POSIX with no-follow, folder-relative opens, and `fcntl` is imported only
when a lock is taken.

## Trust

Approval requires separate OS privilege. The operator runs `authorize` as root
from a trusted root-owned deployment of this module and interpreter, after
reviewing the manifest and independently computing its SHA-256. It publishes
an exact versioned record binding the digest to the canonical state root in
the fixed system directory `/etc/agent-bridge/release-approvals` (physically
`/private/etc/...` on macOS). The filename is the SHA-256 of that record.
Neither arguments nor environment variables can select another authority.

`accept`, the gate and `install` all verify the record. They walk every
authority ancestor from `/` with no-follow directory descriptors, require
root ownership and no group/other write bits on ancestors and evidence,
and reject effective write access by the calling account, including ACL
grants. Evidence must be a regular file and match the expected bytes exactly.
Missing, unreadable, writable or mismatched evidence refuses approval.
Clients running as root are refused: they have no independent authority.

Hooked clients must run as ordinary accounts without unattended root access
or permission to alter the trusted deployment. Elevation and the system
administrator are the trust boundary. A TTY or a shell-text deny is not proof
of human presence; tool calls can allocate pseudo-terminals. This feature
does not protect against a client already granted root privileges. If the OS
cannot verify effective access, approval is unavailable
(`operator_approval_unavailable`). Windows installation remains unavailable.

The state-root store is a cache of content, not an approval authority:

```text
<state_root>/release-install/approved/<sha256-of-manifest-bytes>.json
<state_root>/release-install/blobs/<sha256-of-each-payload-file>
```

The gate protects `<state_root>` for covered tool calls; its shell heuristic
does not prevent every possible write there. Forging the cache grants
nothing without the separate root-owned authorization. A hooked client can
cache and install an already authorized release, but cannot authorize a new
digest or state root. The installer reads payload bytes only from `blobs/`
by hash. The gate and the installer both call
`release_install.plan()`, which loads the manifest by digest, checks that the
bytes hash to that digest, validates the schema and derives the write set.
Both layers therefore verify the same digest and the same write set.

## Write set

`release_install.write_targets()` is the one calculation of every path the
installer may create, replace, rename or unlink. It is deterministic and has
no duplicates. In manifest order, for each payload it lists:

* each folder on the way to the file, inside the task (the installer may
  create a missing one),
* the file's sibling temporary,
* the file.

Last come `task.json`'s temporary and `task.json`. The task folder itself,
anything above it, and the lock files (opened read-only, never written) are
not in the set.

Each temporary is named `.<name>.<first 16 hex of the manifest digest>.release-install-tmp`
(`release_install.temp_name()`). The installer derives every temporary from
that same function in preflight, write and rollback. The gate runs its
protected-state, link, repository and ownership checks on the whole set before
anything is written. A protected temporary therefore denies the install
(`gate_state_protected`) like a protected payload would.

## Manifest schema (`agent-bridge.release-install.v1`)

Exact keys. Unknown or missing keys, duplicate JSON keys and non-finite
numbers are refused.

```json
{
  "schema": "agent-bridge.release-install.v1",
  "task": "synthetic-task",
  "claim": {"owner": "codex-session", "stage": "release"},
  "locks": ["nightly/state/owner.lock", "nightly/queue.lock", "task.json.claim.lock"],
  "files": [
    {"path": "release/launcher_01.py", "sha256": "<64 hex>", "previous_sha256": null},
    {"path": "release/test_release_pin.py", "sha256": "<64 hex>", "previous_sha256": null},
    {"path": "nightly/state/snapshot.json", "sha256": "<64 hex>", "previous_sha256": null},
    {"path": "nightly/review.json", "sha256": "<64 hex>", "previous_sha256": null},
    {"path": "nightly/queue.jsonl", "sha256": "<64 hex>", "previous_sha256": "<64 hex of current file>"}
  ],
  "activation": {
    "field": "current_stable_nightly_assignment",
    "previous": {"value": {"release": "r0"}},
    "value": {"release": "r1", "channel": "stable"}
  }
}
```

* `task`: the one existing task folder. The command's `--task` must match it.
* `claim`: the owner and stage that must hold a live claim in `task.json`
  (`workload.claim`, unreleased, lease more than 30 seconds away) and a live
  `owned` ledger row. The gate also requires that row's route to be the
  calling client.
* `locks`: exactly the task's three canonical lock files, in this order. No
  other lock set is accepted.
* `files`: 1 to 64 entries. `path` is relative, `/`-separated, at most 4
  segments, each `[A-Za-z0-9][A-Za-z0-9_.-]*`, so no `..`, no dot files and
  no `.git`. A path may not be `task.json`, a lock file, a folder holding a
  lock file (`nightly`, `nightly/state`), or anything under `task.json` or a
  lock file. The comparison ignores case. `previous_sha256` is `null` when
  the file must be absent, or the hash the current file must have.
* `activation.previous`: `{"absent": true}` or `{"value": <json>}`, the value
  `current_stable_nightly_assignment` must hold before replacement.

The operator acceptance fixture in `tests/test_release_install.py` builds the
expected shape: 16 launcher modules and a pin test (17 Python files),
`nightly/state/snapshot.json`, `nightly/review.json`, `nightly/queue.jsonl`,
and the activation last.

## Reading files

Every file the installer or `accept` reads (manifest, blob, payload,
destination, temporary, `task.json`) is opened with `O_NOFOLLOW|O_NONBLOCK`,
so a link is refused and a FIFO cannot block the open. The open descriptor is
then checked with `fstat` before any file object is made. Anything but a
regular file (folder, FIFO, socket, device) is refused:
`destination_type_refused` in the task, `payload_missing` for an operator
payload, `blob_missing` and `manifest_not_approved` in the store. Each
descriptor is closed exactly once on every path, including a failure while it
is wrapped.

## Order of operations

1. Load the approved manifest by digest and verify it. Load every blob and
   verify its hash. A bad hash stops here, before anything in the task is
   opened.
2. Open the task root, then the task folder, from `/` with `O_NOFOLLOW`.
   Refuse any link (`link_refused`) and any `.git` on the way, in the task,
   or in any folder walked inside it (`nested_repository`).
3. Take the locks, each through the same no-follow folder walk, read-only,
   `flock(LOCK_EX|LOCK_NB)`: `nightly/state/owner.lock`, then
   `nightly/queue.lock`, then `task.json.claim.lock`. Missing is
   `lock_missing`, held by anyone is `lock_busy`, a link is `link_refused`.
   A lock file is never created, written or replaced, and there is no other
   fence. Then verify the claim in `task.json` and the ledger row.
4. Preflight the whole write set before the first write. Each payload must be
   at its new hash (skipped), or at `previous_sha256`/absent (pending).
   Anything else is `stale_state`. A destination that is not a regular file
   is `destination_type_refused`. Existing folders on the way must be plain
   folders (`destination_type_refused`, `link_refused`). Every temporary in
   the set, including `task.json`'s, must be absent or this install's own
   leftover: a regular file whose bytes are a prefix of what this manifest
   writes there, or of the file's current bytes. Anything else is
   `temporary_conflict` and is left in place. The activation field must be at
   `previous` or already at `value`, otherwise `stale_activation`.
5. Write each pending file through its owner-only temporary, `fsync`,
   rename, then `fsync` the folder. New files are `0600` and new folders
   `0700`. A replaced file keeps its mode without group or other write.
6. Last, re-read `task.json` under the same locks, re-verify the claim and
   ledger, and rewrite it with only `current_stable_nightly_assignment`
   changed. Every other field, including `workload.claim`, is preserved by
   value. Formatting is normalised to two-space JSON.
7. Re-open every destination and `task.json` and compare the final bytes.
   Only then is `installed` reported.

Any failure in steps 5 to 7, including an `fsync` that fails after a rename,
rolls back in reverse: `task.json` first, then the payloads, then the created
folders. Every restored file is re-read and compared. The error then says
`... rolled back`. Every refusal before step 5 writes nothing.

## Operator usage

First, in the operator's own terminal, review the manifest and payload and
compute the manifest's digest independently. Run the privileged step using
a trusted root-owned deployment and interpreter (never elevate an
agent-writable checkout or Python installation):

```text
sudo /trusted/root-owned/python -I -S -B /trusted/root-owned/release_install.py authorize --state-root /canonical/state/root --manifest-sha256 <reviewed-digest>
sudo -k
```

Run `sudo -k` immediately after `authorize` to invalidate the cached sudo
ticket, or run the privileged step from a terminal no agent tool can drive.
A cached sudo ticket in an agent-reachable terminal tab counts as unattended
root access.

This writes only system authorization evidence; it never reads or writes the
user's payload or cache as root. The operator controls privilege elevation.
Do not grant hooked clients passwordless elevation to this command or its
interpreter. Existing state-root approvals require this new authorization;
they are not automatically promoted.

Then, as the ordinary account, cache the reviewed payload. Use the same
interpreter the gate hook runs (`bin/agent-bridge-python`); the gate pins it
by exact path. Literal hooked `accept` commands are denied as a routing aid;
the independent evidence check protects all spellings.

Accept a reviewed payload (folder laid out as the manifest's `path`s):

```bash
PY=/absolute/realpath/of/the/gate/hook/python   # must equal the hook's sys.executable
"$PY" -I -S -B src/agent_bridge/orchestration/release_install.py accept \
  --state-root  "$STATE_ROOT" \
  --manifest    /path/to/reviewed/manifest.json \
  --payload-dir /path/to/reviewed/payload \
  --tasks-root  "$HOME/Claude/team-state/tasks" \
  --capacity-db "$STATE_ROOT/capacity.sqlite3"
```

It first requires privileged authorization, then re-hashes every payload file
against the manifest before it writes
anything, publishes the blobs and the manifest, and prints `manifest_sha256`
and the exact canonical `command`. Accepting the same content twice is a
no-op. Different bytes under an existing hash are refused (`store_conflict`).

Install as the owning ordinary client, or the ordinary operator account.
Use the exact single-line literal `command` printed by `accept`, pasted
unchanged. It includes resolved paths and canonical quoting. The following
shows the shape only; replace placeholders with literal canonical paths and
the digest. Shell variables, substitutions and line continuations do not
qualify for the gate's canonical exemption:

```text
/usr/bin/env -i LANG=en_US.UTF-8 /canonical/gate/python -I -S -B /canonical/release_install.py install --state-root /canonical/state/root --tasks-root /canonical/tasks/root --capacity-db /canonical/capacity.sqlite3 --task <item> --manifest-sha256 <hex>
```

Output is one JSON line: `installed`, `already_installed`, or
`{"status": "refused", "code": ...}` with exit status 2.

Revoke authorization as root by deleting the system evidence file named in
`authorize`'s output. Subsequent gate and installer checks refuse even if the
entire cache remains. Deleting `approved/<hex>.json` only removes the cache;
an authorized client can repopulate it until system authorization is revoked.

## Recovery

* Refused before writing: fix the named condition (renew the claim, wait for
  the lock holder, re-approve a manifest whose `previous_sha256` matches) and
  run the same command again.
* Failed during writing and `rolled back`: verified back to the previous
  state. Run the same command again.
* `rollback_incomplete`: the rollback stopped at the first restore it could
  not verify. It never reverts a payload while `task.json` may still carry
  the activation, so every file is at either its previous or its new hash.
  Run the same command again. It normally completes forward, or reports
  `already_installed` if the activation had landed. A crash during rollback
  can leave a temporary containing previous bytes that do not match the new
  or current bytes; this requires the `temporary_conflict` recovery below.
* Interrupted (crash, power loss): each rename is followed by a folder
  `fsync`, so the activation is never durable before the payloads. Run the
  same command again. A file at neither hash is refused as `stale_state` for
  an operator to inspect. A leftover temporary this install wrote (its bytes
  are a prefix of the new or current bytes) is replaced on the next write.
  A rollback temporary holding previous bytes may instead require manual
  inspection and removal; automatic recovery of that crash window is not
  implemented.
* `temporary_conflict`: a file with this manifest's temporary name holds
  something else. The coded refusal's `detail` names the file relative to the
  task folder, including after an interrupted rollback. The installer never
  deletes it. Inspect it, move it away, and run the same command again.
  Files with any other name, including the
  generic `.<name>.release-install-tmp`, are never touched.
* After success, the same command reports `already_installed` and writes
  nothing.

## Gate codes

`release_install` (allow), `release_install_unresolved`,
`release_install_not_approved`, `release_install_task_missing`,
`release_install_link`, `release_install_repository`,
`release_install_claim`, plus the existing `gate_state_protected`,
`task_owned_elsewhere`, `task_claim_conflict`, `task_claim_unavailable` and
`stage_db_unavailable`. The installer re-checks everything at run time and
takes the locks only then. The gate's allow is not a token.

Approval refusals include `operator_privilege_required` for unprivileged
`authorize`, `operator_approval_untrusted` for unsafe authority or evidence,
and `operator_approval_unavailable` for missing or unverifiable evidence.
