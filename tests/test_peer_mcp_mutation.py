import io
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_bridge.peer_mcp import PeerServer, RoomClient, schema
from agent_bridge import peer_mcp


class PeerMCPMutationTests(unittest.TestCase):
    def runtime_client(self, port=54821, tokens=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'peer-runtime.json').write_text(json.dumps({
            'port': port, 'tokens': {'codex': 'invented-token'} if tokens is None else tokens,
        }), encoding='utf-8')
        return RoomClient('codex', root)

    def test_init_uses_registered_fallback(self):
        client = self.runtime_client()
        self.assertIn('codex', client.participants)
        self.assertIn('claude', client.participants)

    def test_registered_participants_verifies_directory_and_returns_runtime_list(self):
        client = self.runtime_client()
        (client.root / 'peer-runtime.json').write_text(json.dumps({
            'participants': ['codex', 'claude', 'gemini'],
        }), encoding='utf-8')
        with patch.object(peer_mcp, 'verify_private_directory') as verify:
            self.assertEqual(client.registered_participants(), ('codex', 'claude', 'gemini'))
        verify.assert_called_once_with(client.root)

    def test_registered_participants_rejects_duplicates(self):
        client = self.runtime_client()
        (client.root / 'peer-runtime.json').write_text(json.dumps({
            'participants': ['codex', 'codex'],
        }), encoding='utf-8')
        with patch.object(peer_mcp, 'verify_private_directory'):
            self.assertEqual(client.registered_participants(), client.participants)
            self.assertIn('claude', client.participants)

    def test_request_uses_local_origin_token_and_parses_response(self):
        client = self.runtime_client()
        opener = MagicMock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"ok":true}')))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(peer_mcp, 'verify_private_directory'), patch.object(peer_mcp.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(client._request('status'), {'ok': True})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:54821/api/peer-rounds/codex/status')
        self.assertEqual(request.get_header('Authorization'), 'Bearer invented-token')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, None, None, None, None))

    def test_request_rejects_invalid_runtime_before_transport(self):
        client = self.runtime_client(port=0)
        with patch.object(peer_mcp, 'verify_private_directory'), patch.object(peer_mcp.urllib.request, 'build_opener') as build:
            with self.assertRaises(ValueError):
                client._request('status')
        build.assert_not_called()

    def test_request_accepts_upper_port_boundary(self):
        client = self.runtime_client(port=65535)
        opener = MagicMock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{}')))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(peer_mcp, 'verify_private_directory'), patch.object(peer_mcp.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(client._request('status'), {})

    def test_request_accepts_response_at_size_limit(self):
        client = self.runtime_client()
        body = b'{"ok":true}' + b' ' * (2_000_000 - len(b'{"ok":true}'))
        opener = MagicMock()
        response = Mock(read=Mock(return_value=body))
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(peer_mcp, 'verify_private_directory'), patch.object(peer_mcp.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(client._request('status'), {'ok': True})
        response.read.assert_called_once_with(2_000_001)

    def test_prepare_and_status_forward_to_request(self):
        client = self.runtime_client()
        with patch.object(client, '_request', side_effect=[{'ok': True}, {'ok': False}]) as request:
            self.assertEqual(client.prepare({'question': 'Invented question'}), {'ok': True})
            self.assertEqual(client.status({}), {'ok': False})
        self.assertEqual(request.call_args_list[0].args, ('prepare', {'question': 'Invented question'}))
        self.assertEqual(request.call_args_list[1].args, ('status',))

    def test_read_rejects_noncanonical_round_id(self):
        client = self.runtime_client()
        with patch.object(client, '_request') as request:
            with self.assertRaises(ValueError):
                client.read({'round_id': 'A6F12000-0000-4000-8000-000000000001'})
        request.assert_not_called()

    def test_read_forwards_canonical_round_id(self):
        client = self.runtime_client()
        round_id = 'a6f12000-0000-4000-8000-000000000001'
        with patch.object(client, '_request', return_value={'ok': True}) as request:
            self.assertEqual(client.read({'round_id': round_id}), {'ok': True})
        request.assert_called_once_with(round_id)

    def test_schema_disallows_extra_properties(self):
        self.assertEqual(schema({'round_id': {'type': 'string'}}, ['round_id']), {
            'type': 'object', 'additionalProperties': False,
            'properties': {'round_id': {'type': 'string'}}, 'required': ['round_id'],
        })

    def test_peer_server_limits_targets_to_other_participants(self):
        client = Mock()
        client.registered_participants.return_value = ('codex', 'claude')
        server = PeerServer('codex', client)
        targets = server.tools['peers_prepare']['inputSchema']['properties']['targets']
        self.assertEqual(targets['maxItems'], 1)
        self.assertEqual(targets['items']['enum'], ['claude'])

    def test_main_status_uses_tool_handler(self):
        fake = Mock()
        fake.call_tool.return_value = {'structuredContent': {'ok': True}, 'isError': False}
        with patch.object(peer_mcp, 'PeerServer', return_value=fake), patch('builtins.print') as output:
            self.assertEqual(peer_mcp.main(['--caller', 'codex', 'status']), 0)
        fake.serve.assert_not_called()
        fake.call_tool.assert_called_once_with({'name': 'peers_status', 'arguments': {}})
        output.assert_called_once_with('{"ok": true}')

    def test_main_accepts_input_at_size_limit(self):
        fake = Mock()
        fake.call_tool.return_value = {'structuredContent': {'ok': True}, 'isError': False}
        raw = '{"question":"Invented question"}'
        raw += ' ' * (65536 - len(raw))
        with patch.object(peer_mcp, 'PeerServer', return_value=fake), patch.object(peer_mcp.sys, 'stdin', io.StringIO(raw)), patch('builtins.print'):
            self.assertEqual(peer_mcp.main(['--caller', 'codex', 'prepare']), 0)
        fake.call_tool.assert_called_once()
        self.assertEqual(fake.call_tool.call_args.args[0]['arguments'], {'question': 'Invented question'})

    def test_main_returns_error_exit_status(self):
        fake = Mock()
        fake.call_tool.return_value = {'structuredContent': {'ok': False}, 'isError': True}
        with patch.object(peer_mcp, 'PeerServer', return_value=fake), patch('builtins.print'):
            self.assertEqual(peer_mcp.main(['--caller', 'codex', 'status']), 1)
