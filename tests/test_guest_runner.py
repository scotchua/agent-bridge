"""Portable tests for the pinned in-guest runner.

Almost everything here runs on any platform. The classes marked as in-guest
behaviour spawn the runner's own subprocess wrapper or resolve its fixed
Linux paths, and are skipped on a Windows host: the runner only ever executes
inside the Linux guest, and hiding that behind a mock would test the mock. The runner's real work is file reads
and one subprocess, so the decisions are factored into pure functions and the
subprocess is injected, which means the security properties can be tested
without a WSL guest. What cannot be tested here is that the installed copy in
a real image behaves the same; that needs a live Windows host and is not
claimed anywhere.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
from pathlib import Path
import os
import sys
import tarfile
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr
from agent_bridge.orchestration import windows_wsl as ww
from agent_bridge.orchestration import windows_wsl_runtime as wr


class _FakeCapsule:
    """Stands in for the tmpfs capsule: no mount, same interface."""

    def __init__(self, auth, tool):
        self.auth, self.tool, self.root = auth, tool, "/run/fake"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def child_env(self):
        return {} if self.auth is None else {"CLAUDE_CONFIG_DIR": "/run/fake"}

    def prepare(self, tools, env, timeout):
        return None

RUNNER_SOURCE = ROOT / "src/agent_bridge/orchestration/guest_runner.py"

VERSIONS = {
    "schema_version": 1,
    "tools": {
        "node": {"version": "20.11.1", "path": "/usr/local/bin/node", "sha256": "a" * 64},
        "claude": {"version": "1.2.3", "path": "/usr/local/bin/claude", "sha256": "b" * 64},
        "codex": {"version": "0.9.0", "path": "/usr/local/bin/codex", "sha256": "c" * 64},
    },
}


def _request(**overrides):
    fields = {
        "schema_version": gr.SCHEMA_VERSION,
        "mode": gr.MODE_TOOL,
        "tool": "node",
        "args": ["--version"],
        "workdir": "/workspace/job",
        "timeout_seconds": 30,
        "env": {"HOME": "/root"},
        "stdin": "",
        "auth": None,
        "workspace_tar_b64": None,
    }
    fields.update(overrides)
    return fields


class SourceIndependenceTests(unittest.TestCase):
    """The file is copied verbatim into the image, so it may not depend on
    anything outside the image."""

    def test_the_runner_imports_nothing_from_agent_bridge(self):
        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("agent_bridge"), alias.name)
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("agent_bridge"),
                                 node.module)
                self.assertEqual(node.level, 0, "no relative imports inside the image")

    def test_the_runner_uses_only_standard_library_modules(self):
        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names <= set(sys.stdlib_module_names), sorted(names))

    def test_the_runner_never_reads_the_ambient_environment(self):
        """A child's environment is rebuilt, never inherited."""
        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        referenced = {ast.unparse(node) for node in ast.walk(tree)
                      if isinstance(node, (ast.Attribute, ast.Name))}
        self.assertNotIn("os.environ", referenced)
        self.assertNotIn("os.getenv", referenced)


class CanaryConformanceTests(unittest.TestCase):
    """The host compares canary stdout byte for byte, so the two sides
    agreeing is the entire contract."""

    def test_both_sides_name_the_same_canaries_in_the_same_order(self):
        self.assertEqual(gr.CANARY_NAMES, wr.CANARY_ORDER)

    def test_the_host_expects_the_path_this_runner_is_installed_at(self):
        self.assertEqual(gr.RUNNER_PATH, ww.GUEST_RUNNER_PATH)

    def test_the_host_and_guest_agree_on_the_allowed_workdir_prefixes(self):
        self.assertEqual(gr.ALLOWED_WORKDIR_PREFIXES, ww.ALLOWED_GUEST_PATH_PREFIXES)

    def test_the_host_and_guest_agree_on_the_environment_allowlist(self):
        self.assertEqual(set(gr.CHILD_ENV_KEYS), set(ww.ALLOWED_GUEST_ENV_KEYS))

    def test_every_canary_line_matches_what_the_host_expects_exactly(self):
        manifest = ww.parse_manifest({
            "schema_version": 1,
            "distro_release": "22.04.3",
            "rootfs_sha256": "d" * 64,
            "node_version": "20.11.1",
            "claude_version": "1.2.3",
            "codex_version": "0.9.0",
        })
        runner_digest = "e" * 64
        expected = wr.canary_expectations(manifest, runner_digest)

        conf = ww.WSL_CONF_CONTENTS.encode("utf-8")
        with mock.patch.object(gr, "_read_bytes", return_value=conf):
            self.assertEqual(gr.canary_wsl_conf(), expected[gr.CANARY_WSL_CONF])
        with mock.patch.object(gr, "sha256_file", return_value=runner_digest):
            self.assertEqual(gr.canary_guest_runner(),
                             expected[gr.CANARY_GUEST_RUNNER])
        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
                mock.patch.object(gr, "sha256_file",
                                  side_effect=lambda path: {
                                      "/usr/local/bin/node": "a" * 64,
                                      "/usr/local/bin/claude": "b" * 64,
                                      "/usr/local/bin/codex": "c" * 64}[path]):
            self.assertEqual(gr.canary_versions(), expected[gr.CANARY_VERSIONS])

    def test_the_host_accepts_the_exact_bytes_the_runner_writes(self):
        stdout = io.BytesIO()
        with mock.patch.dict(gr.CANARIES, {gr.CANARY_INTEROP: lambda: "wsl-interop-absent:ok"}):
            self.assertEqual(gr.main(["--canary", gr.CANARY_INTEROP], io.BytesIO(), stdout), 0)
        self.assertTrue(wr._matches_exactly(stdout.getvalue(), "wsl-interop-absent:ok"))


