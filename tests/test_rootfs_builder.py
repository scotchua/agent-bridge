import json
from pathlib import Path
import tempfile
import unittest

from tools import build_windows_rootfs as builder


class RootfsBuilderRecipeTests(unittest.TestCase):
    def test_loader_preserves_every_download_integrity_pin(self):
        integrity = "sha512-" + "A" * 86 + "=="
        raw = {
            "architecture": "arm64",
            "base_image": "library/debian",
            "base_digest": "sha256:" + "a" * 64,
            "distro_release": "12.12",
            "node_version": "20.19.0",
            "node_tarball_sha256": "b" * 64,
            "claude_version": "2.1.260",
            "claude_integrity": integrity,
            "claude_native_integrity": integrity,
            "codex_version": "0.153.3",
            "codex_integrity": integrity,
            "codex_native_integrity": integrity,
            "apt_packages": (
                "ca-certificates=1", "curl=1", "git=1", "nftables=1",
                "openssl=1", "python3-minimal=1", "xz-utils=1",
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            recipe = builder.load_recipe(path)
        self.assertEqual(recipe.node_tarball_sha256, raw["node_tarball_sha256"])
        self.assertEqual(recipe.claude_integrity, raw["claude_integrity"])
        self.assertEqual(recipe.codex_integrity, raw["codex_integrity"])
        self.assertEqual(recipe.claude_native_integrity,
                         raw["claude_native_integrity"])
        self.assertEqual(recipe.codex_native_integrity,
                         raw["codex_native_integrity"])


if __name__ == "__main__":
    unittest.main()
