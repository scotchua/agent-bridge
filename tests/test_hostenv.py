"""Host capability resolution, and the two hardcodes it replaced.

The defect these tests guard against was not subtle and it was not caught by
any existing test, because every test of the execution lanes ran only on
macOS. Two constants decided where both bounded implementation lanes could
run at all:

* ``GIT_BIN = "/Library/Developer/CommandLineTools/usr/bin/git"``, so on a
  Mac without the standalone Command Line Tools, and on every other host,
  the first git call spawned a path that does not exist. The lane reported
  ``TaskError: command spawn failed``: no program, no path, no cause.
* ``_assert_macos()``, which refused the whole lane on any non-Darwin host
  even though only the verification step needs confinement.

So the first test here is a literal source check. A comment saying "do not
hard-code this again" is not enforcement; a failing test is.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import claude_task, codex_task, hostenv

LANES = (
    Path(__file__).resolve().parents[1] / "src/agent_bridge/execution/claude_task.py",
    Path(__file__).resolve().parents[1] / "src/agent_bridge/execution/codex_task.py",
)


class NoRehardcoding(unittest.TestCase):
    def test_no_lane_hard_codes_a_git_path(self):
        for lane in LANES:
            text = lane.read_text(encoding="utf-8")
            self.assertNotIn("CommandLineTools", text, f"{lane.name} hard-codes a git path again")
            self.assertNotIn('GIT_BIN="', text, f"{lane.name} reintroduced the GIT_BIN constant")

    def test_no_lane_refuses_by_platform_name(self):
        """The prose may explain the old assert; the code may not contain it.

        Matched as a definition and as a call rather than as a word, so the
        docstring that records why it went away does not fail this.
        """
        for lane in LANES:
            text = lane.read_text(encoding="utf-8")
            self.assertNotIn("def _assert_macos", text, f"{lane.name} redefines the platform assert")
            self.assertNotIn("_assert_macos()", text, f"{lane.name} calls the platform assert")
            self.assertNotIn('platform.system()!="Darwin"', text,
                             f"{lane.name} refuses by platform name again")

    def test_only_hostenv_names_the_sandbox_binary(self):
        """One place decides which confinement binary exists."""
        for lane in LANES:
            self.assertNotIn('"/usr/bin/sandbox-exec"', lane.read_text(encoding="utf-8"))

    def test_both_lanes_resolve_git_through_hostenv(self):
        for module in (claude_task, codex_task):
            self.assertEqual(module._git_bin(), hostenv.resolve_git())


class ResolveGit(unittest.TestCase):
    def test_finds_the_git_this_host_actually_has(self):
        resolved = hostenv.resolve_git()
        self.assertTrue(resolved.is_absolute())
        self.assertTrue(os.access(resolved, os.X_OK))

    def test_a_host_with_no_git_names_every_path_it_tried(self):
        with self.assertRaises(hostenv.HostCapabilityError) as caught:
            hostenv.resolve_git({"PATH": "/nonexistent-agent-bridge-probe"}, system="Plan9")
        self.assertEqual(caught.exception.code, "git_unavailable")
        # The whole point: the refusal is readable without a debugger.
        self.assertIn("/nonexistent-agent-bridge-probe", caught.exception.detail)
        self.assertIn("tried", caught.exception.detail)

    def test_path_is_the_fallback_not_the_only_source(self):
        """A candidate is preferred, so behaviour does not follow the shell."""
        candidates = hostenv.GIT_CANDIDATES.get(hostenv.platform.system(), ())
        present = [c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)]
        if not present:
            self.skipTest("no fixed candidate exists on this host, so PATH is the source")
        self.assertEqual(str(hostenv.resolve_git({"PATH": ""})), present[0])


class SpawnDiagnostics(unittest.TestCase):
    """The message that replaced "command spawn failed"."""

    def test_a_missing_program_is_named(self):
        for module in (claude_task, codex_task):
            detail = module._spawn_detail(["/no/such/program", "--version"])
            self.assertIn("/no/such/program", detail)
            self.assertIn("no such file", detail)

    def test_a_directory_is_distinguished_from_a_missing_file(self):
        detail = claude_task._spawn_detail([os.path.dirname(os.path.abspath(__file__))])
        self.assertIn("is a directory", detail)

    def test_a_non_executable_file_is_distinguished(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "plain"
            target.write_text("not a program\n")
            os.chmod(target, 0o644)
            if os.access(target, os.X_OK):
                self.skipTest("this account can execute a mode-0644 file")
            self.assertIn("not executable", claude_task._spawn_detail([str(target)]))

    def test_the_old_message_is_gone(self):
        for lane in LANES:
            self.assertNotIn('TaskError("command spawn failed")',
                             lane.read_text(encoding="utf-8"))


class ConfinementSelection(unittest.TestCase):
    def test_an_unsupported_host_is_refused_by_name_not_run_unconfined(self):
        for host in ("Plan9", "Windows"):
            with self.assertRaises(hostenv.HostCapabilityError) as caught:
                hostenv.confinement("synthetic", system=host)
            self.assertEqual(caught.exception.code, "verification_confinement_unavailable")
            self.assertIn(host, caught.exception.detail)

    def test_every_selectable_backend_denies_network(self):
        """Not negotiable. A backend that cannot deny network is not offered."""
        for backend in (hostenv.MACOS_CONFINEMENT, hostenv.LINUX_CONFINEMENT):
            self.assertTrue(backend.denies_network, backend.name)

    def test_a_backend_that_confines_no_reads_carries_synthetic_material_only(self):
        self.assertEqual(hostenv.LINUX_CONFINEMENT.classifications,
                         frozenset({"synthetic"}))
        self.assertFalse(hostenv.LINUX_CONFINEMENT.confines_reads)
        self.assertFalse(hostenv.LINUX_CONFINEMENT.permits("internal_nonclient"))
        self.assertFalse(hostenv.LINUX_CONFINEMENT.permits("public"))

    def test_every_selectable_backend_confines_writes(self):
        """The property an adversarial review rejected this file for lacking.

        The first version of the Linux backend denied network and nothing
        else, and leaned on the harness's post-run snapshot of the source
        repository as the write boundary. That snapshot cannot see a write to
        $HOME or to another checkout, so it was not a boundary. No backend is
        offered now unless it confines writes.
        """
        for backend in (hostenv.MACOS_CONFINEMENT, hostenv.LINUX_CONFINEMENT):
            self.assertTrue(backend.confines_writes, backend.name)

    def test_only_an_independently_verified_backend_says_so(self):
        self.assertTrue(hostenv.MACOS_CONFINEMENT.independently_verified)
        self.assertFalse(hostenv.LINUX_CONFINEMENT.independently_verified)

    def test_the_verified_backend_carries_every_allowed_classification(self):
        for classification in ("synthetic", "public", "internal_nonclient"):
            self.assertTrue(hostenv.MACOS_CONFINEMENT.permits(classification))

    def test_this_host_selects_a_backend_or_says_why_not(self):
        try:
            backend = hostenv.confinement("synthetic")
        except hostenv.HostCapabilityError as exc:
            self.assertIn(exc.code, {"verification_confinement_unavailable",
                                     "verification_confinement_insufficient"})
            return
        self.assertIn(backend.name, {hostenv.MACOS_SANDBOX_EXEC,
                                     hostenv.LINUX_USERNS})

    def test_a_lane_turns_a_host_refusal_into_a_lane_refusal_with_the_code(self):
        """The code survives into the harness's own error text."""
        for module in (claude_task, codex_task):
            original = hostenv.confinement
            hostenv.confinement = lambda *a, **k: (_ for _ in ()).throw(
                hostenv.HostCapabilityError("verification_confinement_unavailable", "probe"))
            try:
                with self.assertRaises(module.TaskError) as caught:
                    module._require_confinement("synthetic")
            finally:
                hostenv.confinement = original
            self.assertIn("verification_confinement_unavailable", str(caught.exception))