class HostMountCanaryTests(unittest.TestCase):
    def test_passes_when_no_host_path_and_no_host_filesystem_is_present(self):
        with mock.patch.object(gr.os.path, "exists", return_value=False), \
                mock.patch.object(gr, "_read_bytes",
                                  return_value=b"proc /proc proc rw 0 0\n"):
            self.assertEqual(gr.canary_host_mount(), "host-mount-absent:ok")

    def test_a_present_host_path_fails(self):
        with mock.patch.object(gr.os.path, "exists",
                               side_effect=lambda p: p == "/mnt/c"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_host_mount()
        self.assertEqual(caught.exception.code, "host_mount_present")

    def test_a_host_filesystem_mounted_elsewhere_still_fails(self):
        """Absence of /mnt/c is not absence of the host: drvfs can be
        mounted anywhere."""
        with mock.patch.object(gr.os.path, "exists", return_value=False), \
                mock.patch.object(gr, "_read_bytes",
                                  return_value=b"C:\\ /somewhere drvfs rw 0 0\n"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_host_mount()
        self.assertEqual(caught.exception.code, "host_filesystem_mounted")

    def test_an_unreadable_mount_table_is_not_proof_of_absence(self):
        with mock.patch.object(gr.os.path, "exists", return_value=False), \
                mock.patch.object(gr, "_read_bytes", side_effect=OSError(13, "denied")):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_host_mount()
        self.assertEqual(caught.exception.code, "mounts_unreadable")

    def test_every_host_filesystem_type_is_detected(self):
        for fstype in sorted(gr.HOST_FILESYSTEM_TYPES):
            self.assertTrue(gr.host_filesystem_present([("/elsewhere", fstype)]), fstype)

    def test_an_ordinary_guest_mount_table_is_clean(self):
        mounts = gr.parse_mounts(
            "/dev/sdc / ext4 rw,relatime 0 0\n"
            "proc /proc proc rw,nosuid 0 0\n"
            "tmpfs /run tmpfs rw 0 0\n")
        self.assertFalse(gr.host_filesystem_present(mounts))


class InteropCanaryTests(unittest.TestCase):
    def test_passes_when_no_interop_registration_exists(self):
        with mock.patch.object(gr.os.path, "exists", return_value=False), \
                mock.patch.object(gr.os.path, "isdir", return_value=False):
            self.assertEqual(gr.canary_interop(), "wsl-interop-absent:ok")

    def test_a_registered_interop_handler_fails(self):
        with mock.patch.object(gr.os.path, "exists",
                               side_effect=lambda p: p.endswith("WSLInterop")):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_interop()
        self.assertEqual(caught.exception.code, "interop_registered")

    def test_a_renamed_interop_entry_in_binfmt_misc_still_fails(self):
        with mock.patch.object(gr.os.path, "exists", return_value=False), \
                mock.patch.object(gr.os.path, "isdir", return_value=True), \
                mock.patch.object(gr.os, "listdir", return_value=["status", "wsl_interop"]):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_interop()
        self.assertEqual(caught.exception.code, "interop_registered")


class VersionsCanaryTests(unittest.TestCase):
    def test_a_tool_whose_hash_does_not_match_fails_rather_than_reciting(self):
        """The inventory says what the image contains. If the file on disk is
        a different file, reporting the recorded version would report a
        pinned image that is not the pinned image."""
        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
                mock.patch.object(gr, "sha256_file", return_value="f" * 64):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_versions()
        self.assertEqual(caught.exception.code, "tool_hash_mismatch")

    def test_an_unreadable_tool_fails(self):
        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
                mock.patch.object(gr, "sha256_file", side_effect=OSError(2, "missing")):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_versions()
        self.assertEqual(caught.exception.code, "tool_unreadable")

    def test_a_well_formed_inventory_parses(self):
        parsed = gr.load_versions(VERSIONS)
        self.assertEqual(set(parsed), set(gr.TOOL_NAMES))
        self.assertEqual(parsed["node"]["version"], "20.11.1")

    def test_every_malformed_inventory_shape_is_rejected(self):
        cases = {
            "versions_malformed": ["not a mapping", None, 7],
            "versions_schema_unsupported": [{"schema_version": 2, "tools": {}}],
            "versions_tools_mismatch": [
                {"schema_version": 1, "tools": {"node": {}}},
                {"schema_version": 1, "tools": "no"},
            ],
        }
        for code, values in cases.items():
            for value in values:
                with self.assertRaises(gr.GuestRunnerError) as caught:
                    gr.load_versions(value)
                self.assertEqual(caught.exception.code, code, value)

    def test_a_relative_or_traversing_tool_path_is_rejected(self):
        for path in ("usr/local/bin/node", "/usr/../etc/shadow", ""):
            broken = json.loads(json.dumps(VERSIONS))
            broken["tools"]["node"]["path"] = path
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.load_versions(broken)
            self.assertEqual(caught.exception.code, "versions_entry_malformed", path)

    def test_a_non_hex_digest_is_rejected(self):
        broken = json.loads(json.dumps(VERSIONS))
        broken["tools"]["codex"]["sha256"] = "z" * 64
        with self.assertRaises(gr.GuestRunnerError):
            gr.load_versions(broken)


class RequestValidationTests(unittest.TestCase):
    def test_a_well_formed_request_is_accepted(self):
        parsed = gr.validate_request(_request())
        self.assertEqual(parsed["tool"], "node")
        self.assertEqual(parsed["timeout_seconds"], 30.0)

    def test_an_unknown_tool_is_refused(self):
        for tool in ("bash", "/bin/sh", "python3", ""):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_request(tool=tool))
            self.assertEqual(caught.exception.code, "tool_not_allowed", tool)

    def test_a_request_cannot_name_a_path_to_execute(self):
        """The request carries a tool name; the runner resolves the path from
        the image inventory. A request that could name a path could execute
        anything in the guest."""
        with self.assertRaises(gr.GuestRunnerError):
            gr.validate_request(_request(tool="/usr/local/bin/node"))
        self.assertNotIn("path", gr.REQUEST_KEYS)
        self.assertNotIn("command", gr.REQUEST_KEYS)

    def test_unknown_or_missing_keys_are_refused(self):
        extra = _request()
        extra["shell"] = True
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(extra)
        self.assertEqual(caught.exception.code, "request_keys_invalid")
        missing = _request()
        del missing["env"]
        with self.assertRaises(gr.GuestRunnerError):
            gr.validate_request(missing)

    def test_a_workdir_outside_the_allowlist_is_refused(self):
        for workdir in ("/mnt/c/Users", "/etc", "/workspace/../etc", "relative"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_request(workdir=workdir))
            self.assertEqual(caught.exception.code, "workdir_not_allowed", workdir)

    def test_an_environment_key_outside_the_allowlist_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_request(env={"LD_PRELOAD": "/tmp/evil.so"}))
        self.assertEqual(caught.exception.code, "env_key_not_allowed")

    def test_a_credential_shaped_environment_key_is_refused(self):
        for key in ("ANTHROPIC_API_KEY", "OPENAI_TOKEN", "AWS_SECRET",
                    "SESSION_ID", "AUTH_HEADER"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_request(env={key: "x"}))
            self.assertEqual(caught.exception.code, "env_key_not_allowed", key)

    def test_timeouts_are_bounded_on_both_sides(self):
        for timeout in (0, -1, gr.MAX_TIMEOUT_SECONDS + 1, True, "30"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_request(timeout_seconds=timeout))
            self.assertEqual(caught.exception.code, "timeout_invalid", timeout)

    def test_oversized_input_is_refused_rather_than_truncated(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_request(stdin="x" * (gr.MAX_REQUEST_BYTES + 1)))
        self.assertEqual(caught.exception.code, "stdin_too_large")

    def test_too_many_or_oversized_arguments_are_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_request(args=["x"] * (gr.MAX_COMMAND_ARGS + 1)))
        self.assertEqual(caught.exception.code, "args_invalid")
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_request(args=["x" * (gr.MAX_ARG_BYTES + 1)]))
        self.assertEqual(caught.exception.code, "args_too_large")

    def test_a_null_byte_anywhere_is_refused(self):
        for field in ({"args": ["a\x00b"]}, {"workdir": "/workspace/a\x00b"},
                      {"env": {"HOME": "/root\x00"}}):
            with self.assertRaises(gr.GuestRunnerError):
                gr.validate_request(_request(**field))


