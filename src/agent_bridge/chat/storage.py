"""Transactional chat history. No model execution occurs in this module."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

PARTICIPANTS = ('claude', 'codex')
LABELS = ('public', 'synthetic', 'internal')


class RoomStore:
    def __init__(self, path: Path, max_chars: int = 12000, participants=None):
        self.path, self.max_chars = Path(path), max_chars
        # Optional providers are added by the launcher only when explicitly enabled.
        self.participants = tuple(participants or ('claude', 'codex'))
        if not self.participants or len(set(self.participants)) != len(self.participants):
            raise ValueError('At least one distinct participant is required')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise ValueError('Unsupported chat database version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY, title TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    room TEXT REFERENCES rooms(id), author TEXT, text TEXT, classification TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS requests(room TEXT, id TEXT, payload TEXT, result TEXT, PRIMARY KEY(room,id));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, room TEXT, target TEXT,
                    through_seq INTEGER, status TEXT, error TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS sessions(room TEXT, target TEXT, conversation TEXT,
                    cursor INTEGER DEFAULT 0, PRIMARY KEY(room,target));
                CREATE TABLE IF NOT EXISTS room_preferences(room TEXT PRIMARY KEY, lead TEXT, participants TEXT);
                CREATE TABLE IF NOT EXISTS discussion_jobs(job TEXT PRIMARY KEY, round_id TEXT, turn INTEGER, role TEXT);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_room(self, title: str) -> dict:
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError('Room title must contain 1–100 characters')
        room = {'id': str(uuid.uuid4()), 'title': title.strip(), 'created': time.time()}
        with self.db() as db:
            db.execute('INSERT INTO rooms VALUES(:id,:title,:created)', room)
        return room

    def rooms(self) -> list:
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM rooms ORDER BY created')]

    def _room(self, db, room):
        if not isinstance(room, str) or str(uuid.UUID(room)) != room:
            raise ValueError('Invalid room ID')
        if not db.execute('SELECT 1 FROM rooms WHERE id=?', (room,)).fetchone():
            raise ValueError('Room not found')

    def submit(self, room_id: str, request_id: str, text: str, recipients: list[str], classification: str, mode: str = 'chat', lead: str = None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > self.max_chars:
            raise ValueError(f'Message must contain 1–{self.max_chars} characters')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('Invalid request ID')
        if not isinstance(recipients, list) or any(p not in self.participants for p in recipients):
            raise ValueError('Unknown recipient')
        if classification not in LABELS:
            raise ValueError('Unsupported classification; credentials and secrets are excluded')
        if mode not in ('chat', 'discuss', 'note'):
            raise ValueError('Unknown conversation mode')
        recipients = sorted(set(recipients))
        if mode == 'note' and recipients:
            raise ValueError('Notes cannot target agents')
        if mode == 'discuss' and (len(recipients) < 2 or lead not in recipients):
            raise ValueError('Select at least two participants, including the lead')
        payload = json.dumps([text, recipients, classification] +
                             ([mode, lead] if mode == 'discuss' else [mode] if mode == 'note' else []))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._room(db, room_id)
            old = db.execute('SELECT * FROM requests WHERE room=? AND id=?', (room_id, request_id)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('Request ID already used for different content')
                return json.loads(old['result'])
            active = db.execute("SELECT j.id,d.round_id FROM jobs j LEFT JOIN discussion_jobs d ON d.job=j.id WHERE j.room=? AND j.status IN ('queued','running')", (room_id,)).fetchall()
            if active and (mode == 'discuss' or any(r['round_id'] for r in active)):
                raise ValueError('Wait for this round or stop it before sending another message')
            now = time.time()
            seq = db.execute('INSERT INTO messages(room,author,text,classification,created) VALUES(?,?,?,?,?)',
                             (room_id, 'Human', text, classification, now)).lastrowid
            ids = []
            if mode == 'note':
                result = {'message_seq': seq, 'job_ids': ids}
                db.execute('INSERT INTO requests VALUES(?,?,?,?)', (room_id, request_id, payload, json.dumps(result)))
                return result
            round_id = str(uuid.uuid4()) if mode == 'discuss' else None
            turns = [(p, 'perspective') for p in recipients]
            if round_id:
                turns.append((lead, 'summary'))
            for turn, (peer, role) in enumerate(turns):
                job_id = str(uuid.uuid4())
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', (job_id, room_id, peer, seq, 'queued', None, now))
                if round_id:
                    db.execute('INSERT INTO discussion_jobs VALUES(?,?,?,?)', (job_id, round_id, turn, role))
                ids.append(job_id)
            result = {'message_seq': seq, 'job_ids': ids}
            db.execute('INSERT INTO requests VALUES(?,?,?,?)', (room_id, request_id, payload, json.dumps(result)))
            return result

    def snapshot(self, room_id: str) -> dict:
        with self.db() as db:
            self._room(db, room_id)
            return {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE room=? ORDER BY {order}', (room_id,))]
                    for table, order in [('messages', 'seq'), ('jobs', 'created'), ('sessions', 'target')]}

    def recover_interrupted(self) -> int:
        with self.db() as db:
            return db.execute("UPDATE jobs SET status='failed', error='Interrupted by restart. Request a new response to retry.' WHERE status IN ('queued','running')").rowcount

    def claim_next(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT j.*,d.round_id,d.turn,d.role FROM jobs j LEFT JOIN discussion_jobs d ON d.job=j.id WHERE j.status='queued' AND NOT EXISTS (SELECT 1 FROM jobs active WHERE active.room=j.room AND active.target=j.target AND active.status='running') AND NOT EXISTS (SELECT 1 FROM discussion_jobs prior JOIN jobs pj ON pj.id=prior.job WHERE prior.round_id=d.round_id AND prior.turn<d.turn AND pj.status IN ('queued','running')) ORDER BY j.created,j.rowid LIMIT 1").fetchone()
            if row is None:
                return None
            job = dict(row)
            if job['round_id']:
                job['through_seq'] = db.execute('SELECT MAX(seq) FROM messages WHERE room=?', (job['room'],)).fetchone()[0]
            db.execute("UPDATE jobs SET status='running',through_seq=? WHERE id=?", (job['through_seq'], job['id']))
            return job

    def context(self, job: dict) -> tuple:
        with self.db() as db:
            session = db.execute('SELECT * FROM sessions WHERE room=? AND target=?', (job['room'], job['target'])).fetchone()
            cursor = session['cursor'] if session else 0
            rows = [dict(r) for r in db.execute('SELECT * FROM messages WHERE room=? AND seq>? AND seq<=? ORDER BY seq', (job['room'], cursor, job['through_seq']))]
            labels = [r[0] for r in db.execute('SELECT classification FROM messages WHERE room=? AND seq<=?', (job['room'], job['through_seq']))]
            return rows, dict(session) if session else None, labels

    def is_running(self, job_id: str) -> bool:
        with self.db() as db:
            row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            return bool(row and row[0] == 'running')

    def finish(self, job: dict, text: str, classification: str, conversation: str):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM jobs WHERE id=? AND status='running'", (job['id'],)).fetchone():
                return
            db.execute('INSERT INTO messages(room,author,text,classification,created) VALUES(?,?,?,?,?)',
                       (job['room'], job['target'], text, classification, time.time()))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(room,target) DO UPDATE SET conversation=excluded.conversation,cursor=excluded.cursor',
                       (job['room'], job['target'], conversation, job['through_seq']))
            db.execute("UPDATE jobs SET status='completed' WHERE id=?", (job['id'],))

    def fail(self, job: dict, message: str):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE id=? AND status='running'", (message, job['id']))

    def stop(self, room_id: str) -> dict:
        with self.db() as db:
            self._room(db, room_id)
            count = db.execute("UPDATE jobs SET status='cancelled',error='Stopped. A provider call already in progress may still finish and consume usage.' WHERE room=? AND status IN ('queued','running')", (room_id,)).rowcount
            return {'cancelled': count}

    def reset_session(self, room_id: str, target: str):
        with self.db() as db:
            self._room(db, room_id)
            if db.execute("SELECT 1 FROM jobs WHERE room=? AND target=? AND status IN ('queued','running')", (room_id, target)).fetchone():
                raise ValueError('Stop pending work before starting a fresh consultation')
            db.execute('DELETE FROM sessions WHERE room=? AND target=?', (room_id, target))

    def preferences(self, room_id, lead=None, participants=None):
        with self.db() as db:
            self._room(db, room_id)
            if lead is not None:
                if lead not in self.participants or not isinstance(participants, list) or any(p not in self.participants for p in participants):
                    raise ValueError('Invalid room preferences')
                db.execute('INSERT INTO room_preferences VALUES(?,?,?) ON CONFLICT(room) DO UPDATE SET lead=excluded.lead,participants=excluded.participants', (room_id, lead, json.dumps(sorted(set(participants)))))
            row = db.execute('SELECT * FROM room_preferences WHERE room=?', (room_id,)).fetchone()
            if row:
                return {'lead': row['lead'], 'participants': [p for p in json.loads(row['participants']) if p in self.participants]}
            return {'lead': self.participants[0], 'participants': [p for p in self.participants if p in PARTICIPANTS]}
    def rename_room(self, room_id, title):
        if not isinstance(title,str) or not title.strip() or len(title)>100:
            raise ValueError('Room title must contain 1-100 characters')
        with self.db() as db:
            self._room(db,room_id)
            db.execute('UPDATE rooms SET title=? WHERE id=?',(title.strip(),room_id))
        return {'ok':True}

    def delete_room(self, room_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._room(db,room_id)
            db.execute('DELETE FROM discussion_jobs WHERE job IN (SELECT id FROM jobs WHERE room=?)',(room_id,))
            for table in ('messages','requests','jobs','sessions','room_preferences'):
                db.execute(f'DELETE FROM {table} WHERE room=?',(room_id,))
            db.execute('DELETE FROM rooms WHERE id=?',(room_id,))
        return {'ok':True}
