"""The guest-side authenticated provider probe.

What this replaces: the provider lane used to be opened by running
``claude --version`` or ``codex --version`` inside the guest. That prints a
string and exits zero with a valid session, with a worthless one, and with no
session at all, so it cannot tell apart the three cases the lane exists to
tell apart. These tests are mostly about that: a run that only produces a
version string must never reach the verdict that opens the lane.

Nothing here contacts a provider. The provider is a seam receiving
``(argv, cwd, env, stdin, timeout)``, which is how a real authenticated
operation's *handling* is testable without an account.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr


CLAUDE_CAPSULE = {"kind": gr.AUTH_KINDS["claude"], "token": "t" * 40}
CODEX_CAPSULE = {"kind": gr.AUTH_KINDS["codex"], "token": "c" * 40}


def _claude_success(text=gr.AUTH_PROBE_SENTINEL, **overrides):
    payload = {"type": "result", "subtype": "success", "is_error": False,
               "result": text, "usage": {"input_tokens": 12, "output_tokens": 3}}
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


class ProbeTestCase(unittest.TestCase):
    """The capsule machinery needs a tmpfs mount it cannot have here.

    Only the parts that can run without root are exercised: request handling,
    verdict classification, and the promise that no output leaves the guest.
    """

    def _verdict(self, provider, returncode, stdout, stderr=b"", *,
                 last_message=None):
        if provider == "claude":
            return gr._claude_probe_verdict(returncode, stdout, stderr)
        return gr._codex_probe_verdict(returncode, stdout, stderr, last_message)


class ClaudeVerdictTests(ProbeTestCase):
    def test_a_real_result_carrying_the_sentinel_is_authenticated(self):
        self.assertEqual(self._verdict("claude", 0, _claude_success()),
                         gr.PROBE_AUTHENTICATED)

    def test_a_version_string_is_never_authenticated(self):
        """The whole reason this module exists."""
        for output in (b"1.2.3\n", b"claude 1.2.3\n", b"Claude Code v1.2.3"):
            self.assertEqual(self._verdict("claude", 0, output),
                             gr.PROBE_FAILED, output)

    def test_a_result_without_the_sentinel_is_not_authenticated(self):
        self.assertEqual(
            self._verdict("claude", 0, _claude_success("Hello, how can I help?")),
            gr.PROBE_NO_SENTINEL)

    def test_a_result_with_no_usage_block_did_not_come_from_a_model(self):
        payload = json.dumps({"type": "result", "subtype": "success",
                              "is_error": False,
                              "result": gr.AUTH_PROBE_SENTINEL}).encode("utf-8")
        self.assertEqual(self._verdict("claude", 0, payload),
                         gr.PROBE_NO_SENTINEL)

    def test_an_error_result_is_not_authenticated_even_with_the_sentinel(self):
        payload = _claude_success(is_error=True)
        self.assertNotEqual(self._verdict("claude", 0, payload),
                            gr.PROBE_AUTHENTICATED)

    def test_the_wrong_result_subtype_is_not_success(self):
        payload = _claude_success(subtype="error_max_turns")
        self.assertNotEqual(self._verdict("claude", 0, payload),
                            gr.PROBE_AUTHENTICATED)

    def test_an_echoed_prompt_is_not_a_result(self):
        """Finding the sentinel somewhere proves only that it was sent."""
        self.assertEqual(
            self._verdict("claude", 0, gr.AUTH_PROBE_PROMPT.encode("utf-8")),
            gr.PROBE_FAILED)

    def test_a_refused_session_is_classified_as_rejected(self):
        for message in (b"Invalid API key", b"authentication_error",
                        b"401 Unauthorized", b"OAuth token has expired",
                        b"Please run /login", b"invalid_grant"):
            self.assertEqual(self._verdict("claude", 1, b"", message),
                             gr.PROBE_REJECTED, message)

    def test_an_unrelated_failure_is_not_a_rejection(self):
        self.assertEqual(self._verdict("claude", 1, b"", b"disk full"),
                         gr.PROBE_FAILED)


class CodexVerdictTests(ProbeTestCase):
    def test_the_sentinel_in_the_answer_file_is_authenticated(self):
        self.assertEqual(
            self._verdict("codex", 0, b"{}",
                          last_message=gr.AUTH_PROBE_SENTINEL.encode("utf-8")),
            gr.PROBE_AUTHENTICATED)

    def test_the_sentinel_only_in_the_stream_is_not_enough(self):
        """The stream carries the prompt as well as the answer."""
        stream = json.dumps({"prompt": gr.AUTH_PROBE_PROMPT}).encode("utf-8")
        self.assertEqual(self._verdict("codex", 0, stream, last_message=b"hi"),
                         gr.PROBE_NO_SENTINEL)

    def test_a_missing_answer_file_is_not_authenticated(self):
        self.assertNotEqual(self._verdict("codex", 0, b"{}"),
                            gr.PROBE_AUTHENTICATED)

    def test_a_version_string_is_never_authenticated(self):
        self.assertNotEqual(
            self._verdict("codex", 0, b"codex-cli 1.0.0\n", last_message=b"1.0.0"),
            gr.PROBE_AUTHENTICATED)

    def test_a_nonzero_exit_is_classified_before_the_file_is_read(self):
        self.assertEqual(
            self._verdict("codex", 1, b"", b"not logged in",
                          last_message=gr.AUTH_PROBE_SENTINEL.encode("utf-8")),
            gr.PROBE_REJECTED)


class RequestShapeTests(unittest.TestCase):
    def _request(self, **overrides):
        payload = {"schema_version": gr.SCHEMA_VERSION,
                   "mode": gr.MODE_AUTH_PROBE, "provider": "claude",
                   "workdir": "/workspace/job", "timeout_seconds": 60,
                   "env": {"HOME": "/root"}, "auth": dict(CLAUDE_CAPSULE)}
        payload.update(overrides)
        return payload

    def test_a_well_formed_probe_request_validates(self):
        validated = gr.validate_request(self._request())
        self.assertEqual(validated["mode"], gr.MODE_AUTH_PROBE)
        self.assertEqual(validated["provider"], "claude")

    def test_a_probe_without_a_capsule_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(self._request(auth=None))
        self.assertEqual(caught.exception.code, "auth_required")

    def test_a_capsule_of_the_wrong_kind_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(self._request(auth=dict(CODEX_CAPSULE)))
        self.assertEqual(caught.exception.code, "auth_kind_invalid")

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(self._request(provider="node"))
        self.assertEqual(caught.exception.code, "provider_not_allowed")

    def test_the_probe_carries_no_brief_workspace_or_verification(self):
        for extra in ("brief", "workspace_tar_b64", "verify_argv", "args"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(self._request(**{extra: "anything"}))
            self.assertEqual(caught.exception.code, "request_keys_invalid")

    def test_a_secret_shaped_environment_key_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError):
            gr.validate_request(
                self._request(env={"HOME": "/root", "ANTHROPIC_API_KEY": "x"}))


class FixedInputTests(unittest.TestCase):
    """Nothing about the probe is a caller's choice."""

    def test_the_sentinel_is_specific_enough_not_to_appear_by_accident(self):
        self.assertGreater(len(gr.AUTH_PROBE_SENTINEL), 24)
        self.assertIn(gr.AUTH_PROBE_SENTINEL, gr.AUTH_PROBE_PROMPT)

    def test_the_probe_argv_grants_no_tools(self):
        argv = gr.auth_probe_argv("claude", "/usr/bin/claude", workdir="/w",
                                  last_message_path="/w/m")
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("Write", argv)

    def test_the_codex_probe_is_read_only(self):
        argv = gr.auth_probe_argv("codex", "/usr/bin/codex", workdir="/w",
                                  last_message_path="/w/m")
        self.assertIn("read-only", argv)
        self.assertIn("--ignore-user-config", argv)

    def test_an_api_key_variable_is_a_refusal_not_a_detail(self):
        self.assertIn("ANTHROPIC_API_KEY", gr.API_KEY_ENV_KEYS)
        self.assertIn("OPENAI_API_KEY", gr.API_KEY_ENV_KEYS)

    def test_every_verdict_is_from_the_fixed_vocabulary(self):
        self.assertEqual(len(set(gr.PROBE_VERDICTS)), len(gr.PROBE_VERDICTS))
        for verdict in gr.PROBE_VERDICTS:
            self.assertTrue(verdict.startswith("auth_probe_"), verdict)


