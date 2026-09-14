"""Portable tests for the pinned rootfs recipe, build plan and manifest.

Everything here is pure or operates on small tarballs built in a temporary
directory, so none of it needs a container engine, Linux, or Windows. What is
not tested is that a real build of this recipe produces a bootable WSL2 guest.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr
from agent_bridge.orchestration import windows_rootfs as wrf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_signing  # noqa: E402 - test-only Ed25519 signer
from agent_bridge.orchestration import windows_wsl as ww

DIGEST = "sha256:" + "a" * 64
ROOTFS_HASH = "b" * 64
RUNNER_HASH = "c" * 64


def _recipe(**overrides):
    fields = dict(
        architecture="arm64",
        base_image="library/debian",
        base_digest=DIGEST,
        distro_release="12.5",
        node_version="20.11.1",
        claude_version="1.2.3",
        codex_version="0.9.0",
        node_tarball_sha256="d" * 64,
        claude_integrity="sha512-" + "A" * 86 + "==",
        codex_integrity="sha512-" + "B" * 86 + "==",
        apt_packages=("ca-certificates=20230311+deb12u1",
                      "nftables=1.0.6-2+deb12u2", "git=1:2.39.5-0+deb12u2"),
    )
    fields.update(overrides)
    return wrf.RootfsRecipe(**fields)


class RecipePinningTests(unittest.TestCase):
    """An image whose contents depend on when it was built cannot be
    described by a manifest."""

    def test_a_fully_pinned_recipe_validates(self):
        wrf.validate_recipe(_recipe())
        self.assertEqual(wrf.base_reference(_recipe()), "library/debian@" + DIGEST)

    def test_an_unknown_architecture_is_refused(self):
        for architecture in ("x86", "armv7", "", "AMD64"):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.validate_recipe(_recipe(architecture=architecture))

    def test_both_supported_architectures_are_accepted(self):
        for architecture in wrf.ARCHITECTURES:
            wrf.validate_recipe(_recipe(architecture=architecture))

    def test_a_tag_cannot_stand_in_for_a_digest(self):
        """A tag can be moved; a digest cannot. Two builds of one recipe must
        start from the same bytes."""
        for base in ("library/debian:12", "library/debian@sha256:" + "a" * 64):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.validate_recipe(_recipe(base_image=base))

    def test_a_malformed_digest_is_refused(self):
        for digest in ("", "sha256:short", "a" * 64, "sha512:" + "a" * 64,
                       "sha256:" + "A" * 64):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.validate_recipe(_recipe(base_digest=digest))

    def test_an_unpinned_version_string_is_refused(self):
        for version in ("latest", "stable", "default", ""):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.validate_recipe(_recipe(node_version=version))
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.validate_recipe(_recipe(claude_version=version))

    def test_an_unpinned_apt_package_is_refused(self):
        for package in ("curl", "", "curl>=1.0"):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.validate_recipe(_recipe(apt_packages=(package,)))


class BuildPlanTests(unittest.TestCase):
    def test_the_build_targets_the_recipes_architecture_explicitly(self):
        argv = wrf.build_argv(_recipe(architecture="amd64"),
                              context_dir="/build/ctx", image_tag="ab:amd64")
        self.assertIn("--platform", argv)
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/amd64")

    def test_the_build_is_not_run_through_a_shell(self):
        argv = wrf.build_argv(_recipe(), context_dir="/build/ctx", image_tag="ab:x")
        for token in argv:
            self.assertNotIn("&&", token)
            self.assertNotIn(";", token)

    def test_only_a_known_container_engine_is_accepted(self):
        for engine in ("docker", "podman"):
            wrf.build_argv(_recipe(), context_dir="/c", image_tag="t", engine=engine)
        for engine in ("bash", "", "docker; rm -rf /"):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.build_argv(_recipe(), context_dir="/c", image_tag="t", engine=engine)

    def test_an_image_tag_with_whitespace_is_refused(self):
        for tag in ("", "a b", "a\tb"):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.build_argv(_recipe(), context_dir="/c", image_tag=tag)

    def test_the_export_uses_a_filesystem_tarball_not_a_layer_archive(self):
        """wsl --import wants a filesystem tar. Handing it an OCI layer
        archive produces a distro whose root is a set of blobs."""
        steps = wrf.export_argv("ab:arm64")
        verbs = [step[1] for step in steps]
        self.assertEqual(verbs, ["create", "export", "rm"])
        self.assertNotIn("save", verbs)

    def test_the_export_always_removes_its_throwaway_container(self):
        steps = wrf.export_argv("ab:arm64")
        self.assertIn("--force", steps[-1])

    def test_output_filenames_carry_the_architecture(self):
        for architecture in wrf.ARCHITECTURES:
            names = wrf.output_names(_recipe(architecture=architecture))
            for value in names.values():
                self.assertIn(architecture, value)


class DockerfileTests(unittest.TestCase):
    def _text(self, **overrides):
        return wrf.dockerfile(_recipe(**overrides), guest_runner_sha256=RUNNER_HASH)

    def test_the_build_fails_if_the_guest_runner_is_not_the_pinned_file(self):
        text = self._text()
        self.assertIn("sha256sum " + ww.GUEST_RUNNER_PATH, text)
        self.assertIn(RUNNER_HASH, text)

    def test_the_guest_boundary_file_is_installed(self):
        self.assertIn("COPY wsl.conf /etc/wsl.conf", self._text())

    def test_the_pinned_versions_reach_the_installer(self):
        text = self._text()
        for value in ("20.11.1", "1.2.3", "0.9.0", "arm64"):
            self.assertIn(value, text)

    def test_the_installer_script_is_removed_from_the_shipped_image(self):
        """Anything left in the image is part of what the manifest pins."""
        self.assertIn("rm -f /tmp/install-pinned-tools", self._text())

    def test_the_base_image_appears_only_as_a_digest(self):
        text = self._text()
        self.assertIn("FROM library/debian@" + DIGEST, text)
        self.assertNotIn("FROM library/debian:", text)

    def test_the_packages_the_guest_depends_on_reach_the_image(self):
        text = self._text()
        for name in wrf.REQUIRED_APT_PACKAGES:
            self.assertIn(name, text)

    def test_the_dockerfile_is_a_pure_function_of_its_inputs(self):
        self.assertEqual(self._text(), self._text())

    def test_a_malformed_runner_hash_is_refused(self):
        for digest in ("", "z" * 64, "c" * 63, "C" * 64):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.dockerfile(_recipe(), guest_runner_sha256=digest)


class ManifestTests(unittest.TestCase):
    def test_the_manifest_is_exactly_what_the_runtime_will_accept(self):
        manifest = wrf.build_manifest(_recipe(), rootfs_sha256=ROOTFS_HASH)
        parsed = ww.parse_manifest(manifest)
        self.assertEqual(parsed.rootfs_sha256, ROOTFS_HASH)
        self.assertEqual(parsed.distro_release, "12.5")

    def test_the_manifest_carries_no_extra_keys(self):
        """The runtime's parser rejects unknown keys, so an extra field here
        would produce a manifest this project's own runtime refuses."""
        manifest = wrf.build_manifest(_recipe(), rootfs_sha256=ROOTFS_HASH)
        self.assertEqual(set(manifest), {
            "schema_version", "distro_release", "rootfs_sha256",
            "node_version", "claude_version", "codex_version"})

    def test_a_malformed_rootfs_hash_is_refused(self):
        for digest in ("", "b" * 63, "B" * 64, None):
            with self.assertRaises(wrf.RootfsRecipeError):
                wrf.build_manifest(_recipe(), rootfs_sha256=digest)

    def test_the_sidecar_carries_the_provenance_the_manifest_cannot(self):
        sidecar = wrf.build_sidecar(
            _recipe(), rootfs_sha256=ROOTFS_HASH, guest_runner_sha256=RUNNER_HASH,
            built_at="2026-03-01T12:00:00+00:00",
            tool_hashes={"node": "d" * 64, "claude": "e" * 64, "codex": "f" * 64})
        self.assertEqual(sidecar["architecture"], "arm64")
        self.assertEqual(sidecar["base_digest"], DIGEST)
        self.assertEqual(sidecar["guest_runner_sha256"], RUNNER_HASH)
        json.dumps(sidecar)

    def test_the_sidecar_records_the_boundary_file_hash_the_host_will_check(self):
        sidecar = wrf.build_sidecar(
            _recipe(), rootfs_sha256=ROOTFS_HASH, guest_runner_sha256=RUNNER_HASH,
            built_at="now", tool_hashes={})
        self.assertEqual(
            sidecar["wsl_conf_sha256"],
            hashlib.sha256(ww.WSL_CONF_CONTENTS.encode("utf-8")).hexdigest())

    def test_a_malformed_tool_hash_in_the_sidecar_is_refused(self):
        with self.assertRaises(wrf.RootfsRecipeError):
            wrf.build_sidecar(_recipe(), rootfs_sha256=ROOTFS_HASH,
                              guest_runner_sha256=RUNNER_HASH, built_at="now",
                              tool_hashes={"node": "nope"})


