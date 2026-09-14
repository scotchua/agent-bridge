"""The signed test image, and the release procedure that never holds a key.

Two things are under test here and they are different claims.

The **fixture** claim is that a complete trust chain can be built offline and
reproducibly: rootfs bytes, to a manifest, to canonical bytes, to a digest, to
a signature, to an anchor the project's own verifier accepts. Every link had a
test before this; the chain did not, and a chain is where this kind of thing
breaks.

The **procedure** claim is stronger and is the one that matters: no private
key is read, written, derived, or accepted anywhere in this repository. That
is tested three ways here. The signing module has no signing primitive. The
release tool refuses a file that is a key by name or by content. And a scan of
the whole working tree finds nothing key-shaped.

No release has been signed. ``RELEASE_TRUST_ANCHORS`` is empty and a test
below keeps it that way until a person with a real key changes it.
"""

from __future__ import annotations

import filecmp
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import make_test_image as fixture  # noqa: E402
import release_signing as rs  # noqa: E402
from agent_bridge.orchestration import signing  # noqa: E402
from agent_bridge.orchestration import windows_rootfs as wrf  # noqa: E402


class ReproducibleImageTests(unittest.TestCase):
    """Two builds, byte for byte."""

    def setUp(self):
        self.first = Path(tempfile.mkdtemp())
        self.second = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.first, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.second, ignore_errors=True)

    def test_every_artefact_is_identical_across_two_builds(self):
        fixture.build(self.first)
        fixture.build(self.second)
        for name in fixture.OUTPUT_NAMES:
            with self.subTest(name):
                self.assertTrue(
                    filecmp.cmp(self.first / name, self.second / name,
                                shallow=False),
                    f"{name} differs between two builds of one recipe")

    def test_the_self_check_agrees(self):
        self.assertTrue(fixture.check_reproducible())

    def test_the_tarball_carries_no_build_machine_facts(self):
        import tarfile

        fixture.build(self.first)
        with tarfile.open(self.first / "rootfs.tar") as archive:
            members = archive.getmembers()
        self.assertTrue(members)
        for member in members:
            with self.subTest(member.name):
                self.assertEqual(member.mtime, wrf.SOURCE_DATE_EPOCH)
                self.assertEqual(member.uname, "")
                self.assertEqual(member.gname, "")
                # Nothing in the guest needs setuid, and a fixture that
                # shipped one would be a fixture teaching the wrong lesson.
                self.assertFalse(member.mode & 0o6000)

    def test_the_members_are_in_a_fixed_order(self):
        import tarfile

        fixture.build(self.first)
        with tarfile.open(self.first / "rootfs.tar") as archive:
            names = archive.getnames()
        self.assertEqual(names, sorted(names))

    def test_the_recipe_passes_the_projects_own_validator(self):
        # A fixture built from a recipe the validator refuses would prove
        # nothing about the real path.
        wrf.validate_recipe(fixture.test_recipe())

    def test_the_built_at_stamp_is_pinned_not_read_from_a_clock(self):
        fixture.build(self.first)
        sidecar = json.loads((self.first / "sidecar.json").read_text())
        self.assertEqual(sidecar["built_at"], fixture.BUILT_AT)


