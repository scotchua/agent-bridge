"""The POSIX launcher interpreter resolver stays usable with a desktop PATH."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import python_runtime  # noqa: E402


class PythonLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.bin = self.base / "bin"
        self.home.mkdir()
        self.bin.mkdir()

    def test_every_posix_launcher_uses_the_shared_resolver(self) -> None:
        for name in python_runtime.LAUNCHERS:
            text = (ROOT / "bin" / name).read_text(encoding="utf-8")
            self.assertIn('. "$HERE/agent-bridge-python"', text, name)

    def fake(self, name: str, *, version: str = "3.14.0", supported: bool = True,
             label: str = "") -> Path:
        path = self.bin / name
        path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo \"Python " + version + "\"; exit 0; fi\n"
            "if [ \"$1\" = \"-P\" ] && [ \"$2\" = \"-c\" ]; then\n"
            "  case \"$3\" in\n"
            f"    *sys.version_info*) [ -z \"${{FAKE_LOG:-}}\" ] || printf '%s\\n' 'probe-{label}' >> \"$FAKE_LOG\"; "
            + ("exit 0" if supported else "exit 1") + ";;\n"
            f"    *) exec {shlex.quote(sys.executable)} \"$@\";;\n"
            "  esac\n"
            "fi\n"
            f"[ -z \"${{FAKE_LOG:-}}\" ] || printf '%s\\n' '{label}' >> \"$FAKE_LOG\"\n"
            "printf '%s\\n' '{\"hookSpecificOutput\": {\"hookEventName\": \"PreToolUse\", \"permissionDecision\": \"allow\"}}'\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def environment(self, **extra: str) -> dict[str, str]:
        return {
            "HOME": str(self.home), "TMPDIR": str(self.base),
            "PATH": str(self.bin) + ":/usr/bin:/bin", **extra,
        }

    def write_record(self, value: str) -> Path:
        record = Path(python_runtime.record_path(str(self.home)))
        record.parent.mkdir(mode=0o700, exist_ok=True)
        record.parent.chmod(0o700)
        if record.is_symlink():
            record.unlink()
        record.write_text(value + "\n", encoding="utf-8")
        record.chmod(0o600)
        return record

    def gate(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "bin" / "agent-bridge-gate-hook"), "--client", "claude",
             "--state-root", str(self.base / "state")],
            input="{}", text=True, capture_output=True, env=env, cwd=ROOT,
            timeout=20, shell=False,
        )

    def test_minimal_path_with_only_python_39_denies_once_and_names_version(self) -> None:
        self.fake("python3", version="3.9.6", supported=False)
        completed = self.gate(self.environment())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payloads = [json.loads(line) for line in completed.stdout.splitlines() if line]
        self.assertEqual(len(payloads), 1)
        reason = payloads[0]["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("no Python 3.11 or newer found; python3 is 3.9.6", reason)
        self.assertEqual(payloads[0]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_recorded_path_is_used_without_a_compatibility_probe(self) -> None:
        recorded = self.fake("recorded", label="recorded")
        self.write_record(str(recorded))
        log = self.base / "calls"
        completed = self.gate(self.environment(FAKE_LOG=str(log)))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["recorded"])

    def test_path_fallback_chooses_the_newest_supported_name(self) -> None:
        self.fake("python3.14", label="3.14")
        self.fake("python3.12", label="3.12")
        log = self.base / "calls"
        completed = self.gate(self.environment(FAKE_LOG=str(log)))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["probe-3.14", "3.14"])

    def test_agent_bridge_python_overrides_the_record_and_path(self) -> None:
        recorded = self.fake("recorded", label="recorded")
        override = self.fake("override", label="override")
        python_runtime.record(str(recorded), home=str(self.home))
        log = self.base / "calls"
        completed = self.gate(self.environment(FAKE_LOG=str(log), AGENT_BRIDGE_PYTHON=str(override)))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["override"])

    def test_mcp_launcher_reports_the_same_missing_python_reason(self) -> None:
        self.fake("python3", version="3.9.6", supported=False)
        completed = subprocess.run(
            [str(ROOT / "bin" / "agent-bridge-mcp"), "--caller", "claude"],
            text=True, capture_output=True, env=self.environment(), cwd=ROOT,
            timeout=20, shell=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("no Python 3.11 or newer found; python3 is 3.9.6", completed.stderr)

    def test_doctor_flags_a_missing_recorded_interpreter_for_every_launcher(self) -> None:
        missing = self.base / "gone-python"
        python_runtime.record(str(missing), home=str(self.home))
        report = python_runtime.doctor(str(self.home))
        self.assertEqual(report["recorded_issue"], "record_invalid_interpreter")
        self.assertTrue(all(
            entry["source"] == "PATH fallback (record rejected)"
            for entry in report["launchers"].values()))
        self.assertEqual(python_runtime.record_path(str(self.home)), report["record_path"])
        self.assertEqual(Path(report["record_path"]).stat().st_mode & 0o077, 0)

    def test_unsafe_record_falls_back_to_path(self) -> None:
        cases = {
            "group_writable": lambda record: record.chmod(0o620),
            "parent_group_writable": lambda record: record.parent.chmod(0o720),
            "symlink": lambda record: (record.unlink(), record.symlink_to(self.base / "record-target")),
            "relative_interpreter": lambda record: record.write_text("recorded\n", encoding="utf-8"),
        }
        for name, make_unsafe in cases.items():
            with self.subTest(name=name):
                fallback = self.fake("python3.14", label=f"fallback-{name}")
                record = self.write_record(str(self.fake("recorded", label=f"recorded-{name}")))
                if name == "symlink":
                    target = self.base / "record-target"
                    target.write_text(str(self.fake("record-target-python", label="target")) + "\n", encoding="utf-8")
                make_unsafe(record)
                log = self.base / f"calls-{name}"
                completed = self.gate(self.environment(FAKE_LOG=str(log)))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"], "allow")
                self.assertEqual(log.read_text(encoding="utf-8").splitlines(),
                                 [f"probe-fallback-{name}", f"fallback-{name}"])
                self.assertEqual(python_runtime.recorded(str(self.home)), None)
                with mock.patch.dict(os.environ, self.environment(), clear=False):
                    report = python_runtime.doctor(str(self.home))
                expected = "record_not_owner_only" if name != "relative_interpreter" else "record_invalid_interpreter"
                self.assertEqual(report["recorded_issue"], expected)
                self.assertEqual(report["launchers"]["agent-bridge-gate-hook"]["interpreter"], str(fallback))

    def test_record_owned_by_another_user_is_ignored(self) -> None:
        recorded = self.fake("recorded", label="recorded")
        self.write_record(str(recorded))
        status = os.lstat(python_runtime.record_path(str(self.home)))
        with mock.patch.object(python_runtime.os, "getuid", return_value=status.st_uid + 1):
            self.assertIsNone(python_runtime.recorded(str(self.home)))
            report = python_runtime.doctor(str(self.home))
        self.assertEqual(report["recorded_issue"], "record_not_owner_only")
