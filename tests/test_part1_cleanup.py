"""Offline checks for ownership-scoped cleanup and fail-closed reporting."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'lab/task/part1/cleanup.py'
spec = importlib.util.spec_from_file_location('course_cleanup', MODULE)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / 'part1_resources.json'

    def remember(self, kind='video', rid='course-video', **kwargs):
        cleanup.remember(kind, rid, ledger=self.ledger, **kwargs)

    def test_remember_is_idempotent_but_keeps_repeated_uploads(self):
        self.remember(name='traffic_short')
        self.remember(name='traffic_short')
        self.remember(rid='second-upload', name='traffic_short')
        self.assertEqual(len(json.loads(self.ledger.read_text())), 2)

    def test_no_ledger_does_not_discover_and_delete_arbitrary_sources(self):
        worker = cleanup.Cleanup(ledger=self.ledger)
        with patch.object(worker, 'api') as api:
            worker.run(confirm=True)
            api.assert_not_called()

    def test_explicit_confirmation_required(self):
        self.remember(name='traffic_short')
        with self.assertRaises(ValueError):
            cleanup.Cleanup(ledger=self.ledger).run()

    def test_caption_index_wildcard_rejected_before_any_request(self):
        self.remember('caption', '*')
        with self.assertRaises(ValueError):
            cleanup.Cleanup(ledger=self.ledger)

    def test_queries_are_source_scoped_not_match_all(self):
        self.remember(name='traffic_short')
        worker = cleanup.Cleanup(ledger=self.ledger)
        queries = worker.metadata_queries()
        self.assertEqual(queries[0][1], {'terms': {'sensorId.keyword': ['traffic_short']}})
        self.assertEqual(queries[2][1], {'terms': {'sensor.id.keyword': ['course-video']}})

    def test_partial_response_is_not_success(self):
        worker = cleanup.Cleanup(ledger=self.ledger)
        with patch.object(worker.session, 'request') as call:
            call.return_value.content = b'{}'
            call.return_value.json.return_value = {'status': 'partial'}
            with self.assertRaises(RuntimeError):
                worker.api('DELETE', '/agent/api/v1/videos/owned')

    def test_ambiguous_live_name_stops_before_deletion(self):
        self.remember('live', 'owned', name='Camera_1')
        worker = cleanup.Cleanup(ledger=self.ledger)
        with patch.object(worker, 'sensors', return_value={
                'owned': {'sensorId': 'owned', 'name': 'Camera_1'},
                'other': {'sensorId': 'other', 'name': 'Camera_1'}}), patch.object(worker, 'api') as api:
            with self.assertRaises(RuntimeError):
                worker.run(confirm=True)
            api.assert_not_called()

    def test_clear_pointers_preserves_other_exercises_and_input_files(self):
        self.remember(name='traffic_short')
        root = Path(self.tmp.name)
        registry = root / 'search_sources.json'
        registry.write_text(json.dumps({'ours': {'sensor_id': 'course-video'},
                                        'theirs': {'sensor_id': 'part2-video'}}))
        selection = root / 'search_selection.json'
        selection.write_text(json.dumps({'sensor_id': 'part2-video'}))
        video = root / 'traffic_short.mp4'
        video.write_bytes(b'original video')
        cleanup.Cleanup(ledger=self.ledger).clear_saved_results()
        self.assertEqual(list(json.loads(registry.read_text())), ['theirs'])
        self.assertTrue(selection.exists())
        self.assertEqual(video.read_bytes(), b'original video')

    def test_late_metadata_prevents_clean_state(self):
        self.remember(name='traffic_short')
        worker = cleanup.Cleanup(ledger=self.ledger)
        def api(method, path, **kwargs):
            if path.endswith('/timelines'):
                return {}
            if path.endswith('/v1/files'):
                return {'data': []}
            return {'count': 1}
        with patch.object(worker, 'sensors', return_value={}), \
             patch.object(worker, 'indices', return_value=['mdx-raw-test']), \
             patch.object(worker, 'api', side_effect=api):
            with self.assertRaisesRegex(RuntimeError, 'still arriving'):
                worker.verify()

    def test_cleanup_drains_late_writes_before_success(self):
        self.remember(name='traffic_short')
        worker = cleanup.Cleanup(ledger=self.ledger)
        with patch.object(worker, 'sensors', return_value={}), \
             patch.object(worker, 'remove_video'), \
             patch.object(worker, 'remove_metadata') as purge, \
             patch.object(worker, 'clear_saved_results') as clear, \
             patch.object(worker, 'verify', side_effect=[cleanup.MetadataPending(), None, None]) as verify, \
             patch.object(cleanup.time, 'sleep'):
            worker.run(confirm=True)
        self.assertEqual(purge.call_count, 2)
        self.assertEqual(verify.call_count, 3)
        clear.assert_called_once()

    def test_version_conflicts_are_rechecked_not_treated_as_fatal(self):
        self.remember(name='traffic_short')
        worker = cleanup.Cleanup(ledger=self.ledger)
        with patch.object(worker, 'indices', return_value=['mdx-behavior-test']), \
             patch.object(worker, 'api', return_value={'version_conflicts': 1, 'failures': []}) as api:
            worker.remove_metadata()
        for call in api.call_args_list:
            self.assertEqual(call.kwargs['params']['conflicts'], 'proceed')
            self.assertIn('terms', call.kwargs['json']['query'])


if __name__ == '__main__':
    unittest.main()
