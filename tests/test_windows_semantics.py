"""Windows behaviour that a POSIX-shaped test would never catch.

Four things behave differently enough on Windows to be worth their own file,
and all four are load-bearing here:

  * **Paths.** Win32 resolves names a string comparison thinks are ordinary
    files: reserved device names in any directory, alternate data streams,
    trailing dots and spaces that are silently stripped. Every identity check
    in this project is a string comparison somewhere.
  * **Locking.** There is no ``flock``. The lock is a byte range held on an
    OS handle through ``msvcrt.locking``, and what matters is that it is
    exclusive across processes and that the kernel releases it on a crash.
  * **Line endings.** Text written on Windows grows carriage returns. A file
    the bridge edits in place must keep the line endings it found, and a file
    whose bytes are hashed or signed must not be translated at all.
  * **Processes.** There is no ``fork`` and no ``execve``: Windows takes a
    single command *string* and each program parses it for itself. That is
    why nothing here builds a shell string, and why the one place that must
    produce one (``schtasks /TR``) refuses anything it cannot quote.

Most of this is pure and runs on every platform, which is the point: a
Windows-only test that runs on one runner is a test that stops being read. The
handful that need the real operating system say so.

None of this has been validated on a live Windows host.
"""

from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import (
    windows_activation as wact,
    windows_privacy as wpriv,
    windows_wsl as ww,
)

ON_WINDOWS = os.name == "nt"

#: A registration the onboarding editor will accept, so the line-ending tests
#: exercise the real editor rather than a stand-in.
COMMAND = {"command": "python3", "args": ["-m", "agent_bridge.mcp"]}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class ReservedDeviceNameTests(unittest.TestCase):
    """``C:\\agent-bridge\\NUL`` is not a file. It is the null device."""

    #: Every DOS device name is reserved in every directory, with or without
    #: an extension, in any case.
    RESERVED = ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9")

    def _refused(self, value):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", value)

    def test_every_reserved_name_is_refused_as_a_leaf(self):
        for name in self.RESERVED:
            with self.subTest(name):
                self._refused(f"C:\\agent-bridge\\{name}")

    def test_every_reserved_name_is_refused_as_a_directory(self):
        for name in self.RESERVED:
            with self.subTest(name):
                self._refused(f"C:\\agent-bridge\\{name}\\record.json")

    def test_an_extension_does_not_make_it_a_file(self):
        # This is the case people get wrong: NUL.txt is still the null device.
        self._refused("C:\\agent-bridge\\NUL.txt")
        self._refused("C:\\agent-bridge\\con.log")

    def test_case_does_not_matter(self):
        for spelling in ("nul", "Nul", "nUl", "NUL"):
            with self.subTest(spelling):
                self._refused(f"C:\\agent-bridge\\{spelling}")

    def test_a_name_that_merely_contains_one_is_allowed(self):
        # NULL, CONFIG and COM10 are ordinary names. Over-refusing would make
        # the check something users work around rather than trust.
        for allowed in ("NULL", "CONFIG", "COM10", "console", "prnt"):
            with self.subTest(allowed):
                ww._validate_windows_host_path(
                    "p", f"C:\\agent-bridge\\{allowed}\\x.exe")

    def test_the_forward_slash_spelling_is_checked_too(self):
        self._refused("C:/agent-bridge/NUL/record.json")

    @unittest.skipUnless(ON_WINDOWS, "needs the real Win32 namespace")
    def test_the_operating_system_agrees_that_nul_is_not_a_file(self):
        directory = tempfile.mkdtemp()
        target = os.path.join(directory, "NUL")
        with open(target, "wb") as handle:
            handle.write(b"this goes nowhere")
        # The write succeeded and nothing exists. That is exactly the failure
        # the validator prevents: a record that reports written and is not.
        self.assertFalse(os.path.isfile(target))


