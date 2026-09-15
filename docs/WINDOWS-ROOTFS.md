# Building the pinned Agent Bridge guest image

The Windows delegation guest is an ephemeral WSL2 distribution imported from a
tarball. This document covers building that tarball, what is pinned, and what
reproducibility claim is and is not being made.

No image is committed to this repository. `build/` and `*.tar` are gitignored.
The tarball is distributed out of band and checked against its manifest hash on
the machine that imports it.

## Recipe status

`tools/rootfs/recipes/arm64.json` contains observed, exact pins and is
buildable. `tools/rootfs/recipes/amd64.json` remains an explicit, refused stub
until its architecture-specific pins are observed. A test holds that
distinction so neither status can change silently.

The runtime packages include exact versions for `nftables`, `git`, and
`openssl`. OpenSSL is explicit because the build uses it to verify SHA-512
integrity; it is not left as an accidental transitive dependency.

## Nothing here is learned after the download

Every fetched artefact is checked against a value that was in this repository
before anything was fetched:

* the Node tarball against `node_tarball_sha256`. The `SHASUMS256.txt` fetch is
  gone: a checksum file served from the same origin over the same connection is
  learned after the download and proves only that the origin agrees with itself
* both provider wrapper tarballs and both architecture-native tarballs against
  their four npm `sha512-<base64>` integrity pins. All four are verified before
  any is extracted. They are then unpacked directly into fixed global module
  paths. No `npm install` or dependency resolution runs in the image build;
  the only provider lifecycle script is Claude's local postinstall, after its
  native dependency is already present and verified

`validate_recipe` refuses a recipe missing any of the five pins, so an unpinned
download cannot reach a build.

## The artifact workflow is unreleasable, and says so in code

A pinned manifest answers "are these the bytes the manifest describes". It
cannot answer "and who said that manifest was right", because it is the thing
being asked about. That second question needs a release-bound trust anchor:
`windows_rootfs.TrustAnchor`, naming a release, an architecture, and the sha256
of the exact manifest that release published.

`RELEASE_TRUST_ANCHORS` is empty. No release of this project has published a
signed manifest, so `verify_manifest_trust` refuses every manifest with
`manifest_untrusted:no_release_anchor`, and `release_blockers()` returns
`manifest_trust_anchor_missing`. The build script prints
`ARTIFACT_WORKFLOW_BLOCKER` and every outstanding blocker after a successful
build, and the sidecar records both the `manifest_sha256` a maintainer would
sign and the blockers standing at build time.

### A digest match is not trust

The trust check now requires a real signature, verified. It is not optional and
it is not deferred to a caller who may not pass one.

* An anchor with an empty signature is refused as
  `manifest_untrusted:signature_missing`, whatever its digest says. An anchor
  whose `manifest_sha256` matches the manifest byte for byte but carries no
  signature **never returns trusted**: a digest proves the manifest is the one
  the anchor names, and says nothing about who wrote the anchor.
* An anchor with no public key is refused as
  `manifest_untrusted:public_key_missing`, and `release_blockers` reports
  `manifest_public_key_missing:<release>` for any anchor missing one.
* A signature that does not verify is refused as
  `manifest_untrusted:signature_invalid`; a verifier that raises is refused as
  `manifest_untrusted:signature_error`. There is no outcome where verification
  did not happen and the manifest is trusted anyway. The old
  `signature_unverified` result is gone.

`orchestration/signing.py` is the verifier: Ed25519 per RFC 8032 §6, in pure
standard library, verification only. Signing lives in the tests, because nothing
in the shipped code has any business holding a release key.
`verify_manifest_trust` still accepts a `verify_signature` callable for a caller
with a hardware key or a different algorithm, but the default is a real
verification rather than a refusal to decide.

#### The small-order forgery, and why this is not a library call

The first version of this file accepted `A = R = <identity point>, S = 0`. That
signature verifies against **any** message: with `A` the identity the
verification equation collapses to `[S]B == R`, the message never enters the
arithmetic, and one 64-byte constant signs anything without knowing any private
key. The order-2 point does the same.

The obvious remedy is "call a vetted library instead". That was tried and
measured rather than assumed, and it does not hold here:

* **Availability.** This project is standard library only on Python 3.11+, and
  both the guest image and a stock Windows Python ship without `cryptography`
  or any other Ed25519 implementation. A trust check skipped when an optional
  import is missing is a trust check an attacker removes by uninstalling a
  package.
