import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

CLAUDE_TASK = Path(__file__).resolve().parent.parent / "src" / "agent_bridge" / "execution" / "claude_task.py"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import claude_task as module
from agent_bridge.execution import hostenv


def requires_confinement(case):
    """Skip, by name, a test that needs a verification confinement backend.

    GitHub's Ubuntu runners cannot enter an unprivileged network namespace and
    a Mac without sandbox-exec is equally unserved, so on those hosts
    ``hostenv.confinement`` refuses and the lane cannot run verification at
    all. These tests exercise the lane end to end, or the boundary itself, so
    they genuinely need one. Saying so and skipping is how this project
    already treats a host that cannot satisfy a fixture (see
    tests/platform_support.py); failing would report a defect in the code
    when the fact is a property of the machine.

    It is deliberately NOT applied to tests about request validation. Those
    must pass everywhere, which is why ``run_task`` validates the request
    before it probes the host.
    """
    try:
        hostenv.confinement("synthetic")
    except hostenv.HostCapabilityError as exc:
        case.skipTest("no verification confinement backend on this host: " + exc.code)

from agent_bridge.execution import claude_task
from agent_bridge.execution.claude_task import (
    TaskError, _command, _env, _git, _relevant_paths, _remove, _run,
    _result_detail, _sandboxed, _source_state, _with_result_detail, main, run_task)



def _isolated_store(case, root):
    """A canonical store under a temporary home, so no real one is touched.

    The lane accepts exactly one directory, derived from the user's home. Tests
    therefore need their own home rather than their own directory, and must not
    reach the developer's real ~/.agent-bridge/claude-home.
    """

    home = root / "home"
    store = home / ".agent-bridge" / "claude-home"
    store.mkdir(mode=0o700, parents=True)
    # Both variables, not just HOME: Path.home() resolves through
    # os.path.expanduser, and ntpath.expanduser (what that is on Windows)
    # checks USERPROFILE first and never consults HOME at all. Patching only
    # HOME left Path.home() pointing at the real developer profile on
    # Windows, so the lane correctly refused this fixture as "not its own
    # store" rather than the test ever reaching what it meant to exercise.
    patch = mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)})
    patch.start()
    case.addCleanup(patch.stop)
    return store

class ClaudeTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("before\n")
        subprocess.run(["git", "add", "value.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Replace the fixture value.\n")
        self.fake = self.root / "claude"
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf 'after\\n' > value.txt\nprintf '{\"result\":\"done\",\"is_error\":false}\\n'\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.store = _isolated_store(self, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_cli_command_keeps_the_bounded_tool_set(self):
        argv = _command(self.fake, "sonnet", "high")
        self.assertIn("--safe-mode", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")
        self.assertNotIn("Bash", argv)
        self.assertNotIn("--restricted", argv)
        self.assertNotIn("--permission-prompts", argv)

    def test_returns_patch_and_does_not_touch_source(self):
        requires_confinement(self)
        result = run_task(brief=self.brief, repo=self.repo,
                          task_root=self.root / "tasks", claude_bin=self.fake,
                          claude_config_dir=self.store,
                     classification="synthetic", model="fake", effort="low",
                          verify_argv=[["git", "diff", "--check"]])
        error_log = Path(result["job_dir"]) / "verify-1.stderr"
        self.assertEqual(result["status"], "complete", {"receipt": result, "stderr": error_log.read_text() if error_log.exists() else None})
        self.assertEqual((self.repo / "value.txt").read_text(), "before\n")
        patch = Path(result["patch"]).read_text()
        self.assertIn("+after", patch)
        receipt = json.loads((Path(result["job_dir"]) / "receipt.json").read_text())
        self.assertFalse(receipt["permission_to_land"])
        self.assertEqual(receipt["verification"][0]["returncode"], 0)
        self.assertTrue(receipt["fresh_worktree_patch_match"])
        self.assertFalse((Path(result["job_dir"]) / "generation-worktree").exists())
        self.assertFalse((Path(result["job_dir"]) / "verification-worktree").exists())

    def test_a_bom_prefixed_brief_is_not_rejected(self):
        # A Windows editor or PowerShell's default encoding can prepend a
        # UTF-8 BOM to a brief file this project never wrote itself.
        requires_confinement(self)
        self.brief.write_bytes(b"\xef\xbb\xbf" + b"Replace the fixture value.\n")
        result = run_task(brief=self.brief, repo=self.repo,
                          task_root=self.root / "tasks", claude_bin=self.fake,
                          claude_config_dir=self.store,
                     classification="synthetic", model="fake", effort="low",
                          verify_argv=[["git", "diff", "--check"]])
        self.assertEqual(result["status"], "complete")

    def test_a_verify_command_cannot_swap_the_delivered_patch(self):
        """Confinement stops this; this check does not depend on confinement.

        ``run_task`` returns a path, and verification runs between writing
        that file and returning it. A verify command that reached the job
        directory could leave the receipt recording one digest while the
        caller applied different bytes. The job directory is a confinement
        canary on Linux and outside the sandbox profile on macOS, so this is
        defence in depth, which is the point: it holds if either of those is
        ever widened.
        """
        requires_confinement(self)      # run_task refuses before generation
        real = claude_task._sandboxed

        def rewrite(command, tree, scratch, env, timeout, backend):
            result = real(command, tree, scratch, env, timeout, backend)
            for job in (self.root / "tasks").rglob("changes.patch"):
                job.write_bytes(b"--- not the patch that was generated\n")
            return result

        with mock.patch.object(claude_task, "_sandboxed", rewrite):
            with self.assertRaises(TaskError) as caught:
                run_task(brief=self.brief, repo=self.repo,
                         task_root=self.root / "tasks", claude_bin=self.fake,
                         claude_config_dir=self.store, classification="synthetic",
                         model="fake", effort="low",
                         verify_argv=[["git", "diff", "--check"]])
        self.assertEqual(str(caught.exception),
                         "delivered patch changed during verification")

    def test_refuses_client_material_before_dispatch(self):
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo,
                     task_root=self.root / "tasks", claude_bin=self.fake,
                     claude_config_dir=self.store,
                     classification="client_derived", model="fake", effort="low",
                     verify_argv=[["git", "diff", "--check"]])
        self.assertFalse((self.root / "tasks").exists())

    def test_refuses_when_task_root_cannot_be_protected(self):
        # mkdir(mode=) and chmod are no-ops on Windows; task_root must be
        # brought under a real ACL through the platform layer, and a host
        # that cannot do that must refuse before any job directory exists.
        # The check under test runs after the confinement-backend probe, so
        # a host with no backend at all (not yet wired for Windows) never
        # reaches it -- same reason other full-path tests in this file skip.
        requires_confinement(self)
        with mock.patch.object(claude_task.wpv, "require_private_directory",
                              side_effect=claude_task.wpv.PrivacyError("directory_not_owner_only")):
            with self.assertRaises(TaskError) as caught:
                run_task(brief=self.brief, repo=self.repo,
                         task_root=self.root / "tasks", claude_bin=self.fake,
                         claude_config_dir=self.store, classification="synthetic",
                         model="fake", effort="low",
                         verify_argv=[["git", "diff", "--check"]])
        self.assertIn("task root could not be protected", str(caught.exception))
        self.assertEqual(list((self.root / "tasks").iterdir()), [])

    def test_refuses_when_job_directory_cannot_be_protected(self):
        requires_confinement(self)
        task_root = self.root / "tasks"

        def fails_only_for_job(path, *, root):
            if Path(path) != task_root:
                raise claude_task.wpv.PrivacyError("directory_not_owner_only")

        with mock.patch.object(claude_task.wpv, "require_private_directory",
                              side_effect=fails_only_for_job):
            with self.assertRaises(TaskError) as caught:
                run_task(brief=self.brief, repo=self.repo,
                         task_root=task_root, claude_bin=self.fake,
                         claude_config_dir=self.store, classification="synthetic",
                         model="fake", effort="low",
                         verify_argv=[["git", "diff", "--check"]])
        self.assertIn("job directory could not be protected", str(caught.exception))

    def test_refuses_shell_and_missing_verification(self):
        for commands in ([], [["sh", "-c", "touch escaped"]], [["python3", "-c", "print(1)"]]):
            with self.subTest(commands=commands), self.assertRaises(TaskError):
                run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                         claude_bin=self.fake, claude_config_dir=self.store,
                         classification="synthetic", model="fake",
                         effort="low", verify_argv=commands)

    def test_main_reports_the_refusal_text_not_just_the_class(self):
        # The queue's receipts once said only {"ok": false, "error": "TaskError"}.
        completed = subprocess.run(
            [sys.executable, str(CLAUDE_TASK), str(self.brief), "--repo", str(self.repo),
             "--claude-bin", str(self.fake), "--claude-config-dir", str(self.store),
             "--classification", "synthetic", "--task-root", str(self.root / "tasks"),
             "--verify-json", json.dumps(["python3", "-c", "print(1)"])],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        failure = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(failure, {"ok": False, "error": "TaskError",
                                   "error_detail": "Python verification is limited to "
                                                   "python -m pytest or python -m unittest"})

    def test_refuses_api_auth(self):
        requires_confinement(self)
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"api_key\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\n")
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                     claude_bin=self.fake, claude_config_dir=self.store,
                         classification="synthetic", model="fake",
                     effort="low", verify_argv=[["git", "diff", "--check"]])
        receipt_path = next((self.root / "tasks").glob("*/receipt.json"))
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["cleanup"]["generation_removed"])
        self.assertTrue(receipt["cleanup"]["verification_removed"])
        self.assertTrue(receipt["source_integrity_match"])

    def test_refuses_invalid_json_and_empty_patch(self):
        for output in ("not-json", '{\"result\":\"done\",\"is_error\":false}'):
            with self.subTest(output=output):
                self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf '%s\\n' '" + output + "'\n")
                with self.assertRaises(TaskError):
                    run_task(brief=self.brief, repo=self.repo, task_root=self.root / ("tasks-" + str(len(output))),
                             claude_bin=self.fake, claude_config_dir=self.store,
                         classification="synthetic", model="fake",
                             effort="low", verify_argv=[["git", "diff", "--check"]])

    def test_environment_does_not_inherit_api_credentials_or_git_config(self):
        old = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GIT_CONFIG_GLOBAL")}
        try:
            os.environ["ANTHROPIC_API_KEY"] = "secret"
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "secret"
            os.environ["GIT_CONFIG_GLOBAL"] = "/tmp/hostile"
            env = _env()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        finally:
            for key, value in old.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value

    def test_timeout_kills_descendant_process_group(self):
        marker = self.root / "escaped"
        program = ("import subprocess,time,sys; "
                   "subprocess.Popen([sys.executable,'-c',"
                   f"\"import time,pathlib; time.sleep(.6); pathlib.Path({str(marker)!r}).write_text('bad')\"]); "
                   "time.sleep(10)")
        with self.assertRaises(TaskError):
            _run([sys.executable, "-c", program], cwd=self.root, env=_env(), timeout=0.1)
        time.sleep(0.8)
        self.assertFalse(marker.exists())

    # The confinement backend is now selected per host (execution/hostenv.py),
    # so these assert what the *selected* backend claims rather than what one
    # operating system happens to provide. A test that asserted macOS
    # semantics everywhere would have to be skipped off macOS, and a skipped
    # boundary test is one nobody reads.

    def test_selected_backend_denies_network(self):
        """Every backend must deny network. None is selected if it cannot."""
        requires_confinement(self)
        backend = hostenv.confinement("synthetic")
        tree = self.root / "net-tree"; tree.mkdir()
        code = ("import socket,sys\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
                "except OSError:\n"
                "    sys.exit(0)\n"
                "sys.exit(9)\n")
        result = _sandboxed([sys.executable, "-c", code], tree,
                            self.root / "net-scratch", _env(), 20, backend)
        self.assertTrue(backend.denies_network)
        self.assertEqual(result.returncode, 0, result.stderr[:400])
        self.assertEqual(result.sandbox_backend, backend.name)

    def test_backend_write_confinement_matches_its_claim(self):
        """A write above the worktree is refused exactly when claimed."""
        requires_confinement(self)
        backend = hostenv.confinement("synthetic")
        tree = self.root / "write-tree"; tree.mkdir()
        outside = self.root / "outside"
        result = _sandboxed(
            [sys.executable, "-c", f"open({str(outside)!r},'w').write('escape')"],
            tree, self.root / "write-scratch", _env(), 20, backend)
        if backend.confines_writes:
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(outside.exists())
        else:
            # Stated, not assumed: this backend is not the write boundary.
            # run_task's source-integrity snapshot is, and
            # test_detects_source_checkout_mutation proves it.
            self.assertTrue(outside.exists())

    def test_backend_read_confinement_matches_its_claim(self):
        requires_confinement(self)
        backend = hostenv.confinement("synthetic")
        tree = self.root / "read-tree"; tree.mkdir()
        protected = self.root / "client-secret"; protected.write_text("canary-secret")
        result = _sandboxed([sys.executable, "-c", f"print(open({str(protected)!r}).read())"],
                            tree, self.root / "read-scratch", _env(), 20, backend)
        if backend.confines_reads:
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"canary-secret", result.stdout + result.stderr)
        else:
            # Why such a backend carries synthetic material only.
            self.assertIn(b"canary-secret", result.stdout)
            self.assertEqual(backend.classifications, frozenset({"synthetic"}))

    def test_a_backend_that_confines_no_reads_refuses_real_material(self):
        requires_confinement(self)
        backend = hostenv.confinement("synthetic")
        if backend.confines_reads:
            self.assertTrue(backend.permits("internal_nonclient"))
            return
        for classification in ("internal_nonclient", "public"):
            with self.assertRaises(hostenv.HostCapabilityError) as caught:
                hostenv.confinement(classification)
            self.assertEqual(caught.exception.code,
                             "verification_confinement_insufficient")

    def test_cleanup_timeout_is_bounded_failure(self):
        tree = self.root / "cleanup-tree"; tree.mkdir()
        timed = SimpleNamespace(spawn_failed=False, timed_out=True, cap_exceeded=False,
                                descendant_held_pipes=False, returncode=None, stdout=b"", stderr=b"")
        with mock.patch("agent_bridge.execution.claude_task.runner.run", return_value=timed):
            self.assertFalse(_remove(self.repo, tree, _env()))

    def test_detects_source_checkout_mutation(self):
        requires_confinement(self)
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"--version\"*) echo fake; exit;; *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf 'corrupt\\n' > " + str(self.repo / "value.txt") + "\nprintf 'after\\n' > value.txt\nprintf '{\"result\":\"done\",\"is_error\":false}\\n'\n")
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                     claude_bin=self.fake, claude_config_dir=self.store,
                     classification="synthetic", model="fake", effort="low",
                     verify_argv=[["git", "diff", "--check"]])

    def test_refuses_ignored_untracked_model_output(self):
        (self.repo / ".gitignore").write_text("ignored.tmp\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore fixture"], cwd=self.repo, check=True)
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"--version\"*) echo fake; exit;; *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf hidden > ignored.tmp\nprintf 'after\\n' > value.txt\nprintf '{\"result\":\"done\",\"is_error\":false}\\n'\n")
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                     claude_bin=self.fake, claude_config_dir=self.store,
                     classification="synthetic", model="fake", effort="low",
                     verify_argv=[["git", "diff", "--check"]])



