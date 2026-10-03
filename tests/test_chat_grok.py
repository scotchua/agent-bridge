from pathlib import Path
import os
import json
import io
import importlib.util
import sys
import unittest
from unittest.mock import patch

from test_chat_storage import StorageTests
from agent_bridge.chat.grok import GrokAdapter, receive, respond, job_path
from agent_bridge.chat.policy import RoomPolicy


class GrokTests(StorageTests):
    def adapter(self, name='grok'):
        root = Path(self.temp.name) / name
        adapter = GrokAdapter(root, RoomPolicy(False))
        (root / 'bot-manifest.json').write_text(json.dumps({
            'product': 'Grok Bot', 'version': 'fixture',
            'tools': ['grok_room.py next --wait', 'grok_room.py reply <job-id>'],
            'allowlist': ['grok_room.py next --wait', 'grok_room.py reply <job-id>'],
            'approved_by': 'Scott'}), encoding='utf-8')
        return adapter

    @unittest.skipIf(os.name == 'nt', 'Windows queue privacy is verified by ACL tests')
    def test_adapter_sets_private_umask_before_operator_writes_manifest(self):
        previous_umask = os.umask(0o022)
        try:
            root = Path(self.temp.name) / 'private-manifest'
            GrokAdapter(root, RoomPolicy(False))
            manifest = root / 'bot-manifest.json'
            manifest.write_text('{}', encoding='utf-8')
            self.assertEqual(manifest.stat().st_mode & 0o077, 0)
        finally:
            os.umask(previous_umask)

    def test_restart_cancels_unpulled_request(self):
        adapter = self.adapter()
        root = adapter.root
        receive(root, wait=0)
        job = adapter.start('Do not replay after restart', 'synthetic')
        self.adapter()
        self.assertIsNone(receive(root, wait=0))
        self.assertEqual(adapter.poll(job['job_id'])['status'], 'cancelled')

    def test_disconnected_until_local_bot_checks_in(self):
        adapter = self.adapter()
        self.assertNotEqual(adapter.status()['state'], 'ready')
        receive(adapter.root, wait=0)
        self.assertEqual(adapter.status()['state'], 'ready')

    def test_queue_roundtrip_and_only_one_reply(self):
        adapter = self.adapter()
        receive(adapter.root, wait=0)
        job = adapter.start('A synthetic room prompt', 'synthetic')
        pulled = receive(adapter.root, wait=0)
        self.assertTrue(pulled['prompt'].endswith('A synthetic room prompt'))
        self.assertIsNone(receive(adapter.root, wait=0))
        respond(adapter.root, job['job_id'], 'Reply from existing Grok Bot')
        self.assertEqual(adapter.poll(job['job_id'])['status'], 'complete')
        self.assertEqual(adapter.read(job['job_id'])['peer_response'], 'Reply from existing Grok Bot')
        self.assertFalse(job_path(adapter.root, job['job_id']).exists())
        with self.assertRaises(ValueError):
            respond(adapter.root, job['job_id'], 'duplicate')

    def test_poll_and_read_hold_the_queue_lock(self):
        from agent_bridge import store
        adapter = self.adapter('locked-read')
        receive(adapter.root, wait=0)
        job = adapter.start('A synthetic room prompt', 'synthetic')
        receive(adapter.root, wait=0)
        respond(adapter.root, job['job_id'], 'Reply from existing Grok Bot')
        with patch.object(store, 'file_lock', wraps=store.file_lock) as file_lock:
            self.assertEqual(adapter.poll(job['job_id'])['status'], 'complete')
            self.assertEqual(adapter.read(job['job_id'])['peer_response'], 'Reply from existing Grok Bot')
        self.assertEqual(file_lock.call_count, 2)

    def test_expired_and_traversal_jobs_are_refused(self):
        adapter = self.adapter()
        with self.assertRaises(ValueError):
            respond(adapter.root, '../escape', 'bad')
        with self.assertRaises(ValueError):
            respond(adapter.root, '00000000-0000-0000-0000-000000000000', 'unknown')

    def test_unicode_transcript_roundtrip(self):
        adapter = self.adapter()
        receive(adapter.root, wait=0)
        prompt = 'Garden club “Budding” — 🌱'
        job = adapter.start(prompt, 'synthetic')
        self.assertTrue(receive(adapter.root, wait=0)['prompt'].endswith(prompt))
        respond(adapter.root, job['job_id'], prompt)
        self.assertEqual(adapter.read(job['job_id'])['peer_response'], prompt)

    def test_fresh_consultation_marks_new_context(self):
        adapter = self.adapter('fresh')
        receive(adapter.root, wait=0)
        first = adapter.start('Room history', 'synthetic')
        record = receive(adapter.root, wait=0)
        self.assertIn('Fresh room consultation', record['prompt'])
        self.assertTrue(record['prompt'].endswith('Room history'))
        second = adapter.start('Room history', 'synthetic')
        self.assertNotEqual(first['conversation_id'], second['conversation_id'])

    def test_classification_refusal_does_not_create_a_queue_job(self):
        adapter = self.adapter()
        with self.assertRaises(ValueError):
            adapter.start('client text', 'client-derived')
        self.assertEqual(list(adapter.root.glob('*.json')), [adapter.root / 'bot-manifest.json'])

    def test_malformed_foreign_json_is_quarantined(self):
        root = Path(self.temp.name) / 'malformed'
        root.mkdir()
        (root / 'foreign.json').write_text('{"not":"a queue job"}', encoding='utf-8')
        adapter = self.adapter('malformed')
        self.assertTrue((root / 'quarantine').is_dir())
        self.assertIsNone(receive(adapter.root, wait=0))

    def test_mismatched_filename_and_invalid_expiry_are_quarantined(self):
        adapter = self.adapter('invalid-records')
        wrong_name = adapter.root / (str(__import__('uuid').uuid4()) + '.json')
        wrong_name.write_text(json.dumps({'job_id': str(__import__('uuid').uuid4()), 'status': 'queued', 'expires': 1}), encoding='utf-8')
        bad_expiry = adapter.root / (str(__import__('uuid').uuid4()) + '.json')
        bad_expiry.write_text(json.dumps({'job_id': bad_expiry.stem, 'status': 'queued', 'expires': True}), encoding='utf-8')
        self.assertIsNone(receive(adapter.root, wait=0))
        quarantined = list((adapter.root / 'quarantine').glob('*.invalid'))
        self.assertEqual(len(quarantined), 2)

    def test_manifest_is_required_and_operator_declared(self):
        root = Path(self.temp.name) / 'manifest'
        adapter = GrokAdapter(root, RoomPolicy(False))
        receive(root, wait=0)
        self.assertNotEqual(adapter.status()['state'], 'ready')
        manifest = {
            'product': 'Grok Bot', 'version': 'fixture',
            'tools': ['grok_room.py next --wait', 'grok_room.py reply <job-id>'],
            'allowlist': ['grok_room.py next --wait', 'grok_room.py reply <job-id>'],
            'approved_by': 'Scott',
        }
        (root / 'bot-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(adapter.status()['state'], 'ready')
        self.assertIn('operator-declared', adapter.status()['detail'])

    def test_helper_reads_reply_from_standard_input_and_has_no_file_option(self):
        script = Path(__file__).resolve().parents[1] / 'grok_room.py'
        spec = importlib.util.spec_from_file_location('grok_room', script)
        grok_room = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(grok_room)
        root = Path(self.temp.name) / 'queue'
        with patch.object(grok_room, 'respond') as respond, patch.object(sys, 'argv', ['grok_room.py', 'reply', '00000000-0000-0000-0000-000000000000', '--queue-dir', str(root)]), patch.object(sys, 'stdin', io.StringIO('reply text')):
            grok_room.main()
        respond.assert_called_once_with(root.resolve(), '00000000-0000-0000-0000-000000000000', 'reply text')
        with self.assertRaisesRegex(SystemExit, '2'):
            with patch.object(sys, 'argv', ['grok_room.py', 'reply', '00000000-0000-0000-0000-000000000000', '--file', 'reply.txt']):
                grok_room.main()
