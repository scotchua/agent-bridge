"""Focused offline checks for the Codex backend's decision points."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from harness import Sandbox
from agent_bridge import runner
from agent_bridge.backends import codex_backend
from agent_bridge.errors import BrokerError, ErrorCategory


class CodexBackendMutationTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def outcome(self, events=(), *, thread_id=None, message=b'{"summary":"Aster Works"}',
                stdout=None, code=0, **flags):
        with tempfile.TemporaryDirectory() as attempt:
            if message is not None:
                with open(os.path.join(attempt, 'last_message.txt'), 'wb') as handle:
                    handle.write(message)
            if stdout is None:
                stdout = b'\n'.join(json.dumps(event).encode() for event in events)
            result = runner.RunResult(argv=[], returncode=code, stdout=stdout, stderr=b'',
                                      duration_seconds=0, pgid=None,
                                      **({'timed_out': False} | flags))
            with patch.object(runner, 'run', return_value=result) as call:
                outcome = codex_backend.run_consultation(
                    self.sb.cfg, prompt='Aster Works account review', schema_file='/synthetic/schema',
                    thread_id=thread_id, workspace=self.sb.root, attempt_dir=attempt)
            return outcome, call

    def test_executable_uses_pin_then_discovery_then_name(self):
        self.sb.cfg.raw['peers']['codex']['executable'] = '/synthetic/codex'
        with patch.object(codex_backend.preflight, 'discover_executable', return_value='/discovered/codex') as discover:
            self.assertEqual(codex_backend._executable(self.sb.cfg), '/synthetic/codex')
            discover.assert_not_called()
            self.sb.cfg.raw['peers']['codex']['executable'] = ''
            self.assertEqual(codex_backend._executable(self.sb.cfg), '/discovered/codex')
        with patch.object(codex_backend.preflight, 'discover_executable', return_value=None):
            self.assertEqual(codex_backend._executable(self.sb.cfg), 'codex')

    def test_build_argv_start_and_resume_are_distinct(self):
        self.sb.cfg.raw['peers']['codex'].update({
            'executable': '/synthetic/codex', 'model': 'synthetic-model',
            'sandbox_mode': 'read-only', 'reasoning_effort': 'high'})
        kwargs = dict(schema_file='/synthetic/schema', last_message_file='/synthetic/message',
                      workspace='/synthetic/workspace')
        start = codex_backend.build_argv(self.sb.cfg, thread_id=None, **kwargs)
        resume = codex_backend.build_argv(self.sb.cfg, thread_id='thread-aster', **kwargs)
        self.assertEqual(start[:2], ['/synthetic/codex', 'exec'])
        self.assertEqual(resume[:5], ['/synthetic/codex', 'exec', 'resume', 'thread-aster', '-'])
        for argv in (start, resume):
            self.assertIn('--json', argv)
            self.assertIn('--ignore-user-config', argv)
            self.assertIn('--ignore-rules', argv)
            self.assertIn('--strict-config', argv)
            self.assertEqual(argv[argv.index('--output-schema') + 1], '/synthetic/schema')
            self.assertEqual(argv[argv.index('--output-last-message') + 1], '/synthetic/message')
            self.assertIn('sandbox_mode="read-only"', argv)
            self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(start[start.index('-C') + 1], '/synthetic/workspace')
        self.assertEqual(start[start.index('-s') + 1], 'read-only')
        self.assertEqual(start[start.index('-m') + 1], 'synthetic-model')
        for option in ('-C', '-s', '-m'):
            self.assertNotIn(option, resume)

    def test_parse_events_ignores_noise_and_tracks_distinct_threads(self):
        stream = (b'noise\n{}\n{broken\n{"type":"thread.started","thread_id":"aster-1"}\n'
                  b'{"type":"thread.started","thread_id":"aster-1"}\n'
                  b'{"type":"thread.started","thread_id":"birch-2"}\n'
                  b'{"type":"error","message":"first error"}\n'
                  b'{"type":"turn.failed","error":{"message":"second error"}}\n'
                  b'{"type":"turn.completed"}\n')
        thread, errors, events, ids = codex_backend.parse_events(stream)
        self.assertEqual(thread, 'aster-1')
        self.assertEqual(ids, ['aster-1', 'birch-2'])
        self.assertEqual(errors, ['first error', 'second error'])
        self.assertEqual(len(events), 7)

    def test_parse_events_rejects_empty_and_nonstring_ids(self):
        stream = (b'{"type":"thread.started","thread_id":""}\n'
                  b'{"type":"thread.started","thread_id":42}\n'
                  b'{"type":"error","message":42}\n'
                  b'{"type":"thread.started","thread_id":"cedar-3"}')
        thread, errors, _, ids = codex_backend.parse_events(stream)
        self.assertEqual((thread, errors, ids), ('cedar-3', [], ['cedar-3']))

    def test_parse_events_accepts_indented_json(self):
        thread, _, events, ids = codex_backend.parse_events(
            b'  {"type":"thread.started","thread_id":"aster-1"}  \n')
        self.assertEqual((thread, ids, len(events)), ('aster-1', ['aster-1'], 1))

    def test_parse_events_uses_top_level_error_message(self):
        _, errors, _, _ = codex_backend.parse_events(
            b'{"type":"error","error":{"message":"nested"},"message":"top-level"}')
        self.assertEqual(errors, ['top-level'])

    def test_run_consultation_passes_isolated_execution_inputs(self):
        out, call = self.outcome([{'type': 'thread.started', 'thread_id': 'aster-1'}])
        self.assertEqual(out.category, ErrorCategory.OK)
        self.assertEqual(out.peer_session_id, 'aster-1')
        self.assertEqual(out.payload, {'summary': 'Aster Works'})
        self.assertEqual(out.extraction_path, 'output_last_message:json')
        self.assertEqual(out.raw_last_message, b'{"summary":"Aster Works"}')
        self.assertEqual(call.call_args.kwargs['cwd'], self.sb.root)
        self.assertEqual(call.call_args.kwargs['stdin_data'], 'Aster Works account review')
        self.assertEqual(call.call_args.kwargs['env']['CODEX_HOME'],
                         self.sb.cfg.peer('codex')['codex_home'])
        self.assertEqual(call.call_args.kwargs['pgid_file'].split(os.sep)[-1], 'peer.pgid')
        self.assertEqual(call.call_args.kwargs['stdout_cap'],
                         self.sb.cfg.limit('peer_stdout_max_bytes'))

    def test_run_consultation_creates_isolated_home(self):
        home = self.sb.cfg.peer('codex')['codex_home']
        self.assertFalse(os.path.exists(home))
        self.outcome([{'type': 'thread.started', 'thread_id': 'aster-1'}])
        self.assertTrue(os.path.isdir(home))

    def test_run_consultation_rejects_home_config(self):
        home = self.sb.cfg.peer('codex')['codex_home']
        os.makedirs(home)
        with open(os.path.join(home, 'config.toml'), 'w', encoding='utf-8') as handle:
            handle.write('[mcp_servers.synthetic]\ncommand="synthetic"\n')
        with self.assertRaises(BrokerError) as raised:
            self.outcome([{'type': 'thread.started', 'thread_id': 'aster-1'}])
        self.assertEqual(raised.exception.category, ErrorCategory.PEER_HOME_CONFIG_PRESENT)

    def test_run_consultation_honours_requested_thread_and_rejects_migration(self):
        out, _ = self.outcome([{'type': 'thread.started', 'thread_id': 'other-2'}],
                              thread_id='aster-1')
        self.assertEqual(out.category, ErrorCategory.PEER_SESSION_MIGRATED)
        self.assertEqual(out.peer_session_id, 'aster-1')
        self.assertTrue(out.notes['thread_migrated'])
        self.assertFalse(out.notes['thread_id_honoured'])
        out, _ = self.outcome([{'type': 'thread.started', 'thread_id': 'aster-1'},
                               {'type': 'thread.started', 'thread_id': 'other-2'}])
        self.assertEqual(out.category, ErrorCategory.PEER_SESSION_MIGRATED)
        self.assertTrue(out.notes['thread_id_ambiguous'])

    def test_run_consultation_failure_precedence(self):
        events = [{'type': 'thread.started', 'thread_id': 'aster-1'}]
        cases = [
            ({'spawn_failed': True, 'timed_out': True}, ErrorCategory.PEER_SPAWN_FAILURE),
            ({'timed_out': True, 'cap_exceeded': True}, ErrorCategory.PEER_TIMEOUT),
            ({'cap_exceeded': True, 'descendant_held_pipes': True}, ErrorCategory.PEER_OUTPUT_TOO_LARGE),
            ({'descendant_held_pipes': True}, ErrorCategory.PEER_OUTPUT_INCOMPLETE),
        ]
        for flags, category in cases:
            with self.subTest(flags=flags):
                out, _ = self.outcome(events, code=2, **flags)
                self.assertEqual(out.category, category)

    def test_run_consultation_output_cap_boundary(self):
        self.sb.cfg.raw['limits']['peer_stdout_max_bytes'] = 128
        cap = self.sb.cfg.limit('peer_stdout_max_bytes')
        stream = b' ' * cap
        out, _ = self.outcome(stdout=stream)
        self.assertEqual(out.category, ErrorCategory.PEER_OUTPUT_TOO_LARGE)

    def test_run_consultation_failed_turn_and_auth_reason(self):
        events = [{'type': 'thread.started', 'thread_id': 'aster-1'},
                  {'type': 'turn.failed', 'error': {'message': '401 Unauthorized'}}]
        out, _ = self.outcome(events)
        self.assertEqual(out.category, ErrorCategory.PEER_AUTH_FAILURE)
        self.assertEqual(out.notes['auth_failure_reason'], 'http_401')
        self.assertEqual(out.notes['auth_failure_source'], 'failed_process_diagnostics')
        out, _ = self.outcome(events[:1], code=2)
        self.assertEqual(out.category, ErrorCategory.PEER_NONZERO_EXIT)

    def test_run_consultation_missing_or_bad_last_message(self):
        events = [{'type': 'thread.started', 'thread_id': 'aster-1'}]
        for message in (None, b'', b'prose about Aster Works'):
            with self.subTest(message=message):
                out, _ = self.outcome(events, message=message)
                self.assertEqual(out.category, ErrorCategory.PEER_OUTPUT_MALFORMED)
        out, _ = self.outcome([], message=b'{"summary":"Aster Works"}')
        self.assertEqual(out.category, ErrorCategory.PEER_SESSION_ID_MISSING)
        self.assertEqual(out.payload, {'summary': 'Aster Works'})

    def test_run_consultation_last_message_size_boundary(self):
        valid = b'{"summary":"Aster Works"}'
        self.sb.cfg.raw['limits']['peer_last_message_max_bytes'] = len(valid)
        cap = self.sb.cfg.limit('peer_last_message_max_bytes')
        out, _ = self.outcome([{'type': 'thread.started', 'thread_id': 'aster-1'}],
                              message=valid)
        self.assertEqual(out.category, ErrorCategory.OK)
        out, _ = self.outcome([{'type': 'thread.started', 'thread_id': 'aster-1'}],
                              message=b' ' * (cap + 1))
        self.assertEqual(out.category, ErrorCategory.PEER_OUTPUT_TOO_LARGE)
