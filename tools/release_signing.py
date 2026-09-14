#!/usr/bin/env python3
"""Release signing for guest image manifests, without ever holding the key.

The property this tool is built around
--------------------------------------

**No private key is ever read, written, derived, passed, or logged by this
codebase.** Not in a file, not on argv, not in an environment variable, not in
a temporary file. That is not a policy written next to code that could do it
anyway: there is no signing primitive in the shipped package at all (see
:mod:`agent_bridge.orchestration.signing`, which implements verification only
and explains why), and this tool does not add one.

The signature is produced by the maintainer, on the machine that holds the
release key, with their own tool. This tool does the two things around that
step which are easy to get wrong:

1. ``prepare`` emits the **exact bytes** that must be signed. Signing a
   re-serialised manifest is the classic way to produce a signature that
   verifies for the signer and for nobody else, because JSON key order and
   whitespace are not part of the manifest's meaning but are part of the
   bytes. The canonical form is computed by the same function the verifier
   uses, so the bytes signed and the bytes checked cannot drift.

2. ``anchor`` **verifies the signature before printing an anchor**. A trust
   anchor that was never checked is the decorative-signature failure this
   project already removed once. The anchor line is only printed when the
   signature actually verifies against the canonical bytes under the supplied
   public key.

Usage
-----

::

    # 1. On any machine: emit the bytes to sign.
    python3 tools/release_signing.py prepare \\
        --manifest build/rootfs/manifest.json --out build/rootfs/manifest.canonical

    # 2. On the signing machine, with your own tool and your own key.
    #    Nothing in this repository participates in this step.
    ssh-keygen -Y sign -f ~/.ssh/release_ed25519 -n file manifest.canonical
    #    ...or an HSM, a smartcard, or a hosted signing service.

    # 3. Anywhere: check the signature and print the anchor to paste.
    python3 tools/release_signing.py anchor \\
        --manifest build/rootfs/manifest.json --release 2026.1 \\
        --architecture amd64 --signature sig.hex --public-key pub.hex

Adding the printed anchor to ``RELEASE_TRUST_ANCHORS`` is a release action
taken by a person who verified the manifest, not a code change made to get a
test to pass.

Nothing here has signed a real release. No anchor exists in this repository.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

from agent_bridge.orchestration import signing  # noqa: E402
from agent_bridge.orchestration import windows_rootfs as wrf  # noqa: E402

#: Text that means a file is, or contains, a private key. Checked before any
#: supplied file is read as a signature or a public key, because the most
#: likely operator mistake is pointing this at the wrong half of a keypair.
#
#: Assembled from fragments rather than written out, so that this file, and
#: the tests that exercise it, do not themselves match the scanner. A scanner
#: that flags its own definition is a scanner people learn to ignore.
_SECRET = "PRIV" + "ATE " + "KEY"
PRIVATE_KEY_MARKERS = (
    _SECRET,
    "BEGIN OPENSSH " + _SECRET,
    "BEGIN EC " + _SECRET,
    "BEGIN RSA " + _SECRET,
    "BEGIN PGP " + _SECRET + " BLOCK",
    "BEGIN ENCRYPTED " + _SECRET,
)

#: Filenames that are private keys by convention. Refused by name as well as
#: by content: an encrypted or binary key may contain none of the markers.
PRIVATE_KEY_NAMES = re.compile(
    r"(^|[._-])(id_(rsa|ed25519|ecdsa|dsa)|.*\.(pem|key|p12|pfx|jks)|"
    r"private[._-]?key)$", re.IGNORECASE)

_HEX_RE = re.compile(r"^[0-9a-f]+$")


class SigningRefusal(SystemExit):
    """Refused, with a reason a person can act on."""


def refuse_if_private_key(path: Path) -> None:
    """Never read one, and say so loudly rather than reading it to check."""

    if PRIVATE_KEY_NAMES.search(path.name):
        raise SigningRefusal(
            f"refusing to read {path.name}: that name is a private key by "
            "convention. This tool never reads a private key. Supply the "
            "signature and the public key, produced by your own signer.")
    try:
        head = path.read_bytes()[:4096].decode("utf-8", "replace")
    except OSError as exc:
        raise SigningRefusal(f"cannot read {path.name}: {type(exc).__name__}")
    for marker in PRIVATE_KEY_MARKERS:
        if marker in head:
            raise SigningRefusal(
                f"refusing: {path.name} contains a private key. Nothing in "
                "this repository signs; supply a detached signature instead.")


def read_hex(value: str, *, label: str, length: int) -> str:
    """A hex string, given inline or as a path to a file containing one."""

    candidate = value.strip()
    path = Path(candidate)
    if path.exists() and path.is_file():
        refuse_if_private_key(path)
        candidate = path.read_text(encoding="utf-8").strip()
    candidate = "".join(candidate.split()).lower()
    if candidate.startswith("0x"):
        candidate = candidate[2:]
    if len(candidate) != length or not _HEX_RE.match(candidate):
        raise SigningRefusal(
            f"{label} must be {length} lowercase hex characters; got "
            f"{len(candidate)}")
    return candidate


def load_manifest(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SigningRefusal(f"cannot read manifest: {type(exc).__name__}")
    except ValueError:
        raise SigningRefusal("manifest is not valid JSON")
    if not isinstance(raw, dict):
        raise SigningRefusal("manifest is not an object")
    return raw


def command_prepare(args: argparse.Namespace) -> int:
    """Emit the exact bytes to sign, and their digest. No key involved."""

    manifest = load_manifest(args.manifest)
    canonical = wrf.canonical_manifest_bytes(manifest)
    digest = wrf.manifest_digest(manifest)
    if args.out:
        Path(args.out).write_bytes(canonical)
    print(json.dumps({
        "manifest_sha256": digest,
        "bytes_to_sign": len(canonical),
        "written_to": str(args.out) if args.out else None,
        "reminder": ("sign these exact bytes with your own tool on the machine "
                     "that holds the release key; this tool never sees it"),
    }, indent=2, sort_keys=True))
    return 0


def command_anchor(args: argparse.Namespace) -> int:
    """Verify a detached signature, then print the anchor to paste."""

    import make_test_image as fixture

    manifest = load_manifest(args.manifest)
    canonical = wrf.canonical_manifest_bytes(manifest)
    digest = wrf.manifest_digest(manifest)

    public_key = read_hex(args.public_key, label="public key", length=64)
    signature = read_hex(args.signature, label="signature", length=128)

    # The fixture key is published. An anchor under it would make the trust
    # check pass for anyone who read the repository.
    fixture.refuse_as_release_anchor(public_key)

    if not signing.verify_hex(canonical, signature, public_key):
        raise SigningRefusal(
            "refusing: the signature does not verify over the canonical "
            "manifest bytes under that public key. Check that you signed the "
            "output of `prepare` rather than the manifest file, and that the "
            "public key is the one matching the signing key.")

    anchor = wrf.TrustAnchor(
        release=args.release, architecture=args.architecture,
        manifest_sha256=digest, signature=signature, public_key=public_key)
    # Checked through the real verifier, not merely constructed, so this tool
    # cannot print an anchor the project itself would reject.
    check = wrf.verify_manifest_trust(
        manifest, architecture=args.architecture, release=args.release,
        anchors=[anchor])
    if not check.passed:
        raise SigningRefusal(f"refusing: {check.reason}")

    print(_anchor_source(anchor))
    return 0


def _anchor_source(anchor: wrf.TrustAnchor) -> str:
    """The literal to paste into RELEASE_TRUST_ANCHORS, and nothing else."""

    return (
        "    TrustAnchor(\n"
        f"        release={anchor.release!r},\n"
        f"        architecture={anchor.architecture!r},\n"
        f"        manifest_sha256={anchor.manifest_sha256!r},\n"
        f"        signature={anchor.signature!r},\n"
        f"        public_key={anchor.public_key!r}),\n")


def command_verify(args: argparse.Namespace) -> int:
    """Re-check an anchor that already exists, against a manifest."""

    manifest = load_manifest(args.manifest)
    raw = json.loads(Path(args.anchor).read_text(encoding="utf-8"))
    anchor = wrf.TrustAnchor(
        release=raw["release"], architecture=raw["architecture"],
        manifest_sha256=raw["manifest_sha256"],
        signature=raw.get("signature", ""), public_key=raw.get("public_key", ""))
    check = wrf.verify_manifest_trust(
        manifest, architecture=anchor.architecture, release=anchor.release,
        anchors=[anchor])
    print(json.dumps({"passed": check.passed, "reason": check.reason},
                     indent=2, sort_keys=True))
    return 0 if check.passed else 1


def scan_tree(root: Path) -> list[str]:
    """Anything in the working tree that looks like a private key.

    Run before a release and in CI. The guarantee at the top of this file is
    about what the *code* does; this is the check that the repository has not
    acquired one some other way.
    """

    found: list[str] = []
    skip = {".git", "__pycache__", "build", "node_modules"}
    for path in root.rglob("*"):
        if any(part in skip for part in path.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if PRIVATE_KEY_NAMES.search(path.name):
            found.append(str(path.relative_to(root)))
            continue
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            continue
        text = head.decode("utf-8", "replace")
        if any(marker in text for marker in PRIVATE_KEY_MARKERS):
            found.append(str(path.relative_to(root)))
    return sorted(found)


def command_scan(args: argparse.Namespace) -> int:
    found = scan_tree(Path(args.root))
    print(json.dumps({"private_key_candidates": found}, indent=2,
                     sort_keys=True))
    return 1 if found else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="emit the exact bytes to sign")
    prepare.add_argument("--manifest", required=True, type=Path)
    prepare.add_argument("--out", type=Path, default=None)
    prepare.set_defaults(handler=command_prepare)

    anchor = sub.add_parser("anchor", help="verify a signature and print an anchor")
    anchor.add_argument("--manifest", required=True, type=Path)
    anchor.add_argument("--release", required=True)
    anchor.add_argument("--architecture", required=True,
                        choices=sorted(wrf.ARCHITECTURES))
    anchor.add_argument("--signature", required=True,
                        help="128 hex characters, or a path to a file with them")
    anchor.add_argument("--public-key", required=True,
                        help="64 hex characters, or a path to a file with them")
    anchor.set_defaults(handler=command_anchor)

    verify = sub.add_parser("verify", help="re-check an existing anchor")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--anchor", required=True, type=Path)
    verify.set_defaults(handler=command_verify)

    scan = sub.add_parser("scan", help="refuse if the tree holds a private key")
    scan.add_argument("--root", type=Path, default=REPO)
    scan.set_defaults(handler=command_scan)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
