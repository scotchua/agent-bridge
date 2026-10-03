import sys
import subprocess
from pathlib import Path
from test_chat_storage import StorageTests
from agent_bridge.chat.__main__ import _local_opener, build_app
from unittest.mock import patch


class LaunchTests(StorageTests):
    def test_launcher_sets_private_file_creation_policy_before_database(self):
        from agent_bridge.chat import __main__ as launch
        with patch.object(launch.bridge_store, 'set_umask', wraps=launch.bridge_store.set_umask) as private:
            app = build_app(Path(self.temp.name)/'private-state')
            self.addCleanup(app[0].server_close)
            private.assert_called_once()

    def test_optional_providers_are_not_registered_by_default(self):
        app = build_app(Path(self.temp.name)/'configured')
        self.addCleanup(app[0].server_close)
        self.assertEqual(set(app[2].adapters), {'claude', 'codex'})

    def test_local_first_large_prompt_is_capped_before_provider_dispatch(self):
        app = build_app(Path(self.temp.name)/'local-first')
        self.addCleanup(app[0].server_close)
        adapter = app[2].adapters['claude']
        adapter.cfg.raw['local_first'] = {'enabled': True, 'read_gate_min_bytes': 1}
        with patch.object(adapter, 'status', return_value={'state': 'ready'}), patch('agent_bridge.chat.adapters.broker.start') as start:
            with self.assertRaises(ValueError):
                adapter.start('large enough', 'synthetic')
        start.assert_not_called()

    def test_explicit_hermes_path_registers_optional_adapter(self):
        executable = Path(self.temp.name) / 'hermes.exe'
        executable.write_bytes(b'fixture')
        app = build_app(Path(self.temp.name) / 'hermes-state', hermes_executable=executable)
        self.addCleanup(app[0].server_close)
        self.assertEqual(set(app[2].adapters), {'claude', 'codex', 'hermes'})

    def test_explicit_grok_state_registers_optional_adapter(self):
        app = build_app(Path(self.temp.name)/'grok-state', grok_state_dir=Path(self.temp.name)/'grok')
        self.addCleanup(app[0].server_close)
        self.assertEqual(set(app[2].adapters), {'claude', 'codex', 'grok'})

    def test_ready_grok_is_not_given_a_peer_round_token(self):
        with patch('agent_bridge.chat.__main__.GrokAdapter.status', return_value={'state': 'ready'}):
            server, store, _, _ = build_app(Path(self.temp.name) / 'grok-room', grok_state_dir=Path(self.temp.name) / 'grok-queue')
        self.addCleanup(server.server_close)
        self.assertIn('grok', store.participants)
        self.assertNotIn('grok', server.rounds_token)
        self.assertNotIn('grok', server.peer_rounds.participants)

    def test_grok_queue_cannot_be_inside_room_state(self):
        root = Path(self.temp.name) / 'room'
        with self.assertRaises(ValueError):
            build_app(root, grok_state_dir=root / 'grok')
        with self.assertRaises(ValueError):
            build_app(root, grok_state_dir=root.parent)
    def test_existing_instance_probe_disables_proxy_and_redirects(self):
        with patch('agent_bridge.chat.__main__.urllib.request.build_opener') as build:
            _local_opener()
            handlers = build.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsNone(handlers[1].redirect_request(None, None, 302, '', {}, 'http://proxy.invalid'))

    def test_launch_recovers_without_model_calls(self):
        root = Path(self.temp.name) / 'state with spaces'
        app = build_app(root, allow_client=False)
        self.addCleanup(app[0].server_close)
        store = app[1]
        room = store.rooms()[0]['id']
        store.submit(room, 'r', 'test', ['claude'], 'synthetic')
        app[0].server_close()
        restarted = build_app(root, allow_client=False)
        self.addCleanup(restarted[0].server_close)
        self.assertEqual(restarted[1].snapshot(room)['jobs'][0]['status'], 'failed')
        # The runtime stores the canonical filesystem path. On macOS, a
        # temporary directory can be spelled /var/... while resolve() returns
        # /private/var/..., even though both names identify the same folder.
        self.assertEqual(restarted[1].path.parent, root.resolve())

    def test_entrypoint_help_works_from_other_directory(self):
        script = Path(__file__).resolve().parents[1] / 'start_chat.py'
        result = subprocess.run([sys.executable, str(script), '--help'], cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--open', result.stdout)
