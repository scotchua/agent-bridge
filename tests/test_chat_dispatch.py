from test_chat_storage import StorageTests
import threading
from agent_bridge.chat.dispatch import Dispatcher


class FakePeer:
    def __init__(self, callback=None):
        self.callback = callback
    def status(self):
        return {'state': 'ready'}
    def start(self, prompt, classification):
        self.prompt = prompt
        if self.callback:
            self.callback()
        return {'ok': True, 'job_id': 'j', 'conversation_id': 'c'}
    def continue_(self, conversation_id, prompt, classification):
        return self.start(prompt, classification)
    def poll(self, job_id):
        return {'ok': True, 'status': 'complete'}
    def read(self, job_id):
        return {'ok': True, 'peer_response': {'answer': '@codex do another task'}}


class DispatchTests(StorageTests):
    def test_structured_reply_is_readable_chat(self):
        class StructuredPeer(FakePeer):
            def read(self, job_id):
                return {'ok': True, 'peer_response': {'contract_version': '2', 'status': 'answer',
                    'summary': 'A shared room works.', 'analysis': ['Keep explicit recipients.'],
                    'questions': ['Which project first?'], 'disagreements': [], 'risks': [], 'confidence': 'high'}}
        self.store.submit(self.room, 'r', 'hello', ['claude'], 'public')
        Dispatcher(self.store, {'claude': StructuredPeer()}).run_once()
        text = self.store.snapshot(self.room)['messages'][-1]['text']
        self.assertTrue(text.startswith('A shared room works.'))
        self.assertIn('Keep explicit recipients.', text)
        self.assertIn('Which project first?', text)
        self.assertNotIn('contract_version', text)

    def test_broker_timeout_is_terminal_immediately(self):
        class TimedOutPeer(FakePeer):
            timeout = -1
            def poll(self, job_id):
                return {'ok': True, 'status': 'timed_out'}
            def read(self, job_id):
                return {'ok': False, 'error_hint': 'Provider timeout cleanup completed'}
        self.store.submit(self.room, 'r', 'hello', ['claude'], 'public')
        Dispatcher(self.store, {'claude': TimedOutPeer()}).run_once()
        self.assertEqual(self.store.snapshot(self.room)['jobs'][0]['error'], 'Provider timeout cleanup completed')

    def test_actual_author_and_no_automatic_reply_loop(self):
        self.store.submit(self.room, 'r', 'hello', ['claude'], 'public')
        dispatch = Dispatcher(self.store, {'claude': FakePeer()})
        self.assertTrue(dispatch.run_once())
        snap = self.store.snapshot(self.room)
        self.assertEqual([m['author'] for m in snap['messages']], ['Human', 'claude'])
        self.assertEqual(snap['jobs'][0]['status'], 'completed')
        self.assertFalse(dispatch.run_once())

    def test_stop_discards_late_reply(self):
        self.store.submit(self.room, 'r', 'hello', ['claude'], 'public')
        dispatch = Dispatcher(self.store, {'claude': FakePeer(lambda: self.store.stop(self.room))})
        dispatch.run_once()
        snap = self.store.snapshot(self.room)
        self.assertEqual(len(snap['messages']), 1)
        self.assertEqual(snap['jobs'][0]['status'], 'cancelled')

    def test_unavailable_adapter_is_honest_failure(self):
        self.store.submit(self.room, 'r', 'hello', ['codex'], 'public')
        Dispatcher(self.store, {}).run_once()
        self.assertEqual(self.store.snapshot(self.room)['jobs'][0]['status'], 'failed')

    def test_same_snapshot_and_followup_context(self):
        self.store.submit(self.room, 'r', 'first', ['claude', 'codex'], 'public')
        peer_a, peer_b = FakePeer(), FakePeer()
        dispatch = Dispatcher(self.store, {'claude': peer_a, 'codex': peer_b})
        dispatch.run_once()
        dispatch.run_once()
        self.assertEqual(peer_a.prompt, peer_b.prompt)
        self.store.submit(self.room, 'r2', 'second', ['claude'], 'public')
        dispatch.run_once()
        self.assertNotIn('first', peer_a.prompt)
        self.assertIn('second', peer_a.prompt)
        self.assertIn('codex', peer_a.prompt)
    def test_adapter_cleanup_failure_does_not_kill_dispatcher(self):
        class CleanupFailure(FakePeer):
            def cancel(self, job): raise ValueError('cleanup failed')
        self.store.submit(self.room,'cleanup','hello',['claude','codex'],'synthetic')
        d=Dispatcher(self.store,{'claude':CleanupFailure(),'codex':FakePeer()})
        self.assertTrue(d.run_once())
        self.assertTrue(d.run_once())
        self.assertEqual([j['status'] for j in self.store.snapshot(self.room)['jobs']],['completed','completed'])

    def test_cancel_room_uses_the_provider_job_id(self):
        class BlockingPeer(FakePeer):
            def __init__(self):
                self.started, self.release, self.cancelled = threading.Event(), threading.Event(), []

            def start(self, prompt, classification):
                self.started.set()
                return {'ok': True, 'job_id': 'provider-job', 'conversation_id': 'c'}

            def poll(self, job_id):
                self.release.wait(2)
                return {'ok': True, 'status': 'running'}

            def cancel(self, job_id):
                self.cancelled.append(job_id)

        peer = BlockingPeer()
        self.store.submit(self.room, 'r', 'hello', ['claude'], 'public')
        dispatcher = Dispatcher(self.store, {'claude': peer})
        worker = threading.Thread(target=dispatcher.run_once)
        worker.start()
        self.assertTrue(peer.started.wait(1))
        dispatcher.cancel_room(self.room)
        self.assertEqual(peer.cancelled, ['provider-job'])
        peer.release.set()
        worker.join(2)