class AlternateDataStreamTests(unittest.TestCase):
    """``record.json:hidden`` is a different stream on the same file."""

    def test_a_stream_suffix_is_refused(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "C:\\agent-bridge\\record.json:hidden")

    def test_an_executable_stream_is_refused(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "C:\\agent-bridge\\readme.txt:payload.exe")

    def test_a_stream_on_a_directory_component_is_refused(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "C:\\agent-bridge:s\\record.json")

    def test_the_drive_colon_is_not_mistaken_for_a_stream(self):
        ww._validate_windows_host_path("p", "C:\\agent-bridge\\record.json")


class TrailingDotAndSpaceTests(unittest.TestCase):
    """Win32 strips them; a string comparison does not."""

    def test_a_trailing_space_is_refused(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "C:\\agent-bridge\\record.json ")

    def test_a_trailing_dot_is_refused(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "C:\\agent-bridge\\record.")

    def test_a_trailing_space_on_a_directory_is_refused(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "C:\\agent-bridge \\record.json")

    def test_an_interior_dot_is_fine(self):
        ww._validate_windows_host_path("p", "C:\\agent-bridge\\v1.2.3\\record.json")

    @unittest.skipUnless(ON_WINDOWS, "needs the real Win32 namespace")
    def test_the_operating_system_treats_the_two_spellings_as_one_file(self):
        directory = tempfile.mkdtemp()
        plain = os.path.join(directory, "record.json")
        with open(plain, "wb") as handle:
            handle.write(b"{}")
        # Opening the space-suffixed spelling reaches the same file, which is
        # why two unequal strings cannot be trusted to be two files.
        with open(plain + " ", "rb") as handle:
            self.assertEqual(handle.read(), b"{}")


