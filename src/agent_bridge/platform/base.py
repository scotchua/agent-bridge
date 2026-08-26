"""Platform interface for filesystem and process primitives.

Names describe the guarantees agent-bridge needs rather than the mechanism a
platform uses to provide them. Implementations must preserve the documented
return values and error handling expected by the callers.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import Any, Iterator, Protocol


class Platform(Protocol):
    """OS services required by the bridge."""

    supports_owner_only_permissions: bool

    def set_owner_only_umask(self) -> None: ...

    def enforce_owner_only_file(self, fd: int) -> None: ...

    @contextlib.contextmanager
    def lock_exclusive(self, fd: int, lock_path: str,
                       timeout: float) -> Iterator[None]: ...

    def spawn_isolated(self, argv: list[str], *, cwd: str,
                       env: dict[str, str]) -> subprocess.Popen[bytes]: ...

    def isolated_process_group(self, pid: int) -> int | None: ...

    def terminate_process_tree(self, group_id: int,
                               grace: float) -> dict[str, Any]: ...

    def process_tree_alive(self, group_id: int) -> bool: ...

    def process_identity(self, pid: int) -> str: ...

    def process_group_identity(self, pid: int) -> tuple[str, str] | None: ...

    def is_own_process_group(self, group_id: int) -> bool: ...

    def read_streams_with_caps(
        self,
        proc: subprocess.Popen[bytes],
        stdin_data: str,
        timeout: float,
        stdout_cap: int,
        stderr_cap: int,
        post_exit_drain_seconds: float,
    ) -> tuple[bytes, bytes, bool, bool, bool]: ...
