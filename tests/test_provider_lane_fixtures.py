"""The guest provider lane, end to end, against provider CLIs that really run.

Everywhere else the lane is tested with a ``runner`` seam: a function that
returns a hand-written ``(returncode, stdout, stderr)`` triple. That is the
right way to test the decisions, and it is not a test of the lane, because the
triple never came from a process. Three of the things this lane exists to get
right are invisible to it:

  * the real subprocess path, with its process group, bounded readers, stdin
    writer thread and timeout, actually running a provider;
  * the answer file, which Codex writes and the probe reads through its own
    descriptor, and which a mocked triple simply does not have; and
  * the environment, which has to carry a subscription session and must not
    carry a metered API key. A mock cannot fail that test because a mock never
    reads its environment.

So this file runs :func:`guest_runner.execute` with no runner seam at all,
against the fixtures in ``tests/fakes/fake_guest_*.py``. What stays mocked is
only what cannot exist off a provisioned guest: the tmpfs auth capsule, which
needs mount(8) and root, and the nftables verification policy, which needs a
kernel to load a table into. Both have their own dedicated tests.

Not a substitute for live provider validation. No real model is contacted
here, and nothing in this file demonstrates that a real subscription session
survives the boundary. It demonstrates that the lane reaches the right verdict
about the bytes a provider CLI produces.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr

FAKES = ROOT / "tests" / "fakes"

#: The guest is Linux. These tests spawn processes and resolve /usr/bin/git,
#: so they run where that is meaningful and say so where it is not.
POSIX_ONLY = unittest.skipIf(os.name == "nt",
                             "the guest runner is a Linux-only component")


def _wrapper(target: Path, directory: Path, name: str, mode: str) -> str:
    """A tiny exec shim: this interpreter, that fixture, this mode.

    Two things are pinned here rather than left to the environment, and both
    for the same reason. The interpreter, because the child environment the
    guest runner builds carries a fixed PATH that a virtualenv is deliberately
    not on, so a ``#!/usr/bin/env python3`` shebang would resolve to whatever
    the system happens to have. And the mode, because that same rebuild
    strips any variable not on the allowlist, so a mode passed through the
    environment would vanish and every test would quietly run the default.

    Putting the mode on argv also leaves the child environment untouched,
    which is what lets the tests that assert on the child environment say
    something true.
    """

    path = directory / name
    path.write_text(
        "#!/bin/sh\nexec {} {} --fake-mode {} \"$@\"\n".format(
            json.dumps(sys.executable), json.dumps(str(target)),
            json.dumps(mode)),
        encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class _LocalCapsule:
    """The auth capsule without the tmpfs mount.

    Same environment, same login call, ordinary directory. The mount is what
    keeps a session out of the image's writable layer inside a real guest;
    it needs root and mount(8), and it has its own tests. Everything the
    provider can observe about the capsule is reproduced.
    """

    def __init__(self, auth, tool):
        self.auth, self.tool = auth, tool
        self.root = None
        self._directory = None

    def __enter__(self):
        if self.auth is not None:
            self._directory = tempfile.mkdtemp(prefix="capsule-", dir="/tmp")
            self.root = self._directory
        return self

    def __exit__(self, *_):
        if self._directory:
            shutil.rmtree(self._directory, ignore_errors=True)
        self.auth = None

    def child_env(self):
        if self.auth is None or self.root is None:
            return {}
        if self.tool == "claude":
            return {"CLAUDE_CODE_OAUTH_TOKEN": self.auth["token"],
                    "CLAUDE_CONFIG_DIR": os.path.join(self.root, "claude")}
        return {"CODEX_HOME": os.path.join(self.root, "codex")}

    def prepare(self, tools, env, timeout):
        """The real prepare(), including the Codex login subprocess."""

        if self.auth is None or self.root is None:
            return
        if self.tool == "claude":
            os.makedirs(os.path.join(self.root, "claude"), mode=0o700,
                        exist_ok=True)
            return
        codex_home = os.path.join(self.root, "codex")
        os.makedirs(codex_home, mode=0o700, exist_ok=True)
        completed = subprocess.run(
            [tools["codex"]["path"], "login", "--with-access-token"],
            input=(self.auth["token"] + "\n").encode("utf-8"),
            env=dict(env), cwd="/", shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=min(120.0, timeout))
        if completed.returncode != 0:
            raise gr.GuestRunnerError("auth_login_rejected")


def _tarball(files: dict[str, bytes]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _LaneHarness(unittest.TestCase):
    """Shared plumbing: real fixtures on disk, real subprocesses, real git."""

    provider = "claude"

    def setUp(self):
        self.bin_dir = Path(tempfile.mkdtemp(prefix="fakes-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.bin_dir, ignore_errors=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="job-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self._install("ok")

    def _install(self, mode):
        """Lay down both provider shims for one behaviour."""

        self.claude = _wrapper(FAKES / "fake_guest_claude.py",
                               self.bin_dir, "claude", mode)
        self.codex = _wrapper(FAKES / "fake_guest_codex.py",
                              self.bin_dir, "codex", mode)

    def _tools(self):
        def entry(path):
            return {"path": path,
                    "sha256": hashlib.sha256(
                        Path(path).read_bytes()).hexdigest()}

        return {"claude": entry(self.claude), "codex": entry(self.codex)}

    def _request(self, **overrides):
        # A fresh workdir per request. The unpacker refuses to materialize a
        # workspace over an existing one, which is correct, and which means a
        # test that runs the lane twice needs two of them.
        self.workdir = Path(tempfile.mkdtemp(prefix="job-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        auth = ({"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": "sk-session-token"}
                if self.provider == "claude"
                else {"kind": gr.AUTH_KIND_CODEX_ACCESS,
                      "token": "codex-session-token"})
        fields = {
            "schema_version": gr.SCHEMA_VERSION,
            "mode": gr.MODE_PROVIDER_JOB,
            "provider": self.provider,
            "model": "default",
            "effort": "low",
            "brief": "Write the file.\n",
            "verify_argv": [["git", "status", "--porcelain"]],
            "workdir": str(self.workdir),
            "timeout_seconds": 60,
            "verify_timeout_seconds": 30,
            "env": {"HOME": "/root"},
            "auth": auth,
            "workspace_tar_b64": _tarball({"README.md": b"hello\n"}),
        }
        fields.update(overrides)
        return fields

    def _probe_request(self, **overrides):
        """The probe's own request shape: no brief, no workspace, no checks.

        Built separately rather than by deleting keys from a job, because the
        runner refuses a probe carrying a key a probe has no business having,
        and building it by subtraction would test the subtraction.
        """

        base = self._request()
        fields = {key: base[key] for key in
                  ("schema_version", "provider", "env", "workdir",
                   "timeout_seconds", "auth")}
        fields["mode"] = gr.MODE_AUTH_PROBE
        fields.update(overrides)
        return fields

    def _execute(self, request, mode="ok"):
        """Run for real. Only the mount and the firewall are stood in for."""

        self._install(mode)
        with mock.patch.object(gr, "read_versions", return_value=self._tools()), \
             mock.patch.object(gr, "_AuthCapsule", _LocalCapsule), \
             mock.patch.object(gr, "enforce_verification_egress",
                               lambda: "verify-egress-denied:fixture"):
            return gr.execute(gr.validate_request(request))


@POSIX_ONLY
class ClaudeProviderJobTests(_LaneHarness):
    provider = "claude"

    def test_a_real_run_produces_a_real_patch(self):
        response = self._execute(self._request())
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)
        self.assertEqual(response["reason"], "ok")
        patch = base64.b64decode(response["diff_b64"]).decode("utf-8")
        # The fixture wrote a file, git saw it, and the patch says so. None of
        # that is arranged by the test: it is what the subprocess did.
        self.assertIn("IMPLEMENTED.md", patch)
        self.assertIn("Write the file.", patch)

    def test_the_brief_reaches_the_provider_only_on_stdin(self):
        response = self._execute(self._request())
        patch = base64.b64decode(response["diff_b64"]).decode("utf-8")
        # The fixture writes back exactly what it read from stdin, so the
        # patch containing the brief is direct evidence of the route.
        self.assertIn("Write the file.", patch)

    def test_a_failing_check_is_verification_failed_not_complete(self):
        # `git diff --exit-code` exits 1 when a tracked file changed, and the
        # workspace commit the unpacker made is what makes it tracked.
        request = self._request(verify_argv=[["git", "diff", "--exit-code"]])
        response = self._execute(request, mode="modify_tracked")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["harness_status"],
                         gr.HARNESS_VERIFICATION_FAILED)
        self.assertNotEqual(response["verification"][0]["returncode"], 0)

    def test_a_provider_that_exits_nonzero_never_reaches_verification(self):
        response = self._execute(self._request(), mode="nonzero")
        self.assertEqual(response["status"], "aborted")
        self.assertEqual(response["reason"], "provider_nonzero_exit")
        self.assertEqual(response["exit_code"], 3)
        self.assertEqual(response["verification"], [])

    def test_runaway_output_aborts_rather_than_being_captured(self):
        response = self._execute(self._request(), mode="runaway")
        self.assertEqual(response["status"], "aborted")
        self.assertIn(response["reason"], ("output_too_large",))
        self.assertEqual(response["stdout_b64"], "")

    def test_a_provider_that_hangs_is_killed_and_aborted(self):
        response = self._execute(self._request(timeout_seconds=2), mode="hang")
        self.assertEqual(response["status"], "aborted")
        self.assertEqual(response["reason"], "job_timed_out")

    def test_the_session_never_reaches_a_verification_command(self):
        # `git status` cannot report an environment, so the check is made
        # where it can be: the verification environment is rebuilt from the
        # request, and the capsule contributes to the provider env only.
        request = self._request(verify_argv=[["git", "status"]])
        response = self._execute(request)
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)
        environment = json.loads(self._env_of("claude"))
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
        verify_env = gr.build_child_env({"HOME": "/root"})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", verify_env)

    def _env_of(self, provider):
        """What the provider actually saw, reported by the fixture itself."""

        response = self._execute(self._request(verify_argv=[["git", "status"]]),
                                 mode="report_env")
        return base64.b64decode(response["stdout_b64"]).decode("utf-8")

    def test_the_provider_ran_under_the_pinned_interpreter(self):
        # Guards the shim itself: if the exec line were wrong the fixture
        # would not run and every other assertion here would be vacuous.
        response = self._execute(self._request(), mode="report_env")
        self.assertNotEqual(response["stdout_b64"], "")

    def test_no_metered_api_key_reaches_the_provider(self):
        environment = json.loads(self._env_of("claude"))
        for key in gr.API_KEY_ENV_KEYS:
            self.assertNotIn(key, environment)

    def test_the_provider_environment_is_rebuilt_not_inherited(self):
        environment = set(json.loads(self._env_of("claude")))
        # Variables this test process is full of and the child must not be.
        # Nothing about the caller's shell, toolchain or test run crosses.
        for leaked in ("VIRTUAL_ENV", "PYTEST_CURRENT_TEST", "USER",
                       "LOGNAME", "SSH_AUTH_SOCK", "TMPDIR"):
            self.assertNotIn(leaked, environment)
        # What is left is the guest's own allowlist, the capsule's two
        # additions, and the handful of variables /bin/sh sets for itself in
        # the shim this test uses to pin the interpreter.
        shell_additions = {"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}
        self.assertLessEqual(
            environment - shell_additions,
            set(gr.CHILD_ENV_KEYS) | {"CLAUDE_CODE_OAUTH_TOKEN",
                                      "CLAUDE_CONFIG_DIR"})

    def test_the_token_never_appears_in_any_returned_field(self):
        response = self._execute(self._request(), mode="report_env")
        self.assertNotIn("sk-session-token", json.dumps(response))


@POSIX_ONLY
class CodexProviderJobTests(_LaneHarness):
    provider = "codex"

    def test_a_real_run_logs_in_and_produces_a_patch(self):
        response = self._execute(self._request())
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)
        patch = base64.b64decode(response["diff_b64"]).decode("utf-8")
        self.assertIn("IMPLEMENTED.md", patch)

    def test_a_rejected_login_aborts_before_the_provider_runs(self):
        request = self._request(auth={"kind": gr.AUTH_KIND_CODEX_ACCESS,
                                      "token": "rejected-session-token"})
        response = self._execute(request)
        self.assertEqual(response["status"], "aborted")
        self.assertEqual(response["reason"], "auth_login_rejected")
        # Nothing ran, so nothing was written.
        self.assertFalse((self.workdir / "IMPLEMENTED.md").exists())

    def test_the_login_writes_into_the_capsule_not_a_real_home(self):
        home = Path(os.path.expanduser("~")) / ".codex"
        before = home.exists()
        self._execute(self._request())
        self.assertEqual(home.exists(), before,
                         "the login escaped the capsule's CODEX_HOME")

    def test_the_token_is_not_on_the_login_argv(self):
        # The fixture would have failed the login if it had not received the
        # token on stdin, so a successful job is the assertion.
        response = self._execute(self._request())
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)

    def test_the_token_never_appears_in_any_returned_field(self):
        response = self._execute(self._request(), mode="report_env")
        self.assertNotIn("codex-session-token", json.dumps(response))


@POSIX_ONLY
class ClaudeAuthProbeTests(_LaneHarness):
    """Every probe verdict, reached from bytes a process wrote."""

    provider = "claude"

    def _verdict(self, mode, **overrides):
        return self._execute(self._probe_request(**overrides), mode=mode)

    def test_a_real_answer_is_authenticated(self):
        response = self._verdict("probe_ok")
        self.assertEqual(response["reason"], gr.PROBE_AUTHENTICATED)
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)

    def test_a_refused_session_is_rejected_not_merely_failed(self):
        self.assertEqual(self._verdict("rejected")["reason"], gr.PROBE_REJECTED)

    def test_a_version_string_is_not_an_authenticated_turn(self):
        self.assertEqual(self._verdict("not_json")["reason"], gr.PROBE_FAILED)

    def test_an_echoed_prompt_is_not_an_authenticated_turn(self):
        # The sentinel is in the output. It is in the output because the
        # fixture printed the prompt back, which is precisely the false pass
        # the envelope check exists to catch.
        self.assertEqual(self._verdict("echo_prompt")["reason"],
                         gr.PROBE_FAILED)

    def test_an_error_envelope_carrying_the_sentinel_is_not_a_pass(self):
        self.assertEqual(self._verdict("is_error")["reason"], gr.PROBE_FAILED)

    def test_a_non_success_subtype_carrying_the_sentinel_is_not_a_pass(self):
        self.assertEqual(self._verdict("wrong_subtype")["reason"],
                         gr.PROBE_FAILED)

    def test_a_result_with_no_usage_block_is_not_a_model_turn(self):
        self.assertEqual(self._verdict("no_usage")["reason"],
                         gr.PROBE_NO_SENTINEL)

    def test_a_clean_answer_without_the_sentinel_is_named_as_such(self):
        self.assertEqual(self._verdict("no_sentinel")["reason"],
                         gr.PROBE_NO_SENTINEL)

    def test_a_hanging_probe_times_out_rather_than_hanging_the_lane(self):
        self.assertEqual(self._verdict("hang", timeout_seconds=2)["reason"],
                         gr.PROBE_TIMED_OUT)

    def test_a_probe_never_returns_provider_output(self):
        for mode in ("probe_ok", "rejected", "no_sentinel", "not_json"):
            with self.subTest(mode):
                response = self._verdict(mode)
                self.assertEqual(response["stdout_b64"], "")
                self.assertEqual(response["stderr_b64"], "")

    def test_a_metered_key_in_the_environment_shuts_the_lane(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            request = self._probe_request(env={"HOME": "/root"})
            with mock.patch.object(gr, "build_child_env",
                                   lambda e: {"HOME": "/root",
                                              "ANTHROPIC_API_KEY": "sk-metered"}):
                response = self._execute(request, mode="probe_ok")
        self.assertEqual(response["reason"], gr.PROBE_API_KEY_PRESENT)


@POSIX_ONLY
class CodexAuthProbeTests(_LaneHarness):
    """Codex answers in a file, so the file is what decides."""

    provider = "codex"

    def _verdict(self, mode, **overrides):
        return self._execute(self._probe_request(**overrides), mode=mode)

    def test_the_answer_file_is_what_makes_the_probe_pass(self):
        self.assertEqual(self._verdict("probe_ok")["reason"],
                         gr.PROBE_AUTHENTICATED)

    def test_the_sentinel_in_the_stream_alone_is_not_a_pass(self):
        # Every mode echoes the prompt, and the prompt contains the sentinel.
        # This mode writes no answer file, so a stdout-scraping probe would
        # pass here and this one must not.
        self.assertEqual(self._verdict("no_answer_file")["reason"],
                         gr.PROBE_FAILED)

    def test_an_empty_answer_file_is_not_an_answer(self):
        self.assertEqual(self._verdict("empty_answer")["reason"],
                         gr.PROBE_NO_SENTINEL)

    def test_a_wrong_answer_is_named_as_such(self):
        self.assertEqual(self._verdict("no_sentinel")["reason"],
                         gr.PROBE_NO_SENTINEL)

    def test_a_refused_session_is_rejected(self):
        self.assertEqual(self._verdict("rejected")["reason"], gr.PROBE_REJECTED)

    def test_a_login_the_provider_refuses_is_a_rejected_session(self):
        response = self._verdict(
            "probe_ok", auth={"kind": gr.AUTH_KIND_CODEX_ACCESS,
                              "token": "rejected-session-token"})
        self.assertEqual(response["reason"], gr.PROBE_REJECTED)

    def test_the_answer_file_does_not_survive_the_probe(self):
        self._verdict("probe_ok")
        leftovers = [p.name for p in self.workdir.iterdir()
                     if p.name.startswith(".agent-bridge-probe")]
        self.assertEqual(leftovers, [])

    def test_a_probe_never_returns_provider_output(self):
        response = self._verdict("probe_ok")
        self.assertEqual(response["stdout_b64"], "")
        self.assertEqual(response["stderr_b64"], "")


@POSIX_ONLY
class FixtureHonestyTests(_LaneHarness):
    """The fixtures are only useful if they are the shapes they claim to be."""

    def test_the_claude_fixture_prints_the_documented_envelope(self):
        self._install("probe_ok")
        completed = subprocess.run(
            [self.claude, "-p", "--output-format", "json"], input=b"hi",
            capture_output=True)
        parsed = json.loads(completed.stdout)
        self.assertEqual(parsed["type"], "result")
        self.assertEqual(parsed["subtype"], "success")
        self.assertFalse(parsed["is_error"])
        self.assertIn(gr.AUTH_PROBE_SENTINEL, parsed["result"])
        self.assertIsInstance(parsed["usage"], dict)

    def test_the_codex_fixture_writes_its_answer_to_the_named_file(self):
        self._install("probe_ok")
        answer = self.workdir / "answer.txt"
        subprocess.run(
            [self.codex, "exec", "--json", "--output-last-message", str(answer)],
            input=b"hi", capture_output=True)
        self.assertEqual(answer.read_text(encoding="utf-8"),
                         gr.AUTH_PROBE_SENTINEL)

    def test_the_codex_fixture_puts_the_prompt_in_the_stream(self):
        self._install("no_answer_file")
        completed = subprocess.run(
            [self.codex, "exec", "--json"], input=b"contains the sentinel",
            capture_output=True)
        self.assertIn(b"contains the sentinel", completed.stdout)

    def test_neither_fixture_imports_anything_outside_the_standard_library(self):
        for name in ("fake_guest_claude.py", "fake_guest_codex.py"):
            source = (FAKES / name).read_text(encoding="utf-8")
            self.assertNotIn("agent_bridge", source)


if __name__ == "__main__":
    unittest.main()
