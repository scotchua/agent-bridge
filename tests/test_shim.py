#!/usr/bin/env python3
"""The fake-executable shim, and the property that keeps Windows working.

The bug this guards against is invisible from the machine it is written on.
A `.py` path configured as a peer executable runs fine on POSIX, which honours
the shebang, and fails on Windows with `WinError 193`, because CreateProcess
has no interpreter to fall back on. Development happens on macOS, so nothing
here goes red until someone else clones the repo.

The test that matters is the last one: it does not inspect the shim, it
executes it. A shim that is written correctly and cannot be run is the same
outcome as no shim at all.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests", "fakes"))
import shim  # noqa: E402

FAKES = [os.path.join(REPO, "tests", "fakes", f"fake_{peer}.py")
         for peer in ("claude", "codex")]


class TestShim(unittest.TestCase):
    def test_it_reports_the_platform_it_is_on(self):
        self.assertEqual(shim.WINDOWS, os.name == "nt")

    def test_posix_gets_the_script_itself(self):
        if shim.WINDOWS:
            self.skipTest("POSIX behaviour")
        for fake in FAKES:
            self.assertEqual(fake, shim.executable_for(fake))

    def test_windows_gets_a_cmd_beside_the_fake_not_in_a_temp_dir(self):
        """Durability is the whole reason the shim is not written to TEMP.

        setup_cmd.is_durable() refuses an executable under a temp root. A
        shim placed there is refused by the product's own check, which is how
        test_candidate_verification_path failed on Windows.
        """
        if not shim.WINDOWS:
            self.skipTest("Windows behaviour")
        from agent_bridge import setup_cmd
        for fake in FAKES:
            path = shim.executable_for(fake)
            self.assertTrue(path.endswith(".cmd"), path)
            self.assertEqual(os.path.dirname(fake), os.path.dirname(path))
            self.assertTrue(setup_cmd.is_durable(path),
                            f"the product refuses its own test shim: {path}")

    def test_it_is_idempotent_and_leaves_no_temporary_files(self):
        directory = os.path.join(REPO, "tests", "fakes")
        first = [shim.executable_for(f) for f in FAKES]
        second = [shim.executable_for(f) for f in FAKES]
        self.assertEqual(first, second)
        leftovers = [n for n in os.listdir(directory) if n.endswith(".tmp")]
        self.assertEqual([], leftovers, f"temporary files left behind: {leftovers}")

    def test_a_second_call_does_not_rewrite_the_file(self):
        """Idempotent by path is not idempotent in fact.

        The first version wrote the shim in text mode with a literal \r\n,
        so Windows translated it to \r\r\n on disk while universal newlines
        stripped the CRs back out on read. The comparison could never match
        and every call republished the file. Returning the same path told us
        nothing, which is why this asserts on the bytes and the mtime.
        """
        if not shim.WINDOWS:
            self.skipTest("no file is written on POSIX")
        path = shim.executable_for(FAKES[0])
        before = (open(path, "rb").read(), os.stat(path).st_mtime_ns)
        again = shim.executable_for(FAKES[0])
        after = (open(again, "rb").read(), os.stat(again).st_mtime_ns)
        self.assertEqual(before[0], after[0], "content changed on a second call")
        self.assertEqual(before[1], after[1],
                         "the file was republished; the content check is not matching")
        self.assertNotIn(b"\r\r\n", after[0], "newline translated twice")

    def test_the_returned_path_actually_runs(self):
        """The property, asserted by execution rather than by inspection.

        Runs exactly as the product does: subprocess with shell off, argv
        built as [executable, "--version"]. This is the call that raised
        WinError 193 from preflight.observed_version.
        """
        for fake in FAKES:
            with self.subTest(fake=os.path.basename(fake)):
                path = shim.executable_for(fake)
                proc = subprocess.run([path, "--version"], capture_output=True,
                                      text=True, shell=False, timeout=60,
                                      check=False)
                self.assertEqual(0, proc.returncode,
                                 f"{path} -> {proc.returncode}: {proc.stderr}")
                self.assertTrue(proc.stdout.strip(), "no version reported")


class TestNoFakeIsConfiguredAsARawPyPath(unittest.TestCase):
    """A tripwire for one known regression. Not a proof, and not a sound one.

    One new `os.path.join(..., "fakes", "fake_x.py")` used as a peer
    executable re-breaks Windows, silently from here. Both known occurrences
    were of that shape: the Sandbox harness, and the timeout canary's stub.

    Its limits, from Codex review 9f6612e5, which constructed both cases. A
    BROKEN shape that passes: a raw path assigned on the line after an
    unrelated executable_for() call, satisfying the window, then configured as
    an executable later. A CORRECT shape that fails: a raw path assigned, then
    wrapped in executable_for() several lines further down. Computed filenames
    evade it entirely. Treat a pass as "the known mistake is absent", never as
    "no fake is misconfigured".
    """

    def test_only_the_shim_hands_out_a_fakes_py_path(self):
        import pathlib
        offenders = []
        root = pathlib.Path(REPO)
        for py in sorted(list((root / "tests").rglob("*.py"))
                         + list((root / "canaries").rglob("*.py"))
                         + list((root / "src").rglob("*.py"))):
            if py.name in ("shim.py", os.path.basename(__file__)):
                continue
            try:
                lines = py.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(lines, 1):
                if line.strip().startswith("#"):
                    continue
                # A reference to a fake SCRIPT, not to the directory holding
                # them: `sys.path.insert(..., "fakes")` is not a peer
                # executable and must not be flagged.
                if 'fake_' not in line or '.py' not in line:
                    continue
                # executable_for() may open the call a line or two above, as
                # the canary's does, so look at the enclosing statement rather
                # than this line alone.
                window = "\n".join(lines[max(0, n - 3):n])
                if "executable_for" in window or "fake_shim" in window:
                    continue
                offenders.append(f"{py.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(
            [], offenders,
            "wrap the path in shim.executable_for(); a bare .py cannot be "
            f"executed on Windows: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
