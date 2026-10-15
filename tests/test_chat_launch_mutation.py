import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

from agent_bridge import config
from agent_bridge.chat import __main__ as launch


class BuildAppMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        env = patch.dict(os.environ, {'AGENT_BRIDGE_CONFIG': config.DEFAULT_CONFIG_PATH})
        env.start()
        self.addCleanup(env.stop)

    def build_offline(self, root, **kwargs):
        server = SimpleNamespace(server_port=12345, server_close=lambda: None)
        with patch.object(launch, 'create_server', return_value=server):
            return launch.build_app(root, **kwargs)

    def test_build_app_prepares_and_verifies_room_root(self):
        root = self.base / 'private-room'
        with patch.object(launch, 'prepare_private_directory', wraps=launch.prepare_private_directory) as prepare, \
                patch.object(launch, 'verify_private_directory', wraps=launch.verify_private_directory) as verify:
            self.build_offline(root)
        self.assertIn(call(root.resolve()), prepare.call_args_list)
        self.assertIn(call(root.resolve() / 'bridge'), prepare.call_args_list)
        verify.assert_called_once_with(root.resolve())

    def test_build_app_refuses_configured_state_outside_room(self):
        root = self.base / 'room'
        root.mkdir()
        cfg = config.load(config.DEFAULT_CONFIG_PATH)
        raw = copy.deepcopy(cfg.raw)
        raw['state_root'] = str(self.base / 'other-state')
        (root / 'broker.json').write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, 'dedicated private directory'):
            self.build_offline(root)

    def test_build_app_refuses_client_opt_in_without_bridge_policy(self):
        with self.assertRaisesRegex(ValueError, 'Client-derived material is not enabled'):
            self.build_offline(self.base / 'client-room', allow_client=True)

    def test_build_app_requires_hermes_file(self):
        executable = self.base / 'hermes-folder'
        executable.mkdir()
        with self.assertRaisesRegex(ValueError, 'Hermes executable must be a file'):
            self.build_offline(self.base / 'hermes-room', hermes_executable=executable)

    def test_build_app_excludes_unready_hermes_from_participants(self):
        executable = self.base / 'hermes.exe'
        executable.write_bytes(b'invented executable')
        with patch.object(launch.HermesAdapter, 'status', return_value={'state': 'unavailable'}):
            server, store, dispatcher, _ = self.build_offline(
                self.base / 'hermes-room', hermes_executable=executable)
        self.assertIn('hermes', dispatcher.adapters)
        self.assertNotIn('hermes', store.participants)
        self.assertNotIn('hermes', server.rounds_token)

    def test_build_app_caps_prompt_at_both_limits(self):
        _, default_store, _, _ = self.build_offline(self.base / 'default-budget')
        self.assertEqual(default_store.max_chars, 12000)
        cfg = config.load(config.DEFAULT_CONFIG_PATH)
        cfg.raw = copy.deepcopy(cfg.raw)
        cfg.raw['limits']['prompt_start_max_chars'] = 13000
        with patch.object(launch.config, 'load', return_value=cfg):
            _, narrow_store, _, _ = self.build_offline(self.base / 'narrow-budget')
        self.assertEqual(narrow_store.max_chars, 11000)

    def test_build_app_uses_full_size_tokens(self):
        with patch.object(launch.secrets, 'token_urlsafe', side_effect=lambda size: 'invented-token') as tokens:
            self.build_offline(self.base / 'token-room')
        self.assertEqual(tokens.call_args_list, [call(32), call(32), call(32)])


class MainMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'room'

    def unavailable_lock(self):
        lock = MagicMock()
        lock.__enter__.side_effect = BlockingIOError('invented busy lock')
        return patch.object(launch.bridge_store, 'file_lock', return_value=lock)

    def test_main_probes_healthy_existing_room(self):
        self.root.mkdir()
        (self.root / 'runtime.json').write_text(json.dumps({'port': 12345, 'token': 'invented-token'}))
        response = MagicMock()
        response.__enter__.return_value.status = 200
        opener = Mock()
        opener.open.return_value = response
        with self.unavailable_lock(), patch.object(launch, '_local_opener', return_value=opener), \
                patch.object(launch, 'build_app') as build, patch.object(launch.webbrowser, 'open') as browser:
            self.assertEqual(launch.main(['--state-dir', str(self.root), '--open']), 0)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:12345/api/rooms')
        self.assertEqual(request.get_header('Authorization'), 'Bearer invented-token')
        browser.assert_called_once_with('http://127.0.0.1:12345/#token=invented-token')
        build.assert_not_called()

    def test_main_rejects_stale_or_missing_existing_room(self):
        self.root.mkdir()
        runtime = self.root / 'runtime.json'
        runtime.write_text(json.dumps({'port': 12345, 'token': 'invented-token'}))
        response = MagicMock()
        response.__enter__.return_value.status = 503
        opener = Mock()
        opener.open.return_value = response
        with self.unavailable_lock(), patch.object(launch, '_local_opener', return_value=opener), \
                patch.object(launch, 'build_app') as build:
            with self.assertRaisesRegex(SystemExit, 'Another room instance'):
                launch.main(['--state-dir', str(self.root)])
            runtime.unlink()
            with self.assertRaisesRegex(SystemExit, 'Another room instance'):
                launch.main(['--state-dir', str(self.root)])
        build.assert_not_called()

    def test_main_writes_runtime_starts_workers_and_cleans_up(self):
        lock = MagicMock()
        dispatcher = SimpleNamespace(loop=Mock(), closed=SimpleNamespace(set=Mock()))
        rounds = SimpleNamespace(loop=Mock(), closed=SimpleNamespace(set=Mock()),
                                 participants=('claude', 'codex'))
        server = SimpleNamespace(server_port=12345, rounds_token={'claude': 'invented-peer-token'},
                                 peer_rounds=rounds, serve_forever=Mock(), server_close=Mock())
        with patch.object(launch.bridge_store, 'file_lock', return_value=lock) as file_lock, \
                patch.object(launch, 'build_app', return_value=(server, None, dispatcher, 'invented-token')), \
                patch.object(launch.bridge_store, 'atomic_write_json', wraps=launch.bridge_store.atomic_write_json) as write, \
                patch.object(launch.threading, 'Thread') as thread:
            self.assertEqual(launch.main(['--state-dir', str(self.root)]), 0)
        file_lock.assert_called_once_with(str(self.root.resolve() / 'instance.lock'), timeout=0.2)
        self.assertEqual(write.call_count, 2)
        self.assertEqual({Path(item.args[0]).name for item in write.call_args_list},
                         {'runtime.json', 'peer-runtime.json'})
        self.assertEqual(thread.return_value.start.call_count, 2)
        server.serve_forever.assert_called_once_with()
        dispatcher.closed.set.assert_called_once_with()
        rounds.closed.set.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertFalse((self.root / 'runtime.json').exists())
        self.assertFalse((self.root / 'peer-runtime.json').exists())
        lock.__exit__.assert_called_once_with(None, None, None)


if __name__ == '__main__':
    unittest.main()
