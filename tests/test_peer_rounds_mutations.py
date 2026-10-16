import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_bridge.chat.rounds import PeerRounds
from agent_bridge.chat.storage import RoomStore
from test_peer_rounds import Fake


class RoundMutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RoomStore(Path(self.tmp.name) / 'chat.sqlite')
        self.adapters = {peer: Fake() for peer in ('claude', 'codex')}
        self.rounds = PeerRounds(self.store, self.adapters)
        self.args = dict(request_id='invented-1', targets=['claude'], question='Compare invented ledgers',
                         context='Fictional account balance: 42', source_classification='synthetic')

    def prepare(self, **changes):
        return self.rounds.prepare('codex', dict(self.args, **changes))

    def test_init_expires_old_pending_rows_immediately(self):
        with patch('agent_bridge.chat.rounds.time.time', return_value=1):
            row = self.prepare()
        with patch('agent_bridge.chat.rounds.time.time', return_value=3602):
            PeerRounds(self.store, self.adapters)
        with self.store.db() as db:
            status = db.execute('SELECT status FROM peer_rounds WHERE id=?', (row['round_id'],)).fetchone()[0]
        self.assertEqual(status, 'expired')

    def test_prepare_accepts_exact_required_keys(self):
        args = dict(self.args)
        del args['context']
        self.assertEqual(self.rounds.prepare('codex', args)['selection']['context'], '')

    def test_prepare_accepts_each_length_limit(self):
        for key, size in (('request_id', 100), ('question', 8000), ('context', 4000)):
            with self.subTest(key=key):
                args = dict(self.args, request_id=key)
                args[key] = 'x' * size
                self.rounds.prepare('codex', args)

    def test_prepare_rejects_blank_required_text(self):
        for key in ('request_id', 'question'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.prepare(**{key: '   '})

    def test_prepare_local_first_limit_includes_equal_size(self):
        prompt_size = len((self.args['question'] + '\n' + self.args['context']).encode('utf-8'))
        class Config:
            def local_first_enabled(self): return True
            def local_first_min_bytes(self): return prompt_size
        self.adapters['claude'].cfg = Config()
        with self.assertRaises(ValueError):
            self.prepare()

    def test_prepare_refuses_fifty_first_pending_round(self):
        for number in range(50):
            self.prepare(request_id=f'invented-{number}')
        with self.assertRaises(ValueError):
            self.prepare(request_id='invented-over-capacity')

    def test_active_respects_closed_event_for_running_round(self):
        row = self.prepare()
        with self.store.db() as db:
            db.execute("UPDATE peer_rounds SET status='running' WHERE id=?", (row['round_id'],))
        self.rounds.closed.set()
        self.assertFalse(self.rounds._active(row['round_id']))

    def test_approve_allows_exact_expiry_boundary(self):
        with patch('agent_bridge.chat.rounds.time.time', return_value=100):
            row = self.prepare()
        with patch('agent_bridge.chat.rounds.time.time', return_value=3700):
            self.assertEqual(self.rounds.approve(row['round_id'])['status'], 'approved')

    def test_approve_returns_status_to_review_caller(self):
        row = self.prepare()
        self.assertEqual(self.rounds.approve(row['round_id'], reject=True),
                         {'ok': True, 'status': 'rejected'})

    def test_run_once_idle_result_is_false(self):
        self.assertIs(self.rounds.run_once(), False)

    def test_consult_rejects_oversized_reply(self):
        self.adapters['claude'].read = lambda job: {'ok': True, 'peer_response': 'x' * 100001}
        result = self.rounds._consult('claude', 'Invented question', 'synthetic', lambda: True)
        self.assertEqual(result['status'], 'failed')

    def test_consult_stops_at_exact_deadline(self):
        adapter = self.adapters['claude']
        adapter.timeout = 0
        polls = []
        def poll(job):
            polls.append(job)
            return {'ok': True, 'status': 'running'}
        adapter.poll = poll
        with patch('agent_bridge.chat.rounds.time.monotonic', side_effect=[10, 10, 11]), \
             patch.object(self.rounds.closed, 'wait'):
            result = self.rounds._consult('claude', 'Invented question', 'synthetic', lambda: True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(polls), 1)

    def test_consult_cancels_job_after_read(self):
        cancelled = []
        self.adapters['claude'].cancel = cancelled.append
        result = self.rounds._consult('claude', 'Invented question', 'synthetic', lambda: True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(cancelled, ['fixture'])

    def test_loop_waits_when_queue_is_idle(self):
        calls = []
        def idle():
            calls.append(1)
            if len(calls) == 2:
                self.rounds.closed.set()
            return False
        def stop(_delay):
            self.rounds.closed.set()
        with patch.object(self.rounds, 'run_once', side_effect=idle), \
             patch.object(self.rounds.closed, 'wait', side_effect=stop) as wait:
            self.rounds.loop()
        self.assertEqual(len(calls), 1)
        wait.assert_called_once_with(0.3)


if __name__ == '__main__': unittest.main()
