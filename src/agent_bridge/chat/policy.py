"""Room policy preserves the bridge's client-data refusal."""
from pathlib import Path
from ..config import Config
from .storage import LABELS
from .windows_security import verify_private_directory


class RoomPolicy:
    def __init__(self, cfg=None, allow_client: bool = False):
        # Keep the old RoomPolicy(False) test/fixture shape while allowing the
        # running room to enforce the bridge's per-peer classification policy.
        if isinstance(cfg, bool) and allow_client is False:
            allow_client, cfg = cfg, None
        if allow_client:
            raise ValueError('Client-derived material is not supported')
        self.cfg = cfg
        self.allow_client = False

    def _allowed(self, target: str) -> tuple[str, ...]:
        if self.cfg is None:
            return LABELS
        if target in ('hermes', 'grok'):
            return self.cfg.allowed_classifications
        return tuple(self.cfg.peer_allowed_classifications(target))

    def effective_classification(self, labels: list[str]) -> str:
        if any(label not in LABELS for label in labels):
            raise ValueError('Client material, credentials and secrets cannot be shared')
        return next((label for label in ('internal', 'synthetic') if label in labels), 'public')

    def authorize(self, target: str, classification: str) -> None:
        if self.cfg is not None:
            if target not in ('hermes', 'grok'):
                try:
                    self.cfg.peer(target)
                except (KeyError, ValueError):
                    raise ValueError('Unknown peer') from None
        elif target not in ('claude', 'codex', 'hermes', 'grok'):
            raise ValueError('Unknown peer')
        if classification not in LABELS or classification not in self._allowed(target):
            raise ValueError('Room policy refuses this classification')


def room_config(cfg: Config, policy: RoomPolicy) -> Config:
    verify_private_directory(Path(cfg.state_root))
    return cfg