@unittest.skipIf(os.name == "nt", "in-guest runner behaviour: Linux guest only")
class ExecutionTests(unittest.TestCase):
    def _execute(self, request=None, runner=None):
        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]):
            return gr.execute(gr.validate_request(request or _request()), runner=runner)

    def test_a_clean_run_reports_completed_with_its_output(self):
        def runner(argv, cwd, env, stdin_data, timeout):
            self.assertEqual(argv, ["/usr/local/bin/node", "--version"])
            self.assertEqual(cwd, "/workspace/job")
            return 0, b"v20.11.1\n", b""

        response = self._execute(runner=runner)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["reason"], "ok")
        self.assertEqual(base64.b64decode(response["stdout_b64"]), b"v20.11.1\n")

    def test_the_child_environment_is_rebuilt_not_inherited(self):
        seen = {}

        def runner(argv, cwd, env, stdin_data, timeout):
            seen.update(env)
            return 0, b"", b""

        self._execute(request=_request(env={"AGENT_BRIDGE_JOB_ID": "job-1"}), runner=runner)
        self.assertEqual(set(seen), set(gr.DEFAULT_CHILD_ENV) | {"AGENT_BRIDGE_JOB_ID"})
        self.assertEqual(seen["PATH"], "/usr/local/bin:/usr/bin:/bin")

    def test_input_reaches_the_child_only_through_its_stdin(self):
        seen = {}

        def runner(argv, cwd, env, stdin_data, timeout):
            seen["stdin"] = stdin_data
            seen["argv"] = argv
            return 0, b"", b""

        self._execute(request=_request(stdin="secret-brief"), runner=runner)
        self.assertEqual(seen["stdin"], "secret-brief")
        self.assertNotIn("secret-brief", " ".join(seen["argv"]))

    def test_a_nonzero_exit_is_not_reported_as_completed(self):
        response = self._execute(runner=lambda *a: (3, b"out", b"err"))
        self.assertEqual(response["status"], "aborted")
        self.assertEqual(response["reason"], "nonzero_exit")
        self.assertEqual(response["exit_code"], 3)

    def test_a_timeout_is_aborted_and_discards_partial_output(self):
        def runner(*args):
            raise TimeoutError("slow")

        response = self._execute(runner=runner)
        self.assertEqual(response["reason"], "job_timed_out")
        self.assertEqual(response["stdout_b64"], "")
        self.assertIsNone(response["exit_code"])

    def test_a_spawn_failure_is_aborted_not_crashed(self):
        def runner(*args):
            raise OSError(2, "no such file")

        self.assertEqual(self._execute(runner=runner)["reason"], "spawn_failed")

    def test_output_over_the_cap_is_aborted_and_discarded(self):
        oversized = b"x" * (gr.MAX_OUTPUT_BYTES + 1)
        response = self._execute(runner=lambda *a: (0, oversized, b""))
        self.assertEqual(response["status"], "aborted")
        self.assertEqual(response["reason"], "output_too_large")
        self.assertTrue(response["truncated"])
        self.assertEqual(response["stdout_b64"], "")

    def test_binary_output_survives_the_json_round_trip(self):
        payload = bytes(range(256))
        response = self._execute(runner=lambda *a: (0, payload, b""))
        decoded = json.loads(json.dumps(response))
        self.assertEqual(base64.b64decode(decoded["stdout_b64"]), payload)


class EntryPointTests(unittest.TestCase):
    def _run(self, argv, stdin=b""):
        out = io.BytesIO()
        code = gr.main(argv, io.BytesIO(stdin), out)
        return code, out.getvalue()

    def test_an_unknown_argv_shape_is_a_usage_error_not_a_guess(self):
        for argv in ([], ["--canary"], ["--run", "extra"], ["--help"],
                     ["--canary", "made-up"], ["--serve"]):
            code, output = self._run(argv)
            self.assertIn(code, (2, 3), argv)
            self.assertEqual(output, b"", argv)

    def test_a_failing_canary_prints_nothing_at_all(self):
        def boom():
            raise gr.GuestRunnerError("host_mount_present")

        with mock.patch.dict(gr.CANARIES, {gr.CANARY_HOST_MOUNT: boom}):
            code, output = self._run(["--canary", gr.CANARY_HOST_MOUNT])
        self.assertEqual(code, 3)
        self.assertEqual(output, b"")

    def test_a_canary_raising_an_os_error_also_prints_nothing(self):
        def boom():
            raise OSError(13, "permission denied reading /etc/wsl.conf")

        with mock.patch.dict(gr.CANARIES, {gr.CANARY_WSL_CONF: boom}):
            code, output = self._run(["--canary", gr.CANARY_WSL_CONF])
        self.assertEqual(code, 3)
        self.assertEqual(output, b"")

    def test_a_malformed_request_produces_a_path_free_response(self):
        code, output = self._run(["--run"], b"{not json")
        response = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(response["reason"], "request_malformed")

    def test_an_oversized_request_is_refused_without_being_parsed(self):
        code, output = self._run(["--run"], b"x" * (gr.MAX_TOTAL_REQUEST_BYTES + 10))
        self.assertEqual(json.loads(output)["reason"], "request_too_large")

    def test_an_empty_request_is_refused(self):
        self.assertEqual(json.loads(self._run(["--run"], b"")[1])["reason"], "request_empty")

    @unittest.skipIf(os.name == "nt", "in-guest runner behaviour: Linux guest only")
    def test_a_valid_request_runs_and_reports_on_one_line(self):
        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
                mock.patch.object(gr, "_subprocess_runner",
                                  return_value=(0, b"ok", b"")):
            code, output = self._run(["--run"], json.dumps(_request()).encode())
        self.assertEqual(code, 0)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertEqual(json.loads(output)["status"], "completed")

    @unittest.skipIf(os.name == "nt", "in-guest runner behaviour: Linux guest only")
    def test_no_response_field_carries_an_operating_system_message(self):
        """Responses become durable records on the host, so they must not
        quote OS error text, which routinely names the file it failed on."""
        def runner(*args):
            raise OSError(2, "No such file or directory: '/root/.secret-path'")

        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
                mock.patch.object(gr, "_untrusted_subprocess_runner",
                                  side_effect=runner):
            code, output = self._run(["--run"], json.dumps(_request()).encode())
        self.assertNotIn("secret-path", output.decode())
        self.assertEqual(json.loads(output)["reason"], "spawn_failed")


