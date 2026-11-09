"""Exercise notebook 2's live-stream cells without touching any service.

Ported from upstream c-fx-17-v2 commit e959647 and adapted to this tree's
cells: the recorder cell (`0926cc76`) and the NVStreamer upload (`770b8dba`)
poll with a progress print and raise on timeout, so the mocks dispatch on the
URL and the clock is faked; `remember` is imported once in the notebook's
first cell, so it is passed in as a global. The parse-and-no-outputs check
covers every shipped notebook under lab/task/part1 (6 at the time of writing).

Run: uv run --offline --no-project --with nbformat --with requests --with pyyaml --with pytest python -I -m pytest task1/tests/test_stream_recording.py -q
"""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

TASK = Path(__file__).resolve().parents[1] / 'lab/task'
NOTEBOOK = TASK / 'part1/02-rt-cv-detection-and-tracking.ipynb'
cells = json.loads(NOTEBOOK.read_text())['cells']
EXPECTED_NOTEBOOKS = 6    # part1: 00-onboarding, 01 to 04, assessment (00_Home.ipynb is not counted)


def cell_source(cell_id):
    return ''.join(next(c['source'] for c in cells if c['id'] == cell_id))


CODE = compile(cell_source('0926cc76'), str(NOTEBOOK), 'exec')


def response(value, text='indices'):
    return SimpleNamespace(json=lambda: value, raise_for_status=lambda: None, text=text)


class FakeClock:
    """time.time() advances only when the cell sleeps, so a poll loop that never
    succeeds reaches its timeout without a real wait."""

    def __init__(self):
        self.now_s = 1000.0

    def time(self):
        return self.now_s

    def sleep(self, seconds):
        self.now_s += seconds

    def module(self):
        return SimpleNamespace(time=self.time, sleep=self.sleep)


