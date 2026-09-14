"""Synthetic regressions for status, classification, and recovery diagnostics."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from harness import Sandbox
from agent_bridge import health, runner, setup_cmd
from agent_bridge.backends import claude_backend, codex_backend
from agent_bridge.backends.base import auth_failure_reason
from agent_bridge.errors import ErrorCategory


def result(stdout=b'', stderr=b'', code=0, **kw):
    return runner.RunResult(argv=[], returncode=code, stdout=stdout, stderr=stderr,
                            timed_out=False, duration_seconds=0, pgid=None, **kw)


class HealthTests(unittest.TestCase):
    def test_allowlisted_status_only(self):
        data = {'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'team',
                'email': 'synthetic-secret', 'accessToken': 'synthetic-secret'}
        with patch.object(runner, 'run', return_value=result(json.dumps(data).encode())) as call:
            report = health.auth_status('claude', '/fake', {'HOME': '/synthetic'})
        self.assertEqual(report['state'], 'signed_in')
        self.assertFalse(report['network_verified'])
        self.assertNotIn('synthetic-secret', json.dumps(report))
        self.assertEqual(call.call_args.args[0],
                         ['/fake', '--safe-mode', '--setting-sources', '', 'auth', 'status'])
        self.assertEqual(call.call_args.kwargs['stdin_data'], '')
        self.assertFalse(os.path.exists(call.call_args.kwargs['cwd']))

    def test_unknown_is_not_false_or_true(self):
        for payload in [b'{}', b'[]', b'no json', b'{"loggedIn":"false"}', b'{"loggedIn":true}']:
            with self.subTest(payload=payload), patch.object(runner, 'run', return_value=result(payload, code=2)):
                self.assertEqual(health.auth_status('claude', '/fake', {})['state'], 'unknown')
        with patch.object(runner, 'run', return_value=result(b'{"loggedIn":false}', code=1)):
            self.assertEqual(health.auth_status('claude', '/fake', {})['state'], 'not_signed_in')

    def test_probe_failure_and_large_output_stay_unknown(self):
        for flag in ['spawn_failed', 'cap_exceeded', 'descendant_held_pipes']:
            with patch.object(runner, 'run', return_value=result(b'{"loggedIn":true}', **{flag:True})):
                self.assertEqual(health.auth_status('claude', '/fake', {})['state'], 'unknown')
        r = result(); r.timed_out = True
        with patch.object(runner, 'run', return_value=r):
            self.assertEqual(health.auth_status('codex', '/fake', {})['probe_error'], 'timeout')

    def test_codex_file_is_not_authentication(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'auth.json'), 'w') as f:
                f.write('{"synthetic":"stale"}')
            with patch.object(runner, 'run', return_value=result(stderr=b'Not logged in', code=1)) as call:
                self.assertFalse(setup_cmd.codex_signed_in('/fake', home))
            self.assertEqual(call.call_args.kwargs['env']['CODEX_HOME'], home)

    def test_codex_unknown_and_secret_status(self):
        with patch.object(runner, 'run', return_value=result(stderr=b'Logged in using an API key - synthetic-secret')):
            report = health.auth_status('codex', '/fake', {})
            self.assertEqual(report['state'], 'signed_in')
            self.assertNotIn('synthetic-secret', json.dumps(report))
        with patch.object(runner, 'run', return_value=result(stderr=b'unexpected')):
            self.assertEqual(health.auth_status('codex', '/fake', {})['state'], 'unknown')

    def test_environment_scrubbing_and_store_context(self):
        sb = Sandbox()
        try:
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'secret', 'CLAUDE_CODE_OAUTH_TOKEN':'secret',
                                        'CLAUDE_CONFIG_DIR':'/wrong', 'CODEX_HOME':'/wrong'}):
                env = health.peer_env(sb.cfg, 'codex')
                self.assertEqual(env['CODEX_HOME'], sb.cfg.peer('codex')['codex_home'])
                for key in ['ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','CLAUDE_CONFIG_DIR']:
                    self.assertNotIn(key, env)
                self.assertNotIn('secret', json.dumps(runner.credential_context(env)))
        finally:
            sb.cleanup()

    def test_version_mismatch_does_not_probe_auth_or_change_pin(self):
        sb = Sandbox()
        try:
            sb.cfg.raw['peers']['claude']['allowed_versions'] = ['unratified']
            before = json.dumps(sb.cfg.raw, sort_keys=True)
            with patch.object(health, 'auth_status') as auth, patch.object(health.preflight, 'discover_executable', return_value=None):
                report = health.inspect_peer(sb.cfg, 'claude')
                auth.assert_not_called()
            self.assertEqual(report['preflight'], 'preflight_version_mismatch')
            self.assertEqual(before, json.dumps(sb.cfg.raw, sort_keys=True))
        finally:
            sb.cleanup()

    def test_contaminated_codex_home_does_not_probe(self):
        sb = Sandbox()
        try:
            home = sb.cfg.peer('codex')['codex_home'];os.makedirs(home)
            with open(os.path.join(home,'config.toml'),'w') as f:f.write('[mcp_servers.recursion]\ncommand="fake"\n')
            with patch.object(health, 'auth_status') as auth, patch.object(health.preflight, 'discover_executable', return_value=None):
                report = health.inspect_peer(sb.cfg, 'codex');auth.assert_not_called()
            self.assertEqual(report['preflight'], 'peer_home_config_present')
        finally:sb.cleanup()


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def claude(self, stdout, code=0, stderr=b''):
        with patch.object(runner, 'run', return_value=result(stdout,stderr,code)):
            return claude_backend.run_consultation(self.sb.cfg, prompt='synthetic', schema={},
                      session_id='session', resume=False, workspace=self.sb.root)

    def codex(self, events, code=0, stderr=b''):
        with tempfile.TemporaryDirectory() as attempt:
            with open(os.path.join(attempt,'last_message.txt'),'w') as f:json.dump({'summary':'synthetic'},f)
            stream = '\n'.join(json.dumps(e) for e in [{'type':'thread.started','thread_id':'session'},*events]).encode()
            with patch.object(runner,'run',return_value=result(stream,stderr,code)):
                return codex_backend.run_consultation(self.sb.cfg,prompt='synthetic',schema_file='/synthetic',
                       thread_id=None,workspace=self.sb.root,attempt_dir=attempt)

    def test_actual_expired_refresh_envelope(self):
        out = self.claude(json.dumps({'is_error':True,'subtype':'success','session_id':'session',
               'result':'Failed to authenticate: OAuth session expired and could not be refreshed'}).encode(),1)
        self.assertEqual(out.category,ErrorCategory.PEER_AUTH_FAILURE)
        self.assertEqual(out.notes['auth_failure_reason'],'oauth_refresh_failed')
        self.assertIn('credential_context',out.notes)

    def test_success_and_malformed_discussion_not_auth_failure(self):
        out=self.claude(json.dumps({'is_error':False,'session_id':'session',
              'result':{'summary':'Run codex login after 401 Unauthorized'}}).encode())
        self.assertEqual(out.category,ErrorCategory.OK)
        out=self.claude(b'Discussion: not logged in, use codex login')
        self.assertEqual(out.category,ErrorCategory.PEER_OUTPUT_MALFORMED)
        out=self.codex([{'type':'turn.completed'}],stderr=b'401 Unauthorized earlier; recovered using codex login')
        self.assertEqual(out.category,ErrorCategory.OK)

    def test_network_and_permission_errors_are_not_auth(self):
        for text in ['Unauthorized file operation', 'codex login helper: DNS resolution failed',
                     '403 Forbidden: model is not available', 'connection timeout']:
            with self.subTest(text=text):
                self.assertIsNone(auth_failure_reason(text))
                self.assertEqual(self.codex([],1,text.encode()).category,ErrorCategory.PEER_NONZERO_EXIT)

    def test_codex_failed_turn_nested_message_and_recovered_events(self):
        out=self.codex([{'type':'turn.failed','error':{'message':'401 Unauthorized'}}])
        self.assertEqual(out.category,ErrorCategory.PEER_AUTH_FAILURE)
        self.assertEqual(out.notes['auth_failure_reason'],'http_401')
        out=self.codex([{'type':'error','message':'401 Unauthorized'}, {'type':'turn.completed'}])
        self.assertEqual(out.category,ErrorCategory.OK)

    def test_nonzero_claude_cannot_be_success(self):
        out=self.claude(json.dumps({'is_error':False,'session_id':'session','result':{'summary':'synthetic'}}).encode(),2)
        self.assertEqual(out.category,ErrorCategory.PEER_NONZERO_EXIT)

    def test_structured_output_exhaustion_needs_error_envelope(self):
        for field, value in [('subtype', 'error_max_structured_output_retries'),
                             ('terminal_reason', 'structured_output_retry_exhausted')]:
            with self.subTest(field=field):
                envelope = {'is_error': True, 'session_id': 'session', field: value,
                            'errors': ['Failed to provide valid structured output after 5 attempts']}
                out = self.claude(json.dumps(envelope).encode(), 1)
                self.assertEqual(out.category, ErrorCategory.PEER_STRUCTURED_OUTPUT_EXHAUSTED)
                envelope.update(is_error=False, result={'summary': 'synthetic'})
                out = self.claude(json.dumps(envelope).encode())
                self.assertEqual(out.category, ErrorCategory.OK)

    def test_structured_output_exhaustion_not_retried_and_quarantined(self):
        self.sb.env(FAKE_CLAUDE_MODE='structured_output_exhausted')
        started, status, response = self.sb.run_to_completion('codex')
        self.assertEqual(status['error_category'], 'peer_output_schema_invalid')
        self.assertEqual(status['diagnostic_category'], 'peer_structured_output_exhausted')
        self.assertEqual(response['error_category'], 'peer_structured_output_exhausted')
        from agent_bridge import broker
        polled = broker.poll(self.sb.cfg, 'codex', {'job_id': started['job_id']})
        self.assertEqual(polled['error_category'], 'peer_structured_output_exhausted')
        prov = self.sb.provenance(started['job_id'])
        self.assertEqual(prov['attempt_count'], 1)
        self.assertFalse(prov['retries_exhausted'])
        self.assertFalse(response['ok'])
        self.assertFalse(response['retryable_by_caller'])
        self.assertIn('No independent review was completed', response['error_hint'])
        self.assertNotIn('Failed to provide', json.dumps(response))
        self.assertTrue(os.path.isfile(os.path.join(
            self.sb.cfg.job_dir(started['job_id']), 'quarantine', 'attempt-1', 'stdout.bin')))

    def test_diagnostic_compatibility_is_closed_and_category_bound(self):
        from agent_bridge.errors import category_from_status
        self.assertEqual(category_from_status({'error_category':'peer_output_schema_invalid'}),
                         ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID)
        self.assertEqual(category_from_status({'error_category':'peer_output_schema_invalid',
                         'diagnostic_category':'synthetic-secret'}), ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID)
        self.assertEqual(category_from_status({'error_category':'peer_timeout',
                         'diagnostic_category':'peer_structured_output_exhausted'}), ErrorCategory.PEER_TIMEOUT)
        self.assertEqual(category_from_status({'error_category':'unknown'}), ErrorCategory.INTERNAL_ERROR)

    def test_raw_failed_login_and_refresh_rejection(self):
        self.assertEqual(self.claude(b'Not logged in',1).category,ErrorCategory.PEER_AUTH_FAILURE)
        out=self.codex([{'type':'error','message':'refresh_token_reused'}],1)
        self.assertEqual(out.notes['auth_failure_reason'],'oauth_refresh_rejected')


if __name__ == '__main__':
    unittest.main()