* **It would not have fixed it.** OpenSSL, through `cryptography` 50.0.0,
  accepts that same forgery and the order-2 variant as well. Both were run
  against it. A vetted verifier would have inherited the bug.

What removes it is rejecting small-order points, which `_is_small_order` does
by testing `[8]P == identity`, the whole torsion subgroup, computed rather
than listed, in both the public-key and the commitment position. That is
strictly stronger than the library behaviour measured.

The coverage is stated in `VERIFICATION_COVERAGE` rather than implied by the
presence of an RFC number: the four RFC 8032 §7.1 vectors, a genuine round
trip, the eight small-order encodings rejected in both positions, non-canonical
`y` (`y >= p`) and non-canonical `S` (`S >= L`). It is not an audited
implementation and it is not constant-time; every input it sees is public.
`tests/test_signing.py` asserts the published eight encodings against the
module's own derivation in both directions, so the module and the test have to
agree about what the subgroup is.

The signed bytes are `canonical_manifest_bytes(manifest)`, so a signature is over
the manifest content and not over a formatting of it.

Regressions cover the unsigned, wrong-key and wrong-signature cases, and one
genuinely signed positive case built with the test-only signer.

Releasing therefore requires, all of it, before the word is used: real observed
pins in both `amd64.json` and `arm64.json`, a built image for both
architectures, a published signature over each manifest, and the matching
anchors committed here.

## Build

```bash
python3 tools/build_windows_rootfs.py --recipe tools/rootfs/recipes/arm64.json --out-dir build/rootfs
```

`--engine` accepts `docker` (default) or `podman`. One architecture per run.
Both `amd64` and `arm64` recipes exist because Windows on ARM is a real target
and an x64 image will not boot there; the architecture appears in every output
filename so the two cannot be confused.

Outputs, in `--out-dir`:

* `agent-bridge-rootfs-<arch>.tar`, the normalised filesystem tarball
* `agent-bridge-rootfs-<arch>.manifest.json`, exactly the keys the runtime's
  `parse_manifest` accepts, and nothing else
* `agent-bridge-rootfs-<arch>.sidecar.json`, the build provenance: architecture,
  base image digest, guest runner hash, `wsl.conf` hash, the sha256 of every
  installed tool, the canonical `manifest_sha256`, and the release blockers
  outstanding at build time

## What is pinned

A recipe is refused by `validate_recipe` unless every one of these is fixed:

* the base image by **digest**, never a tag; a tag is a moving target and an
  image that changed under you is not the image you reviewed
* every apt package as `name=version`
* the Node tarball version **and** the sha256 of its bytes, verified before
  extraction; an unverified archive extracted as root is the whole supply chain
  in one step
* the `@anthropic-ai/claude-code` and `@openai/codex` versions and the npm
  integrity of each package tarball, verified before npm executes anything

The guest runner's sha256 is computed from the copy in this checkout and checked
again inside the build, so a build cannot silently ship a different runner than
the host is pinning.

`/etc/agent-bridge/versions.json` is generated **in the image** by
`tools/rootfs/install-pinned-tools`, which hashes each binary after installing
it. The builder then reads that file back out of the exported tarball to write
the sidecar. The inventory has to come from the image because only the image can
say what installing a version actually produced.

## Normalisation

`docker export` output is not stable across runs. `normalise_tar` rewrites the
archive with members sorted, mtime fixed at `SOURCE_DATE_EPOCH` (0), uid/gid/
uname/gname cleared to root, and setuid/setgid bits stripped. The sha256 it
returns is the hash of the file it wrote, which is the hash the manifest pins.

The export is a filesystem tarball (`create`, `export`, `rm`), not `docker save`.
WSL2 imports a filesystem, not a layered OCI image.

### The reproducibility claim, stated exactly

Two builds from the same recipe on the same architecture produce the same
tarball **when the upstream package archives still serve the same bytes for the
pinned versions**. That is a real property and it is what the pinning and
normalisation are for. It is not bit-for-bit reproducibility in the strong
sense: apt and npm can rewrite or withdraw a published artifact, and a build
that reaches a different upstream will produce a different hash. The manifest
hash is what the runtime trusts, so a rebuild that does not match an earlier one
is a signal to investigate, not a failure to route around.

## Distribution

Out of band, with the manifest beside it. The import path verifies the tarball's
sha256 against the manifest before the file is copied into the per-job install
directory. A mismatch is a refusal, not a warning.

## Not validated

None of this has been imported into WSL2 on a live Windows host from this
worktree. A successful build on a development machine is evidence that the build
pipeline works, not that the resulting guest boots, and not that the canaries
pass inside it.
