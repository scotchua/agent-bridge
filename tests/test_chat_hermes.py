import json
import io
import os
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_chat_storage import StorageTests
from agent_bridge.chat.hermes import HermesAdapter, child_environment, decode_result
from agent_bridge.chat.policy import RoomPolicy


class HermesTests(StorageTests):
    def test_child_environment_excludes_unrelated_variables(self):
        environment = child_environment({'PATH': 'path', 'HOME': 'home', 'FAKE_API_KEY': 'secret', 'LANG': 'C'})
        self.assertEqual(environment, {'PATH': 'path', 'HOME': 'home', 'LANG': 'C'})
        self.assertNotIn('FAKE_API_KEY', environment)

    def test_launcher_only_fingerprint_cannot_establish_hermes_readiness(self):
        executable = Path(self.temp.name) / 'bin' / 'hermes.exe'
        executable.parent.mkdir()
        executable.write_bytes(b'fixture launcher')
        adapter = HermesAdapter(executable, Path(self.temp.name) / 'evidence', RoomPolicy(False))
        with self.assertRaises(ValueError):
            adapter.fingerprint()

    def test_status_does_not_depend_on_stale_verification_record(self):
        adapter = HermesAdapter(Path(self.temp.name) / 'hermes', Path(self.temp.name) / 'evidence', RoomPolicy(False))
        evidence = {'fingerprint': 'fixture', 'profile': 'default', 'start_pass': True, 'resume_pass': True}
        (adapter.root / 'verification.json').write_text(json.dumps(evidence), encoding='utf-8')
        with patch.object(adapter, 'fingerprint', return_value='fixture'), patch('agent_bridge.chat.hermes.verify_private_directory'):
            self.assertEqual(adapter.status()['state'], 'ready')

    def test_only_terminal_success_with_session_is_accepted(self):
        text, session = decode_result(json.dumps({'type': 'result', 'exit_code': 0, 'text': 'Hello', 'session_id': 'session-123'}), 0)
        self.assertEqual((text, session), ('Hello', 'session-123'))
        for event in [
            dict(type='result', exit_code=1, text='no', session_id='s'),
            dict(type='text', text='partial'),
            dict(type='result', exit_code=0, text='hi', session_id='--bad'),
        ]:
            with self.assertRaises(ValueError):
                decode_result(json.dumps(event), 0)

    def test_default_profile_and_resumed_session_use_room_context(self):
        executable = Path(self.temp.name) / 'hermes.exe'
        executable.write_bytes(b'fake executable for offline test')
        adapter = HermesAdapter(executable, Path(self.temp.name) / 'hermes', RoomPolicy(False))

        class FakeProcess:
            pid = 12345

            def __init__(self, argv, **kwargs):
                self.argv, self.kwargs = argv, kwargs
                class InputBuffer(io.BytesIO):
                    def close(self):
                        pass
                self.stdin = InputBuffer()
                self.stdout = io.BytesIO((json.dumps({'type': 'result', 'exit_code': 0, 'text': 'Real parsed reply', 'session_id': 'session-123'}) + '\n').encode())
                self.stderr = io.BytesIO()
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        created = []

        def popen(argv, **kwargs):
            self.assertEqual(argv[1:4], ['--profile', 'default', 'chat'])
            self.assertIn('--resume', argv)
            self.assertEqual(argv[argv.index('--resume') + 1], 'session-123')
            self.assertNotIn('--yolo', argv)
            self.assertEqual(argv[argv.index('--toolsets') + 1], 'clarify')
            self.assertIs(kwargs['stdin'], subprocess.PIPE)
            self.assertEqual(kwargs['env'], {'PATH': 'fixture'})
            process = FakeProcess(argv, **kwargs)
            created.append(process)
            return process

        with patch.dict(os.environ, {'PATH': 'fixture', 'FAKE_API_KEY': 'secret'}, clear=True), \
                patch.object(adapter, 'status', return_value={'state': 'ready'}), \
                patch('agent_bridge.chat.hermes.subprocess.Popen', side_effect=popen), \
                patch('agent_bridge.chat.hermes.subprocess.run'):
            result = adapter.continue_('session-123', 'Shared room message', 'synthetic')
        self.assertEqual(adapter.read(result['job_id'])['peer_response'], 'Real parsed reply')
        self.assertEqual(result['conversation_id'], 'session-123')
        self.assertEqual(created[0].stdin.getvalue(), b'Shared room message')

    def test_missing_evidence_cannot_claim_ready(self):
        adapter = HermesAdapter(Path(self.temp.name) / 'absent.exe', Path(self.temp.name) / 'hermes', RoomPolicy(False))
        self.assertNotEqual(adapter.status()['state'], 'ready')

    def test_client_prompt_requires_native_profile_storage_verification(self):
        adapter = HermesAdapter(Path(self.temp.name) / 'hermes.exe', Path(self.temp.name) / 'hermes', RoomPolicy(False))
        with patch.object(adapter, 'status', return_value={'state': 'ready'}), patch('agent_bridge.chat.hermes.subprocess.run', side_effect=AssertionError('Client prompt reached unverified profile')):
            with self.assertRaises(ValueError):
                adapter.start('client fixture', 'client-derived')

    def test_unicode_reply_preserved(self):
        from agent_bridge import store
        job = '00000000-0000-4000-8000-000000000000'
        adapter = HermesAdapter(Path(self.temp.name) / 'hermes.exe', Path(self.temp.name) / 'unicode', RoomPolicy(False))
        folder = adapter.root / job
        folder.mkdir()
        store.atomic_write_json(str(folder / 'result.json'), {'ok': True, 'peer_response': '“Hello” — 🌱'})
        self.assertEqual(adapter.read(job)['peer_response'], '“Hello” — 🌱')


class HermesMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def adapter(self):
        return HermesAdapter(self.root / 'hermes.exe', self.root / 'jobs', RoomPolicy(False))

    def test_child_environment_uses_default_source(self):
        with patch.dict(os.environ, {'PATH': 'synthetic-path', 'FAKE_ACCOUNT': 'Example Works'}, clear=True):
            self.assertEqual(child_environment(), {'PATH': 'synthetic-path'})

    def test_timeout_contract(self):
        self.assertEqual(HermesAdapter.timeout, 135)

    def test_cleanup_stale_jobs_preserves_cutoff_and_recent_jobs(self):
        adapter = self.adapter()
        cutoff = 1_000_000 - 24 * 3600
        boundary = adapter.root / 'boundary'
        recent = adapter.root / 'recent'
        old = adapter.root / 'old'
        for path, stamp in ((boundary, cutoff), (recent, cutoff + 1), (old, cutoff - 1)):
            path.mkdir()
            os.utime(path, (stamp, stamp))
        with patch('agent_bridge.chat.hermes.time.time', return_value=1_000_000):
            adapter._cleanup_stale_jobs()
        self.assertTrue(boundary.exists())
        self.assertTrue(recent.exists())
        self.assertFalse(old.exists())

    def test_constructor_removes_stale_jobs(self):
        jobs = self.root / 'jobs'
        old = jobs / 'old'
        old.mkdir(parents=True)
        os.utime(old, (1, 1))
        with patch('agent_bridge.chat.hermes.time.time', return_value=1_000_000):
            self.adapter()
        self.assertFalse(old.exists())

    def test_fingerprint_hashes_launcher_and_implementation(self):
        executable = self.root / 'bin' / 'hermes.exe'
        executable.parent.mkdir()
        executable.write_bytes(b'Example Works launcher')
        implementation = self.root / 'hermes-agent' / 'hermes_cli'
        implementation.mkdir(parents=True)
        pieces = [b'one', b'two', b'three']
        for name, content in zip(('main.py', 'stream_json.py', 'cli_init_mixin.py'), pieces):
            (implementation / name).write_bytes(content)
        adapter = HermesAdapter(executable, self.root / 'jobs', RoomPolicy(False))
        self.assertEqual(adapter.fingerprint(), hashlib.sha256(b'Example Works launcher' + b''.join(pieces)).hexdigest())

    def test_status_removes_stale_jobs(self):
        adapter = self.adapter()
        old = adapter.root / 'old'
        old.mkdir()
        os.utime(old, (1, 1))
        with patch.object(adapter, 'fingerprint', return_value='synthetic'), \
                patch('agent_bridge.chat.hermes.verify_private_directory'), \
                patch('agent_bridge.chat.hermes.time.time', return_value=1_000_000):
            self.assertEqual(adapter.status()['state'], 'ready')
        self.assertFalse(old.exists())

    def test_status_requires_private_directory_verification(self):
        adapter = self.adapter()
        with patch.object(adapter, 'fingerprint', return_value='synthetic'), \
                patch('agent_bridge.chat.hermes.verify_private_directory', side_effect=OSError('synthetic permission denial')):
            self.assertEqual(adapter.status()['state'], 'verification_required')

    def test_terminate_process_group_skips_finished_and_kills_running(self):
        finished = Mock(pid=123, poll=Mock(return_value=0))
        running = Mock(pid=456, poll=Mock(return_value=None))
        with patch('agent_bridge.chat.hermes.os.name', 'posix'), \
                patch('agent_bridge.chat.hermes.signal.SIGKILL', 9, create=True), \
                patch('agent_bridge.chat.hermes.os.killpg') as kill, \
                patch('agent_bridge.chat.hermes.subprocess.run') as run:
            HermesAdapter._terminate_process_group(finished)
            kill.assert_not_called()
            HermesAdapter._terminate_process_group(running)
            kill.assert_called_once_with(456, 9)
            run.assert_not_called()
        with patch('agent_bridge.chat.hermes.os.name', 'nt'), \
                patch('agent_bridge.chat.hermes.subprocess.run') as run:
            HermesAdapter._terminate_process_group(running)
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            self.assertEqual(run.call_args.args[0][:3], ['taskkill', '/PID', '456'])

    def test_run_process_accepts_exact_output_limit(self):
        adapter = self.adapter()
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO(b'abcd')
        process.stderr = io.BytesIO()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch('agent_bridge.chat.hermes.OUTPUT_LIMIT', 4), \
                patch('agent_bridge.chat.hermes.subprocess.Popen', return_value=process):
            self.assertEqual(adapter._run_process(['synthetic'], 'prompt', adapter.root), ('abcd', 0))

    def test_run_process_rejects_zero_remaining_time(self):
        adapter = self.adapter()
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch('agent_bridge.chat.hermes.subprocess.Popen', return_value=process), \
                patch('agent_bridge.chat.hermes.time.monotonic', side_effect=[10, 130]), \
                patch.object(adapter, '_terminate_process_group'):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                adapter._run_process(['synthetic'], 'prompt', adapter.root)

    def test_call_rejects_empty_and_oversized_prompts_before_dispatch(self):
        adapter = self.adapter()
        with patch.object(adapter, 'status', return_value={'state': 'ready'}), \
                patch.object(adapter, '_run_process') as run:
            for prompt in ('', 'x' * 16001):
                with self.subTest(length=len(prompt)):
                    with self.assertRaisesRegex(ValueError, '16000-character'):
                        adapter.start(prompt, 'synthetic')
            run.assert_not_called()

    def test_poll_validates_job_id_and_reports_completion(self):
        adapter = self.adapter()
        self.assertEqual(adapter.poll('00000000-0000-4000-8000-000000000001'),
                         {'ok': True, 'status': 'complete'})
        with self.assertRaises(ValueError):
            adapter.poll('invented-job')
