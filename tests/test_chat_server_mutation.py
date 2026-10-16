"""Offline request checks for the chat server's local HTTP boundary."""
import json
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_bridge.chat import server as chat_server


class FakeServer:
    server_port = 12345

    def __init__(self, address, handler):
        self.address = address
        self.handler = handler


class ServerChecks(unittest.TestCase):
    def setUp(self):
        self.store = Mock(participants=('alpha', 'beta'))
        self.store.rooms.return_value = []
        self.store.snapshot.return_value = {'messages': []}
        self.dispatcher = Mock(adapters={})
        self.policy = Mock(allow_client=True)
        self.policy.effective_classification.return_value = 'public'
        self.rounds = Mock(participants=('alpha', 'beta'))

    def make_server(self, *, rounds=None, rounds_token=None):
        with patch.object(chat_server, 'ThreadingHTTPServer', FakeServer):
            return chat_server.create_server(
                self.store, self.dispatcher, 'human-token', policy=self.policy,
                rounds=rounds, rounds_token=rounds_token)

    def request(self, server, method, path, body=None, headers=None):
        handler = object.__new__(server.handler)
        handler.server = server
        handler.command = method
        handler.path = path
        payload = json.dumps(body).encode() if body is not None else b''
        handler.headers = {
            'Host': '127.0.0.1:12345', 'Origin': 'http://127.0.0.1:12345',
            'Authorization': 'Bearer human-token', 'Content-Type': 'application/json',
            'Content-Length': str(len(payload)),
        }
        handler.headers.update(headers or {})
        handler.rfile = BytesIO(payload)
        answers = []
        handler.send = lambda status, data, content_type='application/json': answers.append((status, data))
        handler.handle_request()
        return answers[0]


class CreateServerTests(ServerChecks):
    def test_loopback_binding_and_daemon_threads(self):
        server = self.make_server()
        self.assertEqual(server.address, ('127.0.0.1', 0))
        self.assertIs(server.daemon_threads, True)

    def test_round_participants_take_precedence(self):
        self.rounds.participants = ('gamma',)
        server = self.make_server(rounds=self.rounds, rounds_token={'gamma': 'round-token'})
        status, data = self.request(server, 'GET', '/api/peer-rounds/gamma/status',
                                    headers={'Authorization': 'Bearer round-token'})
        self.assertEqual(status, 200)
        self.assertEqual(data['peers'], [])


class RoundCallerTests(ServerChecks):
    def test_dictionary_token_binds_caller(self):
        server = self.make_server(rounds=self.rounds, rounds_token={'alpha': 'round-token'})
        self.assertEqual(self.request(server, 'GET', '/api/peer-rounds/alpha/status',
                                      headers={'Authorization': 'Bearer round-token'})[0], 200)
        self.assertEqual(self.request(server, 'GET', '/api/peer-rounds/beta/status',
                                      headers={'Authorization': 'Bearer round-token'})[0], 403)

    def test_legacy_token_can_access_peer_status(self):
        server = self.make_server(rounds=self.rounds, rounds_token='round-token')
        self.assertEqual(self.request(server, 'GET', '/api/peer-rounds/alpha/status',
                                      headers={'Authorization': 'Bearer round-token'})[0], 200)


class ParticipantsTests(ServerChecks):
    def test_status_uses_adapter_and_unavailable_fallback(self):
        adapter = Mock()
        adapter.status.return_value = {'state': 'ready'}
        self.dispatcher.adapters = {'alpha': adapter}
        data = self.request(self.make_server(), 'GET', '/api/participants')[1]
        self.assertEqual(data['participants'], [
            {'id': 'alpha', 'state': 'ready'},
            {'id': 'beta', 'state': 'unavailable', 'detail': 'Adapter not connected yet'},
        ])
        self.assertIs(data['allow_client'], True)

    def test_participant_status_is_cached_until_expiry(self):
        adapter = Mock()
        adapter.status.side_effect = [{'state': 'ready'}, {'state': 'busy'}]
        self.dispatcher.adapters = {'alpha': adapter}
        server = self.make_server()
        with patch.object(chat_server.time, 'monotonic', return_value=100):
            self.request(server, 'GET', '/api/participants')
        with patch.object(chat_server.time, 'monotonic', return_value=114):
            self.assertEqual(self.request(server, 'GET', '/api/participants')[1]['participants'][0]['state'], 'ready')
        with patch.object(chat_server.time, 'monotonic', return_value=115):
            self.assertEqual(self.request(server, 'GET', '/api/participants')[1]['participants'][0]['state'], 'ready')
        with patch.object(chat_server.time, 'monotonic', return_value=116):
            self.assertEqual(self.request(server, 'GET', '/api/participants')[1]['participants'][0]['state'], 'busy')
        self.assertEqual(adapter.status.call_count, 2)


