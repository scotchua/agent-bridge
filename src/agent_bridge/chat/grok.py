"""Queue bridge for an existing Grok Bot's approved local computer tool.

This module neither logs into Grok nor impersonates a model response. The
operator's local Bot must claim one queued job and explicitly save one reply.
No subprocess is launched, so no provider environment is inherited here.
"""
import json
import math
import shutil
import time
import uuid
from pathlib import Path

from .. import store
from .windows_security import prepare_private_directory, verify_private_directory

JOB_RETENTION_SECONDS = 24 * 3600
REQUIRED_MANIFEST_KEYS = {'product', 'version', 'tools', 'allowlist'}
ALLOWED_COMMANDS = {
    'grok_room.py next --wait',
    'grok_room.py reply <job-id>',
}


def job_path(root, job_id):
    if str(uuid.UUID(job_id)) != job_id:
        raise ValueError('Invalid job ID')
    return Path(root) / (job_id + '.json')


def _quarantine(path):
    quarantine = path.parent / 'quarantine'
    prepare_private_directory(quarantine)
    target = quarantine / (path.name + '.' + uuid.uuid4().hex + '.invalid')
    try:
        path.replace(target)
    except OSError:
        path.unlink(missing_ok=True)


def _load_record(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        job_id = record.get('job_id') if isinstance(record, dict) else None
        expires = record.get('expires') if isinstance(record, dict) else None
        if (not isinstance(record, dict) or not isinstance(job_id, str)
                or str(uuid.UUID(job_id)) != job_id or path.stem != job_id
                or record.get('status') not in ('queued', 'running', 'complete', 'cancelled', 'timed_out')
                or type(expires) not in (int, float) or not math.isfinite(expires)
                or (record.get('status') == 'complete' and (not isinstance(record.get('text'), str)
                                                            or not 0 < len(record['text'].strip()) <= 100000))):
            raise ValueError('invalid queue record')
        return record
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _quarantine(path)
        return None


def _cleanup(root):
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with store.file_lock(str(root / 'queue.lock')):
        for path in root.glob('*.json'):
            if path.name in ('heartbeat.json', 'bot-manifest.json'):
                continue
            try:
                record = _load_record(path)
                if record is None or path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        quarantine = root / 'quarantine'
        if quarantine.is_dir():
            for path in quarantine.iterdir():
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue


class GrokAdapter:
    timeout = 150

    def __init__(self, root: Path, policy):
        self.root, self.policy = root, policy
        store.set_umask()
        prepare_private_directory(root)
        _cleanup(root)
        with store.file_lock(str(root / 'queue.lock')):
            for path in root.glob('*.json'):
                if path.name in ('heartbeat.json', 'bot-manifest.json'):
                    continue
                record = _load_record(path)
                if record and record.get('status') in ('queued', 'running'):
                    record['status'] = 'cancelled'
                    store.atomic_write_json(str(path), record)

    def status(self):
        try:
            heartbeat = json.loads((self.root / 'heartbeat.json').read_text(encoding='utf-8'))
            if not 0 <= time.time() - heartbeat['at'] < 90:
                raise ValueError('Stale')
            manifest = json.loads((self.root / 'bot-manifest.json').read_text(encoding='utf-8'))
            if (set(manifest) < REQUIRED_MANIFEST_KEYS or manifest.get('product') != 'Grok Bot'
                    or not isinstance(manifest.get('version'), str) or not isinstance(manifest.get('tools'), list)
                    or not isinstance(manifest.get('allowlist'), list)
                    or set(manifest['allowlist']) != ALLOWED_COMMANDS
                    or set(manifest['tools']) != ALLOWED_COMMANDS):
                raise ValueError('Unapproved Bot manifest')
            verify_private_directory(self.root)
            _cleanup(self.root)
        except (OSError, ValueError, KeyError, TypeError):
            return {'state': 'connection_required', 'detail': 'Open the approved Grok Bot, create the manifest in docs/GROK-ROOM-SETUP.md, and run its local connection command. No Grok session is attached yet.'}
        return {'state': 'ready', 'detail': 'An operator-declared Grok Bot manifest and a local check-in are present. The Bot may see room text and return one untrusted reply; it cannot approve rounds or call this bridge.'}

    def start(self, prompt, classification):
        return self.continue_(str(uuid.uuid4()), 'Fresh room consultation: base this answer on the supplied room history, not earlier Bot conversations. This request does not erase your underlying Bot memory.\n' + prompt, classification)

    def continue_(self, conversation_id, prompt, classification):
        if self.status()['state'] != 'ready':
            raise ValueError('Grok Bot is not listening. Run its local connection command first.')
        self.policy.authorize('grok', classification)
        if not isinstance(prompt, str) or not 0 < len(prompt) <= 32000:
            raise ValueError('Grok room context exceeds its limit')
        job_id = str(uuid.uuid4())
        record = {'job_id': job_id, 'conversation_id': conversation_id, 'prompt': prompt,
                  'classification': classification, 'status': 'queued', 'expires': time.time() + 120}
        store.atomic_write_json(str(job_path(self.root, job_id)), record)
        return {'ok': True, 'job_id': job_id, 'conversation_id': conversation_id}

    def poll(self, job_id):
        with store.file_lock(str(self.root / 'queue.lock')):
            path = job_path(self.root, job_id)
            record = _load_record(path)
            if record is None:
                raise ValueError('Unknown request')
            status = record['status']
            if status in ('queued', 'running') and time.time() >= record['expires']:
                status = 'timed_out'
                record['status'] = status
                store.atomic_write_json(str(path), record)
            return {'ok': True, 'status': status}

    def read(self, job_id):
        with store.file_lock(str(self.root / 'queue.lock')):
            path = job_path(self.root, job_id)
            record = _load_record(path)
            if record is None:
                raise ValueError('Unknown request')
            if record['status'] != 'complete':
                return {'ok': False, 'error_hint': 'Grok did not respond in time. Reconnect the Bot and request a new reply.'}
            result = {'ok': True, 'peer_response': record['text']}
            path.unlink(missing_ok=True)
            return result

    def cancel(self, job_id):
        with store.file_lock(str(self.root / 'queue.lock')):
            path = job_path(self.root, job_id)
            record = _load_record(path)
            if record is None:
                return
            if record['status'] in ('queued', 'running'):
                record['status'] = 'cancelled'
                store.atomic_write_json(str(path), record)
                path.unlink(missing_ok=True)


def receive(root: Path, wait: int = 45):
    store.set_umask()
    prepare_private_directory(root)
    _cleanup(root)
    deadline = time.monotonic() + max(0, min(wait, 45))
    while True:
        store.atomic_write_json(str(root / 'heartbeat.json'), {'at': time.time()})
        with store.file_lock(str(root / 'queue.lock')):
            for path in sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime):
                if path.name in ('heartbeat.json', 'bot-manifest.json'):
                    continue
                record = _load_record(path)
                if not record or record.get('status') != 'queued' or record.get('expires', 0) <= time.time():
                    continue
                record['status'] = 'running'
                store.atomic_write_json(str(path), record)
                return record
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def respond(root: Path, job_id: str, text: str):
    if not isinstance(text, str) or not 0 < len(text.strip()) <= 100000:
        raise ValueError('Reply must contain 1–100000 characters')
    verify_private_directory(root)
    with store.file_lock(str(root / 'queue.lock')):
        path = job_path(root, job_id)
        try:
            record = _load_record(path)
            if record is None:
                raise ValueError('Unknown request')
        except FileNotFoundError:
            raise ValueError('Unknown request') from None
        if record['status'] != 'running' or record['expires'] <= time.time():
            raise ValueError('Request is expired, stopped, or already answered')
        record.update(status='complete', text=text)
        store.atomic_write_json(str(path), record)
