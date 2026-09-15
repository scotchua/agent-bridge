# Signing a guest image release

This is the procedure for publishing a guest rootfs that the Windows
delegation lane will accept. It is written so that **no private key ever
enters this repository, this codebase, or any process it starts.**

Nothing here has signed a real release. `RELEASE_TRUST_ANCHORS` is empty, and
a test keeps it empty until a person with a real key changes it.

## Why the key never comes here

`agent_bridge.orchestration.signing` verifies Ed25519 signatures and cannot
produce one. That is not an oversight and it is not a limitation of the
standard library: signing is about twenty lines of the same arithmetic the
verifier already does. It is absent because code in the shipped package that
could sign is code an attacker who reaches that process could sign with.

So the signature is produced by you, on the machine that holds the release
key, with your own tool. The repository's job is only to make sure you sign
the right bytes and that what you produced actually verifies.

## What gets signed

The **canonical manifest bytes**, not the manifest file. JSON key order and
whitespace are not part of a manifest's meaning but they are part of its
bytes, so signing a file somebody re-serialised produces a signature that
verifies for the signer and for nobody else.

`prepare` emits those exact bytes, computed by the same function the verifier
uses, so the two cannot drift.

## The procedure

### 1. Build the image

```bash
python3 tools/build_windows_rootfs.py --recipe tools/rootfs/recipes/arm64.json --out-dir build/rootfs
```

The output directory is gitignored. The tarball is distributed out of band and
checked against the manifest hash on the machine that uses it.

### 2. Emit the bytes to sign

```bash
python3 tools/release_signing.py prepare --manifest build/rootfs/agent-bridge-manifest-arm64.json --out build/rootfs/manifest.canonical
```

It prints the manifest digest and the byte count. It asks for no key and has
no flag that would accept one.

### 3. Sign, on the signing machine, with your own key

Nothing in this repository participates in this step. Any Ed25519 signer over
raw bytes works. For example, with an OpenSSH key:

```bash
ssh-keygen -Y sign -f ~/.ssh/release_ed25519 -n file build/rootfs/manifest.canonical
```

An HSM, a smartcard or a hosted signing service is better. What you need out
of this step is two hex strings: the 128-character detached signature and the
64-character Ed25519 public key.

Move only those two values back. Do not copy the key, do not paste it into a
terminal that logs, and do not place either half inside a checkout.

### 4. Verify and produce the anchor

```bash
python3 tools/release_signing.py anchor \
    --manifest build/rootfs/agent-bridge-manifest-arm64.json \
    --release 2026.1 --architecture arm64 \
    --signature sig.hex --public-key pub.hex
```

This refuses before printing anything if:

- the file you pointed at is a private key, by name or by content,
- the public key is the published test fixture key, or
- the signature does not verify over the canonical bytes.

Only on success does it print a `TrustAnchor(...)` literal.

### 5. Add the anchor, deliberately

Paste the printed literal into `RELEASE_TRUST_ANCHORS` in
`src/agent_bridge/orchestration/windows_rootfs.py`. This is a release action
taken by a person who verified the manifest. It is never a change made to get
a test to pass.

### 6. Check the tree before you publish

```bash
python3 tools/release_signing.py scan
```

Exits non-zero if anything in the working tree looks like a private key, by
filename convention or by content. Run it in CI too; it is already wired into
the workflow.

## The test fixture

`tools/make_test_image.py` builds a complete, tiny, signed image offline and
reproducibly: rootfs tarball, manifest, sidecar and anchor. It exists because
every link of the trust chain had a test and the chain did not.

Its signing key is derived from a constant published in that file. It is
therefore worthless, which is the only safe way to ship a signed fixture: one
signed by a key that had to stay secret would either commit the secret or stop
working. `release_signing.py anchor` refuses that key by name, and a test
asserts it never appears among the shipped anchors.

```bash
python3 tools/make_test_image.py --check   # builds twice, compares every byte
```

## What a signature does not prove

- Not that the image is safe. A signature says the manifest came from the
  holder of the release key; it says nothing about what is inside the tarball.
- Not that the image boots. No image from this worktree has been run on a live
  Windows host.
- Not that the key is still trustworthy. There is no revocation here. A
  compromised key is handled by removing its anchor and publishing a release
  that carries a new one.