class UncAndDevicePathTests(unittest.TestCase):
    """A logon task on a share runs whatever the share serves at sign-in."""

    def test_both_unc_spellings_are_refused(self):
        for value in ("\\\\server\\share\\x.exe", "//server/share/x.exe"):
            with self.subTest(value):
                with self.assertRaises(ww.WindowsWslContractError):
                    ww._validate_windows_host_path("p", value)

    def test_every_device_prefix_is_refused(self):
        for value in ("\\\\?\\C:\\x.exe", "\\\\.\\PhysicalDrive0",
                      "//?/C:/x.exe", "//./PhysicalDrive0"):
            with self.subTest(value):
                with self.assertRaises(ww.WindowsWslContractError):
                    ww._validate_windows_host_path("p", value)

    def test_ntpath_would_have_called_all_of_those_absolute(self):
        # The reason the check cannot be `ntpath.isabs`: it says yes to every
        # one of them.
        for value in ("\\\\server\\share\\x.exe", "\\\\?\\C:\\x.exe"):
            with self.subTest(value):
                self.assertTrue(ntpath.isabs(value))

    def test_a_drive_relative_path_is_refused(self):
        # `C:x.exe` means "x.exe in the current directory of drive C", which
        # is per-process state and not a location.
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "C:x.exe")

    def test_a_rooted_path_with_no_drive_is_refused(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("p", "\\agent-bridge\\x.exe")


class ActivationPathTests(unittest.TestCase):
    """The logon task inherits every one of those rules."""

    def test_a_reserved_device_name_cannot_be_the_task_executable(self):
        with self.assertRaises(wact.ActivationError):
            wact.validate_executable("C:\\agent-bridge\\NUL")

    def test_a_stream_cannot_be_the_task_executable(self):
        with self.assertRaises(wact.ActivationError):
            wact.validate_executable("C:\\agent-bridge\\python.exe:evil")

    def test_a_network_executable_is_refused(self):
        with self.assertRaises(wact.ActivationError):
            wact.validate_executable("\\\\server\\share\\python.exe")

    def test_the_refusal_never_quotes_the_offending_path(self):
        # These messages reach installer output and durable records.
        try:
            wact.validate_executable("\\\\server\\secret-share\\python.exe")
        except wact.ActivationError as exc:
            self.assertNotIn("secret-share", str(exc))
        else:
            self.fail("expected a refusal")


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


class CommandLineQuotingTests(unittest.TestCase):
    """Windows hands a program one string and lets it parse it itself."""

    EXE = "C:\\Program Files\\Python\\python.exe"

    def test_a_path_with_spaces_is_quoted_as_one_argument(self):
        action = wact.build_action([self.EXE, "-m", "agent_bridge.windows_setup"])
        self.assertIn(f'"{self.EXE}"', action)
        self.assertTrue(action.startswith('"'))

    def test_an_argument_without_spaces_is_not_quoted(self):
        action = wact.build_action([self.EXE, "resume"])
        self.assertTrue(action.endswith(" resume"))

    def test_a_double_quote_is_refused_rather_than_escaped(self):
        # schtasks documents no escape, and cmd and the task engine disagree
        # about how one would be parsed.
        with self.assertRaises(wact.ActivationError):
            wact.build_action([self.EXE, 'a"b'])

    def test_a_percent_is_refused_because_the_task_engine_expands_it(self):
        with self.assertRaises(wact.ActivationError):
            wact.build_action([self.EXE, "%USERPROFILE%"])

    def test_every_control_character_is_refused(self):
        for character in ("\x00", "\n", "\r", "\t"):
            with self.subTest(repr(character)):
                with self.assertRaises(wact.ActivationError):
                    wact.build_action([self.EXE, f"a{character}b"])

    def test_an_action_too_long_to_store_is_refused_not_truncated(self):
        long_tail = "x" * wact.MAX_TASK_ACTION_CHARS
        with self.assertRaises(wact.ActivationError):
            wact.build_action([self.EXE, long_tail])

    def test_the_length_ceiling_is_measured_on_the_stored_string(self):
        # Quoting adds characters, so the check has to happen after it.
        room = wact.MAX_TASK_ACTION_CHARS - len(f'"{self.EXE}"') - 1
        wact.build_action([self.EXE, "x" * room])
        with self.assertRaises(wact.ActivationError):
            wact.build_action([self.EXE, "x" * (room + 1)])


class NoShellAnywhereTests(unittest.TestCase):
    """Nothing in the Windows lane builds a command for a shell to parse."""

    MODULES = ("windows_activation.py", "windows_wsl.py", "windows_wsl_runtime.py",
               "windows_wsl_provision.py", "windows_provision_driver.py",
               "windows_delegation.py", "windows_privacy.py", "windows_auth.py",
               "windows_rootfs.py", "windows_evidence.py")

    def test_no_module_ever_passes_shell_true(self):
        for name in self.MODULES:
            source = (ROOT / "src" / "agent_bridge" / "orchestration"
                      / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertNotIn("shell=True", source)

    def test_no_module_invokes_a_command_interpreter(self):
        # cmd.exe and powershell.exe re-parse their arguments, which undoes
        # every quoting decision made above them.
        pattern = re.compile(r"\"(?:cmd|powershell|pwsh)\.exe\"")
        for name in self.MODULES:
            source = (ROOT / "src" / "agent_bridge" / "orchestration"
                      / name).read_text(encoding="utf-8")
            for match in pattern.finditer(source):
                line = source[:match.start()].count("\n") + 1
                # cmd.exe /c ver is the one documented exception: it is how
                # the build number is read, and it takes no variable input.
                context = source.splitlines()[line - 1]
                with self.subTest(f"{name}:{line}"):
                    self.assertIn("ver", context.lower(),
                                  f"{name}:{line}: {context.strip()}")

    def test_the_guest_argv_never_carries_a_host_path(self):
        job = ww.validate_job_spec({
            "command": [ww.GUEST_RUNNER_PATH], "workdir": "/workspace/job",
            "env": {}})
        argv = ww.build_guest_exec_argv(ww.build_distro_name("abc123"), job)
        joined = " ".join(argv)
        self.assertNotIn("C:", joined)
        self.assertNotIn("\\", joined)


class SystemPathPinningTests(unittest.TestCase):
    """PATH is attacker-influenced on Windows; System32 is not."""

    def test_schtasks_is_invoked_by_absolute_path(self):
        argv = wact.unregister_argv()
        self.assertEqual(argv[0], "C:\\Windows\\System32\\schtasks.exe")

    def test_the_registration_argv_is_also_absolute(self):
        argv = wact.register_argv([CommandLineQuotingTests.EXE, "resume"])
        self.assertTrue(ntpath.isabs(argv[0]))
        self.assertTrue(argv[0].upper().startswith("C:\\WINDOWS\\SYSTEM32\\"))


# ---------------------------------------------------------------------------
# Line endings
# ---------------------------------------------------------------------------


class LineEndingTests(unittest.TestCase):
    """Text the bridge edits keeps its endings; bytes it hashes keep theirs."""

    def test_a_crlf_config_keeps_crlf_when_a_block_is_inserted(self):
        from agent_bridge import onboard

        original = "[a]\r\nkey = 1\r\n"
        updated = onboard._toml_registration_update(
            "config.toml", "agent-bridge", COMMAND, text_override=original)
        text = updated.decode("utf-8")
        self.assertIn("\r\n", text)
        # Not one lone LF anywhere: a mixed-ending file is what makes the
        # next read-modify-write produce a whole-file diff.
        self.assertEqual(text.count("\n"), text.count("\r\n"))

    def test_an_lf_config_does_not_grow_carriage_returns(self):
        from agent_bridge import onboard

        updated = onboard._toml_registration_update(
            "config.toml", "agent-bridge", COMMAND,
            text_override="[a]\nkey = 1\n")
        self.assertNotIn(b"\r", updated)

    def test_removal_also_keeps_the_endings_the_file_had(self):
        from agent_bridge import onboard

        crlf = onboard._toml_registration_update(
            "config.toml", "agent-bridge", COMMAND,
            text_override="[a]\r\nkey = 1\r\n").decode("utf-8")
        removed = onboard._remove_toml_registration(
            "config.toml", "agent-bridge", COMMAND, text_override=crlf)
        self.assertIsNotNone(removed)
        text = removed.decode("utf-8")
        self.assertEqual(text.count("\n"), text.count("\r\n"))

    def test_a_record_is_written_and_read_as_bytes_not_text(self):
        """Opened in binary everywhere, so no newline translation happens.

        On Windows, text mode turns every ``\\n`` into ``\\r\\n`` on the way
        out. A record written in text mode and hashed from its bytes hashes
        differently on Windows than on the machine that verified it.
        """

        for name in ("windows_privacy.py", "windows_evidence.py",
                     "windows_provision_driver.py"):
            source = (ROOT / "src" / "agent_bridge" / "orchestration"
                      / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertNotIn('open(path, "w")', source)
                self.assertNotIn(".write_text(", source.replace(
                    "# write_text", ""))

    def test_a_payload_with_crlf_survives_the_privacy_write_unchanged(self):
        directory = Path(tempfile.mkdtemp())
        target = directory / "record.json"
        payload = json.dumps({"a": 1}).encode("utf-8") + b"\r\n"
        wpriv.atomic_private_write(target, payload, secure=lambda fd: None,
                                   root=directory)
        self.assertEqual(target.read_bytes(), payload)

    def test_a_payload_with_lf_is_not_translated_either(self):
        directory = Path(tempfile.mkdtemp())
        target = directory / "record.json"
        payload = b'{"a": 1}\n'
        wpriv.atomic_private_write(target, payload, secure=lambda fd: None,
                                   root=directory)
        self.assertEqual(target.read_bytes(), payload)

    def test_the_guest_accepts_a_canary_line_with_either_ending(self):
        from agent_bridge.orchestration import windows_wsl_runtime as wwr

        # A line read back from a Windows pipe may carry a carriage return
        # that the guest never wrote.
        self.assertTrue(wwr._matches_exactly(b"ok", "ok"))
        self.assertTrue(wwr._matches_exactly(b"ok\n", "ok"))
        self.assertTrue(wwr._matches_exactly(b"ok\r\n", "ok"))
        # Tolerating the carriage return is not tolerating anything after it.
        self.assertFalse(wwr._matches_exactly(b"ok\r\nextra", "ok"))
        self.assertFalse(wwr._matches_exactly(b"ok\r", "ok"))
        self.assertFalse(wwr._matches_exactly(b"\xff\xfeo\x00k\x00", "ok"))


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


_HOLDER = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from agent_bridge.orchestration import windows_privacy as wpriv
    with wpriv.exclusive_lock(sys.argv[2], root=os.path.dirname(sys.argv[2])):
        sys.stdout.write("held\\n")
        sys.stdout.flush()
        time.sleep(float(sys.argv[3]))
""")


class ExclusiveLockTests(unittest.TestCase):
    """Both platforms have a lock. What matters is that it is one lock."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.lock = self.directory / "runtime.lock"

    def _holder(self, seconds):
        process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(ROOT / "src"),
             str(self.lock), str(seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(process.kill)
        # Wait until the child says it actually holds the lock, rather than
        # sleeping and hoping.
        # Text-mode stdout writes the platform line ending, so the child
        # says b"held\r\n" on Windows and b"held\n" elsewhere.
        self.assertEqual(process.stdout.readline().rstrip(b"\r\n"), b"held")
        return process

    def test_a_second_holder_is_refused_while_the_first_holds_it(self):
        self._holder(30)
        with self.assertRaises(wpriv.PrivacyError):
            with wpriv.exclusive_lock(self.lock, timeout=0.5,
                                      root=self.directory):
                self.fail("two processes held one exclusive lock")

    def test_the_lock_is_available_once_the_holder_exits(self):
        holder = self._holder(0.1)
        holder.wait(timeout=30)
        with wpriv.exclusive_lock(self.lock, timeout=5, root=self.directory):
            pass

    def test_a_crashed_holder_leaves_no_stale_lock(self):
        """The whole reason the lock is a handle and not a marker file.

        A process killed without cleanup releases a kernel lock. A process
        killed without cleanup does not delete a lock *file*, and the next run
        then either waits forever or deletes a lock somebody is using.
        """

        holder = self._holder(60)
        holder.kill()
        holder.wait(timeout=30)
        with wpriv.exclusive_lock(self.lock, timeout=10, root=self.directory):
            pass

    def test_the_lock_file_still_exists_afterwards(self):
        # Nothing unlinks it, so there is no window in which two processes
        # each create "the" lock file and each believe they hold it.
        with wpriv.exclusive_lock(self.lock, timeout=5, root=self.directory):
            pass
        self.assertTrue(self.lock.exists())

    def test_the_same_process_can_take_it_again_after_releasing(self):
        for _ in range(3):
            with wpriv.exclusive_lock(self.lock, timeout=5,
                                      root=self.directory):
                pass

    def test_an_unlockable_location_refuses_rather_than_proceeding(self):
        missing = self.directory / "absent" / "runtime.lock"
        with self.assertRaises(wpriv.PrivacyError):
            with wpriv.exclusive_lock(missing, timeout=0.2,
                                      root=self.directory):
                self.fail("locked a path that does not exist")

    @unittest.skipUnless(ON_WINDOWS, "msvcrt is the Windows mechanism")
    def test_windows_uses_the_byte_range_primitive(self):
        import msvcrt

        self.assertTrue(hasattr(msvcrt, "locking"))
        with wpriv.exclusive_lock(self.lock, timeout=5, root=self.directory):
            pass


if __name__ == "__main__":
    unittest.main()