@unittest.skipIf(os.name == "nt", "in-guest runner behaviour: Linux guest only")
class LeastPrivilegeTests(unittest.TestCase):
    def test_production_child_wrapper_requests_privilege_drop(self):
        seen = {}

        def base(*args, **kwargs):
            seen.update(kwargs)
            return 0, b"", b""

        with mock.patch.object(gr, "_subprocess_runner", base), \
             mock.patch.object(gr.os, "geteuid", return_value=0), \
             mock.patch.object(gr, "_sweep_job_user_processes"):
            gr._untrusted_subprocess_runner(
                ["/bin/true"], "/tmp", {}, "", 1.0)
        self.assertIs(seen.get("drop_privileges"), True)

    def test_job_identity_is_fixed_and_not_root(self):
        entry = mock.Mock(pw_uid=gr.JOB_UID, pw_gid=gr.JOB_GID)
        fake_pwd = mock.Mock(getpwnam=mock.Mock(return_value=entry))
        with mock.patch.dict(sys.modules, {"pwd": fake_pwd}):
            self.assertEqual(gr._job_identity(), (gr.JOB_UID, gr.JOB_GID))
        self.assertNotEqual(gr.JOB_UID, 0)

    def test_privilege_drop_uses_native_popen_fields_not_preexec(self):
        entry = mock.Mock(pw_uid=gr.JOB_UID, pw_gid=gr.JOB_GID)
        fake_pwd = mock.Mock(getpwnam=mock.Mock(return_value=entry))
        with mock.patch.dict(sys.modules, {"pwd": fake_pwd}), \
             mock.patch.object(gr.os, "geteuid", return_value=0):
            fields = gr._job_subprocess_kwargs()
        self.assertEqual(fields, {"user": gr.JOB_UID, "group": gr.JOB_GID,
                                  "extra_groups": ()})
        self.assertNotIn("preexec_fn", fields)

    def test_untrusted_wrapper_sweeps_daemonised_descendants(self):
        with mock.patch.object(gr, "_subprocess_runner",
                               return_value=(0, b"", b"")), \
             mock.patch.object(gr, "_sweep_job_user_processes") as sweep:
            gr._untrusted_subprocess_runner(
                ["/bin/true"], "/tmp", {}, "", 1.0)
        sweep.assert_called_once_with()


