# Manifest-bound release install

`src/agent_bridge/orchestration/release_install.py` installs one
operator-approved release into one existing task folder under
`~/Claude/team-state/tasks/<item>`. The delegation gate recognises exactly one
canonical command for it and denies every other spelling
(`release_install_unresolved`). Generic shell routing is unchanged: a dynamic
Python body, a `PYTHONPATH` entry, or any other installer command is still
judged by the ordinary rules, which fail closed.

The installer is standard-library Python only. It imports nothing from
`agent_bridge` or from the task, and it runs under `env -i` with `-I -S -B`,
so dependency repositories on `PYTHONPATH` are neither imported nor treated as
targets. The module imports on every platform because the gate loads it
everywhere. `install` and `accept` refuse with `unsupported_platform` anywhere
but POSIX with no-follow, folder-relative opens, and `fcntl` is imported only
when a lock is taken.

## Trust

Approval is a file in the operator store, not a claim the caller makes:

```text
<state_root>/release-install/approved/<sha256-of-manifest-bytes>.json
<state_root>/release-install/blobs/<sha256-of-each-payload-file>
```

`<state_root>` is the gate's protected state root, so no hooked client can
write there. The installer reads payload bytes only from `blobs/` by hash. It
cannot read anything a caller names. The gate and the installer both call
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

Run these in an ordinary terminal, outside Claude Code and Codex. The gate
denies `accept` from a hooked client. Use the same interpreter the gate hook
runs (`bin/agent-bridge-python`). The gate pins it by exact path.

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

It re-hashes every payload file against the manifest before it writes
anything, publishes the blobs and the manifest, and prints `manifest_sha256`
and the exact canonical `command`. Accepting the same content twice is a
no-op. Different bytes under an existing hash are refused (`store_conflict`).

Install (by the owning client, or the operator). All paths must be canonical
(`realpath`) and equal the gate's own:

```bash
/usr/bin/env -i LANG=en_US.UTF-8 "$PY" -I -S -B \
  "$(realpath src/agent_bridge/orchestration/release_install.py)" install \
  --state-root "$STATE_ROOT" --tasks-root "$TASKS_ROOT" \
  --capacity-db "$CAPACITY_DB" --task <item> --manifest-sha256 <hex>
```

Output is one JSON line: `installed`, `already_installed`, or
`{"status": "refused", "code": ...}` with exit status 2.

Revoke an approval by deleting `approved/<hex>.json`. Blobs may stay. They
grant nothing without a manifest.

## Recovery

* Refused before writing: fix the named condition (renew the claim, wait for
  the lock holder, re-approve a manifest whose `previous_sha256` matches) and
  run the same command again.
* Failed during writing and `rolled back`: verified back to the previous
  state. Run the same command again.
* `rollback_incomplete`: the rollback stopped at the first restore it could
  not verify. It never reverts a payload while `task.json` may still carry
  the activation, so every file is at either its previous or its new hash.
  Run the same command again. It completes forward, or reports
  `already_installed` if the activation had landed.
* Interrupted (crash, power loss): each rename is followed by a folder
  `fsync`, so the activation is never durable before the payloads. Run the
  same command again. A file at neither hash is refused as `stale_state` for
  an operator to inspect. A leftover temporary this install wrote (its bytes
  are a prefix of the new or current bytes) is replaced on the next write.
* `temporary_conflict`: a file with this manifest's temporary name holds
  something else. The installer never deletes it. Inspect it, move it away,
  and run the same command again. Files with any other name, including the
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
