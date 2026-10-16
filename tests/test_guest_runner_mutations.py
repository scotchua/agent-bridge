"""Boundary and failure-path tests found by the guest-runner mutation sweep."""

from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge.orchestration import guest_runner as gr


class HelperMutationTests(unittest.TestCase):
    def test_read_bytes_accepts_the_limit_and_refuses_one_more(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample"
            path.write_bytes(b"abcd")
            self.assertEqual(gr._read_bytes(str(path), 4), b"abcd")
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr._read_bytes(str(path), 3)
            self.assertEqual(caught.exception.code, "file_too_large")

    def test_sha256_file_reads_nonempty_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample"
            path.write_bytes(b"invented contents")
            self.assertEqual(gr.sha256_file(str(path)),
                             hashlib.sha256(b"invented contents").hexdigest())

    def test_parse_mounts_accepts_three_fields(self):
        self.assertEqual(gr.parse_mounts("device /mnt/c drvfs\n"),
                         [("/mnt/c", "drvfs")])

    def test_canary_wsl_conf_refuses_a_file_over_its_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wsl.conf"
            path.write_bytes(b"x" * (64 * 1024 + 1))
            with mock.patch.object(gr, "WSL_CONF_PATH", str(path)):
                with self.assertRaises(gr.GuestRunnerError) as caught:
                    gr.canary_wsl_conf()
            self.assertEqual(caught.exception.code, "file_too_large")

    def test_read_versions_refuses_a_valid_but_oversized_file(self):
        inventory = {"schema_version": gr.VERSIONS_SCHEMA_VERSION,
                     "tools": {name: {"version": "1", "path": "/bin/true",
                                      "sha256": "a" * 64} for name in gr.TOOL_NAMES}}
        payload = json.dumps(inventory).encode("utf-8")
        payload += b" " * (1024 * 1024 + 1 - len(payload))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "versions.json"
            path.write_bytes(payload)
            with mock.patch.object(gr, "VERSIONS_PATH", str(path)):
                with self.assertRaises(gr.GuestRunnerError) as caught:
                    gr.read_versions()
            self.assertEqual(caught.exception.code, "file_too_large")

    def test_default_gateway_accepts_minimal_route_fields(self):
        route = "Iface Destination Gateway\neth0 00000000 04030201\n"
        with mock.patch("builtins.open", return_value=io.StringIO(route)):
            self.assertEqual(gr.default_gateway(), "1.2.3.4")

    def test_probe_destination_treats_refusal_as_reachable(self):
        connection = mock.Mock()
        connection.connect.side_effect = OSError(errno.ECONNREFUSED, "refused")
        with mock.patch("socket.socket", return_value=connection):
            self.assertEqual(gr.probe_destination("192.0.2.1", 445), "refused")


class ValidationMutationTests(unittest.TestCase):
    def test_auth_probe_request_accepts_exact_keys(self):
        raw = {"schema_version": gr.SCHEMA_VERSION, "mode": gr.MODE_AUTH_PROBE,
               "provider": "claude", "workdir": "/workspace/invented",
               "timeout_seconds": 10, "env": {},
               "auth": {"kind": gr.AUTH_KIND_CLAUDE_OAUTH,
                        "token": "invented-session"}}
        self.assertEqual(gr._validate_auth_probe_request(raw)["provider"], "claude")

    def test_validate_timeout_refuses_boolean(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr._validate_timeout(True, "timeout_invalid")
        self.assertEqual(caught.exception.code, "timeout_invalid")

    def test_validate_workdir_refuses_outside_prefix(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr._validate_workdir("/elsewhere/invented")
        self.assertEqual(caught.exception.code, "workdir_not_allowed")

    def test_validate_env_reports_bad_value_type(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            gr._validate_env({"HOME": 3})
        self.assertEqual(caught.exception.code, "env_invalid")

    def test_validate_verify_argv_accepts_exact_command_limit(self):
        with mock.patch.object(gr, "MAX_VERIFY_COMMANDS", 1):
            self.assertEqual(gr.validate_verify_argv([["pytest"]]), [["pytest"]])

    def test_validate_workspace_accepts_exact_encoded_limit(self):
        with mock.patch.object(gr, "MAX_WORKSPACE_B64_CHARS", 4):
            self.assertEqual(gr.validate_workspace("YQ=="), "YQ==")

    def test_build_response_defaults_completed_harness_to_complete(self):
        response = gr.build_response(status="completed", reason="ok",
                                     exit_code=0, stdout=b"", stderr=b"",
                                     truncated=False, duration_seconds=0)
        self.assertEqual(response["harness_status"], gr.HARNESS_COMPLETE)

    def test_git_env_is_a_separate_pinned_mapping(self):
        self.assertEqual(gr._git_env()["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_cap_preserves_exact_limit_without_truncation(self):
        with mock.patch.object(gr, "MAX_OUTPUT_BYTES", 4):
            self.assertEqual(gr._cap(b"abcd"), (b"abcd", False))

    def test_read_request_accepts_exact_byte_limit(self):
        with mock.patch.object(gr, "MAX_TOTAL_REQUEST_BYTES", 4):
            self.assertIsNone(gr.read_request(io.BytesIO(b"null")))


class WorkspaceMutationTests(unittest.TestCase):
    def test_sweep_job_user_processes_refuses_exact_deadline(self):
        with mock.patch.object(gr, "_job_user_pids", side_effect=[[123], []]), \
                mock.patch.object(gr.os, "kill"), \
                mock.patch.object(gr.time, "monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr._sweep_job_user_processes()
        self.assertEqual(caught.exception.code, "job_process_cleanup_failed")

    def test_unpack_workspace_accepts_file_at_member_limit(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as output:
            member = tarfile.TarInfo("invented.txt")
            member.size = 4
            output.addfile(member, io.BytesIO(b"abcd"))
        completed = mock.Mock(returncode=0)
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(gr, "MAX_WORKSPACE_FILE_BYTES", 4), \
                mock.patch.object(gr, "MAX_WORKSPACE_BYTES", len(archive.getvalue())), \
                mock.patch.object(gr, "_git", return_value=completed), \
                mock.patch.object(gr, "_grant_workspace_to_job_user"):
            workdir = str(Path(temporary) / "workspace")
            gr.unpack_workspace(base64.b64encode(archive.getvalue()).decode(), workdir)
            self.assertEqual((Path(workdir) / "invented.txt").read_bytes(), b"abcd")

    def test_extraction_budget_uses_one_for_zero_compressed_bytes(self):
        self.assertEqual(gr._ExtractionBudget(compressed=0).compressed, 1)

    def test_extraction_budget_accepts_exact_member_limit(self):
        with mock.patch.object(gr, "MAX_WORKSPACE_MEMBERS", 2):
            budget = gr._ExtractionBudget(compressed=100)
            budget.count_member()
            budget.count_member()
            self.assertEqual(budget.members, 2)

    def test_extraction_budget_accepts_exact_content_limit(self):
        with mock.patch.object(gr, "MAX_WORKSPACE_CONTENT_BYTES", 4):
            budget = gr._ExtractionBudget(compressed=100)
            budget.count_bytes(4)
            self.assertEqual(budget.written, 4)

    def test_checked_member_name_accepts_exact_path_limit(self):
        with mock.patch.object(gr, "MAX_WORKSPACE_PATH_CHARS", 4):
            self.assertEqual(gr._checked_member_name(SimpleNamespace(name="abcd")),
                             "abcd")

    def test_write_workspace_file_accepts_exact_file_limit(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(gr, "MAX_WORKSPACE_FILE_BYTES", 4):
            budget = gr._ExtractionBudget(compressed=100)
            root = os.path.realpath(temporary)
            gr._write_workspace_file(root, "invented.txt", io.BytesIO(b"abcd"),
                                     budget)
            self.assertEqual((Path(root) / "invented.txt").read_bytes(), b"abcd")


class ExecutionMutationTests(unittest.TestCase):
    def test_apply_verify_egress_policy_rejects_nonzero_exit(self):
        with mock.patch.object(gr.os.path, "isfile", return_value=True), \
                mock.patch.object(gr.subprocess, "run",
                                  return_value=mock.Mock(returncode=1)):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.apply_verify_egress_policy()
        self.assertEqual(caught.exception.code, "verify_egress_apply_failed")

    def test_remove_verify_egress_policy_uses_bounded_timeout(self):
        with mock.patch.object(gr.subprocess, "run") as run:
            gr.remove_verify_egress_policy()
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_auth_capsule_enter_rejects_failed_mount(self):
        capsule = gr._AuthCapsule({"kind": gr.AUTH_KIND_CLAUDE_OAUTH,
                                   "token": "invented-session"}, "claude")
        with mock.patch.object(gr.os, "makedirs"), \
                mock.patch.object(gr.os, "chmod"), \
                mock.patch.object(gr.subprocess, "run",
                                  return_value=mock.Mock(returncode=1)):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                capsule.__enter__()
        self.assertEqual(caught.exception.code, "auth_tmpfs_unavailable")

    def test_auth_capsule_exit_rejects_failed_unmount(self):
        capsule = gr._AuthCapsule(None, "claude")
        capsule._mounted = True
        with mock.patch.object(gr.subprocess, "run",
                               return_value=mock.Mock(returncode=1)), \
                mock.patch.object(gr.os, "chown"), \
                mock.patch.object(gr.os, "chmod"):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                capsule.__exit__()
        self.assertEqual(caught.exception.code, "auth_cleanup_failed")

    def test_auth_capsule_prepare_rejects_failed_login(self):
        with tempfile.TemporaryDirectory() as temporary:
            capsule = gr._AuthCapsule({"kind": gr.AUTH_KIND_CODEX_ACCESS,
                                       "token": "invented-session"}, "codex")
            capsule.root = temporary
            with mock.patch.object(gr.os, "geteuid", return_value=1000), \
                    mock.patch.object(gr.subprocess, "run",
                                      return_value=mock.Mock(returncode=1)), \
                    mock.patch.object(gr, "_sweep_job_user_processes"):
                with self.assertRaises(gr.GuestRunnerError) as caught:
                    capsule.prepare({"codex": {"path": "/bin/true"}}, {}, 30)
            self.assertEqual(caught.exception.code, "auth_login_rejected")

    def test_read_probe_message_accepts_exact_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "answer"
            path.write_bytes(b"abcd")
            with mock.patch.object(gr, "MAX_PROBE_MESSAGE_BYTES", 4):
                self.assertEqual(gr.read_probe_message(str(path)), b"abcd")

    def test_terminate_group_uses_kill_signal(self):
        with mock.patch.object(gr.os, "killpg") as killpg:
            gr._terminate_group(123, mock.Mock())
        killpg.assert_called_once_with(123, 9)

    def test_bounded_git_times_out_at_exact_deadline(self):
        process = mock.Mock(pid=123, returncode=0, stdout=io.BytesIO(),
                            stderr=io.BytesIO())
        process.poll.return_value = 0
        with mock.patch.object(gr.subprocess, "Popen", return_value=process), \
                mock.patch.object(gr.os, "getpgid", return_value=123), \
                mock.patch.object(gr, "_terminate_group"), \
                mock.patch.object(gr.time, "monotonic", side_effect=[0.0, 1.0]):
            with self.assertRaises(TimeoutError):
                gr._bounded_git(["status"], "/tmp", limit=4, timeout=1.0)

    def test_subprocess_runner_times_out_at_exact_deadline(self):
        process = mock.Mock(pid=123, returncode=0, stdout=io.BytesIO(),
                            stderr=io.BytesIO(), stdin=io.BytesIO())
        process.poll.return_value = 0
        with mock.patch.object(gr.os.path, "isdir", return_value=True), \
                mock.patch.object(gr.subprocess, "Popen", return_value=process), \
                mock.patch.object(gr.os, "getpgid", return_value=123), \
                mock.patch.object(gr, "_terminate_group"), \
                mock.patch.object(gr.time, "monotonic", side_effect=[0.0, 1.0]):
            with self.assertRaises(TimeoutError):
                gr._subprocess_runner(["/bin/true"], "/tmp", {}, "", 1.0)


if __name__ == "__main__":
    unittest.main()
