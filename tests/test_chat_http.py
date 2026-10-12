import http.client
import json
import threading
import copy
from io import BytesIO
from unittest.mock import patch
from test_chat_storage import StorageTests
from agent_bridge import config
from agent_bridge.chat.dispatch import Dispatcher
from agent_bridge.chat.policy import RoomPolicy
from agent_bridge.chat.server import create_server


class HTTPTests(StorageTests):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.store, Dispatcher(self.store, {}), 'test-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        h = {'Authorization': 'Bearer test-token', 'Origin': self.origin, 'Content-Type': 'application/json'}
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_missing_auth_and_foreign_origin_are_refused(self):
        for headers in [{'Authorization': ''}, {'Origin': 'https://evil.example'}, {'Host': 'evil.example'}]:
            self.assertEqual(self.request('GET', '/api/rooms', headers=headers)[0], 403)

    def test_human_get_without_origin_is_allowed(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request('GET', '/api/rooms', headers={'Authorization': 'Bearer test-token'})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        conn.close()

    def test_note_roundtrip_and_literal_content(self):
        body = {'request_id': 'x', 'text': '<script>alert(1)</script>', 'recipients': [], 'classification': 'public', 'mode': 'note'}
        self.assertEqual(self.request('POST', f'/api/rooms/{self.room}/messages', body)[0], 200)
        status, data = self.request('GET', f'/api/rooms/{self.room}')
        self.assertEqual(status, 200)
        snapshot = json.loads(data)
        self.assertEqual(snapshot['messages'][0]['text'], body['text'])
        self.assertEqual(snapshot['jobs'], [])

    def test_bad_paths_and_oversized_body(self):
        self.assertEqual(self.request('GET', '/../../config/broker.json')[0], 404)
        self.assertEqual(self.request('POST', '/api/rooms', {'title': 'x' * 70000})[0], 413)

    def test_unavailable_recipient_is_rejected_before_save(self):
        body = {'request_id': 'x', 'text': 'hello', 'recipients': ['codex'], 'classification': 'public'}
        self.assertEqual(self.request('POST', f'/api/rooms/{self.room}/messages', body)[0], 400)
        self.assertEqual(self.store.snapshot(self.room)['messages'], [])

    def test_rename_and_delete_require_confirmation(self):
        self.assertEqual(self.request('POST',f'/api/rooms/{self.room}/rename',{'title':'Renamed'})[0],200)
        self.assertEqual(self.store.rooms()[0]['title'],'Renamed')
        self.assertEqual(self.request('POST',f'/api/rooms/{self.room}/delete',{})[0],400)
        self.assertEqual(len(self.store.rooms()),1)
        self.assertEqual(self.request('POST',f'/api/rooms/{self.room}/delete',{'confirm':True})[0],200)
        self.assertEqual(self.store.rooms(),[])


class HTTPPolicyTests(StorageTests):
    def test_room_history_classification_restricts_recipient_before_save(self):
        class ReadyPeer:
            def status(self):
                return {'state': 'ready'}

            def start(self, prompt, classification):
                self.classification = classification
                return {'ok': True, 'job_id': 'fake-job', 'conversation_id': 'fake-conversation'}

            def poll(self, job_id):
                return {'ok': True, 'status': 'complete'}

            def read(self, job_id):
                return {'ok': True, 'peer_response': 'Invented reply'}

        cfg = config.load(config.DEFAULT_CONFIG_PATH)
        cfg.raw = copy.deepcopy(cfg.raw)
        cfg.raw['peers']['codex']['allowed_source_classifications'] = ['public']
        adapters = {'claude': ReadyPeer(), 'codex': ReadyPeer()}

        class FakeServer:
            server_port = 12345

            def __init__(self, address, handler):
                self.handler = handler

        dispatcher = Dispatcher(self.store, adapters)
        with patch('agent_bridge.chat.server.ThreadingHTTPServer', FakeServer):
            server = create_server(self.store, dispatcher, 'test-token', policy=RoomPolicy(cfg))

        def request(body):
            handler = object.__new__(server.handler)
            handler.server = server
            handler.command = 'POST'
            handler.path = f'/api/rooms/{self.room}/messages'
            handler.headers = {'Host': '127.0.0.1:12345', 'Origin': 'http://127.0.0.1:12345',
                               'Authorization': 'Bearer test-token', 'Content-Type': 'application/json',
                               'Content-Length': str(len(json.dumps(body).encode()))}
            handler.rfile = BytesIO(json.dumps(body).encode())
            response = []
            handler.send = lambda status, data, content_type='application/json': response.append((status, data))
            handler.handle_request()
            return response[0]

        self.store.submit(self.room, 'earlier', 'Invented internal planning note', [], 'internal', mode='note')
        body = {'request_id': 'restricted', 'text': 'Invented public follow-up',
                'recipients': ['codex'], 'classification': 'public'}
        self.assertEqual(request(body)[0], 400)
        snapshot = self.store.snapshot(self.room)
        self.assertEqual([message['text'] for message in snapshot['messages']], ['Invented internal planning note'])
        self.assertEqual(snapshot['jobs'], [])
        with self.store.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM requests WHERE room=? AND id=?',
                                         (self.room, 'restricted')).fetchone())

        body.update(request_id='allowed', recipients=['claude'])
        self.assertEqual(request(body)[0], 200)
        snapshot = self.store.snapshot(self.room)
        self.assertEqual([message['text'] for message in snapshot['messages']],
                         ['Invented internal planning note', 'Invented public follow-up'])
        self.assertEqual([job['target'] for job in snapshot['jobs']], ['claude'])
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(adapters['claude'].classification, 'internal')
