"""Launch the local room; model calls remain gated by bridge evidence."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import threading
import urllib.request
import webbrowser
from .. import config, store as bridge_store
from .storage import RoomStore
from .dispatch import Dispatcher
from .adapters import BridgeAdapter
from .hermes import HermesAdapter
from .grok import GrokAdapter
from .rounds import PeerRounds
from .policy import RoomPolicy, room_config
from .server import create_server
from .windows_security import prepare_private_directory, verify_private_directory

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def _local_opener():
    # The room bearer token must never go through an environment proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def build_app(root: Path, allow_client: bool = False, hermes_executable: Path | None = None, grok_state_dir: Path | None = None):
    bridge_store.set_umask()
    root = Path(root).resolve()
    prepare_private_directory(root)
    state = root / 'bridge'
    prepare_private_directory(state)
    candidate = root / 'broker.json'
    if candidate.exists():
        cfg = config.load(str(candidate))
        if Path(cfg.state_root).resolve() != state:
            raise ValueError('Room bridge state must stay inside its dedicated private directory')
    else:
        raw = copy.deepcopy(config.load().raw)
        raw['state_root'] = str(state)
        cfg = config.Config(raw, str(candidate))
    policy = RoomPolicy(cfg, allow_client)
    cfg = room_config(cfg, policy)
    participants = list(config.PEERS)
    verify_private_directory(root)
    adapters = {p: BridgeAdapter(cfg, p, root / 'canary-results.json') for p in participants}
    if hermes_executable is not None:
        hermes_executable = Path(hermes_executable).resolve(strict=True)
        if not hermes_executable.is_file():
            raise ValueError('Hermes executable must be a file')
        adapters['hermes'] = HermesAdapter(hermes_executable, root / 'hermes', policy)
        if adapters['hermes'].status().get('state') == 'ready':
            participants.append('hermes')
    if grok_state_dir is not None:
        grok_root = Path(grok_state_dir).resolve()
        if grok_root == root or grok_root in root.parents or root in grok_root.parents:
            raise ValueError('Grok queue directory must be separate from the room state directory')
        adapters['grok'] = GrokAdapter(grok_root, policy)
        if adapters['grok'].status().get('state') == 'ready':
            participants.append('grok')
    elif 'grok' in adapters:
        del adapters['grok']
    room_store = RoomStore(root / 'chat.sqlite', max_chars=min(12000, cfg.prompt_budget('start') - 2000), participants=tuple(participants))
    if not room_store.rooms():
        room_store.create_room('My agents')
    room_store.recover_interrupted()
    dispatcher = Dispatcher(room_store, adapters)
    token = secrets.token_urlsafe(32)
    # Grok communicates only through its local queue helper; it has no MCP peer client.
    peer_participants = [p for p in participants if p != 'grok']
    round_adapters = {p: adapters[p] for p in peer_participants}
    rounds = PeerRounds(room_store, round_adapters, policy)
    rounds_token = {caller: secrets.token_urlsafe(32) for caller in peer_participants}
    server = create_server(room_store, dispatcher, token, policy=policy, rounds=rounds, rounds_token=rounds_token)
    server.peer_rounds, server.rounds_token = rounds, rounds_token
    return server, room_store, dispatcher, token


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local internal agent room')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.agent-bridge' / 'chat')
    parser.add_argument('--hermes-executable', type=Path, help='Optional installed Hermes CLI; always uses its default profile')
    parser.add_argument('--grok-state-dir', type=Path, help='Opt in to the existing Grok Bot queue at this private state directory')
    args = parser.parse_args(argv)
    root = args.state_dir.resolve()
    prepare_private_directory(root)
    runtime_path = root / 'runtime.json'
    # A protected instance lock keeps a second launch from recovering live jobs.
    try:
        lock = bridge_store.file_lock(str(root / 'instance.lock'), timeout=0.2)
        lock.__enter__()
    except Exception:
        try:
            runtime = json.loads(runtime_path.read_text())
            url = f"http://127.0.0.1:{int(runtime['port'])}"
            request = urllib.request.Request(url + '/api/rooms', headers={'Authorization': 'Bearer ' + runtime['token']})
            with _local_opener().open(request, timeout=3) as response:
                if response.status != 200:
                    raise ValueError('Existing room is not responding')
            if args.open:
                webbrowser.open(url + '/#token=' + runtime['token'])
            return 0
        except Exception:
            raise SystemExit('Another room instance is starting or not responding. Retry shortly.')
    server = dispatcher = None
    try:
        server, _, dispatcher, token = build_app(root, hermes_executable=args.hermes_executable, grok_state_dir=args.grok_state_dir)
        bridge_store.atomic_write_json(str(runtime_path), {'port': server.server_port, 'token': token})
        bridge_store.atomic_write_json(str(root / 'peer-runtime.json'), {'port': server.server_port, 'tokens': server.rounds_token, 'participants': list(server.peer_rounds.participants)})
        thread = threading.Thread(target=dispatcher.loop, daemon=True)
        thread.start()
        threading.Thread(target=server.peer_rounds.loop, daemon=True).start()
        if args.open:
            webbrowser.open(f'http://127.0.0.1:{server.server_port}/#token={token}')
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if dispatcher:
            dispatcher.closed.set()
        if server:
            server.peer_rounds.closed.set()
            server.server_close()
        runtime_path.unlink(missing_ok=True)
        (root / 'peer-runtime.json').unlink(missing_ok=True)
        lock.__exit__(None, None, None)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