class AuthCapsuleTests(unittest.TestCase):
    """The session capsule: required where it belongs, refused everywhere else."""

    def test_a_provider_job_without_a_capsule_is_refused(self):
        for tool in ("claude", "codex"):
            with self.subTest(tool=tool):
                with self.assertRaises(gr.GuestRunnerError) as caught:
                    gr.validate_request(_request(tool=tool, auth=None))
                self.assertEqual(caught.exception.code, "auth_required")

    def test_a_non_provider_job_with_a_capsule_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_request(
                tool="node", auth={"kind": gr.AUTH_KIND_CLAUDE_OAUTH,
                                   "token": "abc"}))
        self.assertEqual(caught.exception.code, "auth_not_accepted")

    def test_each_provider_accepts_only_its_own_capsule_kind(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_request(
                tool="claude", auth={"kind": gr.AUTH_KIND_CODEX_ACCESS,
                                     "token": "abc"}))
        self.assertEqual(caught.exception.code, "auth_kind_invalid")

    def test_a_well_formed_capsule_is_accepted(self):
        validated = gr.validate_request(_request(
            tool="claude",
            auth={"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": "sk-ant-oat-x"}))
        self.assertEqual(validated["auth"]["kind"], gr.AUTH_KIND_CLAUDE_OAUTH)

    def test_a_token_with_whitespace_or_control_characters_is_refused(self):
        for bad in ("a b", "a\nb", "a\tb", "a\x00b", "a\rb"):
            with self.subTest(bad=bad):
                with self.assertRaises(gr.GuestRunnerError):
                    gr.validate_request(_request(
                        tool="claude",
                        auth={"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": bad}))

    def test_an_oversized_token_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_request(
                tool="claude",
                auth={"kind": gr.AUTH_KIND_CLAUDE_OAUTH,
                      "token": "a" * (gr.MAX_AUTH_TOKEN_BYTES + 1)}))
        self.assertEqual(caught.exception.code, "auth_too_large")

    def test_extra_capsule_keys_are_refused(self):
        with self.assertRaises(gr.GuestRunnerError):
            gr.validate_request(_request(
                tool="claude",
                auth={"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": "a",
                      "scope": "all"}))

    def test_the_claude_capsule_sets_the_oauth_variable_and_never_an_api_key(self):
        capsule = gr._AuthCapsule(
            {"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": "tok"}, "claude")
        capsule.root = "/run/x"
        env = capsule.child_env()
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].startswith("/run/x"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_the_codex_capsule_relocates_its_home_and_carries_no_token_in_env(self):
        capsule = gr._AuthCapsule(
            {"kind": gr.AUTH_KIND_CODEX_ACCESS, "token": "tok"}, "codex")
        capsule.root = "/run/x"
        env = capsule.child_env()
        self.assertTrue(env["CODEX_HOME"].startswith("/run/x"))
        self.assertNotIn("tok", json.dumps(env))

    def test_no_capsule_means_no_added_environment(self):
        self.assertEqual(gr._AuthCapsule(None, "node").child_env(), {})

    def test_a_token_is_redacted_out_of_child_output(self):
        auth = {"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": "sk-secret"}
        self.assertEqual(gr.redact(b"before sk-secret after", auth),
                         b"before [redacted] after")

    def test_redaction_is_a_no_op_without_a_capsule(self):
        self.assertEqual(gr.redact(b"plain", None), b"plain")

    def test_a_capsule_token_never_reaches_a_response(self):
        auth = {"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": "sk-secret"}

        def runner(argv, cwd, env, stdin_data, timeout):
            return 0, b"leaked sk-secret", b"also sk-secret"

        request = dict(gr.validate_request(_request(tool="claude", auth=auth)))
        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
             mock.patch.object(gr, "_AuthCapsule", _FakeCapsule):
            response = gr.execute(request, runner=runner)
        blob = json.dumps(response)
        self.assertNotIn("sk-secret", base64.b64decode(
            response["stdout_b64"]).decode())
        self.assertNotIn("sk-secret", blob)

    def test_the_unproven_parts_are_named_not_glossed(self):
        text = gr.AUTH_UNPROVEN
        self.assertIn("portability", text)
        self.assertIn("expires", text)
        self.assertIn("no API key", text)
        self.assertIs(gr.AUTHENTICATION_BLOCKER, gr.AUTH_UNPROVEN)

    def test_the_handoff_names_each_provider_documented_mechanism(self):
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", gr.AUTH_HANDOFF)
        self.assertIn("--with-access-token", gr.AUTH_HANDOFF)
        self.assertIn("neither is an API key", gr.AUTH_HANDOFF)


class WorkspaceTests(unittest.TestCase):
    """The repository goes in through the pipe and the patch comes back."""

    def _tar(self, files):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, body in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def test_absent_is_allowed(self):
        self.assertIsNone(gr.validate_workspace(None))

    def test_a_non_string_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError):
            gr.validate_workspace(b"bytes")

    def test_invalid_base64_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_workspace("not base64!!")
        self.assertEqual(caught.exception.code, "workspace_invalid")

    def test_an_oversized_workspace_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_workspace("A" * (gr.MAX_WORKSPACE_B64_CHARS + 4))
        self.assertEqual(caught.exception.code, "workspace_too_large")

    def test_the_request_bound_leaves_room_for_a_workspace(self):
        self.assertGreater(gr.MAX_TOTAL_REQUEST_BYTES, gr.MAX_WORKSPACE_B64_CHARS)
        self.assertGreater(gr.MAX_TOTAL_REQUEST_BYTES, gr.MAX_REQUEST_BYTES)

    def test_an_absolute_member_path_is_refused(self):
        payload = self._tar({"/etc/passwd": b"x"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.unpack_workspace(payload, os.path.join(tmp, "job"))
        self.assertEqual(caught.exception.code, "workspace_member_unsafe")

    def test_a_traversing_member_path_is_refused(self):
        payload = self._tar({"../escape": b"x"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.unpack_workspace(payload, os.path.join(tmp, "job"))
        self.assertEqual(caught.exception.code, "workspace_member_unsafe")

    def test_a_symlink_member_is_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/shadow"
            archive.addfile(info)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.unpack_workspace(payload, os.path.join(tmp, "job"))
        self.assertEqual(caught.exception.code, "workspace_member_unsafe")

    def test_a_malformed_archive_is_refused(self):
        payload = base64.b64encode(b"not a tar at all").decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.unpack_workspace(payload, os.path.join(tmp, "job"))
        self.assertEqual(caught.exception.code, "workspace_malformed")

    @unittest.skipUnless(os.path.isfile("/usr/bin/git"), "git not at the pinned path")
    def test_a_real_round_trip_produces_the_patch_the_job_made(self):
        payload = self._tar({"a.txt": b"before\n"})
        with tempfile.TemporaryDirectory() as tmp:
            workdir = os.path.join(tmp, "job")
            gr.unpack_workspace(payload, workdir)
            self.assertEqual(gr.capture_diff(workdir), b"")
            with open(os.path.join(workdir, "a.txt"), "wb") as handle:
                handle.write(b"after\n")
            patch = gr.capture_diff(workdir)
        self.assertIn(b"-before", patch)
        self.assertIn(b"+after", patch)

    def test_a_workspace_free_job_reports_no_diff(self):
        self.assertEqual(gr.build_response(
            status="completed", reason="ok", exit_code=0, stdout=b"", stderr=b"",
            truncated=False, duration_seconds=0.0)["diff_b64"], "")

    def test_a_directory_without_git_metadata_yields_no_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(gr.capture_diff(tmp), b"")


class EgressPolicyTests(unittest.TestCase):
    """The network claim has to match what is enforced."""

    def test_the_posture_does_not_claim_the_guest_is_offline(self):
        self.assertIn("outbound internet access", gr.NETWORK_POSTURE)
        self.assertIn("not", gr.NETWORK_POSTURE.lower())

    def test_every_private_range_is_dropped(self):
        ruleset = gr.egress_ruleset()
        for cidr in gr.BLOCKED_EGRESS_V4:
            self.assertIn(f"ip daddr {cidr} drop", ruleset)
        for cidr in gr.BLOCKED_EGRESS_V6:
            self.assertIn(f"ip6 daddr {cidr} drop", ruleset)

    def test_loopback_is_still_allowed(self):
        self.assertIn("oifname lo accept", gr.egress_ruleset())

    def test_the_link_local_range_that_reaches_the_windows_host_is_dropped(self):
        self.assertIn("169.254.0.0/16", gr.BLOCKED_EGRESS_V4)

    def test_a_missing_tool_refuses_rather_than_running_unfenced(self):
        with mock.patch.object(gr.os.path, "isfile", return_value=False):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.apply_egress_policy()
        self.assertEqual(caught.exception.code, "egress_tool_missing")

    def test_a_nonzero_nft_exit_is_a_refusal(self):
        completed = types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
        with mock.patch.object(gr.os.path, "isfile", return_value=True), \
             mock.patch.object(gr.subprocess, "run", return_value=completed):
            with self.assertRaises(gr.GuestRunnerError):
                gr.apply_egress_policy()

    def test_the_canary_reads_the_rules_back_out_of_the_kernel(self):
        listing = gr.egress_ruleset() + "drop\n" * 10
        completed = types.SimpleNamespace(
            returncode=0, stdout=listing.encode(), stderr=b"")
        with mock.patch.object(gr.os.path, "isfile", return_value=True), \
             mock.patch.object(gr.subprocess, "run", return_value=completed):
            line = gr.canary_egress()
        self.assertTrue(line.startswith(gr.CANARY_EGRESS + ":"))
        self.assertIn("169.254.0.0/16", line)

    def test_a_table_that_is_missing_a_range_fails_the_canary(self):
        partial = "table inet agent_bridge_egress { drop }"
        completed = types.SimpleNamespace(
            returncode=0, stdout=partial.encode(), stderr=b"")
        with mock.patch.object(gr.os.path, "isfile", return_value=True), \
             mock.patch.object(gr.subprocess, "run", return_value=completed):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.canary_egress()
        self.assertEqual(caught.exception.code, "egress_policy_absent")

    def test_the_network_canaries_are_last_so_cheaper_proofs_fail_first(self):
        self.assertEqual(gr.CANARY_NAMES[-2:],
                         (gr.CANARY_EGRESS, gr.CANARY_EGRESS_PROBE))


@unittest.skipIf(os.name == "nt", "in-guest runner behaviour: Linux guest only")
class BoundedStreamingTests(unittest.TestCase):
    """Output is capped while it streams, and nothing can hang the runner."""

    def _run(self, script, timeout=20.0, workdir=None):
        return gr._subprocess_runner(
            [sys.executable, "-c", script], workdir or str(ROOT),
            {"PATH": "/usr/bin:/bin"}, "", timeout)

    def test_a_normal_child_returns_its_output(self):
        code, out, err = self._run(
            "import sys;sys.stdout.write('hi');sys.stderr.write('bye')")
        self.assertEqual((code, out, err), (0, b"hi", b"bye"))

    def test_stdin_reaches_the_child(self):
        code, out, _ = gr._subprocess_runner(
            [sys.executable, "-c", "import sys;sys.stdout.write(sys.stdin.read())"],
            str(ROOT), {"PATH": "/usr/bin:/bin"}, "payload", 20.0)
        self.assertEqual(out, b"payload")

    def test_a_child_that_ignores_stdin_does_not_break_the_run(self):
        code, out, _ = gr._subprocess_runner(
            [sys.executable, "-c", "print('done')"], str(ROOT),
            {"PATH": "/usr/bin:/bin"}, "x" * 100000, 20.0)
        self.assertEqual(code, 0)
        self.assertIn(b"done", out)

    def test_runaway_output_is_stopped_rather_than_buffered_forever(self):
        with mock.patch.object(gr, "MAX_OUTPUT_BYTES", 4096):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                self._run("import sys\nwhile True: sys.stdout.write('x'*4096)")
        self.assertEqual(caught.exception.code, "output_too_large")

    def test_a_timeout_raises_and_does_not_hang(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self._run("import time;time.sleep(30)", timeout=1.0)
        self.assertLess(time.monotonic() - started, 20.0)

    def test_a_descendant_holding_the_pipe_cannot_hang_the_runner(self):
        # The child exits immediately but leaves a grandchild holding stdout.
        # communicate() would block here until the grandchild exited.
        script = (
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
            "sys.stdout.write('parent-done')\n"
        )
        started = time.monotonic()
        code, out, _ = self._run(script, timeout=25.0)
        self.assertLess(time.monotonic() - started, 20.0)
        self.assertIn(b"parent-done", out)

    def test_a_missing_workdir_is_refused_before_any_spawn(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._run("pass", workdir="/nonexistent-workdir-for-a-test")
        self.assertEqual(caught.exception.code, "workdir_missing")



def _tarball(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _provider_request(**overrides):
    fields = {
        "schema_version": gr.SCHEMA_VERSION,
        "mode": gr.MODE_PROVIDER_JOB,
        "provider": "claude",
        "model": "sonnet",
        "effort": "medium",
        "brief": "Implement the thing.\n",
        "verify_argv": [["git", "status"]],
        "workdir": "/workspace/job",
        "timeout_seconds": 300,
        "verify_timeout_seconds": 60,
        "env": {"HOME": "/root"},
        "auth": {"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": "sk-session-token"},
        "workspace_tar_b64": _tarball({"README.md": b"hello\n"}),
    }
    fields.update(overrides)
    return fields


class _ProviderCapsule:
    """The capsule without the mount: same environment, no privileges."""

    def __init__(self, auth, tool):
        self.auth, self.tool, self.root = auth, tool, "/run/fake/session"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def child_env(self):
        if self.auth is None:
            return {}
        if self.tool == "claude":
            return {"CLAUDE_CODE_OAUTH_TOKEN": self.auth["token"],
                    "CLAUDE_CONFIG_DIR": self.root + "/claude"}
        return {"CODEX_HOME": self.root + "/codex"}

    def prepare(self, tools, env, timeout):
        return None


class ProviderJobValidationTests(unittest.TestCase):
    """The shape the execution queue actually dispatches."""

    def test_a_well_formed_provider_job_validates(self):
        parsed = gr.validate_request(_provider_request())
        self.assertEqual(parsed["mode"], gr.MODE_PROVIDER_JOB)
        self.assertEqual(parsed["verify_argv"], [["git", "status"]])
        self.assertEqual(set(parsed), gr.PROVIDER_REQUEST_KEYS - {"schema_version"})

    def test_the_mode_is_declared_not_inferred(self):
        request = _provider_request()
        del request["mode"]
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(request)
        self.assertEqual(caught.exception.code, "request_mode_invalid")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(mode="whatever"))
        self.assertEqual(caught.exception.code, "request_mode_invalid")

    def test_a_tool_shaped_key_in_a_provider_job_is_refused(self):
        request = _provider_request()
        request["tool"] = "node"
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(request)
        self.assertEqual(caught.exception.code, "request_keys_invalid")

    def test_a_provider_job_needs_a_workspace(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(workspace_tar_b64=None))
        self.assertEqual(caught.exception.code, "workspace_required")

    def test_a_provider_job_needs_a_capsule(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(auth=None))
        self.assertEqual(caught.exception.code, "auth_required")

    def test_a_capsule_for_the_other_provider_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(
                auth={"kind": gr.AUTH_KIND_CODEX_ACCESS, "token": "x"}))
        self.assertEqual(caught.exception.code, "auth_kind_invalid")

    def test_a_non_provider_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(provider="node"))
        self.assertEqual(caught.exception.code, "provider_not_allowed")

    def test_an_empty_brief_is_refused(self):
        for value in ("", "   \n"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_provider_request(brief=value))
            self.assertEqual(caught.exception.code, "brief_invalid")

    def test_an_oversized_brief_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(
                brief="x" * (gr.MAX_BRIEF_BYTES + 1)))
        self.assertEqual(caught.exception.code, "brief_too_large")

    def test_a_model_name_that_is_not_a_plain_token_is_refused(self):
        for value in ("sonnet; rm -rf /", "a b", "$(x)", "x" * 200, ""):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_provider_request(model=value))
            self.assertEqual(caught.exception.code, "model_invalid")

    def test_no_verification_at_all_is_refused(self):
        for value in ([], None, "git status"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_provider_request(verify_argv=value))
            self.assertEqual(caught.exception.code, "verify_argv_required")

    def test_a_non_allowlisted_verify_program_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(verify_argv=[["bash", "-c", "x"]]))
        self.assertEqual(caught.exception.code, "verify_program_not_allowed")

    def test_a_pathful_verify_program_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(
                verify_argv=[["/workspace/job/git", "status"]]))
        self.assertEqual(caught.exception.code, "verify_program_not_allowed")

    def test_a_writing_git_verification_is_refused(self):
        for command in (["git", "push"], ["git"]):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.validate_request(_provider_request(verify_argv=[command]))
            self.assertEqual(caught.exception.code, "verify_git_not_read_only")

    def test_python_verification_is_limited_to_pytest(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(verify_argv=[["python3", "-c", "x"]]))
        self.assertEqual(caught.exception.code, "verify_python_not_pytest")
        gr.validate_request(_provider_request(
            verify_argv=[["python3", "-m", "pytest", "-q"]]))

    def test_too_many_verification_commands_are_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(
                verify_argv=[["git", "status"]] * (gr.MAX_VERIFY_COMMANDS + 1)))
        self.assertEqual(caught.exception.code, "verify_argv_too_many")

    def test_a_control_character_in_a_verify_argument_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr.validate_request(_provider_request(
                verify_argv=[["git", "status", "a\nb"]]))
        self.assertEqual(caught.exception.code, "verify_argv_invalid")

    def test_a_secret_bearing_environment_key_is_still_refused(self):
        with self.assertRaises(gr.GuestRunnerError):
            gr.validate_request(_provider_request(
                env={"ANTHROPIC_API_KEY": "x"}))


