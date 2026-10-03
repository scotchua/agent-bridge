from test_chat_storage import StorageTests
from test_chat_dispatch import FakePeer
from agent_bridge.chat.dispatch import Dispatcher

class DiscussionTests(StorageTests):
    def test_bounded_sequential_round_and_summary(self):
        a,b=FakePeer(),FakePeer()
        d=Dispatcher(self.store,{'claude':a,'codex':b})
        result=self.store.submit(self.room,'round','Think together',['claude','codex'],'public',mode='discuss',lead='codex')
        self.assertEqual(len(result['job_ids']),3)
        self.assertEqual(result,self.store.submit(self.room,'round','Think together',['claude','codex'],'public',mode='discuss',lead='codex'))
        d.run_once(); d.run_once()
        self.assertIn('@codex do another task',b.prompt)
        d.run_once()
        self.assertIn('Summarize',b.prompt)
        self.assertFalse(d.run_once())
        self.assertEqual(len(self.store.snapshot(self.room)['messages']),4)
    def test_round_blocks_interleaving_and_stop_cancels_summary(self):
        self.store.submit(self.room,'r','topic',['claude','codex'],'public',mode='discuss',lead='claude')
        with self.assertRaises(ValueError): self.store.submit(self.room,'other','new topic',['codex'],'public')
        d=Dispatcher(self.store,{'claude':FakePeer(lambda:self.store.stop(self.room))})
        d.run_once()
        self.assertFalse(d.run_once())
        self.assertEqual(len(self.store.snapshot(self.room)['messages']),1)
    def test_requires_selected_lead_and_two_participants(self):
        for peers,lead in [(['claude'],'claude'),(['claude','codex'],'invalid')]:
            with self.assertRaises(ValueError): self.store.submit(self.room,'r','topic',peers,'public',mode='discuss',lead=lead)

    def test_preferences_survive_reopen(self):
        from agent_bridge.chat.storage import RoomStore
        self.store.preferences(self.room, 'codex', ['claude', 'codex'])
        self.assertEqual(RoomStore(self.path).preferences(self.room), {'lead':'codex','participants':['claude','codex']})
    def test_two_participants_have_exactly_three_turns(self):
        peers={p:FakePeer() for p in ('claude','codex')}
        self.store.submit(self.room,'r','topic',list(peers),'public',mode='discuss',lead='codex')
        d=Dispatcher(self.store,peers)
        for _ in range(3): self.assertTrue(d.run_once())
        self.assertFalse(d.run_once())
        self.assertEqual(len(self.store.snapshot(self.room)['messages']),4)
    def test_failed_participant_does_not_loop_or_block_summary(self):
        self.store.submit(self.room,'r','topic',['claude','codex'],'public',mode='discuss',lead='codex')
        d=Dispatcher(self.store,{'codex':FakePeer()})
        for _ in range(3): self.assertTrue(d.run_once())
        self.assertFalse(d.run_once())
        self.assertEqual([j['status'] for j in self.store.snapshot(self.room)['jobs']],['failed','completed','completed'])
    def test_new_room_discussion_includes_registered_providers(self):
        self.assertEqual(set(self.store.preferences(self.room)['participants']), {'claude','codex'})

    def test_new_room_defaults_exclude_optional_providers(self):
        from agent_bridge.chat.storage import RoomStore
        store = RoomStore(self.path, participants=('claude', 'codex', 'hermes', 'grok'))
        room = store.create_room('Optional providers are manual')['id']
        self.assertEqual(store.preferences(room), {'lead': 'claude', 'participants': ['claude', 'codex']})
