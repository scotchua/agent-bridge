import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_bridge.chat.adapters import BridgeAdapter


class Config:
    def __init__(self, enabled=False, minimum=5):
        self.enabled = enabled
        self.minimum = minimum

    def request_timeout(self, target):
        return {'claude': 30, 'codex': 40}[target]

    def local_first_enabled(self):
        return self.enabled

    def local_first_min_bytes(self):
        return self.minimum


class BridgeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.evidence = Path('synthetic-verification.json')

    def adapter(self, target='claude'):
        return BridgeAdapter(self.cfg, target, self.evidence)

    def test_init_rejects_unknown_target_and_maps_peer_timeout(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported bridge target'):
            self.adapter('unknown')
        claude = self.adapter('claude')
        codex = self.adapter('codex')
        self.assertIs(claude.cfg, self.cfg)
        self.assertEqual((claude.target, claude.evidence, claude.caller, claude.timeout),
                         ('claude', self.evidence, 'codex', 45))
        self.assertEqual((codex.caller, codex.timeout), ('claude', 55))

    def test_status_ready_requires_valid_evidence_and_signed_in_peer(self):
        adapter = self.adapter()
        evidence = {'synthetic': True}
        with patch('agent_bridge.chat.adapters.store.read_json', return_value=evidence) as read, \
                patch('agent_bridge.chat.adapters.setup_cmd.validate_promotion') as validate, \
                patch('agent_bridge.chat.adapters.health.inspect_peer',
                      return_value={'auth': {'state': 'signed_in'}}) as inspect:
            self.assertEqual(adapter.status(), {'state': 'ready'})
        read.assert_called_once_with(str(self.evidence))
        validate.assert_called_once_with(self.cfg, evidence)
        inspect.assert_called_once_with(self.cfg, 'claude')

    def test_status_invalid_evidence_requires_verification(self):
        adapter = self.adapter()
        for error in (OSError, ValueError, KeyError, TypeError):
            with self.subTest(error=error.__name__), \
                    patch('agent_bridge.chat.adapters.store.read_json', side_effect=error), \
                    patch('agent_bridge.chat.adapters.health.inspect_peer') as inspect:
                result = adapter.status()
                self.assertEqual(result['state'], 'verification_required')
                self.assertIn('full live verification', result['detail'])
                inspect.assert_not_called()

    def test_status_unsigned_peer_requires_login(self):
        adapter = self.adapter()
        for report in ({'auth': {'state': 'signed_out'}}, {}, {'auth': {}}):
            with self.subTest(report=report), \
                    patch('agent_bridge.chat.adapters.store.read_json', return_value={}), \
                    patch('agent_bridge.chat.adapters.setup_cmd.validate_promotion'), \
                    patch('agent_bridge.chat.adapters.health.inspect_peer', return_value=report):
                result = adapter.status()
                self.assertEqual(result['state'], 'login_required')
                self.assertIn('Sign in', result['detail'])

    def test_status_preflight_failure_is_failed(self):
        adapter = self.adapter()
        with patch('agent_bridge.chat.adapters.store.read_json', return_value={}), \
                patch('agent_bridge.chat.adapters.setup_cmd.validate_promotion'), \
                patch('agent_bridge.chat.adapters.health.inspect_peer', side_effect=RuntimeError('synthetic failure')):
            result = adapter.status()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('preflight failed', result['detail'])

    def test_admit_refuses_nonready_status_with_detail(self):
        adapter = self.adapter()
        with patch.object(adapter, 'status', return_value={'state': 'ready'}):
            self.assertIsNone(adapter._admit())
        with patch.object(adapter, 'status', return_value={'state': 'login_required', 'detail': 'Synthetic sign in required'}):
            with self.assertRaisesRegex(ValueError, 'Synthetic sign in required'):
                adapter._admit()

    def test_check_local_first_uses_enabled_byte_threshold(self):
        adapter = self.adapter()
        self.assertIsNone(adapter._check_local_first('sixteen byte text'))
        self.cfg.enabled = True
        self.assertIsNone(adapter._check_local_first('four'))
        with self.assertRaisesRegex(ValueError, 'local_first limit'):
            adapter._check_local_first('12345')
        with self.assertRaisesRegex(ValueError, 'local_first limit'):
            adapter._check_local_first('ééé')

    def test_start_checks_admission_and_forwards_payload(self):
        adapter = self.adapter()
        expected = {'job_id': 'synthetic-job'}
        with patch.object(adapter, 'status', return_value={'state': 'login_required', 'detail': 'Synthetic sign in required'}), \
                patch('agent_bridge.chat.adapters.broker.start') as start:
            with self.assertRaisesRegex(ValueError, 'Synthetic sign in required'):
                adapter.start('Aster Works summary', 'synthetic')
            start.assert_not_called()
        self.cfg.enabled = True
        with patch.object(adapter, 'status', return_value={'state': 'ready'}), \
                patch('agent_bridge.chat.adapters.broker.start') as start:
            with self.assertRaises(ValueError):
                adapter.start('12345', 'synthetic')
            start.assert_not_called()
            self.cfg.enabled = False
            start.return_value = expected
            self.assertIs(adapter.start('Aster Works summary', 'synthetic'), expected)
            start.assert_called_once_with(self.cfg, 'codex',
                                          {'prompt': 'Aster Works summary', 'source_classification': 'synthetic'})

    def test_continue_checks_admission_and_forwards_conversation(self):
        adapter = self.adapter('codex')
        expected = {'job_id': 'synthetic-followup'}
        with patch.object(adapter, 'status', return_value={'state': 'verification_required', 'detail': 'Synthetic verification required'}), \
                patch('agent_bridge.chat.adapters.broker.continue_') as continue_:
            with self.assertRaisesRegex(ValueError, 'Synthetic verification required'):
                adapter.continue_('synthetic-conversation', 'Aster Works follow up', 'synthetic')
            continue_.assert_not_called()
        self.cfg.enabled = True
        with patch.object(adapter, 'status', return_value={'state': 'ready'}), \
                patch('agent_bridge.chat.adapters.broker.continue_') as continue_:
            with self.assertRaises(ValueError):
                adapter.continue_('synthetic-conversation', '12345', 'synthetic')
            continue_.assert_not_called()
            self.cfg.enabled = False
            continue_.return_value = expected
            self.assertIs(adapter.continue_('synthetic-conversation', 'Aster Works follow up', 'synthetic'), expected)
            continue_.assert_called_once_with(self.cfg, 'claude',
                                              {'conversation_id': 'synthetic-conversation',
                                               'prompt': 'Aster Works follow up', 'source_classification': 'synthetic'})

    def test_poll_forwards_job_and_result(self):
        adapter = self.adapter()
        expected = {'status': 'complete'}
        with patch('agent_bridge.chat.adapters.broker.poll', return_value=expected) as poll:
            self.assertIs(adapter.poll('synthetic-job'), expected)
        poll.assert_called_once_with(self.cfg, 'codex', {'job_id': 'synthetic-job'})

    def test_read_forwards_job_and_result(self):
        adapter = self.adapter('codex')
        expected = {'peer_response': 'Synthetic response'}
        with patch('agent_bridge.chat.adapters.broker.read', return_value=expected) as read:
            self.assertIs(adapter.read('synthetic-job'), expected)
        read.assert_called_once_with(self.cfg, 'claude', {'job_id': 'synthetic-job'})


if __name__ == '__main__':
    unittest.main()