class ProviderArgvTests(unittest.TestCase):
    def test_the_claude_argv_carries_no_shell_tool(self):
        argv = gr.provider_argv("claude", "/usr/local/bin/claude", model="sonnet",
                                effort="high", workdir="/workspace/job",
                                last_message_path="/workspace/job/.m")
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--tools") + 1],
                         "Read,Grep,Glob,Edit,Write")
        self.assertNotIn("Bash", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_the_codex_argv_disables_network_and_user_config(self):
        argv = gr.provider_argv("codex", "/usr/local/bin/codex", model="default",
                                effort="low", workdir="/workspace/job",
                                last_message_path="/workspace/job/.m")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.assertNotIn("-m", argv)

    def test_a_named_codex_model_is_passed_through(self):
        argv = gr.provider_argv("codex", "/usr/local/bin/codex", model="gpt-5",
                                effort="low", workdir="/workspace/job",
                                last_message_path="/workspace/job/.m")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5")

    def test_neither_argv_ever_carries_the_brief(self):
        for provider in gr.PROVIDER_TOOLS:
            argv = gr.provider_argv(provider, "/usr/local/bin/" + provider,
                                    model="default", effort="low",
                                    workdir="/workspace/job",
                                    last_message_path="/workspace/job/.m")
            self.assertNotIn("Implement the thing.", " ".join(argv))


class ProviderJobExecutionTests(unittest.TestCase):
    """Run the provider, then the checks, then return the patch."""

    def setUp(self):
        self.calls = []

    def _runner(self, responses):
        replies = list(responses or [])

        def runner(argv, cwd, env, stdin_data, timeout):
            self.calls.append({"argv": list(argv), "cwd": cwd,
                               "env": dict(env), "stdin": stdin_data})
            return replies.pop(0) if replies else (0, b"", b"")

        return runner

    def _execute(self, request=None, responses=None, resolve=None,
                 egress=lambda: "verify-egress-denied:test"):
        """``egress`` stands in for the nftables deny policy.

        There is no nft on this machine and no kernel to load a table into, so
        the enforcement itself is not under test here. What is under test is
        that the job calls it, and refuses when it fails: see
        VerificationEgressTests below.
        """

        resolve = resolve or (lambda name: "/usr/bin/" + name)
        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
             mock.patch.object(gr, "_AuthCapsule", _ProviderCapsule), \
             mock.patch.object(gr, "unpack_workspace", lambda *a: None), \
             mock.patch.object(gr, "capture_diff", lambda workdir: b"PATCH\n"), \
             mock.patch.object(gr, "enforce_verification_egress", egress), \
             mock.patch.object(gr, "verify_program_path", resolve):
            return gr.execute(gr.validate_request(request or _provider_request()),
                              runner=self._runner(responses))

    def test_a_clean_job_reports_complete_and_returns_the_patch(self):
        response = self._execute()
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)
        self.assertEqual(response["reason"], "ok")
        self.assertEqual(base64.b64decode(response["diff_b64"]), b"PATCH\n")
        self.assertEqual(len(response["verification"]), 1)
        self.assertEqual(set(response["verification"][0]), gr.VERIFICATION_KEYS)

    def test_the_brief_reaches_the_provider_on_stdin_only(self):
        self._execute()
        self.assertEqual(self.calls[0]["stdin"], "Implement the thing.\n")
        self.assertNotIn("Implement the thing.", " ".join(self.calls[0]["argv"]))

    def test_a_failing_check_is_verification_failed_not_complete(self):
        response = self._execute(responses=[(0, b"", b""), (1, b"bad\n", b"")])
        self.assertEqual(response["harness_status"],
                         gr.HARNESS_VERIFICATION_FAILED)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["reason"], "verification_failed")
        self.assertEqual(response["verification"][0]["returncode"], 1)

    def test_every_check_runs_even_after_one_fails(self):
        request = _provider_request(verify_argv=[["git", "status"], ["git", "diff"]])
        response = self._execute(request=request,
                                 responses=[(0, b"", b""), (1, b"", b""),
                                            (0, b"", b"")])
        self.assertEqual(len(response["verification"]), 2)
        self.assertEqual(response["harness_status"],
                         gr.HARNESS_VERIFICATION_FAILED)

    def test_a_provider_that_exits_nonzero_never_reaches_verification(self):
        response = self._execute(responses=[(2, b"", b"boom\n")])
        self.assertEqual(response["harness_status"], gr.HARNESS_FAILED)
        self.assertEqual(response["reason"], "provider_nonzero_exit")
        self.assertEqual(response["verification"], [])
        self.assertEqual(len(self.calls), 1)

    def test_verification_never_sees_the_session_environment(self):
        self._execute()
        provider_env, check_env = self.calls[0]["env"], self.calls[1]["env"]
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", provider_env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", check_env)
        self.assertNotIn("CLAUDE_CONFIG_DIR", check_env)

    def test_no_environment_ever_carries_a_metered_api_key(self):
        self._execute()
        for call in self.calls:
            self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
            self.assertNotIn("OPENAI_API_KEY", call["env"])

    def test_the_token_never_appears_in_any_returned_field(self):
        response = self._execute(responses=[(0, b"tok sk-session-token\n", b"")])
        self.assertNotIn("sk-session-token", json.dumps(response))

    def test_the_checks_run_in_the_workspace_by_resolved_path(self):
        self._execute()
        self.assertEqual(self.calls[1]["cwd"], "/workspace/job")
        self.assertEqual(self.calls[1]["argv"], ["/usr/bin/git", "status"])
        self.assertEqual(self.calls[1]["stdin"], "")

    def test_an_absent_verification_program_aborts_the_job(self):
        def missing(name):
            raise gr.GuestRunnerError("verify_program_absent")

        response = self._execute(resolve=missing)
        self.assertEqual(response["harness_status"], gr.HARNESS_ABORTED)
        self.assertEqual(response["reason"], "verify_program_absent")

    def test_a_timeout_aborts_rather_than_reporting_a_verdict(self):
        def runner(argv, cwd, env, stdin_data, timeout):
            raise TimeoutError

        with mock.patch.object(gr, "read_versions", return_value=VERSIONS["tools"]), \
             mock.patch.object(gr, "_AuthCapsule", _ProviderCapsule), \
             mock.patch.object(gr, "unpack_workspace", lambda *a: None):
            response = gr.execute(gr.validate_request(_provider_request()),
                                  runner=runner)
        self.assertEqual(response["harness_status"], gr.HARNESS_ABORTED)
        self.assertEqual(response["reason"], "job_timed_out")

    def test_output_over_the_cap_aborts_and_discards(self):
        big = b"x" * (gr.MAX_OUTPUT_BYTES + 1)
        response = self._execute(responses=[(0, big, b"")])
        self.assertEqual(response["harness_status"], gr.HARNESS_ABORTED)
        self.assertEqual(response["reason"], "output_too_large")
        self.assertEqual(base64.b64decode(response["stdout_b64"]), b"")

    def test_a_codex_job_logs_in_through_the_capsule_not_the_argv(self):
        request = _provider_request(
            provider="codex", model="default",
            auth={"kind": gr.AUTH_KIND_CODEX_ACCESS, "token": "at-secret"})
        response = self._execute(request=request)
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)
        self.assertIn("CODEX_HOME", self.calls[0]["env"])
        self.assertNotIn("at-secret", " ".join(self.calls[0]["argv"]))

    def test_a_response_carries_exactly_the_agreed_keys(self):
        self.assertEqual(set(self._execute()), gr.RESPONSE_KEYS)


