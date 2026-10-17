"""Synthetic regressions for status, classification, and recovery diagnostics."""
import json
import io
import os
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from harness import Sandbox
from agent_bridge import health, runner, setup_cmd
from agent_bridge.backends import claude_backend, codex_backend
from agent_bridge.backends.base import auth_failure_reason
from agent_bridge.errors import ErrorCategory
from agent_bridge.execution import claude_task


def result(stdout=b'', stderr=b'', code=0, **kw):
    return runner.RunResult(argv=[], returncode=code, stdout=stdout, stderr=stderr,
                            timed_out=False, duration_seconds=0, pgid=None, **kw)


class HealthTests(unittest.TestCase):
    def test_claude_subscription_allowlist_matches_execution_lane(self):
        self.assertEqual(health.CLAUDE_SUBSCRIPTION_TYPES,
                         claude_task.ALLOWED_SUBSCRIPTIONS)

    def test_business_subscription_is_forwarded_for_the_execution_lane(self):
        data = {'loggedIn': True, 'authMethod': 'claude.ai',
                'subscriptionType': 'business'}
        with patch.object(runner, 'run', return_value=result(json.dumps(data).encode())):
            report = health.auth_status('claude', '/synthetic/claude', {})
        self.assertEqual(report['subscriptionType'], 'business')

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


class HealthMutationAuthTests(unittest.TestCase):
    def test_probe_limits_and_failure_reason(self):
        failed = result(b'{"loggedIn":true}', spawn_failed=True)
        with patch.object(runner, 'run', return_value=failed) as call:
            report = health.auth_status('claude', '/synthetic/claude', {})
        self.assertEqual(report['probe_error'], 'spawn_failed')
        self.assertEqual(report['state'], 'unknown')
        self.assertEqual({key: call.call_args.kwargs[key] for key in
                          ('timeout', 'grace', 'stdout_cap', 'stderr_cap')},
                         {'timeout': 20, 'grace': 1, 'stdout_cap': 32768,
                          'stderr_cap': 32768})

    def test_claude_allowlist_and_signout_codes(self):
        data = {'loggedIn': False, 'authMethod': 'api_key',
                'apiProvider': 'bedrock', 'subscriptionType': 'team',
                'email': 'person@example.invalid'}
        for code in (0, 1):
            with self.subTest(code=code), patch.object(
                    runner, 'run', return_value=result(json.dumps(data).encode(), code=code)):
                report = health.auth_status('claude', '/synthetic/claude', {})
            self.assertEqual(report['state'], 'not_signed_in')
            self.assertEqual(report['authMethod'], 'api_key')
            self.assertEqual(report['apiProvider'], 'bedrock')
            self.assertEqual(report['subscriptionType'], 'team')
            self.assertNotIn('email', report)
        data['subscriptionType'] = 'free'
        with patch.object(runner, 'run', return_value=result(json.dumps(data).encode())):
            self.assertNotIn('subscriptionType',
                             health.auth_status('claude', '/synthetic/claude', {}))
        data['loggedIn'] = True
        with patch.object(runner, 'run', return_value=result(json.dumps(data).encode(), code=1)):
            self.assertEqual(health.auth_status('claude', '/synthetic/claude', {})['state'],
                             'unknown')

    def test_codex_status_needs_approved_text_and_exit(self):
        cases = [(b'No credentials', 1, 'not_signed_in', None),
                 (b'Not logged in', 1, 'not_signed_in', None),
                 (b'Logged in using ChatGPT', 0, 'signed_in', 'chatgpt'),
                 (b'Logged in using ChatGPT', 1, 'unknown', None),
                 (b'Logged in using an API key', 0, 'signed_in', 'api_key')]
        for output, code, state, method in cases:
            with self.subTest(output=output, code=code), patch.object(
                    runner, 'run', return_value=result(stderr=output, code=code)):
                report = health.auth_status('codex', '/synthetic/codex', {})
            self.assertEqual(report['state'], state)
            self.assertEqual(report.get('authMethod'), method)