class VersionsFileTests(unittest.TestCase):
    """The inventory baked into the image is what the versions canary reads,
    so the builder and the guest runner must agree on its shape."""

    def _tools(self):
        return {
            "node": wrf.ToolPin("20.11.1", "/usr/local/bin/node", "d" * 64),
            "claude": wrf.ToolPin("1.2.3", "/usr/local/bin/claude", "e" * 64),
            "codex": wrf.ToolPin("0.9.0", "/usr/local/bin/codex", "f" * 64),
        }

    def test_the_guest_runner_accepts_what_the_builder_writes(self):
        written = wrf.build_versions_file(_recipe(), self._tools())
        parsed = gr.load_versions(written)
        self.assertEqual(set(parsed), set(gr.TOOL_NAMES))
        self.assertEqual(parsed["node"]["sha256"], "d" * 64)

    def test_the_builder_and_the_guest_runner_name_the_same_tools(self):
        self.assertEqual(set(self._tools()), set(gr.TOOL_NAMES))

    def test_a_missing_or_extra_tool_is_refused(self):
        tools = self._tools()
        del tools["codex"]
        with self.assertRaises(wrf.RootfsRecipeError):
            wrf.build_versions_file(_recipe(), tools)

    def test_a_relative_tool_path_is_refused(self):
        tools = self._tools()
        tools["node"] = wrf.ToolPin("20.11.1", "usr/local/bin/node", "d" * 64)
        with self.assertRaises(wrf.RootfsRecipeError):
            wrf.build_versions_file(_recipe(), tools)

    def test_the_inventory_path_matches_where_the_runner_looks(self):
        self.assertEqual(wrf.VERSIONS_PATH, gr.VERSIONS_PATH)


