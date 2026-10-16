"""Runtime contract checks for the platform interface."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys
import unittest
from typing import Protocol, get_type_hints


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge.platform.base import Platform, StreamReadResult  # noqa: E402


class StreamReadResultContractTests(unittest.TestCase):
    def test_timed_out_is_boolean(self) -> None:
        self.assertIs(get_type_hints(StreamReadResult)["timed_out"], bool)

    def test_cap_exceeded_is_boolean(self) -> None:
        self.assertIs(get_type_hints(StreamReadResult)["cap_exceeded"], bool)

    def test_descendant_held_pipes_is_boolean(self) -> None:
        self.assertIs(get_type_hints(StreamReadResult)["descendant_held_pipes"], bool)


class PlatformContractTests(unittest.TestCase):
    def assert_method(self, name: str, parameters: tuple[str, ...],
                      result: str) -> None:
        self.assertIn(name, Platform.__dict__)
        method = Platform.__dict__[name]
        self.assertEqual(tuple(inspect.signature(method).parameters), parameters)
        self.assertEqual(method.__annotations__["return"], result)

    def test_platform_is_a_protocol(self) -> None:
        self.assertIn(Protocol, Platform.__mro__)

    def test_name_type(self) -> None:
        self.assertIs(get_type_hints(Platform)["name"], str)

    def test_owner_only_support_type(self) -> None:
        self.assertIs(get_type_hints(Platform)["supports_owner_only_permissions"], bool)

    def test_set_owner_only_umask(self) -> None:
        self.assert_method("set_owner_only_umask", ("self",), "None")

    def test_enforce_owner_only_file(self) -> None:
        self.assert_method("enforce_owner_only_file", ("self", "fd"), "None")

    def test_verify_owner_only_path(self) -> None:
        self.assert_method("verify_owner_only_path",
                           ("self", "directory", "probe_file"),
                           "tuple[bool, dict[str, Any]]")

    def test_lock_exclusive(self) -> None:
        self.assert_method("lock_exclusive",
                           ("self", "fd", "lock_path", "timeout"),
                           "Iterator[None]")

    def test_lock_exclusive_is_context_manager(self) -> None:
        self.assertIn("__wrapped__", Platform.lock_exclusive.__dict__)

    def test_spawn_isolated(self) -> None:
        self.assert_method("spawn_isolated", ("self", "argv", "cwd", "env"),
                           "subprocess.Popen[bytes]")
        self.assertEqual(inspect.signature(Platform.spawn_isolated).parameters["cwd"].kind,
                         inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(inspect.signature(Platform.spawn_isolated).parameters["env"].kind,
                         inspect.Parameter.KEYWORD_ONLY)

    def test_isolated_process_group(self) -> None:
        self.assert_method("isolated_process_group", ("self", "pid"), "int | None")

    def test_terminate_process_tree(self) -> None:
        self.assert_method("terminate_process_tree", ("self", "group_id", "grace"),
                           "dict[str, Any]")

    def test_process_liveness(self) -> None:
        self.assert_method("process_liveness", ("self", "pid"), "dict[str, Any]")

    def test_process_alive(self) -> None:
        self.assert_method("process_alive", ("self", "pid"), "bool")

    def test_process_tree_alive(self) -> None:
        self.assert_method("process_tree_alive", ("self", "group_id"), "bool")

    def test_process_group_members(self) -> None:
        self.assert_method("process_group_members", ("self", "group_id"),
                           "list[str] | None")

    def test_process_identity(self) -> None:
        self.assert_method("process_identity", ("self", "pid"), "str")

    def test_process_group_identity(self) -> None:
        self.assert_method("process_group_identity", ("self", "pid"),
                           "tuple[str, str] | None")

    def test_is_own_process_group(self) -> None:
        self.assert_method("is_own_process_group", ("self", "group_id"), "bool")

    def test_read_streams_with_caps(self) -> None:
        self.assert_method("read_streams_with_caps",
                           ("self", "proc", "stdin_data", "timeout", "stdout_cap",
                            "stderr_cap", "post_exit_drain_seconds"),
                           "StreamReadResult")


if __name__ == "__main__":
    unittest.main()