class HealthMutationInspectTests(unittest.TestCase):
    def inspect(self, peer, env, path_cli=None):
        spec = {'executable': '/configured/' + peer, 'allowed_versions': ['approved']}
        cfg = SimpleNamespace(peer=lambda name: spec)
        validated = {'executable': spec['executable'], 'realpath': '/validated/' + peer,
                     'observed_version': 'approved'}
        with (patch.object(health, 'peer_env', return_value=env),
              patch.object(health.preflight, 'discover_executable', return_value=path_cli),
              patch.object(health.preflight, 'check_peer', return_value=validated),
              patch.object(health.preflight, 'assert_peer_home_has_no_config'),
              patch.object(runner, 'credential_context', return_value={'home': 'synthetic'}),
              patch.object(health, 'auth_status', return_value={
                  'state': 'signed_in', 'network_verified': False})):
            return health.inspect_peer(cfg, peer)

    def test_claude_storage_precedence_and_report_fields(self):
        with tempfile.TemporaryDirectory() as root:
            secure = os.path.join(root, 'secure')
            legacy = os.path.join(root, 'legacy')
            os.makedirs(secure)
            os.makedirs(legacy)
            with open(os.path.join(legacy, '.credentials.json'), 'w') as handle:
                handle.write('{}')
            env = {'HOME': root, 'CLAUDE_SECURESTORAGE_CONFIG_DIR': secure,
                   'CLAUDE_CONFIG_DIR': legacy}
            report = self.inspect('claude', env)
            self.assertFalse(report['fallback_file_present'])
            self.assertIn('Keychain', report['recovery'])
            self.assertEqual(report['allowed_versions'], ['approved'])
            self.assertFalse(report['path_differs'])
            self.assertEqual(report['operator_login_argv'],
                             ['/validated/claude', 'auth', 'login', '--claudeai'])
            env.pop('CLAUDE_SECURESTORAGE_CONFIG_DIR')
            self.assertTrue(self.inspect('claude', env)['fallback_file_present'])

    def test_codex_auth_file_and_recovery(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'auth.json'), 'w') as handle:
                handle.write('{}')
            report = self.inspect('codex', {'CODEX_HOME': home, 'HOME': home})
            self.assertTrue(report['auth_file_present'])
            self.assertIn('isolated CODEX_HOME', report['recovery'])
            self.assertEqual(report['operator_login_argv'], ['/validated/codex', 'login'])


class HealthMutationCommandTests(unittest.TestCase):
    def run_health(self, rows, peer=None, json_output=False):
        cfg = SimpleNamespace(raw={'company': 'Maple Ledger Works'})
        args = SimpleNamespace(peer=peer, json=json_output)
        with (patch.object(health, 'inspect_peer', side_effect=lambda cfg, name: rows[name]) as inspect,
              patch.object(health.store, 'utc_now', return_value='2042-03-04T05:06:07Z'),
              patch.object(health, 'effective_config_sha256', return_value='synthetic-hash')):
            output = io.StringIO()
            with redirect_stdout(output):
                code = health.cmd_health(cfg, args)
        return code, output.getvalue(), [call.args[1] for call in inspect.call_args_list]

    def rows(self):
        return {peer: {'peer': peer, 'auth': {'state': 'signed_in'},
                       'preflight': 'ok', 'executable': '/validated/' + peer,
                       'observed_version': 'approved', 'path_differs': peer == 'claude',
                       'path_executable': '/path/claude' if peer == 'claude' else None,
                       'context': {'home': 'synthetic'},
                       'recovery': 'Synthetic recovery steps.'}
                for peer in ('claude', 'codex')}

    def test_json_report_and_selected_peer(self):
        rows = self.rows()
        code, output, called = self.run_health(rows, peer='codex', json_output=True)
        self.assertEqual(code, 0)
        self.assertEqual(called, ['codex'])
        expected = {'checked_at': '2042-03-04T05:06:07Z',
                    'effective_config_sha256': 'synthetic-hash',
                    'scope': 'local credential visibility; no network or token-refresh test',
                    'peers': [rows['codex']]}
        self.assertEqual(output, json.dumps(expected, indent=2, sort_keys=True) + '\n')

    def test_text_report_includes_diagnostics(self):
        code, output, called = self.run_health(self.rows())
        self.assertEqual(code, 0)
        self.assertEqual(called, ['claude', 'codex'])
        for text in ('Bridge health: local checks only',
                     'claude: signed_in (preflight: ok)',
                     'Bridge CLI: /validated/claude (approved)',
                     'Different PATH CLI: /path/claude',
                     'The bridge keeps its tested pin',
                     'Credential context: {"home": "synthetic"}',
                     'Synthetic recovery steps.',
                     'No login or configuration changes were requested',
                     'codex: signed_in (preflight: ok)'):
            with self.subTest(text=text):
                self.assertIn(text, output)
        self.assertNotIn('Different PATH CLI: None', output)

    def test_exit_status_requires_every_peer_to_pass(self):
        rows = self.rows()
        self.assertEqual(self.run_health(rows)[0], 0)
        rows['codex']['auth']['state'] = 'unknown'
        self.assertEqual(self.run_health(rows)[0], 1)
        rows['codex']['auth']['state'] = 'signed_in'
        rows['codex']['preflight'] = 'preflight_version_mismatch'
        self.assertEqual(self.run_health(rows)[0], 1)


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


class ClaudeExecutableMutationTests(unittest.TestCase):
    class Config:
        def __init__(self, spec=None, cap=4096):
            self.spec = spec or {}
            self.cap = cap

        def peer(self, name):
            return self.spec

        def peer_reasoning_effort(self, name):
            return None

        def peer_extra_env(self, name):
            return {}

        def limit(self, name):
            return self.cap

    def test_executable_uses_pin_then_discovery_then_name(self):
        with patch('agent_bridge.preflight.discover_executable', return_value='/synthetic/discovered'):
            self.assertEqual(claude_backend._executable(self.Config({'executable': '/synthetic/pinned'})),
                             '/synthetic/pinned')
            self.assertEqual(claude_backend._executable(self.Config()), '/synthetic/discovered')
        with patch('agent_bridge.preflight.discover_executable', return_value=None):
            self.assertEqual(claude_backend._executable(self.Config()), 'claude')