class TrustChainTests(unittest.TestCase):
    """The whole chain, through the real verifier."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.digests = fixture.build(self.out)
        self.manifest = json.loads(
            (self.out / "manifest.json").read_text(encoding="utf-8"))
        self.anchor = fixture.anchor_from(self.out)

    def _check(self, manifest=None, **overrides):
        return wrf.verify_manifest_trust(
            manifest if manifest is not None else self.manifest,
            architecture=overrides.pop("architecture", "amd64"),
            release=overrides.pop("release", fixture.TEST_RELEASE),
            anchors=[overrides.pop("anchor", self.anchor)])

    def test_the_signed_manifest_is_trusted(self):
        check = self._check()
        self.assertTrue(check.passed, check.reason)

    def test_the_manifest_hash_matches_the_tarball_on_disk(self):
        observed = wrf.sha256_path(str(self.out / "rootfs.tar"))
        self.assertEqual(self.manifest["rootfs_sha256"], observed)

    def test_a_changed_manifest_is_not_trusted(self):
        tampered = dict(self.manifest, node_version="20.19.1")
        self.assertFalse(self._check(tampered).passed)

    def test_a_changed_rootfs_no_longer_matches_its_manifest(self):
        (self.out / "rootfs.tar").write_bytes(b"not the image\n")
        self.assertNotEqual(self.manifest["rootfs_sha256"],
                            wrf.sha256_path(str(self.out / "rootfs.tar")))

    def test_a_stripped_signature_is_not_trusted(self):
        bare = wrf.TrustAnchor(
            release=self.anchor.release, architecture=self.anchor.architecture,
            manifest_sha256=self.anchor.manifest_sha256)
        check = self._check(anchor=bare)
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_missing")

    def test_a_signature_from_another_key_is_not_trusted(self):
        other_public, other_signature = fixture.sign(
            wrf.canonical_manifest_bytes(self.manifest), os.urandom(32))
        swapped = wrf.TrustAnchor(
            release=self.anchor.release, architecture=self.anchor.architecture,
            manifest_sha256=self.anchor.manifest_sha256,
            signature=other_signature, public_key=self.anchor.public_key)
        self.assertFalse(self._check(anchor=swapped).passed)

    def test_a_different_release_finds_no_anchor(self):
        self.assertFalse(self._check(release="2026.1").passed)

    def test_a_different_architecture_finds_no_anchor(self):
        self.assertFalse(self._check(architecture="arm64").passed)

    def test_the_signature_is_over_the_canonical_bytes(self):
        # Directly, not through the verifier, so the two cannot agree with
        # each other while both being wrong about what was signed.
        canonical = wrf.canonical_manifest_bytes(self.manifest)
        self.assertTrue(signing.verify_hex(canonical, self.anchor.signature,
                                           self.anchor.public_key))

    def test_a_re_serialised_manifest_produces_the_same_canonical_bytes(self):
        # The reason `prepare` exists: key order and whitespace must not
        # change what gets signed.
        shuffled = dict(reversed(list(self.manifest.items())))
        self.assertEqual(wrf.canonical_manifest_bytes(shuffled),
                         wrf.canonical_manifest_bytes(self.manifest))


class NoPrivateKeyAnywhereTests(unittest.TestCase):
    """The claim at the top of the release tool, tested rather than asserted."""

    def test_the_shipped_signing_module_cannot_sign(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "signing.py").read_text(encoding="utf-8")
        self.assertNotIn("def sign(", source)
        self.assertNotIn("def sign_hex(", source)

    def test_no_shipped_module_derives_a_keypair(self):
        for path in (ROOT / "src").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                # Signing needs the secret scalar, which comes only from
                # clamping a SHA-512 of a seed. No shipped file does that.
                self.assertNotIn("(1 << 254) - 8", source)

    def test_the_working_tree_holds_nothing_key_shaped(self):
        self.assertEqual(rs.scan_tree(ROOT), [])

    def test_the_scanner_finds_a_planted_key_by_content(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        marker = "-----BEGIN OPENSSH " + "PRIV" + "ATE " + "KEY-----\n"
        (directory / "notes.txt").write_text(marker, encoding="utf-8")
        self.assertEqual(rs.scan_tree(directory), ["notes.txt"])

    def test_the_scanner_finds_a_planted_key_by_name(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / "id_ed25519").write_bytes(b"opaque\n")
        (directory / "release.pem").write_bytes(b"opaque\n")
        self.assertEqual(rs.scan_tree(directory), ["id_ed25519", "release.pem"])

    def test_the_scanner_does_not_flag_its_own_definitions(self):
        # A scanner that flags itself is a scanner people learn to ignore.
        self.assertNotIn("tools/release_signing.py", rs.scan_tree(ROOT))
        self.assertNotIn("tests/test_release_signing.py", rs.scan_tree(ROOT))

    def test_the_tool_refuses_a_file_named_like_a_key(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        key = directory / "id_ed25519"
        key.write_bytes(b"opaque\n")
        with self.assertRaises(SystemExit):
            rs.refuse_if_private_key(key)

    def test_the_tool_refuses_a_file_containing_a_key(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory / "signature.txt"
        path.write_text("-----BEGIN " + "PRIV" + "ATE " + "KEY-----\n",
                        encoding="utf-8")
        with self.assertRaises(SystemExit):
            rs.refuse_if_private_key(path)

    def test_a_key_file_passed_as_a_signature_is_refused_before_reading(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        key = directory / "release.key"
        key.write_bytes(b"a" * 128)
        with self.assertRaises(SystemExit):
            rs.read_hex(str(key), label="signature", length=128)


class ReleaseToolTests(unittest.TestCase):
    """prepare, anchor, verify: the three steps, and their refusals."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        fixture.build(self.out)
        self.manifest_path = self.out / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text())
        self.canonical = wrf.canonical_manifest_bytes(self.manifest)
        # A throwaway key, generated here, never written into the repository.
        self.seed = os.urandom(32)
        self.public, self.signature = fixture.sign(self.canonical, self.seed)

    def _hex_files(self):
        (self.out / "pub.hex").write_text(self.public, encoding="utf-8")
        (self.out / "sig.hex").write_text(self.signature, encoding="utf-8")
        return str(self.out / "sig.hex"), str(self.out / "pub.hex")

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "release_signing.py"), *argv],
            capture_output=True, text=True)

    def test_prepare_writes_exactly_the_bytes_that_get_signed(self):
        target = self.out / "manifest.canonical"
        result = self._run("prepare", "--manifest", str(self.manifest_path),
                           "--out", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), self.canonical)

    def test_prepare_reports_the_digest_the_anchor_will_carry(self):
        result = self._run("prepare", "--manifest", str(self.manifest_path))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["manifest_sha256"],
                         wrf.manifest_digest(self.manifest))

    def test_prepare_never_asks_for_a_key(self):
        help_text = self._run("prepare", "--help").stdout
        self.assertNotIn("--key", help_text)
        self.assertNotIn("--private", help_text)

    def test_anchor_prints_a_pasteable_anchor_for_a_real_signature(self):
        signature, public = self._hex_files()
        result = self._run("anchor", "--manifest", str(self.manifest_path),
                           "--release", "2026.1", "--architecture", "amd64",
                           "--signature", signature, "--public-key", public)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TrustAnchor(", result.stdout)
        self.assertIn(self.public, result.stdout)

    def test_anchor_refuses_a_signature_that_does_not_verify(self):
        wrong = fixture.sign(b"different bytes", self.seed)[1]
        (self.out / "pub.hex").write_text(self.public)
        (self.out / "bad.hex").write_text(wrong)
        result = self._run("anchor", "--manifest", str(self.manifest_path),
                           "--release", "2026.1", "--architecture", "amd64",
                           "--signature", str(self.out / "bad.hex"),
                           "--public-key", str(self.out / "pub.hex"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not verify", result.stderr)

    def test_anchor_refuses_the_published_fixture_key(self):
        public = fixture.test_public_key()
        signature = fixture.sign(self.canonical)[1]
        (self.out / "fpub.hex").write_text(public)
        (self.out / "fsig.hex").write_text(signature)
        result = self._run("anchor", "--manifest", str(self.manifest_path),
                           "--release", "2026.1", "--architecture", "amd64",
                           "--signature", str(self.out / "fsig.hex"),
                           "--public-key", str(self.out / "fpub.hex"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture key", result.stderr)

    def test_a_malformed_signature_is_refused_by_length(self):
        with self.assertRaises(SystemExit):
            rs.read_hex("abc", label="signature", length=128)

    def test_a_non_hex_signature_is_refused(self):
        with self.assertRaises(SystemExit):
            rs.read_hex("z" * 128, label="signature", length=128)

    def test_verify_re_checks_an_existing_anchor(self):
        result = self._run("verify", "--manifest", str(self.manifest_path),
                           "--anchor", str(self.out / "anchor.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_verify_fails_on_a_manifest_the_anchor_does_not_cover(self):
        other = self.out / "other.json"
        other.write_text(json.dumps(dict(self.manifest, node_version="20.19.1")))
        result = self._run("verify", "--manifest", str(other),
                           "--anchor", str(self.out / "anchor.json"))
        self.assertEqual(result.returncode, 1)

    def test_scan_exits_zero_on_this_repository(self):
        self.assertEqual(self._run("scan").returncode, 0)


class NoShippedAnchorTests(unittest.TestCase):
    def test_the_project_ships_no_trust_anchor(self):
        # Adding one is a release action taken by a person who verified the
        # manifest, not a code change made to get a test to pass.
        self.assertEqual(wrf.RELEASE_TRUST_ANCHORS, ())

    def test_the_fixture_key_is_not_among_the_shipped_anchors(self):
        published = fixture.test_public_key()
        for anchor in wrf.RELEASE_TRUST_ANCHORS:
            self.assertNotEqual(anchor.public_key, published)

    def test_an_unanchored_manifest_is_refused_by_name(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        fixture.build(out)
        manifest = json.loads((out / "manifest.json").read_text())
        check = wrf.verify_manifest_trust(manifest, architecture="amd64")
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:no_release_anchor")


if __name__ == "__main__":
    unittest.main()