class SendTests(ServerChecks):
    def test_json_response_has_security_headers_and_bytes(self):
        server = self.make_server()
        handler = object.__new__(server.handler)
        handler.wfile = BytesIO()
        headers = {}
        handler.send_response = lambda status: headers.update(status=status)
        handler.send_header = lambda name, value: headers.update({name: value})
        handler.end_headers = lambda: None
        handler.send(200, {'message': 'café'})
        content = handler.wfile.getvalue()
        self.assertEqual(json.loads(content), {'message': 'café'})
        self.assertEqual(headers['Content-Length'], str(len(content)))
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')


class HandleRequestTests(ServerChecks):
    def test_host_and_origin_are_checked(self):
        server = self.make_server()
        self.assertEqual(self.request(server, 'GET', '/api/rooms', headers={'Host': 'foreign.example'})[0], 403)
        self.assertEqual(self.request(server, 'GET', '/api/rooms', headers={'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.request(server, 'POST', '/api/rooms', {'title': 'Invented room'},
                                      headers={'Origin': ''})[0], 403)

    def test_static_path_and_unknown_path(self):
        server = self.make_server()
        self.assertEqual(self.request(server, 'GET', '/missing')[0], 404)
        self.assertEqual(self.request(server, 'POST', '/app.js', {})[0], 404)

    def test_post_requires_valid_size_type_and_object(self):
        server = self.make_server()
        self.assertEqual(self.request(server, 'POST', '/api/rooms', {'title': 'Invented room'})[0], 200)
        self.assertEqual(self.request(server, 'POST', '/api/rooms', headers={'Content-Length': '0'})[0], 413)
        self.assertEqual(self.request(server, 'POST', '/api/rooms', {'title': 'Invented'},
                                      headers={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.request(server, 'POST', '/api/rooms', ['not an object'])[0], 400)

    def test_post_accepts_body_at_size_limit(self):
        server = self.make_server()
        overhead = len(json.dumps({'title': ''}).encode())
        body = {'title': 'A' * (65536 - overhead)}
        self.assertEqual(len(json.dumps(body).encode()), 65536)
        self.assertEqual(self.request(server, 'POST', '/api/rooms', body)[0], 200)

    def test_round_path_and_approval_body_are_validated(self):
        server = self.make_server(rounds=self.rounds, rounds_token={'alpha': 'round-token'})
        auth = {'Authorization': 'Bearer round-token'}
        self.assertEqual(self.request(server, 'GET', '/api/peer-rounds/alpha', headers=auth)[0], 404)
        self.assertEqual(self.request(server, 'GET', '/api/peer-rounds/unknown/status', headers=auth)[0], 404)
        self.assertEqual(self.request(server, 'POST', '/api/peer-approvals/item', {'approve': 1})[0], 400)
        self.assertEqual(self.request(server, 'POST', '/api/peer-approvals/item', {'approve': False})[0], 200)
        self.rounds.approve.assert_called_with('item', reject=True)

    def test_room_actions_reject_bad_recipients_and_unready_peer(self):
        server = self.make_server()
        path = '/api/rooms/invented/messages'
        body = {'request_id': 'invented-request', 'text': 'Invented note',
                'recipients': 'alpha', 'classification': 'public'}
        self.assertEqual(self.request(server, 'POST', path, body)[0], 400)
        self.dispatcher.adapters = {'alpha': Mock()}
        self.dispatcher.adapters['alpha'].status.return_value = {'state': 'busy', 'detail': 'Busy'}
        body['recipients'] = ['alpha']
        self.assertEqual(self.request(server, 'POST', path, body)[0], 400)
        self.store.submit.assert_not_called()
        self.dispatcher.adapters['alpha'].status.return_value = {'state': 'ready'}
        self.assertEqual(self.request(server, 'POST', path, body)[0], 200)
        self.store.submit.assert_called_once()

    def test_delete_requires_literal_true_and_resets_known_target(self):
        server = self.make_server(rounds=self.rounds)
        path = '/api/rooms/invented/delete'
        self.assertEqual(self.request(server, 'POST', path, {'confirm': 1})[0], 400)
        self.assertEqual(self.request(server, 'POST', path, {'confirm': True})[0], 200)
        self.rounds.delete_room.assert_called_once_with('invented')
        self.dispatcher.cancel_room.assert_called_once_with('invented')
        self.assertEqual(self.request(server, 'POST', '/api/rooms/invented/reset', {'target': 'unknown'})[0], 400)
        self.assertEqual(self.request(server, 'POST', '/api/rooms/invented/reset', {'target': 'alpha'})[0], 200)
        self.store.reset_session.assert_called_once_with('invented', 'alpha')


if __name__ == '__main__':
    unittest.main()