class ClaudeSessionMutationTests(unittest.TestCase):
    def test_new_session_id_is_a_canonical_uuid(self):
        session_id = claude_backend.new_session_id()
        self.assertEqual(str(uuid.UUID(session_id)), session_id)


class ClaudeArgvMutationTests(unittest.TestCase):
    def test_resume_and_fresh_session_flags_are_distinct(self):
        cfg = ClaudeExecutableMutationTests.Config({'executable': '/synthetic/claude'})
        fresh = claude_backend.build_argv(cfg, {}, 'session', False)
        resumed = claude_backend.build_argv(cfg, {}, 'session', True)
        self.assertEqual(fresh[fresh.index('--session-id') + 1], 'session')
        self.assertNotIn('--resume', fresh)
        self.assertEqual(resumed[resumed.index('--resume') + 1], 'session')
        self.assertNotIn('--session-id', resumed)

    def test_model_and_zero_budget_are_passed(self):
        cfg = ClaudeExecutableMutationTests.Config({
            'executable': '/synthetic/claude', 'model': 'claude-fable', 'max_budget_usd': 0})
        argv = claude_backend.build_argv(cfg, {}, 'session', False)
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-fable')
        self.assertEqual(argv[argv.index('--max-budget-usd') + 1], '0')

    def test_isolation_flags_are_present(self):
        cfg = ClaudeExecutableMutationTests.Config({'executable': '/synthetic/claude'})
        argv = claude_backend.build_argv(cfg, {}, 'session', False)
        self.assertIn('--safe-mode', argv)
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv[argv.index('--mcp-config') + 1], '{"mcpServers":{}}')
        self.assertEqual(argv[argv.index('--tools') + 1], '')


class ClaudePayloadMutationTests(unittest.TestCase):
    def test_json_string_result_is_extracted(self):
        self.assertEqual(claude_backend._extract_payload({'result': '{"account":"Maple Works"}'}),
                         ({'account': 'Maple Works'}, 'envelope.result:json_string'))

    def test_missing_payload_stays_missing(self):
        self.assertEqual(claude_backend._extract_payload({}), (None, None))


class ClaudeConsultationMutationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ClaudeExecutableMutationTests.Config({'executable': '/synthetic/claude'})

    def consultation(self, envelope=None, *, stdout=None, cap=4096, **changes):
        self.cfg.cap = cap
        if stdout is None:
            stdout = json.dumps(envelope).encode()
        run_result = result(stdout, **changes)
        with patch.object(runner, 'run', return_value=run_result) as call, \
             patch.object(runner, 'scrubbed_env', return_value={}), \
             patch.object(runner, 'credential_context', return_value={}):
            outcome = claude_backend.run_consultation(
                self.cfg, prompt='synthetic', schema={}, session_id='session',
                resume=False, workspace='/synthetic/workspace')
        self.last_run_call = call.call_args
        return outcome

    def test_default_timeout_and_grace_reach_runner(self):
        self.consultation({'session_id': 'session', 'result': {'account': 'Maple Works'}})
        self.assertEqual(self.last_run_call.kwargs['timeout'], 300.0)
        self.assertEqual(self.last_run_call.kwargs['grace'], 5.0)

    def test_cap_flag_and_exact_limit_reject_output(self):
        self.assertEqual(self.consultation(stdout=b'{}', cap_exceeded=True).category,
                         ErrorCategory.PEER_OUTPUT_TOO_LARGE)
        self.assertEqual(self.consultation(stdout=b'x' * 32, cap=32).category,
                         ErrorCategory.PEER_OUTPUT_TOO_LARGE)

    def test_single_and_multiple_models_and_zero_cost_are_recorded(self):
        envelope = {'session_id': 'session', 'result': {'account': 'Maple Works'},
                    'modelUsage': {'claude-fable-1': {}}, 'total_cost_usd': 0.0}
        outcome = self.consultation(envelope)
        self.assertEqual(outcome.category, ErrorCategory.OK)
        self.assertEqual(outcome.observed_model, 'claude-fable-1')
        self.assertTrue(outcome.notes['total_cost_present'])
        envelope['modelUsage']['claude-fable-2'] = {}
        outcome = self.consultation(envelope)
        self.assertEqual(outcome.observed_models, ['claude-fable-1', 'claude-fable-2'])
        self.assertIsNone(outcome.observed_model)

    def test_success_keeps_payload_and_extraction_path(self):
        outcome = self.consultation({'session_id': 'session',
                                     'structured_output': {'account': 'Maple Works'}})
        self.assertEqual(outcome.category, ErrorCategory.OK)
        self.assertEqual(outcome.payload, {'account': 'Maple Works'})
        self.assertEqual(outcome.extraction_path, 'envelope.structured_output')


if __name__ == '__main__':
    unittest.main()
