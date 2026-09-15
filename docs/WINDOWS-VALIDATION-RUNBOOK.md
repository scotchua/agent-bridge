# Windows validation runbook

Everything that can be closed without a Windows machine has been. What is left
needs a person at a real Windows host, and this is the exact list, in order,
with the command to run and the evidence that says it worked.

Read this first: **nothing below has been run.** The expected outputs are what
the code is written to produce, not observations. A step that produces
something else is information, not a mistake to work around.

## Before you start

- A Windows 11 host, build 22621 or newer, that you can restart.
- Not the `TaxDome` VM. That VM holds client data and is deliberately out of
  scope for this work.
- Python 3.11 or newer on PATH.
- A checkout of this branch.
- Roughly 20 GB free for the guest image.

Each step below is safe to stop after. Nothing in steps 1 to 3 changes the
machine.

---

## Step 1. Confirm the lane refuses before it is proven

```bat
python setup_bridge.py onboard plan --answers answers.json
```

**Expected:** the plan reports `windows_setup` with a current stage, and
automatic delegation is off with the message that it is available only on
macOS and that the Windows lane refuses until a boundary verification has been
recorded on this machine. The consultation bridge is still offered.

**What this proves:** the gate is computed from the machine's record rather
than hard-coded, and this machine does not have one yet. If delegation is
already enabled here, stop and report it: that is the one outcome that would
mean the gate is broken.

---

## Step 2. Run the validation suite on a bare machine

```bat
bin\agent-bridge-windows-setup validate --report validation-before.json
```

**Expected:** exit code 1 and a report whose `verdict` is `not_ready`. The
`platform` check passes (this is a native Windows process, unlike every run so
far). Later checks fail or are `blocked` depending on what the host already
has.

**Evidence to keep:** `validation-before.json`. Every later report is read as a
diff against this one.

**Run it twice** and compare:

```bat
bin\agent-bridge-windows-setup validate --report validation-twice.json
fc validation-before.json validation-twice.json
```

**Expected:** the files differ only inside `envelope` (the timestamp). If any
`checks` entry differs between two runs on an unchanged machine, the suite is
not deterministic and that is a defect worth stopping for.

---

## Step 3. Walk the ladder, read-only

```bat
bin\agent-bridge-windows-setup plan
```

**Expected:** one JSON object naming the current stage, the next stage, who may
take it (`installer`, `installer_elevated`, or `user`), and the remaining
stages. Exit code 0. Nothing is changed.

---

## Step 4. Enable the platform features (needs an administrator)

This is the first step that changes the machine.

```bat
bin\agent-bridge-windows-setup step --admin-consent
```

**Expected:** `status` of `ok` with the stage that was taken, or
`consent_required` naming the consent that is missing. The features enabled are
`VirtualMachinePlatform` and `Microsoft-Windows-Subsystem-Linux`, and nothing
else.

**If it returns `blocked`:** read `reason`. A blocked stage is the ladder
saying a person has to do something, not a failure to retry.

---

## Step 5. The restart, and the resume

```bat
bin\agent-bridge-windows-setup status
```

**Expected before consenting:** `resume_task_registered` is `false` and
`resume` is `null`.

```bat
bin\agent-bridge-windows-setup step --admin-consent --reboot-consent
```

**Expected:** `status` of `reboot_scheduled`. Then, **before the machine
restarts**, in another window:

```bat
bin\agent-bridge-windows-setup status
schtasks /query /tn AgentBridgeSetupResume /v /fo LIST
```

**Expected:** `resume_task_registered` is `true`, `resume` carries the stage
and the attempt count, and `schtasks` shows a task that runs at logon as you,
with no `Run As User` of `SYSTEM` and no highest-privileges flag. The task's
action names your Python and this repository's `setup_bridge.py` with
`windows-setup resume`.

**This is the single most important thing to check before letting the machine
restart.** A reboot with no way back is how this used to strand a machine
halfway through.

After the restart, sign in and wait. The logon task runs on its own.

```bat
bin\agent-bridge-windows-setup status
```

**Expected:** either the resume record is gone and the ladder has advanced (the
resume ran to a terminal state and retired itself), or the record is still
there with an incremented attempt and a stage that needs a consent, which is
the correct behaviour: **no consent survives a reboot.** A resumed stage that
needs one waits for you.

---

## Step 6. Build the guest image

On a machine with Docker or Podman. This does not have to be the Windows host.

The ARM64 recipe is fully pinned and buildable. The AMD64 recipe remains an
explicit refused stub until its platform-specific pins are observed. Select
the recipe matching the Windows host architecture.

```bash
python3 tools/build_windows_rootfs.py --recipe tools/rootfs/recipes/arm64.json --out-dir build/rootfs
```

**Expected:** `build/rootfs/agent-bridge-rootfs-arm64.tar`,
`agent-bridge-manifest-arm64.json` and
`agent-bridge-rootfs-arm64.tar.sha256`.
The manifest's `rootfs_sha256` matches the tarball on disk.

**Check reproducibility before trusting it:**

```bash
python3 tools/build_windows_rootfs.py --recipe tools/rootfs/recipes/arm64.json --out-dir build/rootfs-2
shasum -a 256 build/rootfs/agent-bridge-rootfs-arm64.tar build/rootfs-2/agent-bridge-rootfs-arm64.tar
```

**Expected:** identical hashes. If they differ, something in the recipe is not
pinned and the manifest does not describe the image.

---

## Step 7. Sign the manifest

Full procedure in [RELEASE-SIGNING.md](RELEASE-SIGNING.md). The short form:

```bash
python3 tools/release_signing.py prepare --manifest build/rootfs/manifest.json --out build/rootfs/manifest.canonical
# sign those exact bytes on the machine holding the release key, with your own tool
python3 tools/release_signing.py anchor --manifest build/rootfs/manifest.json \
    --release <name> --architecture amd64 --signature sig.hex --public-key pub.hex
```

**Expected:** a `TrustAnchor(...)` literal on stdout. It is printed only if the
signature actually verifies over the canonical bytes.

**The key never comes into this repository.** Nothing here can sign. Move only
the two hex strings back.

Paste the literal into `RELEASE_TRUST_ANCHORS` in
`src/agent_bridge/orchestration/windows_rootfs.py`. Then:

```bash
python3 tools/release_signing.py scan
```

**Expected:** exit 0 and an empty list. A non-empty list means something
key-shaped reached the tree and must be removed before anything is published.

---

## Step 8. Install the image on the Windows host

```bat
bin\agent-bridge-windows-setup step --image-consent ^
    --image C:\path\rootfs.tar --manifest C:\path\manifest.json
```

**Expected:** `ok`, or a refusal whose `reason` names exactly which trust
condition failed, one of:

- `manifest_untrusted:no_release_anchor` — nothing is anchored at all.
- `manifest_untrusted:no_anchor_for_release` — anchored, but not this release
  or this architecture.
- `manifest_untrusted:digest_mismatch` — the anchor pins different canonical
  bytes than this manifest produces.
- `manifest_untrusted:signature_missing` or `:public_key_missing` — the anchor
  is incomplete.
- `manifest_untrusted:signature_invalid` — the signature does not verify.

A digest match on its own is not trust. Whoever could rewrite the pinned digest
could rewrite the manifest to match it, so the signature is the part of this an
attacker with write access to the repository cannot forge.

**A refusal here is the system working.** Until step 7 is done, this step
refuses by design and the reason will be `manifest_untrusted:no_release_anchor`.

---

## Step 9. The containment canaries, in a real guest

This is the first time any of this runs inside WSL2.

```bat
bin\agent-bridge-windows-setup validate --report validation-after-image.json
```

**Expected:** `distro_registered` passes and reports WSL2, then `guest_runner`,
`canaries` and `egress_policy` pass, then `round_trip` passes with
`job_completed_and_instance_destroyed`. Verdict `ready`.

**Evidence to keep:** `validation-after-image.json`.

**Read the canary failures carefully if there are any.** Each names one
containment property:

- `host-mount-absent` — no Windows path and no host filesystem is visible.
- `wsl-interop-absent` — no registered interop handler; the guest cannot
  launch a Windows program.
- `wsl-conf-sha256` and `guest-runner-sha256` — the image is the reviewed one.
- `pinned-versions` — every tool hash matches the inventory.
- `network-egress-policy` and `network-egress-unreachable` — the deny ruleset
  is in the kernel and a sample of private destinations does not answer.

A canary failure is not something to retry. It means the guest is not contained
the way the design says it is.

Confirm nothing survived:

```bat
wsl.exe --list --verbose
```

**Expected:** no `agent-bridge-` distribution remains.

---

## Step 10. The provider lane (spends real allowance)

This is the only step that costs money or subscription allowance. It runs one
real authenticated model turn, per provider.

```bat
bin\agent-bridge-windows-setup step --provider-consent
```

**Expected:** one of exactly these verdicts per provider, and nothing else:

| Verdict | Means |
|---|---|
| `auth_probe_authenticated` | a model answered with the fixed sentinel |
| `auth_probe_rejected` | the provider refused the session |
| `auth_probe_no_sentinel` | the CLI answered but not with the sentinel |
| `auth_probe_api_key_present` | a metered key was in the environment; the lane stays shut |
| `auth_probe_failed` | something else; the lane stays shut |
| `auth_probe_timed_out` | no answer inside the bound |

**No provider output is returned, ever.** The verdict is the whole result. If
you see provider text anywhere in the output, that is a defect worth stopping
for.

**Two things this step still does not establish**, and they are recorded as
open in the evidence record rather than glossed:

- **Portability.** Whether a session minted on your host is accepted from
  inside the guest, as opposed to one that happens to still be valid.
- **Refresh.** What a session that expires mid-job does.

Both need a longer observation than one probe. Until they are recorded, provider
jobs remain refused by name.

---

## Step 11. Record the verification

```bat
bin\agent-bridge-windows-setup step
python setup_bridge.py onboard status --answers answers.json
```

**Expected:** `provision-evidence.json` exists in the runtime root, carries this
machine's fingerprint and the rootfs and guest-runner hashes, and
`onboard status --answers answers.json` now reports automatic delegation as available rather than
refused.

**Check the binding actually binds.** Copy the evidence file to another
Windows machine and run `onboard status --answers answers.json` there.

**Expected:** delegation is refused on the second machine. The record is bound
to the host fingerprint, and a record that travelled is not evidence about
where it landed.

---

## What to send back

- `validation-before.json`, `validation-twice.json`,
  `validation-after-image.json`.
- The `schtasks /query` output from step 5, with the username redacted.
- The rootfs and manifest hashes from step 6, from both builds.
- The provider verdicts from step 10, which are single tokens and carry nothing
  else.
- Anything that refused, with its `reason`. A refusal with a name is the most
  useful thing this system produces.

Do not send the evidence record, the manifest canonical bytes, or any file from
the runtime root. None of it is needed to read the result, and the runtime root
is per-user private on purpose.

## What no amount of this proves

A green report on one machine is a green report on one machine. It does not
establish that the lane works on a different Windows build, that the image is
safe rather than merely the reviewed one, that the guest cannot reach the
network (the egress check samples, and says so), or that a subscription session
survives the boundary in the two ways step 10 leaves open.
