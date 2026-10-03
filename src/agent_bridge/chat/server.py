"""Loopback-only authenticated JSON API and explicit static asset allowlist."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit
from .storage import PARTICIPANTS
from .policy import RoomPolicy

ASSETS = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/pending.js': ('pending.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
ASSETS.update({'/peer-rounds': ('peer-rounds.html', 'text/html'), '/peer-rounds.js': ('peer-rounds.js', 'text/javascript')})


def create_server(store, dispatcher, token: str, port: int = 0, policy=None, rounds=None, rounds_token=None) -> ThreadingHTTPServer:
    policy = policy or RoomPolicy(False)
    participant_ids = tuple(getattr(rounds, 'participants', None) or getattr(store, 'participants', PARTICIPANTS))
    cache, cache_lock = {}, threading.Lock()

    def round_caller(authorization):
        if not rounds_token:
            return None
        if isinstance(rounds_token, dict):
            for caller, value in rounds_token.items():
                if secrets.compare_digest(authorization, 'Bearer ' + value):
                    return caller
            return None
        return '__legacy__' if secrets.compare_digest(authorization, 'Bearer ' + rounds_token) else None

    def participants():
        with cache_lock:
            if time.monotonic() - cache.get('at', -100) > 15:
                cache['value'] = [{'id': p, **(dispatcher.adapters[p].status() if p in dispatcher.adapters else
                                  {'state': 'unavailable', 'detail': 'Adapter not connected yet'})} for p in participant_ids]
                cache['at'] = time.monotonic()
            return cache['value']

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            pass

        def send(self, status, data, content_type='application/json'):
            content = json.dumps(data, ensure_ascii=False).encode() if content_type == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(content)

        def handle_request(self):
            expected = f'127.0.0.1:{self.server.server_port}'
            origin = 'http://' + expected
            if self.headers.get('Host') != expected:
                return self.send(403, {'error': 'Local origin required'})
            path = urlsplit(self.path).path
            # Browser same-origin GETs normally omit Origin.  A supplied
            # foreign Origin is still refused, and POST below remains strict.
            if path.startswith('/api/') and self.headers.get('Origin') not in (None, origin):
                return self.send(403, {'error': 'Local origin required'})
            if self.command == 'GET' and path in ASSETS:
                name, mime = ASSETS[path]
                return self.send(200, (Path(__file__).parent / 'static' / name).read_bytes(), mime)
            if not path.startswith('/api/'):
                return self.send(404, {'error': 'Not found'})
            authorization = self.headers.get('Authorization', '')
            human = secrets.compare_digest(authorization, 'Bearer ' + token)
            matched_caller = round_caller(authorization)
            round_client = matched_caller is not None
            if not human and not (round_client and path.startswith('/api/peer-rounds/')):
                return self.send(403, {'error': 'Run start_chat.py --open to authenticate this window'})
            if path.startswith('/api/peer-rounds/') and not round_client:
                return self.send(403, {'error': 'Peer round client authentication required'})
            if self.command == 'POST' and self.headers.get('Origin') != origin:
                return self.send(403, {'error': 'Local origin required'})
            try:
                body = {}
                if self.command == 'POST':
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 65536:
                        return self.send(413, {'error': 'Request too large or empty'})
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.send(400, {'error': 'JSON required'})
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError('JSON object required')
                parts = path.strip('/').split('/')
                if rounds is not None and parts[:2] == ['api', 'peer-rounds']:
                    if len(parts) != 4 or parts[2] not in participant_ids:
                        return self.send(404, {'error': 'Not found'})
                    caller, action = parts[2:]
                    if round_client and matched_caller != '__legacy__' and matched_caller != caller:
                        return self.send(403, {'error': 'Caller token does not match this peer'})
                    if self.command == 'POST' and action == 'prepare':
                        result = rounds.prepare(caller, body)
                    elif self.command == 'GET' and action == 'status':
                        result = {'ok': True, 'peers': [p for p in participants() if p['id'] != caller]}
                    elif self.command == 'GET':
                        result = rounds.read(caller, action)
                    else:
                        return self.send(404, {'error': 'Not found'})
                elif rounds is not None and path == '/api/peer-approvals' and self.command == 'GET':
                    result = rounds.pending()
                elif rounds is not None and len(parts) == 3 and parts[:2] == ['api', 'peer-approvals'] and self.command == 'POST':
                    if set(body) != {'approve'} or not isinstance(body['approve'], bool):
                        raise ValueError('Explicit approval or rejection required')
                    result = rounds.approve(parts[2], reject=not body['approve'])
                elif path == '/api/participants' and self.command == 'GET':
                    result = {'participants': participants(), 'allow_client': policy.allow_client}
                elif path == '/api/rooms':
                    result = store.rooms() if self.command == 'GET' else store.create_room(body['title'])
                elif len(parts) >= 3 and parts[:2] == ['api', 'rooms']:
                    room = parts[2]
                    if len(parts) == 3 and self.command == 'GET':
                        result = store.snapshot(room)
                        result['preferences'] = store.preferences(room)
                    elif len(parts) == 4 and self.command == 'POST':
                        if parts[3] == 'messages':
                            label = body['classification']
                            policy.effective_classification([label])
                            if not isinstance(body['recipients'], list):
                                raise ValueError('Recipients must be a list')
                            for target in body['recipients']:
                                policy.authorize(target, label)
                                adapter = dispatcher.adapters.get(target)
                                if adapter is None:
                                    raise ValueError('Selected agent is not connected')
                                state = adapter.status()
                                if state['state'] != 'ready':
                                    raise ValueError(state.get('detail', state['state']))
                            result = store.submit(room, body['request_id'], body['text'], body['recipients'], label, mode=body.get('mode', 'chat'), lead=body.get('lead'))
                        elif parts[3] == 'rename':
                            result = store.rename_room(room, body['title'])
                        elif parts[3] == 'delete':
                            if body.get('confirm') is not True:
                                raise ValueError('Delete confirmation required')
                            if rounds is not None:
                                rounds.delete_room(room)
                            dispatcher.cancel_room(room)
                            result = store.delete_room(room)
                        elif parts[3] == 'preferences':
                            result = store.preferences(room, body['lead'], body['participants'])
                        elif parts[3] == 'stop':
                            if rounds is not None:
                                rounds.stop_room(room)
                            result = dispatcher.stop(room)
                        elif parts[3] == 'reset':
                            if body['target'] not in participant_ids:
                                raise ValueError('Unknown target')
                            store.reset_session(room, body['target'])
                            result = {'ok': True}
                        else:
                            return self.send(404, {'error': 'Not found'})
                    else:
                        return self.send(404, {'error': 'Not found'})
                else:
                    return self.send(404, {'error': 'Not found'})
                self.send(200, result)
            except (ValueError, KeyError, TypeError):
                self.send(400, {'error': 'Invalid request, unavailable agent, or sharing policy restriction. Check participant status and message classification.'})
            except Exception:
                self.send(500, {'error': 'Local service error; your draft has been kept. Restart the launcher if this persists.'})

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server
