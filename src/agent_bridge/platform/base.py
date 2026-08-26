"""Platform interface for filesystem and process primitives.

Names describe the guarantees agent-bridge needs rather than the mechanism a
platform uses to provide them. Implementations must preserve the documented
return values and error handling expected by the callers.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import Any, Iterator, NamedTuple, Protocol


class StreamReadResult(NamedTuple):
    """Result of a capped read, named rather than positional.

    This deliberately is not a bare tuple. The three flags decide how a run is
    classified, and `timed_out` versus `cap_exceeded` is precisely the
    distinction that was got wrong once before: a cap breach is why the bridge
    kills a peer, so reporting it as the peer exiting nonzero made a flooding
    peer look transient and get retried.

    A second implementation returning these in a different order would pass
    every structural test while silently inverting that classification. Naming
    them makes such a mistake a TypeError at the boundary instead.
    """

    stdout: bytes
    stderr: bytes
    timed_out: bool
    cap_exceeded: bool
    descendant_held_pipes: bool


class Platform(Protocol):
    """OS services required by the bridge."""

    supports_owner_only_permissions: bool

    def set_owner_only_umask(self) -> None: ...

    def enforce_owner_only_file(self, fd: int) -> None: ...

    def verify_owner_only_path(self, directory: str,
                               probe_file: str) -> tuple[bool, dict[str, Any]]:
        """Confirm this filesystem actually keeps the state private.

        The guarantee is "state is readable only by its owner, and that was
        confirmed by reading it back". How you confirm it is platform business:
        POSIX reads mode bits, Windows reads the ACL. Comparing st_mode against
        0o700 works on one and is meaningless on the other, where st_mode
        carries only a read-only bit, so a correct Windows ACL would still have
        failed a POSIX-shaped assertion.

        Returns (verified, report). The report is diagnostic detail for an
        operator and its keys are platform-specific by design.
        """
        ...

    @contextlib.contextmanager
    def lock_exclusive(self, fd: int, lock_path: str,
                       timeout: float) -> Iterator[None]: ...

    def spawn_isolated(self, argv: list[str], *, cwd: str,
                       env: dict[str, str]) -> subprocess.Popen[bytes]: ...

    def isolated_process_group(self, pid: int) -> int | None: ...

    def terminate_process_tree(self, group_id: int,
                               grace: float) -> dict[str, Any]: ...

    def process_alive(self, pid: int) -> bool:
        """Whether one process is still running.

        Deliberately part of the interface. `os.kill(pid, 0)` reads as a
        harmless POSIX liveness probe and is not portable at all: on Windows
        signal 0 IS CTRL_C_EVENT, so the same line sends a console interrupt to
        the target's console rather than asking a question about it.
        """
        ...

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
    ) -> StreamReadResult: ...