class ClaudeTaskExitStatusTests(unittest.TestCase):
    """A completed process is not a verified task.

    Independently reported by Charlie; reproduced against the baseline before
    this fix. ``run_task`` returns normally when the patch applied cleanly but
    the verification commands failed, and the CLI printed ok:true and exited
    0 for that, so the execution queue recorded unverified work as complete.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo,
                       check=True)
        (self.repo / "value.txt").write_text("before\n")
        subprocess.run(["git", "add", "value.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Replace the fixture value.\n")
        self.fake = self.root / "claude"
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf 'after\\n' > value.txt\nprintf '{\"result\":\"done\",\"is_error\":false}\\n'\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.store = _isolated_store(self, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def _main(self, check):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([str(self.brief), "--repo", str(self.repo),
                         "--task-root", str(self.root / "tasks"),
                         "--claude-bin", str(self.fake),
                         "--claude-config-dir", str(self.store),
                         "--classification", "synthetic",
                         "--verify-json", json.dumps(check)])
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        return code, json.loads(lines[-1])

    def test_a_clean_run_reports_ok_and_exits_zero(self):
        requires_confinement(self)
        code, payload = self._main(["git", "diff", "--check"])
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "complete")

    def test_a_failed_verification_exits_nonzero(self):
        requires_confinement(self)
        code, payload = self._main(["git", "diff", "--exit-code"])
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"], payload)
        self.assertEqual(payload["status"], "verification_failed", payload)

    def test_the_failing_check_is_recorded_in_the_receipt(self):
        requires_confinement(self)
        _, payload = self._main(["git", "diff", "--exit-code"])
        self.assertTrue(any(entry["returncode"] != 0
                            for entry in payload["verification"]))


class ClaudeTaskSourceIntegrityTests(unittest.TestCase):
    """Content changes to files that were already dirty must be detected.

    Independently reported by Charlie; reproduced against the baseline. git
    status --porcelain=v2 reports HEAD and index hashes for a modified file,
    never the worktree content, so rewriting a file that was already modified
    leaves the status output byte-identical and the old comparison saw nothing.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo,
                       check=True)
        (self.repo / "value.txt").write_text("before\n")
        (self.repo / "staged.txt").write_text("staged one\n")
        subprocess.run(["git", "add", "value.txt", "staged.txt"], cwd=self.repo,
                       check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        # The three states that matter, all dirty before the task starts.
        (self.repo / "value.txt").write_text("already modified\n")
        (self.repo / "staged.txt").write_text("staged two\n")
        subprocess.run(["git", "add", "staged.txt"], cwd=self.repo, check=True)
        (self.repo / "untracked.txt").write_text("untracked one\n")
        self.env = _env()

    def tearDown(self):
        self.temp.cleanup()

    def _status(self):
        return _git(self.repo, "status", "--porcelain=v2",
                    "--untracked-files=all", env=self.env)

    def _snapshot(self):
        return _source_state(self.repo, self.env)

    def test_rewriting_an_already_modified_file_changes_the_snapshot(self):
        before, status_before = self._snapshot(), self._status()
        (self.repo / "value.txt").write_text("modified again\n")
        self.assertEqual(self._status(), status_before,
                         "fixture is wrong: git status must be unchanged")
        self.assertNotEqual(self._snapshot(), before)

    def test_rewriting_an_untracked_file_changes_the_snapshot(self):
        before, status_before = self._snapshot(), self._status()
        (self.repo / "untracked.txt").write_text("untracked two\n")
        self.assertEqual(self._status(), status_before)
        self.assertNotEqual(self._snapshot(), before)

    def test_rewriting_a_staged_files_worktree_copy_changes_the_snapshot(self):
        before = self._snapshot()
        (self.repo / "staged.txt").write_text("staged three\n")
        self.assertNotEqual(self._snapshot(), before)

    def test_changing_the_mode_of_a_dirty_file_changes_the_snapshot(self):
        if os.name == "nt":
            # Windows has no POSIX mode-bit granularity: st_mode is
            # synthesized from a single read-only attribute, so 0o700 and
            # the file's starting 0o666 both mean "writable" and chmod is a
            # no-op (measured: os.stat().st_mode & 0o777 is 0o666 before and
            # after). Asserting this POSIX property on Windows would be
            # asserting something that cannot be true there, not a gap in
            # the snapshot itself, which does react correctly to the one
            # permission distinction Windows actually has (see below).
            self.skipTest("POSIX mode-bit granularity does not exist on Windows")
        before = self._snapshot()
        os.chmod(self.repo / "untracked.txt", 0o700)
        self.assertNotEqual(self._snapshot(), before)

    def test_toggling_read_only_on_windows_changes_the_snapshot(self):
        if os.name != "nt":
            self.skipTest("read-only-attribute toggling is the Windows case")
        before = self._snapshot()
        os.chmod(self.repo / "untracked.txt", stat.S_IREAD)
        self.assertNotEqual(self._snapshot(), before)

    def test_an_unchanged_tree_snapshots_identically(self):
        self.assertEqual(self._snapshot(), self._snapshot())

    def test_deleting_a_dirty_file_changes_the_snapshot(self):
        before = self._snapshot()
        (self.repo / "untracked.txt").unlink()
        self.assertNotEqual(self._snapshot(), before)

    def test_the_snapshot_covers_every_not_clean_path(self):
        self.assertEqual(_relevant_paths(self.repo, self.env),
                         ["staged.txt", "untracked.txt", "value.txt"])

    def test_a_tree_with_too_many_dirty_files_refuses_rather_than_skipping(self):
        with mock.patch.object(module, "MAX_SNAPSHOT_FILES", 1):
            with self.assertRaises(TaskError):
                self._snapshot()

    def test_a_tree_with_too_many_dirty_bytes_refuses_rather_than_skipping(self):
        with mock.patch.object(module, "MAX_SNAPSHOT_BYTES", 4):
            with self.assertRaises(TaskError):
                self._snapshot()

    # -- bounded and nonblocking, not merely bounded ------------------------
    #
    # git never enumerates a FIFO, socket or device node as untracked, so the
    # way one reaches the snapshot is a swap between the enumeration and the
    # open. These exercise the guarantee at the point that matters: the file
    # is opened by this code, and what the descriptor turns out to be is
    # checked on the descriptor.

    def test_a_fifo_is_refused_rather_than_opened_and_waited_on(self):
        """Opening one to read waits for a writer. A snapshot must not hang."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        pipe = self.repo / "pipe"
        os.mkfifo(pipe)
        with self.assertRaises(TaskError) as caught:
            module._hash_regular_file(pipe, hashlib.sha256(), 4096)
        self.assertIn("non-regular", str(caught.exception))

    def test_a_socket_is_refused(self):
        import socket
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no AF_UNIX on this host")
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(endpoint.close)
        try:
            endpoint.bind(str(self.repo / "sock"))
        except OSError:
            self.skipTest("cannot bind a unix socket here")
        with self.assertRaises(TaskError):
            module._hash_regular_file(self.repo / "sock", hashlib.sha256(), 4096)

    def test_a_device_node_is_refused(self):
        node = Path("/dev/null")
        if not node.exists():
            self.skipTest("no /dev/null here")
        with self.assertRaises(TaskError) as caught:
            module._hash_regular_file(node, hashlib.sha256(), 4096)
        self.assertIn("non-regular", str(caught.exception))

    def test_a_symlink_is_never_followed_by_the_reader(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret\n")
        link = self.repo / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("cannot create a symlink on this host "
                          "(elevation or Developer Mode required)")
        with self.assertRaises(TaskError) as caught:
            module._hash_regular_file(link, hashlib.sha256(), 4096)
        self.assertIn("could not read", str(caught.exception))

    def test_a_file_that_grows_while_it_is_read_is_refused(self):
        """A prefix hashed as if it were the whole file would compare equal."""
        target = self.repo / "untracked.txt"
        target.write_bytes(b"a" * 8)
        watched = target.stat().st_ino
        real_read, state = os.read, {"grown": False}

        def growing_read(descriptor, size):
            if not state["grown"] and os.fstat(descriptor).st_ino == watched:
                state["grown"] = True
                with open(target, "ab") as handle:
                    handle.write(b"b" * 4096)
            return real_read(descriptor, size)

        with mock.patch.object(module.os, "read", growing_read):
            with self.assertRaises(TaskError) as caught:
                self._snapshot()
        self.assertIn("grew while the source snapshot", str(caught.exception))

    def test_a_file_that_shrinks_while_it_is_read_is_refused(self):
        target = self.repo / "untracked.txt"
        target.write_bytes(b"a" * 4096)
        watched = target.stat().st_ino
        real_read, state = os.read, {"seen": False}

        def shrinking_read(descriptor, size):
            if os.fstat(descriptor).st_ino != watched:
                return real_read(descriptor, size)
            if state["seen"]:
                return b""
            state["seen"] = True
            return real_read(descriptor, 8)

        with mock.patch.object(module.os, "read", shrinking_read):
            with self.assertRaises(TaskError) as caught:
                self._snapshot()
        self.assertIn("changed size", str(caught.exception))

    def test_a_file_replaced_under_the_descriptor_is_refused(self):
        target = self.repo / "untracked.txt"
        target.write_bytes(b"a" * 16)
        watched = target.stat().st_ino
        real_fstat, state = os.fstat, {"seen": 0}

        def drifting_fstat(descriptor):
            info = real_fstat(descriptor)
            if info.st_ino != watched:
                return info
            state["seen"] += 1
            if state["seen"] == 1:
                return info
            return os.stat_result(
                tuple(info)[:1] + (info.st_ino + 1,) + tuple(info)[2:10])

        with mock.patch.object(module.os, "fstat", drifting_fstat):
            with self.assertRaises(TaskError) as caught:
                self._snapshot()
        self.assertIn("replaced", str(caught.exception))

    def test_a_single_file_over_the_per_file_bound_is_refused(self):
        (self.repo / "untracked.txt").write_bytes(b"x" * 64)
        with mock.patch.object(module, "MAX_SNAPSHOT_FILE_BYTES", 8):
            with self.assertRaises(TaskError) as caught:
                self._snapshot()
        self.assertIn("per-file", str(caught.exception))

    def test_a_directory_entry_is_recorded_without_being_walked(self):
        (self.repo / "untracked.txt").unlink()
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "inner.txt").write_text("inner\n")
        before = self._snapshot()
        (nested / "inner.txt").write_text("inner two\n")
        self.assertNotEqual(self._snapshot(), before)


class ResultDetailTests(unittest.TestCase):
    """``_result_detail`` reads Claude's own explanation back out of its
    JSON envelope: bounded, printable-only, ``None`` when there is none."""

    def test_reads_the_result_text_when_is_error_is_true(self):
        envelope = json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth session expired"}).encode()
        self.assertEqual(_result_detail(envelope), "Failed to authenticate: OAuth session expired")

    def test_none_when_is_error_is_false(self):
        envelope = json.dumps({"is_error": False, "result": "done"}).encode()
        self.assertIsNone(_result_detail(envelope))

    def test_none_when_is_error_is_missing(self):
        self.assertIsNone(_result_detail(json.dumps({"result": "boom"}).encode()))

    def test_none_when_result_is_not_a_string(self):
        self.assertIsNone(_result_detail(json.dumps({"is_error": True, "result": 42}).encode()))

    def test_none_when_result_is_empty(self):
        self.assertIsNone(_result_detail(json.dumps({"is_error": True, "result": ""}).encode()))

    def test_none_when_stdout_is_not_json(self):
        self.assertIsNone(_result_detail(b"not json at all"))

    def test_none_when_stdout_is_not_utf8(self):
        self.assertIsNone(_result_detail(b"\xff\xfe not utf-8"))

    def test_none_when_stdout_is_too_deeply_nested_to_parse(self):
        bomb = (b"[" * 100000) + (b"]" * 100000)
        self.assertIsNone(_result_detail(bomb))

    def test_strips_non_printable_characters(self):
        envelope = json.dumps({"is_error": True, "result": "line one\x00\x07line two"}).encode()
        self.assertEqual(_result_detail(envelope), "line oneline two")

    def test_truncates_to_the_bound(self):
        long_text = "x" * 500
        envelope = json.dumps({"is_error": True, "result": long_text}).encode()
        detail = _result_detail(envelope)
        self.assertEqual(len(detail), module.RESULT_DETAIL_LIMIT)
        self.assertEqual(detail, "x" * module.RESULT_DETAIL_LIMIT)

    def test_with_result_detail_appends_when_present(self):
        envelope = json.dumps({"is_error": True, "result": "boom"}).encode()
        self.assertEqual(_with_result_detail("Claude exited with status 1", envelope),
                         "Claude exited with status 1: boom")

    def test_with_result_detail_falls_back_when_absent(self):
        self.assertEqual(_with_result_detail("Claude exited with status 1", b"not json"),
                         "Claude exited with status 1")


class ClaudeTaskErrorDetailTests(unittest.TestCase):
    """Both TaskError sites in ``run_task`` fold Claude's own explanation
    into the message instead of reporting a bare, undiagnosable failure
    (the defect job d8e5763d demonstrated: an expired OAuth session read
    back through the queue as nothing but the string "TaskError")."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("before\n")
        subprocess.run(["git", "add", "value.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Replace the fixture value.\n")
        self.store = _isolated_store(self, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def _fake(self, body):
        fake = self.root / "claude"
        fake.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in "
            "*\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; "
            "--version) echo fake; exit;; "
            "esac\n"
            + body)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake

    def _run(self, fake):
        return run_task(brief=self.brief, repo=self.repo,
                        task_root=self.root / "tasks", claude_bin=fake,
                        claude_config_dir=self.store,
                        classification="synthetic", model="fake", effort="low",
                        verify_argv=[["git", "diff", "--check"]])

    def test_a_nonzero_exit_with_a_reported_reason_carries_it_in_the_message(self):
        requires_confinement(self)
        fake = self._fake(
            "printf '{\"result\":\"Failed to authenticate: OAuth session expired and could not be refreshed\","
            "\"is_error\":true}\\n'\nexit 1\n")
        with self.assertRaises(TaskError) as ctx:
            self._run(fake)
        self.assertEqual(str(ctx.exception),
                         "Claude exited with status 1: Failed to authenticate: OAuth session expired "
                         "and could not be refreshed")

    def test_a_nonzero_exit_with_no_parseable_reason_keeps_the_bare_message(self):
        requires_confinement(self)
        fake = self._fake("printf 'not json\\n'\nexit 1\n")
        with self.assertRaises(TaskError) as ctx:
            self._run(fake)
        self.assertEqual(str(ctx.exception), "Claude exited with status 1")

    def test_a_zero_exit_that_fails_the_success_contract_carries_the_reason(self):
        requires_confinement(self)
        fake = self._fake(
            "printf 'after\\n' > value.txt\n"
            "printf '{\"result\":\"Failed to authenticate: OAuth session expired and could not be refreshed\","
            "\"is_error\":true}\\n'\n")
        with self.assertRaises(TaskError) as ctx:
            self._run(fake)
        self.assertEqual(str(ctx.exception),
                         "Claude output failed success contract: Failed to authenticate: OAuth session "
                         "expired and could not be refreshed")


if __name__ == "__main__":
    unittest.main()
