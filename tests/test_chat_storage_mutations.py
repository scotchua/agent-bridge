import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_bridge.chat.storage import RoomStore


class StorageMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'room.sqlite'
        self.store = RoomStore(self.path)
        self.room = self.store.create_room('Velluna Studio')['id']

    def test_init_rejects_duplicate_participants(self):
        with self.assertRaises(ValueError):
            RoomStore(Path(self.temp.name) / 'duplicate.sqlite', participants=('claude', 'claude'))

    def test_init_rejects_unknown_database_version(self):
        with self.store.db() as db:
            db.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(ValueError, 'Unsupported chat database version'):
            RoomStore(self.path)

    def test_db_enforces_foreign_keys(self):
        with self.store.db() as db:
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_create_room_accepts_100_character_title(self):
        title = 'V' * 100
        self.assertEqual(self.store.create_room(title)['title'], title)

    def test_submit_accepts_exact_message_limit(self):
        store = RoomStore(self.path, max_chars=4)
        result = store.submit(self.room, 'message-limit', 'Four', [], 'synthetic')
        self.assertEqual(store.snapshot(self.room)['messages'][0]['seq'], result['message_seq'])

    def test_submit_accepts_100_character_request_id(self):
        result = self.store.submit(self.room, 'r' * 100, 'hello', [], 'synthetic')
        self.assertEqual(len(self.store.snapshot(self.room)['messages']), 1)
        self.assertIsInstance(result['message_seq'], int)

    def test_submit_deduplicates_and_sorts_recipients(self):
        result = self.store.submit(self.room, 'repeat', 'Review', ['codex', 'claude', 'codex'], 'synthetic')
        jobs = self.store.snapshot(self.room)['jobs']
        self.assertEqual(len(result['job_ids']), 2)
        self.assertEqual([job['target'] for job in jobs], ['claude', 'codex'])

    def test_context_includes_first_message_without_session(self):
        self.store.submit(self.room, 'first', 'First note', ['claude'], 'synthetic')
        job = self.store.claim_next()
        rows, session, labels = self.store.context(job)
        self.assertEqual([row['text'] for row in rows], ['First note'])
        self.assertIsNone(session)
        self.assertEqual(labels, ['synthetic'])

    def test_stop_reports_cancelled_job_count(self):
        self.store.submit(self.room, 'stop', 'Pause', ['claude', 'codex'], 'synthetic')
        self.assertEqual(self.store.stop(self.room), {'cancelled': 2})

    def test_reset_session_rejects_pending_job(self):
        self.store.submit(self.room, 'pending', 'Question', ['claude'], 'synthetic')
        with self.assertRaisesRegex(ValueError, 'Stop pending work'):
            self.store.reset_session(self.room, 'claude')

    def test_reset_session_removes_previous_conversation(self):
        self.store.submit(self.room, 'answered', 'Question', ['claude'], 'synthetic')
        job = self.store.claim_next()
        self.store.finish(job, 'Answer', 'synthetic', 'invented-conversation')
        self.assertEqual(len(self.store.snapshot(self.room)['sessions']), 1)
        self.store.reset_session(self.room, 'claude')
        self.assertEqual(self.store.snapshot(self.room)['sessions'], [])

    def test_preferences_rejects_unregistered_lead(self):
        with self.assertRaisesRegex(ValueError, 'Invalid room preferences'):
            self.store.preferences(self.room, 'unknown', ['claude'])

    def test_rename_room_accepts_100_character_title(self):
        title = 'R' * 100
        self.store.rename_room(self.room, title)
        self.assertEqual(self.store.rooms()[0]['title'], title)

    def test_rename_room_reports_success(self):
        self.assertEqual(self.store.rename_room(self.room, 'New Velluna room'), {'ok': True})

    def test_delete_room_reports_success(self):
        self.assertEqual(self.store.delete_room(self.room), {'ok': True})


if __name__ == '__main__':
    unittest.main()