@unittest.skipIf(os.name == "nt", "in-guest runner behaviour: Linux guest only")
class VerifyProgramResolutionTests(unittest.TestCase):
    def test_only_fixed_directories_are_searched(self):
        self.assertEqual(gr.VERIFY_BIN_DIRS,
                         ("/usr/local/bin", "/usr/bin", "/bin"))

    def test_resolution_never_consults_path(self):
        seen = []

        def isfile(candidate):
            seen.append(candidate)
            return False

        with mock.patch.object(gr.os.path, "isfile", isfile):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.verify_program_path("git")
        self.assertEqual(caught.exception.code, "verify_program_absent")
        self.assertEqual(seen, [d + "/git" for d in gr.VERIFY_BIN_DIRS])

    def test_a_resolved_program_must_also_be_executable(self):
        with mock.patch.object(gr.os.path, "isfile", lambda c: True), \
             mock.patch.object(gr.os, "access", lambda c, m: False):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.verify_program_path("git")
        self.assertEqual(caught.exception.code, "verify_program_absent")




class EgressReachabilityProbeTests(unittest.TestCase):
    """A ruleset in the kernel is not proof that anything is unreachable."""

    def _probe(self, verdicts, gateway="172.20.0.1"):
        seen = []

        def probe(host, port, timeout=None):
            seen.append((host, port))
            return verdicts.get(host, "blocked")

        with mock.patch.object(gr, "probe_destination", probe), \
             mock.patch.object(gr, "default_gateway", lambda: gateway):
            return gr.canary_egress_probe(), seen

    def test_a_fully_dropped_set_of_destinations_passes(self):
        line, seen = self._probe({})
        self.assertTrue(line.startswith(gr.CANARY_EGRESS_PROBE + ":"))
        self.assertIn(f"targets={len(gr.PROBE_TARGETS) + 1}", line)
        self.assertEqual(len(seen), len(gr.PROBE_TARGETS) + 1)

    def test_one_address_in_every_protected_range_is_tried(self):
        _, seen = self._probe({})
        tried = {host for host, _ in seen}
        for prefix in ("10.", "172.", "192.168.", "169.254.", "100."):
            self.assertTrue(any(host.startswith(prefix) for host in tried),
                            prefix)

    def test_the_guests_own_gateway_is_probed(self):
        _, seen = self._probe({})
        self.assertIn(("172.20.0.1", 445), seen)

    def test_a_destination_that_answers_fails_the_canary(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._probe({"192.168.0.1": "reachable"})
        self.assertEqual(caught.exception.code, "egress_destination_reachable")

    def test_a_refused_connection_is_not_treated_as_blocked(self):
        """A RST means the packet reached something that answered."""
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._probe({"10.0.0.1": "refused"})
        self.assertEqual(caught.exception.code, "egress_destination_refused")

    def test_a_reachable_gateway_fails_the_canary(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._probe({"172.20.0.1": "reachable"})
        self.assertEqual(caught.exception.code, "egress_destination_reachable")

    def test_an_unknown_gateway_is_a_refusal_not_a_pass(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._probe({}, gateway="")
        self.assertEqual(caught.exception.code, "egress_gateway_unknown")

    def test_the_refusal_never_names_the_address_it_reached(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._probe({"192.168.1.1": "reachable"})
        self.assertNotIn("192.168", caught.exception.code)

    def test_a_timeout_is_the_only_thing_a_dropped_packet_can_look_like(self):
        import socket as _socket

        class _Timing:
            def settimeout(self, value):
                return None

            def connect(self, address):
                raise _socket.timeout

            def close(self):
                return None

        with mock.patch.object(_socket, "socket", lambda *a: _Timing()):
            self.assertEqual(gr.probe_destination("10.0.0.1", 445), "blocked")

    def test_an_unreachable_network_counts_as_blocked(self):
        import errno as _errno
        import socket as _socket

        class _Unreachable:
            def settimeout(self, value):
                return None

            def connect(self, address):
                raise OSError(_errno.ENETUNREACH, "no route")

            def close(self):
                return None

        with mock.patch.object(_socket, "socket", lambda *a: _Unreachable()):
            self.assertEqual(gr.probe_destination("10.0.0.1", 445), "blocked")

    def test_a_connected_socket_counts_as_reachable(self):
        import socket as _socket

        class _Open:
            def settimeout(self, value):
                return None

            def connect(self, address):
                return None

            def close(self):
                return None

        with mock.patch.object(_socket, "socket", lambda *a: _Open()):
            self.assertEqual(gr.probe_destination("10.0.0.1", 445), "reachable")


class DefaultGatewayTests(unittest.TestCase):
    ROUTE = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
             "eth0\t000014AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"
             "eth0\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\n")

    def test_the_default_route_is_read_not_assumed(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=self.ROUTE)):
            self.assertEqual(gr.default_gateway(), "172.20.0.1")

    def test_a_table_with_no_default_route_reports_nothing(self):
        rows = "\n".join(self.ROUTE.splitlines()[:2]) + "\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=rows)):
            self.assertEqual(gr.default_gateway(), "")

    def test_an_unreadable_table_reports_nothing_rather_than_guessing(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertEqual(gr.default_gateway(), "")


class NetworkPostureWordingTests(unittest.TestCase):
    """The claim must be exactly what is proven, no wider."""

    def test_public_provider_egress_is_documented_as_allowed(self):
        self.assertIn("outbound internet access", gr.NETWORK_POSTURE)
        self.assertIn("public provider egress is", gr.NETWORK_POSTURE)

    def test_the_ruleset_canary_is_not_described_as_proof_of_unreachability(self):
        text = gr.NETWORK_POSTURE
        self.assertIn("evidence the rules are loaded, and nothing more", text)

    def test_the_sampling_limit_is_stated_rather_than_glossed(self):
        self.assertIn("not proof that every private address is unreachable",
                      gr.NETWORK_POSTURE)

    def test_the_posture_still_refuses_to_call_this_offline(self):
        self.assertIn("Do not describe this sandbox as offline.",
                      gr.NETWORK_POSTURE)


if __name__ == "__main__":
    unittest.main()