class TarNormalisationTests(unittest.TestCase):
    def _member(self, name="usr/bin/tool", mode=0o755):
        member = tarfile.TarInfo(name)
        member.mode = mode
        member.uid = 1000
        member.gid = 1000
        member.uname = "builder"
        member.gname = "builder"
        member.mtime = 1_700_000_000
        member.size = 0
        return member

    def test_build_machine_facts_are_stripped(self):
        normalised = wrf.normalise_member(self._member())
        self.assertEqual(normalised.mtime, wrf.SOURCE_DATE_EPOCH)
        self.assertEqual((normalised.uid, normalised.gid), (0, 0))
        self.assertEqual((normalised.uname, normalised.gname), ("", ""))

    def test_setuid_and_setgid_bits_are_cleared(self):
        """Nothing in this image needs them, and a guest that ships one has a
        privilege boundary nobody reviewed."""
        for mode in (0o4755, 0o2755, 0o6755):
            self.assertEqual(wrf.normalise_member(self._member(mode=mode)).mode & 0o6000, 0)

    def test_ordinary_permission_bits_survive(self):
        self.assertEqual(wrf.normalise_member(self._member(mode=0o644)).mode & 0o777, 0o644)

    def test_members_are_sorted_so_order_cannot_vary(self):
        members = [self._member("b"), self._member("a"), self._member("c")]
        self.assertEqual([m.name for m in wrf.normalised_members(members)],
                         ["a", "b", "c"])

    def test_two_tarballs_differing_only_in_build_facts_normalise_identically(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            digests = []
            for index, (mtime, uid, order) in enumerate(
                    [(1_700_000_000, 1000, ["a", "b"]), (1_800_000_000, 501, ["b", "a"])]):
                source = base / f"in{index}.tar"
                with tarfile.open(source, "w") as archive:
                    for name in order:
                        member = tarfile.TarInfo(name)
                        member.size = 3
                        member.mtime = mtime
                        member.uid = member.gid = uid
                        member.uname = member.gname = f"user{uid}"
                        archive.addfile(member, io.BytesIO(b"xyz"))
                digests.append(wrf.normalise_tar(str(source), str(base / f"out{index}.tar")))
            self.assertEqual(digests[0], digests[1])

    def test_a_normalised_tar_still_contains_its_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "in.tar"
            with tarfile.open(source, "w") as archive:
                member = tarfile.TarInfo("etc/wsl.conf")
                payload = ww.WSL_CONF_CONTENTS.encode("utf-8")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            destination = base / "out.tar"
            wrf.normalise_tar(str(source), str(destination))
            with tarfile.open(destination) as archive:
                extracted = archive.extractfile("etc/wsl.conf").read()
        self.assertEqual(extracted, ww.WSL_CONF_CONTENTS.encode("utf-8"))

    def test_the_returned_hash_is_the_hash_of_the_file_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "in.tar"
            with tarfile.open(source, "w") as archive:
                member = tarfile.TarInfo("a")
                member.size = 0
                archive.addfile(member, io.BytesIO(b""))
            destination = base / "out.tar"
            returned = wrf.normalise_tar(str(source), str(destination))
            self.assertEqual(returned, wrf.sha256_path(str(destination)))


class ImageVerificationTests(unittest.TestCase):
    def _sidecar(self, **overrides):
        fields = dict(architecture="arm64", rootfs_sha256=ROOTFS_HASH)
        fields.update(overrides)
        return fields

    def _manifest(self):
        return wrf.build_manifest(_recipe(), rootfs_sha256=ROOTFS_HASH)

    def test_a_matching_image_passes(self):
        check = wrf.verify_image(manifest=self._manifest(), observed_sha256=ROOTFS_HASH,
                                 sidecar=self._sidecar(), host_architecture="arm64")
        self.assertTrue(check.passed)

    def test_a_hash_mismatch_fails(self):
        check = wrf.verify_image(manifest=self._manifest(), observed_sha256="9" * 64,
                                 sidecar=self._sidecar(), host_architecture="arm64")
        self.assertEqual(check.reason, "rootfs_hash_mismatch")

    def test_the_wrong_architecture_fails_even_when_the_hash_matches(self):
        """It imports fine and then cannot run a single program in the guest."""
        check = wrf.verify_image(manifest=self._manifest(), observed_sha256=ROOTFS_HASH,
                                 sidecar=self._sidecar(), host_architecture="amd64")
        self.assertEqual(check.reason, "architecture_mismatch")

    def test_a_missing_sidecar_is_not_read_as_the_right_architecture(self):
        check = wrf.verify_image(manifest=self._manifest(), observed_sha256=ROOTFS_HASH,
                                 sidecar=None, host_architecture="arm64")
        self.assertEqual(check.reason, "architecture_unknown")

    def test_a_sidecar_disagreeing_with_the_manifest_fails(self):
        check = wrf.verify_image(
            manifest=self._manifest(), observed_sha256=ROOTFS_HASH,
            sidecar=self._sidecar(rootfs_sha256="9" * 64), host_architecture="arm64")
        self.assertEqual(check.reason, "sidecar_hash_mismatch")

    def test_an_unreadable_manifest_fails_closed(self):
        check = wrf.verify_image(manifest={"schema_version": 1},
                                 observed_sha256=ROOTFS_HASH)
        self.assertTrue(check.reason.startswith("manifest_invalid"))

    def test_a_malformed_observed_hash_fails_closed(self):
        check = wrf.verify_image(manifest=self._manifest(), observed_sha256="nope")
        self.assertEqual(check.reason, "observed_hash_invalid")

    def test_host_architecture_mapping_covers_the_real_platform_strings(self):
        self.assertEqual(wrf.host_architecture_for("AMD64"), "amd64")
        self.assertEqual(wrf.host_architecture_for("x86_64"), "amd64")
        self.assertEqual(wrf.host_architecture_for("ARM64"), "arm64")
        self.assertEqual(wrf.host_architecture_for("aarch64"), "arm64")

    def test_an_unrecognised_machine_string_maps_to_nothing(self):
        for machine in ("i686", "armv7l", "", None):
            self.assertIsNone(wrf.host_architecture_for(machine))


class DocumentedLimitationTests(unittest.TestCase):
    def test_reproducibility_is_described_as_comparison_not_proof(self):
        text = wrf.REPRODUCIBILITY_LIMITATION
        self.assertIn("not a bit-for-bit reproducible build", text)
        self.assertIn("comparison, not a proof", text)

    def test_no_image_is_committed_to_git(self):
        self.assertIn("no rootfs tarball is committed to git",
                      wrf.IMAGE_DISTRIBUTION_LIMITATION)
        self.assertEqual([path for path in ROOT.rglob("*.tar")
                          if ".git" not in path.parts], [])

    def test_no_live_validation_is_claimed(self):
        self.assertIn("has been imported into WSL2 on a live", wrf.NO_LIVE_VALIDATION)



class RecipeHonestyTests(unittest.TestCase):
    """A recipe that cannot be built must not validate as if it could."""

    def test_the_all_zero_placeholder_digest_is_refused(self):
        with self.assertRaisesRegex(wrf.RootfsRecipeError, "placeholder"):
            wrf.validate_recipe(_recipe(base_digest=wrf.PLACEHOLDER_DIGEST))

    def test_a_placeholder_provider_version_is_refused(self):
        with self.assertRaisesRegex(wrf.RootfsRecipeError, "placeholder"):
            wrf.validate_recipe(_recipe(claude_version="1.0.0"))
        with self.assertRaisesRegex(wrf.RootfsRecipeError, "placeholder"):
            wrf.validate_recipe(_recipe(codex_version="0.1.0"))

    def test_a_recipe_without_nftables_is_refused(self):
        with self.assertRaisesRegex(wrf.RootfsRecipeError, "nftables"):
            wrf.validate_recipe(_recipe(apt_packages=(
                "ca-certificates=20230311+deb12u1", "git=1:2.39.5-0+deb12u2")))

    def test_a_recipe_without_git_is_refused(self):
        with self.assertRaisesRegex(wrf.RootfsRecipeError, "git"):
            wrf.validate_recipe(_recipe(apt_packages=(
                "ca-certificates=20230311+deb12u1", "nftables=1.0.6-2+deb12u2")))

    def test_the_canary_and_the_diff_depend_on_those_two_packages(self):
        """Named here so the coupling is not accidental."""
        from agent_bridge.orchestration import guest_runner as gr
        self.assertEqual(gr.NFT_PATH, "/usr/sbin/nft")
        self.assertEqual(gr.GIT_PATH, "/usr/bin/git")

    def test_the_shipped_recipes_do_not_yet_validate(self):
        """The recipes in tools/rootfs/recipes are stubs, and say so.

        Nothing in this repository has observed a real base-image digest or a
        real pinned provider release, so the shipped recipe files cannot be
        built. This test exists so that stops being a quiet fact: it fails the
        day someone fills them in, which is the day the claim "the rootfs is
        buildable" becomes true and this test should be replaced by one that
        asserts the opposite.
        """
        import json
        recipes = ROOT / "tools" / "rootfs" / "recipes"
        for path in sorted(recipes.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("NOT BUILDABLE", raw.pop("_stub", ""), path.name)
            raw["apt_packages"] = tuple(raw.get("apt_packages", ()))
            with self.assertRaises(wrf.RootfsRecipeError, msg=path.name):
                wrf.validate_recipe(wrf.RootfsRecipe(**raw))



class DownloadIntegrityTests(unittest.TestCase):
    """Every fetched byte is checked against something that was here first."""

    def test_a_recipe_without_a_node_digest_is_refused(self):
        with self.assertRaisesRegex(wrf.RootfsRecipeError, "node_tarball_sha256"):
            wrf.validate_recipe(_recipe(node_tarball_sha256=""))

    def test_a_node_digest_must_be_a_digest(self):
        with self.assertRaisesRegex(wrf.RootfsRecipeError, "node_tarball_sha256"):
            wrf.validate_recipe(
                _recipe(node_tarball_sha256="REPLACE-WITH-OBSERVED-SHA256"))

    def test_each_provider_package_needs_an_integrity_pin(self):
        for field in ("claude_integrity", "codex_integrity"):
            with self.assertRaisesRegex(wrf.RootfsRecipeError, field):
                wrf.validate_recipe(_recipe(**{field: ""}))
            with self.assertRaisesRegex(wrf.RootfsRecipeError, field):
                wrf.validate_recipe(_recipe(**{field: "sha512-short"}))

    def test_the_dockerfile_hands_every_pin_to_the_installer(self):
        text = wrf.dockerfile(_recipe(), guest_runner_sha256=RUNNER_HASH)
        self.assertIn("--node-sha256 " + "d" * 64, text)
        self.assertIn("--claude-integrity sha512-" + "A" * 86 + "==", text)
        self.assertIn("--codex-integrity sha512-" + "B" * 86 + "==", text)

    def test_the_installer_never_learns_a_hash_from_the_download_origin(self):
        """A checksum file fetched next to the tarball proves nothing."""
        installer = (ROOT / "tools" / "rootfs" / "install-pinned-tools").read_text(
            encoding="utf-8")
        self.assertNotIn("SHASUMS256.txt", installer)
        self.assertIn("--node-sha256", installer)
        self.assertIn("--claude-integrity", installer)
        self.assertIn("--codex-integrity", installer)

    def test_the_installer_verifies_provider_tarballs_before_npm_runs_them(self):
        installer = (ROOT / "tools" / "rootfs" / "install-pinned-tools").read_text(
            encoding="utf-8")
        verify = installer.index("verify_integrity \"$CLAUDE_TGZ\"")
        install = installer.index("npm install --global --no-fund --no-audit \"$CLAUDE_TGZ\"")
        self.assertLess(verify, install)
        # Never "npm install <name>@<version>": that resolves, downloads and
        # executes install scripts in one step with nothing to compare against.
        self.assertNotIn("@anthropic-ai/claude-code@", installer)
        self.assertNotIn("@openai/codex@", installer)


class ReleaseTrustTests(unittest.TestCase):
    """The manifest is a security input, so something must vouch for it."""

    def _manifest(self):
        return wrf.build_manifest(_recipe(), rootfs_sha256=ROOTFS_HASH)

    def test_the_project_ships_no_trust_anchor(self):
        self.assertEqual(wrf.RELEASE_TRUST_ANCHORS, ())

    def test_without_an_anchor_every_manifest_is_untrusted(self):
        check = wrf.verify_manifest_trust(self._manifest(), architecture="arm64")
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:no_release_anchor")

    def test_a_matching_digest_is_not_enough_without_a_signature(self):
        """The regression. This used to return trusted."""
        manifest = self._manifest()
        anchor = wrf.TrustAnchor(release="v1", architecture="arm64",
                                 manifest_sha256=wrf.manifest_digest(manifest))
        check = wrf.verify_manifest_trust(manifest, architecture="arm64",
                                          anchors=(anchor,))
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_missing")

    def test_a_manifest_matching_its_signed_release_anchor_is_trusted(self):
        manifest = self._manifest()
        public, signature = test_signing.sign(
            wrf.canonical_manifest_bytes(manifest), b"v" * 32)
        anchor = wrf.TrustAnchor(release="v1", architecture="arm64",
                                 manifest_sha256=wrf.manifest_digest(manifest),
                                 signature=signature.hex(),
                                 public_key=public.hex())
        check = wrf.verify_manifest_trust(manifest, architecture="arm64",
                                          anchors=(anchor,))
        self.assertTrue(check.passed, check.reason)

    def test_a_manifest_from_another_build_is_refused(self):
        manifest = self._manifest()
        anchor = wrf.TrustAnchor(release="v1", architecture="arm64",
                                 manifest_sha256="e" * 64)
        check = wrf.verify_manifest_trust(manifest, architecture="arm64",
                                          anchors=(anchor,))
        self.assertEqual(check.reason, "manifest_untrusted:digest_mismatch")

    def test_an_anchor_for_the_other_architecture_does_not_count(self):
        manifest = self._manifest()
        anchor = wrf.TrustAnchor(release="v1", architecture="amd64",
                                 manifest_sha256=wrf.manifest_digest(manifest))
        check = wrf.verify_manifest_trust(manifest, architecture="arm64",
                                          anchors=(anchor,))
        self.assertEqual(check.reason, "manifest_untrusted:no_anchor_for_release")

    def test_a_signature_that_does_not_check_out_is_not_a_signature(self):
        """There is no longer an "unverified" outcome: it is checked, or refused."""
        manifest = self._manifest()
        anchor = wrf.TrustAnchor(release="v1", architecture="arm64",
                                 manifest_sha256=wrf.manifest_digest(manifest),
                                 signature="deadbeef", public_key="k")
        check = wrf.verify_manifest_trust(manifest, architecture="arm64",
                                          anchors=(anchor,))
        self.assertEqual(check.reason, "manifest_untrusted:signature_invalid")

    def test_a_rejecting_verifier_refuses_the_manifest(self):
        manifest = self._manifest()
        anchor = wrf.TrustAnchor(release="v1", architecture="arm64",
                                 manifest_sha256=wrf.manifest_digest(manifest),
                                 signature="deadbeef", public_key="k")
        check = wrf.verify_manifest_trust(
            manifest, architecture="arm64", anchors=(anchor,),
            verify_signature=lambda payload, signature, key: False)
        self.assertEqual(check.reason, "manifest_untrusted:signature_invalid")

    def test_a_verifier_that_raises_has_not_accepted_anything(self):
        manifest = self._manifest()
        anchor = wrf.TrustAnchor(release="v1", architecture="arm64",
                                 manifest_sha256=wrf.manifest_digest(manifest),
                                 signature="deadbeef", public_key="k")
        def explode(payload, signature, key):
            raise RuntimeError("no verifier configured")
        check = wrf.verify_manifest_trust(manifest, architecture="arm64",
                                          anchors=(anchor,),
                                          verify_signature=explode)
        self.assertEqual(check.reason, "manifest_untrusted:signature_error")

    def test_an_accepting_verifier_passes(self):
        manifest = self._manifest()
        anchor = wrf.TrustAnchor(release="v1", architecture="arm64",
                                 manifest_sha256=wrf.manifest_digest(manifest),
                                 signature="deadbeef", public_key="k")
        seen = []
        def accept(payload, signature, key):
            seen.append((payload, signature, key))
            return True
        check = wrf.verify_manifest_trust(manifest, architecture="arm64",
                                          anchors=(anchor,),
                                          verify_signature=accept)
        self.assertTrue(check.passed, check.reason)
        self.assertEqual(seen[0][0], wrf.canonical_manifest_bytes(manifest))

    def test_the_digest_is_over_canonical_bytes_not_formatting(self):
        manifest = self._manifest()
        respaced = json.loads(json.dumps(manifest, indent=4))
        self.assertEqual(wrf.manifest_digest(manifest),
                         wrf.manifest_digest(respaced))


class ReleaseBlockerTests(unittest.TestCase):
    """Unreleasable is a value this code returns, not a caveat in a document."""

    def test_the_workflow_is_currently_unreleasable(self):
        blockers = wrf.release_blockers()
        self.assertIn("manifest_trust_anchor_missing", blockers)
        self.assertIn("unreleasable", wrf.ARTIFACT_WORKFLOW_BLOCKER)

    def test_both_architectures_must_be_covered(self):
        anchor = wrf.TrustAnchor(release="v1", architecture="amd64",
                                 manifest_sha256="a" * 64, signature="s")
        blockers = wrf.release_blockers(anchors=(anchor,))
        self.assertIn("manifest_trust_anchor_missing:arm64", blockers)
        self.assertNotIn("manifest_trust_anchor_missing:amd64", blockers)

    def test_an_unsigned_anchor_is_itself_a_blocker(self):
        anchors = tuple(
            wrf.TrustAnchor(release="v1", architecture=architecture,
                            manifest_sha256="a" * 64)
            for architecture in wrf.ARCHITECTURES)
        self.assertIn("manifest_signature_missing:v1",
                      wrf.release_blockers(anchors=anchors))

    def test_a_missing_or_unbuildable_recipe_blocks_the_release(self):
        blockers = wrf.release_blockers({"arm64": _recipe(node_tarball_sha256="")})
        self.assertIn("recipe_missing:amd64", blockers)
        self.assertTrue(
            any(one.startswith("recipe_unbuildable:arm64:") for one in blockers),
            blockers)

    def test_nothing_is_left_when_every_condition_is_met(self):
        anchors = tuple(
            wrf.TrustAnchor(release="v1", architecture=architecture,
                            manifest_sha256="a" * 64, signature="s",
                            public_key="k")
            for architecture in wrf.ARCHITECTURES)
        recipes = {architecture: _recipe(architecture=architecture)
                   for architecture in wrf.ARCHITECTURES}
        self.assertEqual(wrf.release_blockers(recipes, anchors=anchors), ())

    def test_the_sidecar_carries_the_digest_and_the_blockers(self):
        recipe = _recipe()
        manifest = wrf.build_manifest(recipe, rootfs_sha256=ROOTFS_HASH)
        sidecar = wrf.build_sidecar(
            recipe, rootfs_sha256=ROOTFS_HASH, guest_runner_sha256=RUNNER_HASH,
            built_at="2026-01-01T00:00:00+00:00",
            tool_hashes={"node": "f" * 64}, manifest=manifest)
        self.assertEqual(sidecar["manifest_sha256"], wrf.manifest_digest(manifest))
        self.assertIn("manifest_trust_anchor_missing", sidecar["release_blockers"])

    def test_both_shipped_recipes_exist_so_both_architectures_can_be_built(self):
        recipes = ROOT / "tools" / "rootfs" / "recipes"
        names = sorted(path.stem for path in recipes.glob("*.json"))
        self.assertEqual(names, sorted(wrf.ARCHITECTURES))


class ManifestSignatureTests(unittest.TestCase):
    """A digest says which manifest. Only a signature says whose.

    The regression these protect against is the previous behaviour: an anchor
    whose digest matched returned trusted even with no signature at all, and a
    signature that was present but unchecked was reported as merely
    "unverified" while still being carried as if it meant something.
    """

    MANIFEST = {"schema_version": 1, "distro_release": "test",
                "rootfs_sha256": "a" * 64, "node_version": "22.0.0",
                "claude_version": "1.0.0", "codex_version": "1.0.0"}

    def setUp(self):
        self.digest = wrf.manifest_digest(self.MANIFEST)
        self.public, self.signature = test_signing.sign(
            wrf.canonical_manifest_bytes(self.MANIFEST), b"r" * 32)

    def _anchor(self, **overrides):
        fields = {"release": "1.0", "architecture": "amd64",
                  "manifest_sha256": self.digest,
                  "signature": self.signature.hex(),
                  "public_key": self.public.hex()}
        fields.update(overrides)
        return wrf.TrustAnchor(**fields)

    def _verify(self, anchor, **kwargs):
        return wrf.verify_manifest_trust(self.MANIFEST, architecture="amd64",
                                         anchors=[anchor], **kwargs)

    def test_a_properly_signed_anchor_is_trusted(self):
        check = self._verify(self._anchor())
        self.assertTrue(check.passed, check.reason)

    def test_an_unsigned_anchor_is_never_trusted_however_well_the_digest_matches(self):
        check = self._verify(self._anchor(signature="", public_key=""))
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_missing")

    def test_a_signature_with_no_key_to_check_it_against_is_refused(self):
        check = self._verify(self._anchor(public_key=""))
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:public_key_missing")

    def test_whitespace_is_not_a_signature(self):
        check = self._verify(self._anchor(signature="   "))
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_missing")

    def test_a_signature_from_the_wrong_key_is_refused(self):
        other, _ = test_signing.sign(b"anything", b"q" * 32)
        check = self._verify(self._anchor(public_key=other.hex()))
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_invalid")

    def test_a_signature_over_different_bytes_is_refused(self):
        _key, wrong = test_signing.sign(b"some other manifest", b"r" * 32)
        check = self._verify(self._anchor(signature=wrong.hex()))
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_invalid")

    def test_a_tampered_manifest_no_longer_matches_its_own_anchor(self):
        tampered = dict(self.MANIFEST, rootfs_sha256="b" * 64)
        check = wrf.verify_manifest_trust(tampered, architecture="amd64",
                                          anchors=[self._anchor()])
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:digest_mismatch")

    def test_a_verifier_that_raises_has_not_accepted_anything(self):
        def explode(*_args):
            raise RuntimeError("no")

        check = self._verify(self._anchor(), verify_signature=explode)
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_error")

    def test_the_default_verifier_is_the_real_one(self):
        """No verifier supplied must mean checked, not waved through."""
        check = self._verify(self._anchor(signature="ab" * 64))
        self.assertFalse(check.passed)
        self.assertEqual(check.reason, "manifest_untrusted:signature_invalid")

    def test_an_anchor_missing_a_key_is_a_release_blocker(self):
        blockers = wrf.release_blockers(anchors=[self._anchor(public_key="")])
        self.assertIn("manifest_public_key_missing:1.0", blockers)

    def test_an_unsigned_anchor_is_a_release_blocker(self):
        blockers = wrf.release_blockers(
            anchors=[self._anchor(signature="", public_key="")])
        self.assertIn("manifest_signature_missing:1.0", blockers)

    def test_the_signed_property_requires_both_halves(self):
        self.assertTrue(self._anchor().signed)
        self.assertFalse(self._anchor(signature="").signed)
        self.assertFalse(self._anchor(public_key="").signed)


if __name__ == "__main__":
    unittest.main()
