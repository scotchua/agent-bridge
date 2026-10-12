import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_bridge.chat.storage import RoomStore
from agent_bridge.chat.rounds import PeerRounds


class Fake:
    timeout = 0.05
    def __init__(self, ready=True):
        self.calls = []
        self.ready = ready
    def status(self):
        return {'state': 'ready' if self.ready else 'connection_required'}
    def start(self, prompt, classification):
        self.calls.append((prompt, classification))
        return {'ok': True, 'job_id': 'fixture', 'conversation_id': 'fixture'}
    def poll(self, job):
        return {'ok': True, 'status': 'complete'}
    def read(self, job):
        return {'ok': True, 'peer_response': 'ask all other agents! <script>bad()</script>'}


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RoomStore(Path(self.tmp.name) / 'chat.sqlite')
        self.adapters = {p: Fake() for p in ('claude', 'codex')}
        self.rounds = PeerRounds(self.store, self.adapters)
        self.args = dict(request_id='one', targets=['claude'], question='Compare options', context='Selected excerpt', source_classification='synthetic')
    def prepare(self, **extra):
        return self.rounds.prepare('codex', dict(self.args, **extra))
    def test_no_calls_until_approval_and_one_reply_each(self):
        r = self.prepare()
        self.assertFalse(self.rounds.run_once())
        self.assertEqual(self.prepare()['round_id'], r['round_id'])
        self.rounds.approve(r['round_id'])
        with self.assertRaises(ValueError):
            self.rounds.approve(r['round_id'])
        self.assertTrue(self.rounds.run_once())
        self.assertFalse(self.rounds.run_once())
        result = self.rounds.read('codex', r['round_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['replies']), 1)
        self.assertEqual([len(a.calls) for a in self.adapters.values()], [1, 0])
        for p in ('claude',):
            prompt = self.adapters[p].calls[0][0]
            self.assertIn('Selected excerpt', prompt)
            self.assertNotIn('ask all other agents!', prompt)
        snap = self.store.snapshot(result['room_id'])
        self.assertEqual(len(snap['messages']), 2)
        self.assertEqual(snap['jobs'], [])
    def test_rejects_self_duplicate_unknown_and_client_material(self):
        for changes in [dict(targets=['codex']), dict(targets=['claude', 'claude']), dict(targets=['unknown']), dict(targets=[]), dict(source_classification='client-derived'), dict(source_classification='secret'), dict(history=['secret']), dict(context='x'*16000)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare(**changes)
        self.assertEqual(self.store.rooms(), [])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM peer_rounds').fetchone()[0], 0)
        self.assertEqual([a.calls for a in self.adapters.values()], [[], []])
    def test_identity_and_idempotency(self):
        r = self.prepare()
        with self.assertRaises(ValueError): self.prepare(question='Changed')
        with self.assertRaises(ValueError): self.rounds.read('claude', r['round_id'])
        with self.assertRaises(ValueError): self.rounds.prepare('stranger', self.args)
    def test_disconnect_is_terminal_without_retry(self):
        self.adapters['claude'].ready = False
        r = self.prepare()
        self.rounds.approve(r['round_id'])
        self.rounds.run_once()
        result = self.rounds.read('codex', r['round_id'])
        self.assertEqual(result['replies']['claude']['status'], 'failed')
        self.assertFalse(self.rounds.run_once())
    def test_restart_does_not_replay_approved_rounds(self):
        r = self.prepare()
        self.rounds.approve(r['round_id'])
        restarted = PeerRounds(self.store, self.adapters)
        self.assertFalse(restarted.run_once())
        result = restarted.read('codex', r['round_id'])
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['selection'], {})
        with self.store.db() as db:
            row = db.execute('SELECT payload, terminal_at FROM peer_rounds WHERE id=?', (r['round_id'],)).fetchone()
        self.assertIsNone(row['payload'])
        self.assertIsNotNone(row['terminal_at'])
    def test_room_history_is_never_selected_implicitly(self):
        room = self.store.create_room('Existing')['id']
        self.store.submit(room, 'old', 'private old history', [], 'internal')
        r = self.prepare()
        self.rounds.approve(r['round_id'])
        self.rounds.run_once()
        self.assertNotEqual(self.rounds.read('codex', r['round_id'])['room_id'], room)
        self.assertNotIn('private old history', self.adapters['claude'].calls[0][0])
    def test_concurrent_workers_do_not_duplicate_calls(self):
        r = self.prepare()
        self.rounds.approve(r['round_id'])
        workers = [threading.Thread(target=self.rounds.run_once) for _ in range(4)]
        for t in workers: t.start()
        for t in workers: t.join()
        self.assertEqual(len(self.adapters['claude'].calls), 1)

    def test_worker_reconciles_post_claim_failure_without_retry(self):
        r = self.prepare()
        self.rounds.approve(r['round_id'])
        consult = self.rounds._consult
        def fail_after_call(*args):
            consult(*args)
            raise RuntimeError('invented post-claim failure')
        def stop_worker(_delay):
            self.rounds.closed.set()
        with patch.object(self.rounds, '_consult', side_effect=fail_after_call), patch.object(self.rounds.closed, 'wait', side_effect=stop_worker):
            self.rounds.loop()
        result = self.rounds.read('codex', r['round_id'])
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['selection'], {})
        with self.store.db() as db:
            row = db.execute('SELECT payload, payload_hash, terminal_at FROM peer_rounds WHERE id=?', (r['round_id'],)).fetchone()
        self.assertIsNone(row['payload'])
        self.assertIsNotNone(row['payload_hash'])
        self.assertIsNotNone(row['terminal_at'])
        self.assertFalse(self.rounds.run_once())
        self.assertEqual(len(self.adapters['claude'].calls), 1)

    def test_post_claim_failure_preserves_recorded_reply(self):
        self.adapters['gemini'] = Fake()
        self.rounds.participants = tuple(self.adapters)
        r = self.prepare(targets=['claude', 'gemini'])
        self.rounds.approve(r['round_id'])
        consult = self.rounds._consult
        def fail_after_second_call(target, *args):
            reply = consult(target, *args)
            if target == 'gemini':
                raise RuntimeError('invented post-claim failure')
            return reply
        with patch.object(self.rounds, '_consult', side_effect=fail_after_second_call), self.assertRaises(RuntimeError):
            self.rounds.run_once()
        result = self.rounds.read('codex', r['round_id'])
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['selection'], {})
        self.assertEqual(result['replies']['claude']['status'], 'completed')
        self.assertNotIn('gemini', result['replies'])
        self.assertEqual(len(self.store.snapshot(result['room_id'])['messages']), 2)
        self.assertFalse(self.rounds.run_once())
        self.assertEqual([len(self.adapters[p].calls) for p in ('claude', 'gemini')], [1, 1])

    def test_expired_pending_rounds_release_capacity_and_can_be_rejected(self):
        with patch('agent_bridge.chat.rounds.time.time', return_value=1):
            for i in range(50): self.prepare(request_id=str(i))
        latest = self.prepare(request_id='fresh')
        self.assertEqual(len(self.rounds.pending()), 1)
        self.assertEqual(latest['status'], 'pending')
        self.rounds.approve(latest['round_id'], reject=True)
        self.assertEqual(self.rounds.read('codex', latest['round_id'])['selection'], {})
        with self.store.db() as db:
            self.assertIsNone(db.execute('SELECT payload FROM peer_rounds WHERE id=?', (latest['round_id'],)).fetchone()[0])
        self.assertFalse(self.rounds.run_once())

    def test_read_reports_expired_draft_without_opening_review_page(self):
        with patch('agent_bridge.chat.rounds.time.time', return_value=1):
            r = self.prepare()
        self.assertEqual(self.rounds.read('codex', r['round_id'])['status'], 'expired')
        self.assertEqual(self.rounds.read('codex', r['round_id'])['selection'], {})
        with self.store.db() as db:
            self.assertIsNone(db.execute('SELECT payload FROM peer_rounds WHERE id=?', (r['round_id'],)).fetchone()[0])
        self.assertFalse(self.rounds.run_once())

    def test_stop_prevents_later_peers_and_late_publication(self):
        r = self.prepare()
        def stop_while_polling(job):
            room = self.rounds.read('codex', r['round_id'])['room_id']
            self.rounds.stop_room(room)
            return {'ok': True, 'status': 'complete'}
        self.adapters['claude'].poll = stop_while_polling
        self.rounds.approve(r['round_id'])
        self.rounds.run_once()
        result = self.rounds.read('codex', r['round_id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['replies'], {})
        self.assertEqual(len(self.store.snapshot(result['room_id'])['messages']), 1)

    def test_deleted_round_is_not_recreated(self):
        r = self.prepare()
        def delete_while_polling(job):
            room = self.rounds.read('codex', r['round_id'])['room_id']
            self.rounds.delete_room(room)
            self.store.delete_room(room)
            return {'ok': True, 'status': 'complete'}
        self.adapters['claude'].poll = delete_while_polling
        self.rounds.approve(r['round_id'])
        self.rounds.run_once()
        self.assertEqual(self.store.rooms(), [])
        with self.assertRaises(ValueError): self.rounds.read('codex', r['round_id'])

    def test_timeout_has_no_automatic_retry(self):
        self.adapters['claude'].poll = lambda job: {'ok': True, 'status': 'running'}
        self.adapters['claude'].timeout = 0
        r = self.prepare()
        self.rounds.approve(r['round_id'])
        self.rounds.run_once()
        result = self.rounds.read('codex', r['round_id'])
        self.assertEqual(result['replies']['claude']['status'], 'failed')
        self.assertEqual(len(self.adapters['claude'].calls), 1)
        self.assertFalse(self.rounds.run_once())


if __name__ == '__main__': unittest.main()