class NoOutputEscapesTests(unittest.TestCase):
    """A probe reports a verdict. It never reports what the provider said."""

    def test_the_module_never_puts_probe_output_in_a_response(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration" /
                  "guest_runner.py").read_text(encoding="utf-8")
        start = source.index("def _execute_auth_probe(")
        end = source.index("def _execute_provider_job(")
        body = source[start:end]
        self.assertIn('stdout=b""', body)
        self.assertIn('stderr=b""', body)
        # No branch may pass real bytes through.
        self.assertNotIn("stdout=capped", body)
        self.assertNotIn("stdout=stdout", body)
        self.assertNotIn("stderr=stderr", body)


if __name__ == "__main__":
    unittest.main()


class _FakeCapsule:
    """The real capsule's interface over an ordinary directory.

    The production capsule mounts a private tmpfs, which needs root and is not
    available here. Everything else about ``_execute_auth_probe`` is real:
    this double is substituted for the mount alone, so the API-key check, the
    login classification, the argv, the read-then-delete ordering and the
    verdict all run as they do in the guest.
    """

    #: Set by a test to make the provider-side login fail the way a refused
    #: session does.
    login_error = None

    def __init__(self, auth, tool):
        self.auth = auth
        self.tool = tool
        self.root = None
        self.prepared = False
        self.exited = False

    def __enter__(self):
        if self.auth is not None:
            self._temp = tempfile.TemporaryDirectory()
            self.root = self._temp.name
        return self

    def __exit__(self, *_):
        self.exited = True
        if self.root is not None:
            self._temp.cleanup()

    def child_env(self):
        if self.auth is None or self.root is None:
            return {}
        if self.tool == "claude":
            return {"CLAUDE_CODE_OAUTH_TOKEN": self.auth["token"],
                    "CLAUDE_CONFIG_DIR": os.path.join(self.root, "claude")}
        return {"CODEX_HOME": os.path.join(self.root, "codex")}

    def prepare(self, tools, env, timeout):
        if type(self).login_error is not None:
            raise gr.GuestRunnerError(type(self).login_error)
        self.prepared = True


