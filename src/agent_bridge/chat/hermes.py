"""Default-profile Hermes consultations through its supported JSONL CLI.

Hermes is an optional third-party provider. The room only starts it when the
operator explicitly supplies the installed CLI path and has completed the
local verification record for that installation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from .. import store
from .windows_security import prepare_private_directory, verify_private_directory


_ENVIRONMENT_KEYS = (
    'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'TEMP', 'TMP',
    'LANG', 'LC_ALL', 'LC_CTYPE', 'PYTHONIOENCODING', 'PYTHONUTF8',
)
OUTPUT_LIMIT = 4_000_000
ERROR_LIMIT = 1_000_000
JOB_RETENTION_SECONDS = 24 * 3600


def child_environment(source=None) -> dict[str, str]:
    """Return the small, explicit environment Hermes needs to start."""
    source = os.environ if source is None else source
    return {key: source[key] for key in _ENVIRONMENT_KEYS if key in source}


def decode_result(output: str, returncode: int) -> tuple[str, str]:
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict) and event.get('type') == 'result':
                events.append(event)
        except ValueError:
            continue
    if returncode != 0 or len(events) != 1 or events[0].get('exit_code') != 0:
        raise ValueError('Hermes did not return a successful final response. Check its default-profile login.')
    event = events[0]
    if not isinstance(event.get('text'), str) or not event['text'].strip() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', str(event.get('session_id', ''))):
        raise ValueError('Hermes returned an incomplete response or invalid session ID')
    return event['text'], event['session_id']


class HermesAdapter:
    timeout = 135

    def __init__(self, executable: Path, root: Path, policy):
        self.executable, self.root, self.policy = executable, root, policy
        prepare_private_directory(root)
        self._cleanup_stale_jobs()

    def _cleanup_stale_jobs(self):
        cutoff = time.time() - JOB_RETENTION_SECONDS
        if not self.root.exists():
            return
        for child in self.root.iterdir():
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)

    def fingerprint(self):
        files = [self.executable]
        source = self.executable.parent.parent / 'hermes-agent' / 'hermes_cli'
        implementation = [source / name for name in ('main.py', 'stream_json.py', 'cli_init_mixin.py')]
        if not all(path.is_file() for path in implementation):
            raise ValueError('Hermes implementation cannot be verified for this installation layout')
        files += implementation
        digest = hashlib.sha256()
        for file in files:
            digest.update(file.read_bytes())
        return digest.hexdigest()

    def status(self) -> dict:
        try:
            self.fingerprint()
            verify_private_directory(self.root)
            self._cleanup_stale_jobs()
        except (OSError, ValueError):
            return {'state': 'verification_required', 'detail': 'Hermes implementation layout and private state directory could not be verified. Unsupported installation layouts remain disconnected.'}
        return {'state': 'ready', 'detail': 'Default Hermes profile; room sessions support clarification only, not filesystem or messaging tools.'}

    @staticmethod
    def _terminate_process_group(process):
        if process.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=5, shell=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _run_process(self, argv, prompt, job):
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=job, env=child_environment(), shell=False,
            creationflags=flags, start_new_session=(os.name != 'nt'))
        output, errors, overflow = bytearray(), bytearray(), []
        overflow_event = threading.Event()

        def drain(stream, target, limit, label):
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                room = limit - len(target)
                if len(chunk) > room:
                    target.extend(chunk[:max(0, room)])
                    overflow.append(label)
                    overflow_event.set()
                    return
                target.extend(chunk)

        readers = [threading.Thread(target=drain, args=(process.stdout, output, OUTPUT_LIMIT, 'stdout'), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, errors, ERROR_LIMIT, 'stderr'), daemon=True)]
        for reader in readers:
            reader.start()
        try:
            process.stdin.write(prompt.encode('utf-8'))
            process.stdin.close()
            deadline = time.monotonic() + 120
            while True:
                if overflow_event.is_set():
                    self._terminate_process_group(process)
                    process.wait(timeout=5)
                    raise ValueError('Hermes response exceeded the room output limit')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process_group(process)
                    process.wait(timeout=5)
                    raise ValueError('Hermes timed out; its provider call may still consume usage') from None
                try:
                    returncode = process.wait(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            for reader in readers:
                reader.join(timeout=5)
            if process.poll() is None:
                self._terminate_process_group(process)
        if overflow:
            raise ValueError('Hermes response exceeded the room output limit')
        return output.decode('utf-8', errors='replace'), returncode

    def _call(self, prompt, classification, session=None, *, selected_only=False):
        if self.status()['state'] != 'ready':
            raise ValueError('Hermes verification is required')
        self.policy.authorize('hermes', classification)
        if not isinstance(prompt, str) or not 0 < len(prompt) <= 16000:
            raise ValueError('Hermes room context exceeds its 16000-character limit')
        if session and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', session):
            raise ValueError('Invalid Hermes session')
        job_id = str(uuid.uuid4())
        job = self.root / job_id
        prepare_private_directory(job)
        argv = [str(self.executable), '--profile', 'default', 'chat', '--query-file', '-', '--oneshot',
                '--format', 'stream-json', '--toolsets', 'clarify', '--max-turns', '1', '--run-budget', '90', '--source', 'agent-room']
        if selected_only:
            argv.append('--ignore-rules')
        if session:
            argv += ['--resume', session, '--no-restore-cwd']
        try:
            raw, returncode = self._run_process(argv, prompt, job)
            text, session_id = decode_result(raw, returncode)
        except Exception:
            shutil.rmtree(job, ignore_errors=True)
            raise
        store.atomic_write_json(str(job / 'result.json'), {'ok': True, 'peer_response': text})
        return {'ok': True, 'job_id': job_id, 'conversation_id': session_id}

    def start(self, prompt: str, classification: str) -> dict:
        return self._call(prompt, classification)

    def start_selected(self, prompt: str, classification: str) -> dict:
        return self._call(prompt, classification, selected_only=True)

    def continue_(self, conversation_id: str, prompt: str, classification: str) -> dict:
        return self._call(prompt, classification, conversation_id)

    def poll(self, job_id: str) -> dict:
        uuid.UUID(job_id)
        return {'ok': True, 'status': 'complete'}

    def read(self, job_id: str) -> dict:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError('Invalid job ID')
        path = self.root / job_id
        result = json.loads((path / 'result.json').read_text(encoding='utf-8'))
        shutil.rmtree(path, ignore_errors=True)
        return result
