import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_chat_dispatch import FakePeer
from agent_bridge.chat.dispatch import Dispatcher, reply_text
from agent_bridge.chat.storage import RoomStore


class ReplyTextMutationTests(unittest.TestCase):
    def test_plain_text_is_preserved(self):
        self.assertEqual(reply_text('Northstar invoice reviewed'), 'Northstar invoice reviewed')

    def test_structured_risks_are_rendered(self):
        content = {'contract_version': '2', 'summary': 'Northstar review',
                   'risks': [{'issue': 'Late invoice', 'mitigation': 'Check on October 20'},
                             {'issue': 'Missing receipt'}]}
        self.assertEqual(reply_text(content),
                         'Northstar review\n\nRisks:\n• Late invoice — Check on October 20\n• Missing receipt')


class RunOnceMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RoomStore(Path(self.temp.name) / 'room.sqlite')
        self.room = self.store.create_room('Northstar review')['id']

    def submit(self, classification='public'):
        self.store.submit(self.room, 'request-1', 'Review the October invoice',
                          ['claude'], classification)

    def test_empty_queue_returns_false(self):
        self.assertIs(Dispatcher(self.store, {}).run_once(), False)

    def test_missing_adapter_reports_connection_error(self):
        self.submit()
        self.assertTrue(Dispatcher(self.store, {}).run_once())
        self.assertEqual(self.store.snapshot(self.room)['jobs'][0]['error'],
                         'This agent is not connected yet')

    def test_each_classification_reaches_the_adapter(self):
        class RecordingPeer(FakePeer):
            def start(self, prompt, classification):
                self.classification = classification
                return super().start(prompt, classification)

        for classification in ('public', 'synthetic', 'internal', 'client-derived'):
            with self.subTest(classification=classification):
                room = self.store.create_room('Northstar ' + classification)['id']
                self.store.submit(room, 'request-1', 'Review invoice', ['claude'], classification)
                peer = RecordingPeer()
                self.assertTrue(Dispatcher(self.store, {'claude': peer}).run_once())
                self.assertEqual(peer.classification, classification)

    def test_stopped_job_is_not_started_but_run_was_claimed(self):
        class StoppingPeer(FakePeer):
            def status(peer):
                self.store.stop(self.room)
                return {'state': 'ready'}

            def start(peer, prompt, classification):
                self.fail('A stopped job reached the adapter')

        self.submit()
        self.assertIs(Dispatcher(self.store, {'claude': StoppingPeer()}).run_once(), True)
        self.assertEqual(self.store.snapshot(self.room)['jobs'][0]['status'], 'cancelled')

    def test_default_timeout_uses_exact_boundary(self):
        class RunningPeer(FakePeer):
            def __init__(self):
                super().__init__()
                self.poll_count = 0

            def poll(self, job_id):
                self.poll_count += 1
                return {'ok': True, 'status': 'running' if self.poll_count == 1 else 'complete'}

        self.submit()
        peer = RunningPeer()
        with patch('agent_bridge.chat.dispatch.time.monotonic', side_effect=(100, 580.5)):
            self.assertTrue(Dispatcher(self.store, {'claude': peer}).run_once())
        job = self.store.snapshot(self.room)['jobs'][0]
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error'], 'Timed out waiting for the agent; the provider call may still finish')
        self.assertEqual(peer.poll_count, 1)

    def test_reply_is_read_only_after_terminal_poll(self):
        class DelayedPeer(FakePeer):
            def __init__(self):
                super().__init__()
                self.poll_count = 0
                self.read_after = None

            def poll(self, job_id):
                self.poll_count += 1
                return {'ok': True, 'status': 'running' if self.poll_count == 1 else 'complete'}

            def read(self, job_id):
                self.read_after = self.poll_count
                return super().read(job_id)

        self.submit()
        peer = DelayedPeer()
        self.assertTrue(Dispatcher(self.store, {'claude': peer}).run_once())
        self.assertEqual(peer.read_after, 2)
        self.assertEqual(self.store.snapshot(self.room)['jobs'][0]['status'], 'completed')

    def test_close_before_poll_skips_reply_and_cancels_provider_job(self):
        class ClosingPeer(FakePeer):
            def __init__(self):
                super().__init__()
                self.dispatcher = None
                self.polled = 0
                self.cancelled = []

            def start(self, prompt, classification):
                result = super().start(prompt, classification)
                self.dispatcher.closed.set()
                return result

            def poll(self, job_id):
                self.polled += 1
                return super().poll(job_id)

            def cancel(self, job_id):
                self.cancelled.append(job_id)

        self.submit()
        peer = ClosingPeer()
        dispatcher = Dispatcher(self.store, {'claude': peer})
        peer.dispatcher = dispatcher
        self.assertTrue(dispatcher.run_once())
        self.assertEqual(peer.polled, 0)
        self.assertEqual(peer.cancelled, ['j'])
        self.assertEqual(self.store.snapshot(self.room)['jobs'][0]['status'], 'running')

    def test_failure_hint_is_capped_at_500_characters(self):
        class LongErrorPeer(FakePeer):
            def status(self):
                raise ValueError('x' * 501)

        self.submit()
        self.assertTrue(Dispatcher(self.store, {'claude': LongErrorPeer()}).run_once())
        self.assertEqual(self.store.snapshot(self.room)['jobs'][0]['error'], 'x' * 500)


class StopMutationTests(unittest.TestCase):
    def test_stop_returns_store_result(self):
        class Store:
            def stop(self, room_id):
                return {'cancelled': 2, 'room': room_id}

        self.assertEqual(Dispatcher(Store(), {}).stop('northstar-room'),
                         {'cancelled': 2, 'room': 'northstar-room'})


class LoopMutationTests(unittest.TestCase):
    def test_idle_loop_waits_once(self):
        class Store:
            def __init__(self):
                self.claims = 0

            def claim_next(self):
                self.claims += 1
                return None

        class ClosingEvent:
            def __init__(self):
                self.checks = 0
                self.waits = []

            def is_set(self):
                self.checks += 1
                return self.checks > 1

            def wait(self, seconds):
                self.waits.append(seconds)

        store = Store()
        dispatcher = Dispatcher(store, {})
        dispatcher.closed = ClosingEvent()
        dispatcher.loop()
        self.assertEqual(store.claims, 1)
        self.assertEqual(dispatcher.closed.waits, [0.3])


if __name__ == '__main__':
    unittest.main()
