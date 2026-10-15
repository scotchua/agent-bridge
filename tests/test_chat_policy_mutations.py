import unittest

from agent_bridge.chat.policy import RoomPolicy
from agent_bridge.chat.storage import LABELS


class PolicyConfig:
    def __init__(self, routes=()):
        self.client_derived_routes = frozenset(routes)
        self.allowed_classifications = ('public', 'synthetic')

    def peer(self, target):
        if target not in ('claude', 'codex'):
            raise ValueError('Unrecognized peer entry')
        return {}

    def peer_allowed_classifications(self, target):
        return ('public', 'synthetic', 'client_derived')


class AllowedTests(unittest.TestCase):
    def test_unconfigured_policy_uses_all_room_labels(self):
        self.assertEqual(RoomPolicy(False)._allowed('claude'), LABELS)

    def test_global_policy_applies_to_hermes_and_grok(self):
        policy = RoomPolicy(PolicyConfig())
        for target in ('hermes', 'grok'):
            with self.subTest(target=target):
                self.assertEqual(policy._allowed(target), ('public', 'synthetic'))


class EffectiveClassificationTests(unittest.TestCase):
    def test_opted_in_client_derived_label_stays_client_derived(self):
        policy = RoomPolicy(PolicyConfig(('peer',)))
        self.assertEqual(policy.effective_classification(['public', 'client-derived']),
                         'client-derived')


class AuthorizeTests(unittest.TestCase):
    def test_configured_policy_rejects_unknown_peer(self):
        policy = RoomPolicy(PolicyConfig())
        with self.assertRaisesRegex(ValueError, '^Unknown peer$'):
            policy.authorize('invented-peer', 'public')

    def test_public_does_not_need_client_opt_in(self):
        RoomPolicy(False).authorize('claude', 'public')


if __name__ == '__main__':
    unittest.main()