class TheLinuxBoundaryProvesItself(unittest.TestCase):
    """The probe runs the real confinement, so these exercise the real thing.

    Every one of these was a live failure while the boundary was being built,
    which is why each is asserted rather than reasoned about:

    * the mount order was wrong, and the kernel refuses to remount a bind
      read-write when its source is already read-only, so sealing the
      filesystem before binding the worktree left the worktree UNWRITABLE and
      every verification command failing;
    * the working directory was inherited from before the namespace, so a
      relative write got EROFS while an absolute write to the same file
      worked;
    * the payload was mapped root with CAP_SYS_ADMIN and could simply remount
      / read-write, which made the whole boundary decorative;
    * os.execv does not search PATH, so an allowlisted bare program name such
      as "pytest" never ran at all.
    """

    def setUp(self):
        if not hostenv._userns_usable():
            self.skipTest("this host cannot establish the Linux write boundary")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.tree = self.base / "tree"
        self.scratch = self.base / "scratch"
        self.outside = self.base / "outside"
        for path in (self.tree, self.scratch, self.outside):
            path.mkdir()

    def confined(self, program, canaries=None):
        argv = hostenv.confined_argv(
            [sys.executable, "-c", program], tree=self.tree, scratch=self.scratch,
            canaries=canaries if canaries is not None else [str(self.outside)])
        return subprocess.run(argv, cwd=str(self.tree), capture_output=True,
                              timeout=60, check=False)

    def test_a_relative_write_in_the_worktree_succeeds(self):
        """The stale working directory bug: this failed with EROFS."""
        completed = self.confined("open('inside.txt','w').write('ok')")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((self.tree / "inside.txt").exists())

    def test_a_write_outside_the_worktree_is_refused(self):
        target = self.outside / "escape.txt"
        completed = self.confined(f"open({str(target)!r},'w').write('bad')")
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(target.exists())

    def test_a_write_to_the_home_directory_is_refused(self):
        target = Path.home() / "agent-bridge-confinement-escape-probe"
        self.addCleanup(lambda: target.unlink() if target.exists() else None)
        completed = self.confined(f"open({str(target)!r},'w').write('bad')",
                                  canaries=[str(Path.home())])
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(target.exists())

    def test_the_payload_cannot_remount_the_filesystem_read_write(self):
        """Without dropping capabilities this returned 0 and the boundary was void."""
        completed = self.confined(
            "import ctypes,sys;l=ctypes.CDLL(None,use_errno=True);"
            "sys.exit(0 if l.mount(b'/',b'/',None,32|4096,None)==0 else 7)")
        self.assertEqual(completed.returncode, 7, completed.stderr)

    def test_a_nested_namespace_buys_the_payload_nothing(self):
        target = self.outside / "nested.txt"
        completed = self.confined(
            "import ctypes;l=ctypes.CDLL(None,use_errno=True);"
            "l.unshare(0x00020000|0x10000000);"
            "l.mount(b'/',b'/',None,32|4096,None);"
            f"open({str(target)!r},'w').write('bad')")
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(target.exists())

    def test_the_network_is_denied(self):
        completed = self.confined(
            "import socket,sys\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
            "except OSError:\n"
            "    sys.exit(0)\n"
            "sys.exit(9)\n")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_a_bare_program_name_is_resolved_on_path(self):
        """os.execv treated the name as a path, so nothing ran."""
        argv = hostenv.confined_argv(["python3", "-c", "print('ran')"],
                                     tree=self.tree, scratch=self.scratch,
                                     canaries=[str(self.outside)])
        completed = subprocess.run(argv, cwd=str(self.tree), capture_output=True,
                                   timeout=60, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(b"ran", completed.stdout)

    def test_a_canary_that_is_writable_refuses_to_run_the_command(self):
        """The self-check is the whole point: no boundary, no work."""
        completed = subprocess.run(
            hostenv.confined_argv([sys.executable, "-c", "print('SHOULD NOT RUN')"],
                                  tree=self.tree, scratch=self.scratch,
                                  # The scratch directory is writable by
                                  # design, so naming it as a canary is a
                                  # contradiction the helper must catch.
                                  canaries=[str(self.scratch)]),
            cwd=str(self.tree), capture_output=True, timeout=60, check=False)
        self.assertEqual(completed.returncode, hostenv.CONFINEMENT_SELFTEST_EXIT)
        self.assertNotIn(b"SHOULD NOT RUN", completed.stdout)
        self.assertIn(b"self-check failed", completed.stderr)

    def test_a_missing_writable_root_is_named_not_a_traceback(self):
        completed = subprocess.run(
            hostenv.confined_argv([sys.executable, "-c", "print('SHOULD NOT RUN')"],
                                  tree=self.tree, scratch=self.base / "never-created",
                                  canaries=[str(self.outside)]),
            cwd=str(self.tree), capture_output=True, timeout=60, check=False)
        self.assertEqual(completed.returncode, hostenv.CONFINEMENT_SELFTEST_EXIT)
        self.assertIn(b"is not a directory", completed.stderr)
        self.assertNotIn(b"SHOULD NOT RUN", completed.stdout)

    def test_a_worktree_path_with_a_space_is_one_argument(self):
        """No shell anywhere, so an awkward path is just an argument."""
        awkward = self.base / "a tree with spaces"
        awkward.mkdir()
        argv = hostenv.confined_argv([sys.executable, "-c", "open('f.txt','w').write('ok')"],
                                     tree=awkward, scratch=self.scratch,
                                     canaries=[str(self.outside)])
        completed = subprocess.run(argv, cwd=str(awkward), capture_output=True,
                                   timeout=60, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((awkward / "f.txt").exists())

    def test_git_still_works_with_a_read_only_git_directory(self):
        """The harness runs git verification commands inside the boundary."""
        source = self.base / "source"
        source.mkdir()
        for command in (["git", "init", "-q", str(source)],
                        ["git", "-C", str(source), "config", "user.email", "a@b.invalid"],
                        ["git", "-C", str(source), "config", "user.name", "A"]):
            subprocess.run(command, check=True, capture_output=True, timeout=60)
        (source / "f.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "-A"], check=True,
                       capture_output=True, timeout=60)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True,
                       capture_output=True, timeout=60)
        linked = self.base / "linked"
        subprocess.run(["git", "-C", str(source), "worktree", "add", "-q", "--detach",
                        str(linked)], check=True, capture_output=True, timeout=60)
        (linked / "f.py").write_text("x = 2\n", encoding="utf-8")
        for index, command in enumerate((["git", "status", "--porcelain"],
                                         ["git", "diff", "--check"])):
            scratch = self.base / f"git-scratch-{index}"
            scratch.mkdir()
            argv = hostenv.confined_argv(command, tree=linked, scratch=scratch,
                                         canaries=[str(self.outside)])
            completed = subprocess.run(argv, cwd=str(linked), capture_output=True,
                                       timeout=60, check=False)
            self.assertEqual(completed.returncode, 0,
                             (command, completed.stderr))


class Describe(unittest.TestCase):
    def test_never_raises_and_names_the_refusal_instead(self):
        for host in ("Darwin", "Linux", "Windows", "Plan9"):
            for classification in ("synthetic", "internal_nonclient"):
                report = hostenv.describe(classification, system=host)
                self.assertEqual(report["platform"], host)
                self.assertTrue("confinement" in report)
                if report["confinement"] is None:
                    self.assertIn("confinement_refusal", report)

    def test_it_reports_the_boundary_rather_than_implying_one(self):
        report = hostenv.describe("synthetic")
        if report.get("confinement") is None:
            self.skipTest("no backend on this host")
        for key in ("denies_network", "confines_reads", "confines_writes"):
            self.assertIsInstance(report[key], bool)


if __name__ == "__main__":
    unittest.main()