class ProbeLifecycleTests(unittest.TestCase):
    """``_execute_auth_probe`` end to end, not its helpers in isolation.

    The defect these exist for: cleanup deleted the Codex answer file in a
    ``finally`` block that ran before the verdict was computed, so a fully
    authenticated Codex probe reported ``failed`` and the lane could never
    open for Codex. Every helper-level test passed while that was true,
    because the helpers were never the broken part.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = os.path.join(self.temp.name, "work")
        self.calls = []
        self.addCleanup(setattr, _FakeCapsule, "login_error", None)
        _FakeCapsule.login_error = None

    def _tools(self):
        return {"claude": {"path": "/usr/local/bin/claude", "version": "1"},
                "codex": {"path": "/usr/local/bin/codex", "version": "1"}}

    def _request(self, provider, *, env=None, timeout=30.0, auth=True):
        capsule = CLAUDE_CAPSULE if provider == "claude" else CODEX_CAPSULE
        return {"mode": gr.MODE_AUTH_PROBE, "provider": provider,
                "workdir": self.workdir, "env": dict(env or {}),
                "timeout_seconds": timeout,
                "auth": dict(capsule) if auth else None}

    def _run(self, provider, responder, **kwargs):
        def call(argv, cwd, env, stdin, timeout):
            self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env),
                               "stdin": stdin, "timeout": timeout})
            return responder(argv, cwd, env, stdin, timeout)

        with mock.patch.object(gr, "_AuthCapsule", _FakeCapsule):
            return gr._execute_auth_probe(self._request(provider, **kwargs),
                                          call, self._tools(),
                                          time.monotonic())

    # -- Codex, the lifecycle that was broken ------------------------------

    def _codex_writer(self, body, returncode=0, stdout=b"{}", stderr=b""):
        """A responder that writes the answer file the way Codex does."""

        def responder(argv, cwd, env, stdin, timeout):
            path = argv[argv.index("--output-last-message") + 1]
            if body is not None:
                Path(path).write_bytes(body)
            return returncode, stdout, stderr
        return responder

    def test_an_authenticated_codex_turn_completes_the_probe(self):
        response = self._run("codex", self._codex_writer(
            gr.AUTH_PROBE_SENTINEL.encode("utf-8")))
        self.assertEqual(response["reason"], gr.PROBE_AUTHENTICATED)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)

    def test_the_codex_answer_file_is_removed_after_it_is_read(self):
        written = []

        def responder(argv, cwd, env, stdin, timeout):
            path = argv[argv.index("--output-last-message") + 1]
            written.append(path)
            Path(path).write_bytes(gr.AUTH_PROBE_SENTINEL.encode("utf-8"))
            return 0, b"{}", b""

        response = self._run("codex", responder)
        self.assertEqual(response["reason"], gr.PROBE_AUTHENTICATED)
        self.assertFalse(os.path.exists(written[0]),
                         "the probe left state behind")

    def test_a_codex_turn_without_the_sentinel_is_not_authenticated(self):
        response = self._run("codex", self._codex_writer(b"some other answer"))
        self.assertEqual(response["reason"], gr.PROBE_NO_SENTINEL)

    def test_a_codex_turn_that_wrote_nothing_is_not_authenticated(self):
        response = self._run("codex", self._codex_writer(None))
        self.assertNotEqual(response["reason"], gr.PROBE_AUTHENTICATED)

    def test_a_codex_version_only_run_is_not_authenticated(self):
        """The exact false positive the probe replaced."""

        response = self._run("codex", self._codex_writer(
            b"codex-cli 1.2.3\n", stdout=b"codex-cli 1.2.3\n"))
        self.assertNotEqual(response["reason"], gr.PROBE_AUTHENTICATED)

    def test_a_rejected_codex_session_is_observed_as_rejected(self):
        response = self._run("codex", self._codex_writer(
            None, returncode=1, stdout=b"", stderr=b"You are not logged in"))
        self.assertEqual(response["reason"], gr.PROBE_REJECTED)

    def test_an_oversized_codex_answer_is_refused_rather_than_read(self):
        big = b"x" * (gr.MAX_PROBE_MESSAGE_BYTES + 1)
        response = self._run("codex", self._codex_writer(
            big + gr.AUTH_PROBE_SENTINEL.encode("utf-8")))
        self.assertNotEqual(response["reason"], gr.PROBE_AUTHENTICATED)

    def test_a_codex_answer_that_is_a_symlink_is_not_followed(self):
        target = os.path.join(self.temp.name, "outside")
        Path(target).write_bytes(gr.AUTH_PROBE_SENTINEL.encode("utf-8"))

        def responder(argv, cwd, env, stdin, timeout):
            path = argv[argv.index("--output-last-message") + 1]
            os.symlink(target, path)
            return 0, b"{}", b""

        response = self._run("codex", responder)
        self.assertNotEqual(response["reason"], gr.PROBE_AUTHENTICATED)

    # -- Claude ------------------------------------------------------------

    def test_an_authenticated_claude_turn_completes_the_probe(self):
        response = self._run("claude", lambda *a: (0, _claude_success(), b""))
        self.assertEqual(response["reason"], gr.PROBE_AUTHENTICATED)
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)

    def test_a_claude_version_only_run_is_not_authenticated(self):
        response = self._run("claude",
                             lambda *a: (0, b"1.2.3 (Claude Code)\n", b""))
        self.assertNotEqual(response["reason"], gr.PROBE_AUTHENTICATED)
        self.assertEqual(response["harness_status"], gr.HARNESS_FAILED)

    def test_a_rejected_claude_session_is_observed_as_rejected(self):
        response = self._run("claude", lambda *a: (1, b"", b"Invalid API key"))
        self.assertEqual(response["reason"], gr.PROBE_REJECTED)

    # -- Shared lifecycle properties ---------------------------------------

    def test_an_api_key_in_the_environment_refuses_before_spawning(self):
        for key in gr.API_KEY_ENV_KEYS:
            with self.subTest(key=key):
                self.calls.clear()
                response = self._run("claude",
                                     lambda *a: (0, _claude_success(), b""),
                                     env={key: "sk-live-value"})
                self.assertEqual(response["reason"], gr.PROBE_API_KEY_PRESENT)
                self.assertEqual(self.calls, [],
                                 "the provider was spawned anyway")

    def test_a_refused_login_is_a_rejected_session_not_a_generic_failure(self):
        _FakeCapsule.login_error = "auth_login_rejected"
        response = self._run("codex", self._codex_writer(b"unused"))
        self.assertEqual(response["reason"], gr.PROBE_REJECTED)
        self.assertEqual(self.calls, [])

    def test_any_other_login_error_is_a_failure_not_a_rejection(self):
        _FakeCapsule.login_error = "auth_login_failed"
        response = self._run("codex", self._codex_writer(b"unused"))
        self.assertEqual(response["reason"], gr.PROBE_FAILED)

    def test_a_timeout_is_its_own_verdict(self):
        def responder(*_):
            raise TimeoutError

        response = self._run("claude", responder)
        self.assertEqual(response["reason"], gr.PROBE_TIMED_OUT)

    def test_a_spawn_failure_is_a_failure_verdict(self):
        def responder(*_):
            raise OSError("no such executable")

        response = self._run("claude", responder)
        self.assertEqual(response["reason"], gr.PROBE_FAILED)

    def test_no_probe_outcome_carries_any_captured_output(self):
        """The verdict crosses the boundary. The provider's words never do."""

        cases = [
            ("claude", lambda *a: (0, _claude_success(), b"")),
            ("claude", lambda *a: (0, b"1.2.3\n", b"noise")),
            ("claude", lambda *a: (1, b"secret-looking", b"Invalid API key")),
            ("codex", self._codex_writer(
                gr.AUTH_PROBE_SENTINEL.encode("utf-8"), stdout=b"chatter")),
            ("codex", self._codex_writer(b"nope", stdout=b"chatter")),
        ]
        for provider, responder in cases:
            with self.subTest(provider=provider):
                response = self._run(provider, responder)
                self.assertEqual(response["stdout_b64"], "")
                self.assertEqual(response["stderr_b64"], "")
                self.assertFalse(response.get("truncated"))

    def test_every_outcome_is_a_known_verdict(self):
        cases = [
            ("claude", lambda *a: (0, _claude_success(), b"")),
            ("claude", lambda *a: (0, b"garbage", b"")),
            ("codex", self._codex_writer(
                gr.AUTH_PROBE_SENTINEL.encode("utf-8"))),
            ("codex", self._codex_writer(None, returncode=2, stderr=b"boom")),
        ]
        for provider, responder in cases:
            with self.subTest(provider=provider):
                self.assertIn(self._run(provider, responder)["reason"],
                              gr.PROBE_VERDICTS)

    def test_the_capsule_is_always_torn_down(self):
        captured = {}
        real = _FakeCapsule

        class _Tracking(real):
            def __enter__(self):
                captured["capsule"] = self
                return real.__enter__(self)

        with mock.patch.object(gr, "_AuthCapsule", _Tracking):
            gr._execute_auth_probe(
                self._request("claude"),
                lambda *a: (_ for _ in ()).throw(OSError("boom")),
                self._tools(), time.monotonic())
        self.assertTrue(captured["capsule"].exited)