class RecordingTests(unittest.TestCase):
    def test_nvstreamer_empty_sensor_id_uses_returned_stream_id(self):
        code = cell_source('770b8dba')
        client = Mock()
        client.post.return_value = response({'sensorId': '', 'streamId': 'our-file'})
        client.get.return_value = response([{'streamId': 'our-file', 'url': 'rtsp://test/our-file'}])
        remember = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, str(NOTEBOOK), 'exec'), {
                'BASE': 'http://nginx', 'CLIP': '/unused.mp4', 'json': json,
                'open': lambda *a, **k: io.BytesIO(b'video'),
                'requests': client, 'remember': remember, 'time': FakeClock().module()})
        client.get.assert_called_once_with(
            'http://nginx/nvstreamer/api/v1/sensor/our-file/streams', timeout=30)
        remember.assert_called_with('nvstreamer', 'our-file', name='live_traffic', stream_id='our-file')

    def test_nvstreamer_stream_never_exposed_raises_on_timeout(self):
        client = Mock()
        client.post.return_value = response({'sensorId': 'our-file', 'streamId': 'our-file'})
        client.get.return_value = response([])
        with contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(TimeoutError, 'not exposed'):
            exec(compile(cell_source('770b8dba'), str(NOTEBOOK), 'exec'), {
                'BASE': 'http://nginx', 'CLIP': '/unused.mp4', 'json': json,
                'open': lambda *a, **k: io.BytesIO(b'video'),
                'requests': client, 'remember': Mock(), 'time': FakeClock().module()})

    def test_registration_status_other_than_success_raises(self):
        client = Mock()
        client.post.return_value = response({'status': 'error', 'message': 'bad url'})
        with self.assertRaisesRegex(RuntimeError, 'registration failed'):
            exec(compile(cell_source('4b49547e'), str(NOTEBOOK), 'exec'),
                 {'BASE': 'http://nginx', 'requests': client, 'rtsp': 'rtsp://x'})

    def run_cell(self, initial='off', final='user', timelines=None, cameras=None):
        if timelines is None:
            timelines = [{'startTime': '2026-10-09T12:00:00Z', 'endTime': '2026-10-09T12:01:00Z'}]
        if cameras is None:
            cameras = [{'name': 'other', 'sensorId': 'wrong'},
                       {'name': 'Camera_1', 'sensorId': 'correct'}]
        status_calls = []

        def get(url, **kwargs):
            if url.endswith('/sensor/list'):
                return response(cameras)
            if url.endswith('/streams'):
                return response([{'isMain': True, 'streamId': 'main-stream'}])
            if url.endswith('/status'):
                status_calls.append(url)
                return response({'recordingStatus': initial if len(status_calls) == 1 else final})
            if url.endswith('/timelines'):
                return response(timelines)
            raise AssertionError(f'unexpected GET {url}')

        self.requests = Mock()
        self.requests.get.side_effect = get
        self.requests.post.return_value = response(None)
        self.clock = FakeClock()
        with contextlib.redirect_stdout(io.StringIO()):
            exec(CODE, {'BASE': 'http://nginx', 'requests': self.requests,
                        'remember': Mock(), 'time': self.clock.module()})

    def test_off_stream_is_started_and_correct_source_is_used(self):
        self.run_cell()
        self.requests.post.assert_called_once_with(
            'http://nginx/vios/vst/api/v1/record/main-stream/start', timeout=30)
        self.assertIn('/sensor/correct/streams', self.requests.get.call_args_list[1].args[0])

    def test_already_recording_is_reused(self):
        self.run_cell(initial='alwaysOn', final='alwaysOn')
        self.requests.post.assert_not_called()

    def test_inactive_recorder_is_not_reported_as_ready(self):
        with self.assertRaisesRegex(RuntimeError, 'not active'):
            self.run_cell(final='off')

    def test_empty_timeline_is_not_reported_as_ready(self):
        with self.assertRaisesRegex(TimeoutError, 'No recorded segment'):
            self.run_cell(timelines=[])
        self.assertGreater(self.clock.now_s - 1000.0, 180)

    def test_ambiguous_camera_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Expected one Camera_1'):
            self.run_cell(cameras=[{'name': 'Camera_1'}, {'name': 'Camera_1'}])
        self.requests.post.assert_not_called()

    def test_live_resources_are_recorded_in_the_ledger(self):
        remember = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            exec(CODE, {'BASE': 'http://nginx', 'requests': self._happy_requests(),
                        'remember': remember, 'time': FakeClock().module()})
        remember.assert_called_once_with('live', 'correct', name='Camera_1', stream_id='main-stream')

    def _happy_requests(self):
        def get(url, **kwargs):
            if url.endswith('/sensor/list'):
                return response([{'name': 'Camera_1', 'sensorId': 'correct'}])
            if url.endswith('/streams'):
                return response([{'isMain': True, 'streamId': 'main-stream'}])
            if url.endswith('/status'):
                return response({'recordingStatus': 'user'})
            return response([{'startTime': 'a', 'endTime': 'b'}])
        client = Mock()
        client.get.side_effect = get
        return client

    def test_release_cell_keeps_only_live_kinds(self):
        source = cell_source('nb02-release-code')
        self.assertIn('{"live", "nvstreamer"}', source)
        self.assertIn('run(confirm=True)', source)
        self.assertNotIn('try:', source)

    def test_all_notebook_python_cells_parse_and_outputs_are_clear(self):
        notebooks = sorted(TASK.glob('part1/*.ipynb'))
        self.assertEqual(len(notebooks), EXPECTED_NOTEBOOKS,
                         'notebook count changed; update EXPECTED_NOTEBOOKS: ' + ', '.join(p.name for p in notebooks))
        for path in notebooks:
            with self.subTest(notebook=path.name):
                for cell in json.loads(path.read_text())['cells']:
                    if cell['cell_type'] == 'code':
                        source = ''.join(cell['source'])
                        if not source.lstrip().startswith(('!', '%')):
                            compile(source, f"{path.name}:{cell['id']}", 'exec')
                        self.assertFalse(cell.get('outputs'), f"{path.name} {cell['id']}: outputs not stripped")
                        self.assertIsNone(cell.get('execution_count'), f"{path.name} {cell['id']}: execution count left")


if __name__ == '__main__':
    unittest.main()
