#!/usr/bin/env python3
"""Build a small, reproducible, *signed* test image and its trust material.

Why this exists
---------------

The real guest image needs Docker, a pinned Debian base, a network, and a
recipe whose every value has been observed by a person. None of that is
available in a test, which meant the whole trust chain (rootfs bytes to
manifest to canonical digest to trust anchor to Ed25519 signature) had no
end-to-end exercise. Each link was tested; the chain was not.

This builds a complete, tiny instance of that chain from nothing, in about a
second, with no network and no container engine. It is the fixture that lets
the verification path be run the way a release would run it.

Reproducible, and checkable
---------------------------

Run it twice into two directories and every byte matches, including the
tarball, because the members are normalised through the same
``windows_rootfs`` code the real build uses and ``built_at`` is pinned rather
than read from a clock. ``--check`` rebuilds into a temporary directory and
compares, which is how the reproducibility claim is tested rather than
asserted.

The key
-------

The signing key here is derived from :data:`TEST_SEED`, which is a constant in
this file, published, and therefore worthless. That is deliberate and it is
the only safe way to ship a signed fixture: a fixture signed by a key that had
to be kept secret would either commit the secret or stop working.

Because the key is public, the public key it derives is refused as a release
anchor by :func:`refuse_as_release_anchor`, and a regression test asserts that
it never appears in ``windows_rootfs.RELEASE_TRUST_ANCHORS``. A test key that
could anchor a real release would be the worst possible outcome of a file like
this one.

Nothing here has been run on a live Windows host, and a signed test image is
not a released image.
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_bridge.orchestration import signing  # noqa: E402
from agent_bridge.orchestration import windows_rootfs as wrf  # noqa: E402
from agent_bridge.orchestration import windows_wsl as ww  # noqa: E402

#: A published, worthless signing seed. See the module docstring: a fixture
#: key that had to be secret could not be shipped.
TEST_SEED = bytes(range(32))

#: Written beside the anchor so nobody has to read this file to find out.
TEST_KEY_WARNING = (
    "This key is derived from a constant published in tools/make_test_image.py. "
    "It is a fixture, it protects nothing, and it must never appear in "
    "windows_rootfs.RELEASE_TRUST_ANCHORS.")

#: Fixed so two builds agree. The real build records a real timestamp; a
#: fixture recording one would be a fixture that is not reproducible.
BUILT_AT = "1980-01-01T00:00:00+00:00"

TEST_RELEASE = "test-fixture-0"

#: The files inside the fixture rootfs. Small, fixed, and enough to be a real
#: tarball with a real hash rather than an empty one.
ROOTFS_CONTENT = {
    "etc/wsl.conf": ww.WSL_CONF_CONTENTS.encode("utf-8"),
    "usr/local/bin/agent-bridge-guest-runner": b"#!/usr/bin/env python3\n# fixture\n",
    "etc/agent-bridge/versions.json": b"{}\n",
}

OUTPUT_NAMES = ("rootfs.tar", "manifest.json", "sidecar.json", "anchor.json")


def test_recipe(architecture: str = "amd64") -> wrf.RootfsRecipe:
    """A recipe that passes :func:`windows_rootfs.validate_recipe`.

    Every value is a syntactically valid pin that names nothing real. It has
    to validate, because a fixture built from a recipe the project's own
    validator refuses would prove nothing about the real path.
    """

    filler = "0" * 64
    return wrf.RootfsRecipe(
        architecture=architecture,
        base_image="library/debian",
        base_digest="sha256:" + "1" * 64,
        distro_release="12.9",
        node_version="20.19.0",
        # Not 1.0.0 / 0.1.0: the validator names those as placeholders, and
        # a fixture that had to dodge a real check would be testing the dodge.
        claude_version="2.1.229",
        codex_version="0.51.0",
        node_tarball_sha256=filler,
        claude_integrity="sha512-" + "A" * 86 + "==",
        codex_integrity="sha512-" + "B" * 86 + "==",
        apt_packages=("ca-certificates=20230311+deb12u1",
                      "curl=7.88.1-10+deb12u12",
                      "git=1:2.39.5-0+deb12u2",
                      "nftables=1.0.6-2+deb12u2",
                      "python3-minimal=3.11.2-1+b1",
                      "xz-utils=5.4.1-0.2"),
    )


def write_rootfs(path: Path) -> str:
    """A normalised tarball, through the same normaliser the real build uses."""

    raw = path.with_suffix(".raw")
    with tarfile.open(raw, "w", format=tarfile.GNU_FORMAT) as archive:
        for name in sorted(ROOTFS_CONTENT):
            body = ROOTFS_CONTENT[name]
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755 if name.startswith("usr/local/bin/") else 0o644
            archive.addfile(info, __import__("io").BytesIO(body))
    digest = wrf.normalise_tar(str(raw), str(path))
    raw.unlink()
    return digest


def sign(message: bytes, seed: bytes = TEST_SEED) -> tuple[str, str]:
    """RFC 8032 section 5.1.6, for the fixture key only.

    Signing is not in :mod:`agent_bridge.orchestration.signing` and will not
    be: a release is signed on the machine holding the release key, and code
    in the shipped package that could sign is code an attacker reaching that
    process could sign with. This is a build tool, not shipped runtime, and it
    signs with a key that is published.
    """

    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    prefix = digest[32:]
    public = _compress(signing._point_mul(scalar, signing._G))
    nonce = int.from_bytes(hashlib.sha512(prefix + message).digest(),
                           "little") % signing._Q
    commitment = _compress(signing._point_mul(nonce, signing._G))
    challenge = signing._sha512_modq(commitment + public + message)
    value = (nonce + challenge * scalar) % signing._Q
    return public.hex(), (commitment + value.to_bytes(32, "little")).hex()


def _compress(point) -> bytes:
    x, y, z, _ = point
    inverse = signing._modp_inv(z)
    x, y = x * inverse % signing._P, y * inverse % signing._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def test_public_key() -> str:
    public, _signature = sign(b"")
    return public


def refuse_as_release_anchor(public_key_hex: str) -> None:
    """Refuse to let the fixture key be used as a real anchor.

    Called by the release tooling and by a regression test. It is a small
    thing and it closes the one way a file like this becomes dangerous.
    """

    if public_key_hex.strip().lower() == test_public_key():
        raise SystemExit(
            "refusing: this is the published test fixture key. " + TEST_KEY_WARNING)


def build(out_dir: Path, *, architecture: str = "amd64") -> dict[str, str]:
    """Write all four artefacts and return their digests."""

    out_dir.mkdir(parents=True, exist_ok=True)
    recipe = test_recipe(architecture)
    rootfs = out_dir / "rootfs.tar"
    rootfs_sha256 = write_rootfs(rootfs)

    manifest = wrf.build_manifest(recipe, rootfs_sha256=rootfs_sha256)
    canonical = wrf.canonical_manifest_bytes(manifest)
    manifest_sha256 = wrf.manifest_digest(manifest)

    guest_runner_sha256 = hashlib.sha256(
        ROOTFS_CONTENT["usr/local/bin/agent-bridge-guest-runner"]).hexdigest()
    sidecar = wrf.build_sidecar(
        recipe, rootfs_sha256=rootfs_sha256,
        guest_runner_sha256=guest_runner_sha256, built_at=BUILT_AT,
        tool_hashes={"node": "2" * 64, "claude": "3" * 64, "codex": "4" * 64},
        manifest=manifest)

    # Signed over the canonical manifest bytes, which is exactly what
    # verify_manifest_trust checks. Signing anything else would produce a
    # fixture that verifies here and nowhere real.
    public_key, signature = sign(canonical)
    anchor = {
        "release": TEST_RELEASE,
        "architecture": architecture,
        "manifest_sha256": manifest_sha256,
        "signature": signature,
        "public_key": public_key,
        "_warning": TEST_KEY_WARNING,
    }

    _write_json(out_dir / "manifest.json", manifest)
    _write_json(out_dir / "sidecar.json", sidecar)
    _write_json(out_dir / "anchor.json", anchor)
    return {"rootfs_sha256": rootfs_sha256, "manifest_sha256": manifest_sha256,
            "public_key": public_key}


def anchor_from(out_dir: Path) -> wrf.TrustAnchor:
    """The written anchor, as the type the verifier takes."""

    raw = json.loads((out_dir / "anchor.json").read_text(encoding="utf-8"))
    return wrf.TrustAnchor(
        release=raw["release"], architecture=raw["architecture"],
        manifest_sha256=raw["manifest_sha256"], signature=raw["signature"],
        public_key=raw["public_key"])


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def check_reproducible(architecture: str = "amd64") -> bool:
    """Build twice into two directories and compare every byte."""

    first = Path(tempfile.mkdtemp(prefix="image-a-"))
    second = Path(tempfile.mkdtemp(prefix="image-b-"))
    try:
        build(first, architecture=architecture)
        build(second, architecture=architecture)
        for name in OUTPUT_NAMES:
            if not filecmp.cmp(first / name, second / name, shallow=False):
                return False
        return True
    finally:
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path,
                        help="where to write the four artefacts")
    parser.add_argument("--architecture", default="amd64",
                        choices=sorted(wrf.ARCHITECTURES))
    parser.add_argument("--check", action="store_true",
                        help="build twice and compare, writing nothing")
    args = parser.parse_args(argv)
    if args.check:
        ok = check_reproducible(args.architecture)
        print(json.dumps({"reproducible": ok}, sort_keys=True))
        return 0 if ok else 1
    if args.out_dir is None:
        parser.error("--out-dir is required unless --check is given")
    digests = build(args.out_dir, architecture=args.architecture)
    print(json.dumps(digests, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
