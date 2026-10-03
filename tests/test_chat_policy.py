import copy
from pathlib import Path
from unittest.mock import patch
from test_chat_storage import StorageTests
from agent_bridge import broker, config
from agent_bridge.errors import BrokerError
from agent_bridge.chat.policy import RoomPolicy, room_config


class PolicyTests(StorageTests):
    def test_room_never_relaxes_bridge_client_policy(self):
        cfg = config.load()
        cfg.raw = copy.deepcopy(cfg.raw)
        cfg.raw['state_root'] = str(Path(self.temp.name) / 'bridge')
        with patch('agent_bridge.chat.policy.verify_private_directory'):
            scoped = room_config(cfg, RoomPolicy(False))
        for candidate in (cfg, scoped):
            with self.assertRaises(BrokerError):
                broker._validate_common(candidate, {'prompt': 'fixture', 'source_classification': 'client-derived'}, broker.START_FIELDS, 'claude')
        for peer in ('claude', 'codex'):
            with self.assertRaises(ValueError):
                RoomPolicy(False).authorize(peer, 'client-derived')
        with self.assertRaises(ValueError): RoomPolicy(True)

    def test_classifications_are_not_downgraded(self):
        policy = RoomPolicy(False)
        self.assertEqual(policy.effective_classification(['internal', 'public']), 'internal')
        for label in ('client-derived', 'secret'):
            with self.assertRaises(ValueError): policy.effective_classification([label, 'public'])
        with self.assertRaises(ValueError): policy.authorize('unknown', 'public')

    def test_insecure_storage_refuses_room(self):
        with patch('agent_bridge.chat.policy.verify_private_directory', side_effect=ValueError('ACL failed')):
            with self.assertRaises(ValueError): room_config(config.load(), RoomPolicy(False))

    def test_global_classification_policy_applies_to_grok(self):
        cfg = config.load()
        cfg.raw = copy.deepcopy(cfg.raw)
        cfg.raw['allowed_source_classifications'] = ['public', 'synthetic']
        policy = RoomPolicy(cfg)
        with self.assertRaises(ValueError):
            policy.authorize('grok', 'internal')
