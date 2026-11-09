"""Unit tests for load-course-data.py. No credentials, no network, no S3.

Two ways to run it:

  * Inside nvdli/s3_data_loader (the real loader at /dli/app/loader.py, the
    wrapper mounted at /var/load-course-data.py, as the compose service does):
    every test runs, including the two that exercise the real S3File.
  * Anywhere else (CI, a laptop): `python3 -I task1/tests/test-course-loader.py`.
    The real loader is absent, so a minimal stand-in `loader` module with the
    one base class the wrapper subclasses is installed, the wrapper is loaded
    from ../load-course-data.py, and the two real-S3File tests are skipped with
    a reason. They remain VERIFY ON DEVBOX items (docs/DEVBOX_VERIFY.md).

COURSE_LOADER_PATH and COURSE_LOADER_APP_DIR override the two locations.
"""
import importlib.util
import os
from pathlib import Path
import sys
import types
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parent
LOADER_APP_DIR = Path(os.environ.get('COURSE_LOADER_APP_DIR', '/dli/app'))
WRAPPER = Path(os.environ.get('COURSE_LOADER_PATH', '/var/load-course-data.py'))
if not WRAPPER.is_file():
    WRAPPER = HERE.parent / 'load-course-data.py'
REAL_LOADER = (LOADER_APP_DIR / 'loader.py').is_file()
if REAL_LOADER:
    sys.path.insert(0, str(LOADER_APP_DIR))
else:
    # Stand-in for nvdli/s3_data_loader: only what the wrapper subclasses. No
    # S3File here on purpose; the tests that need the real one are skipped.
    stand_in = types.ModuleType('loader')

    class Loader:  # noqa: D401 - mirrors the base class the wrapper overrides
        def get_data_locations(self):
            raise NotImplementedError('stand-in Loader: not the real nvdli/s3_data_loader')

        def load(self):
            raise NotImplementedError('stand-in Loader: not the real nvdli/s3_data_loader')

    stand_in.Loader = Loader
    sys.modules['loader'] = stand_in

spec = importlib.util.spec_from_file_location('course_loader', WRAPPER)
course = importlib.util.module_from_spec(spec)
spec.loader.exec_module(course)
NEEDS_REAL_LOADER = unittest.skipUnless(
    REAL_LOADER, 'needs the real loader (run inside nvdli/s3_data_loader); VERIFY ON DEVBOX')


class SelectionTests(unittest.TestCase):
    def test_cv_models_and_engines_are_pinned(self):
        cosmos = {'version': 'v1', 'directory': 'model', 'files': [{'name': 'config.json'}]}
        cv = {'version': 'h100-v1', 'directory': 'cv-cache',
              'files': [{'name': 'siglip.plan'}, {'name': 'rtdetr/detector.engine'}]}
        keys = ['data/c-fx-17-v2-models/rt-vlm/v1/model/config.json',
                'data/c-fx-17-v2-models/rt-cv/h100-v1/cv-cache/siglip.plan',
                'data/c-fx-17-v2-models/rt-cv/h100-v1/cv-cache/rtdetr/detector.engine',
                'data/c-fx-17-v2-models/rt-cv/old/cv-cache/siglip.plan',
                'data/c-fx-17-v2-models/rt-cv/h100-v1/cv-cache/unlisted']
        files = [SimpleNamespace(bucket='dli-lms', key=k) for k in keys]
        self.assertEqual([f.key for f in course.select_files(files, cosmos, cv=cv)], keys[:3])
        with self.assertRaisesRegex(RuntimeError, 'missing from S3 listing'):
            course.select_files(files[:2], cosmos, cv=cv)

    def test_embedding_model_and_engines_are_pinned(self):
        cosmos = {'version': 'v1', 'directory': 'model', 'files': [{'name': 'config.json'}]}
        embed = {'version': 'h100-v1', 'directory': 'embed-cache',
                 'files': [{'name': 'checkpoint/model/config.json'},
                           {'name': 'triton/model/video_embeddings/1/video.engine'}]}
        keys = ['data/c-fx-17-v2-models/rt-vlm/v1/model/config.json',
                'data/c-fx-17-v2-models/rt-embed/h100-v1/embed-cache/checkpoint/model/config.json',
                'data/c-fx-17-v2-models/rt-embed/h100-v1/embed-cache/triton/model/video_embeddings/1/video.engine',
                'data/c-fx-17-v2-models/rt-embed/h100-v0/embed-cache/checkpoint/model/config.json',
                'data/c-fx-17-v2-models/rt-embed/h100-v1/embed-cache/unlisted-runtime-file']
        files = [SimpleNamespace(bucket='dli-lms', key=k) for k in keys]
        self.assertEqual([f.key for f in course.select_files(files, cosmos, embedding=embed)], keys[:3])
        with self.assertRaisesRegex(RuntimeError, 'missing from S3 listing'):
            course.select_files(files[:2], cosmos, embedding=embed)

    def test_both_model_caches_are_required_and_version_pinned(self):
        cosmos = {'version': 'v1', 'directory': 'model', 'files': [{'name': 'config.json'}]}
        nemotron = {'version': 'v2', 'directory': 'nim-cache',
                    'files': [{'name': 'ngc/hub/model/blobs/abc'}]}
        keys = ['data/c-fx-17-v2-models/rt-vlm/v1/model/config.json',
                'data/c-fx-17-v2-models/nemotron/v2/nim-cache/ngc/hub/model/blobs/abc',
                'data/c-fx-17-v2-models/nemotron/v1/nim-cache/ngc/hub/model/blobs/abc',
                'data/c-fx-17-v2-models/nemotron/v2/nim-cache/unlisted-runtime-file']
        files = [SimpleNamespace(bucket='dli-lms', key=key) for key in keys]
        self.assertEqual([f.key for f in course.select_files(files, cosmos, nemotron)], keys[:2])
        with self.assertRaisesRegex(RuntimeError, 'missing from S3 listing'):
            course.select_files(files[:1], cosmos, nemotron)

    def test_only_pinned_files_and_course_assets(self):
        manifest = {'version': 'v1', 'directory': 'model', 'files': [{'name': 'config.json'}]}
        keys = ['data/c-fx-17-v2-docs/course-docs/index.html',
                'data/c-fx-17-v2/traffic_short.mp4',
                'data/c-fx-17-v2/search-videos/night_eval.mp4',
                'data/c-fx-17-v2/rt-vlm-cache/old/checkpoint.tar',
                'data/c-fx-17-v2-models/rt-vlm/v1/model/config.json',
                'data/c-fx-17-v2-models/rt-vlm/v1/model/.lock',
                'data/c-fx-17-v2-models/rt-vlm/v0/model/config.json']
        files = [SimpleNamespace(bucket='dli-lms', key=key) for key in keys]
        selected = course.select_files(files, manifest)
        self.assertEqual([item.key for item in selected], keys[:3] + [keys[4]])
        with self.assertRaisesRegex(RuntimeError, 'missing from S3 listing'):
            course.select_files(files[:4], manifest)

    @NEEDS_REAL_LOADER
    def test_real_loader_path_mapping(self):
        from loader import S3File
        from unittest.mock import patch
        file = S3File('dli-lms', 'data/c-fx-17-v2-models/rt-vlm/v1/model/config.json',
                      False, rel='data/c-fx-17-v2-models/')
        with patch('loader.os.path.exists', return_value=True), patch('builtins.print') as output:
            loader = object.__new__(course.CourseLoader)
            loader.dest_dir = '/data'
            loader.get_data_locations = lambda: [file]
            loader.load()
        self.assertIn('/data/models/rt-vlm/v1/model/config.json', output.call_args[0][0])
        file = S3File('dli-lms', 'data/c-fx-17-v2-models/nemotron/v2/nim-cache/ngc/hub/model/blobs/abc',
                      False, rel='data/c-fx-17-v2-models/')
        with patch('loader.os.path.exists', return_value=True), patch('builtins.print') as output:
            loader.get_data_locations = lambda: [file]
            loader.load()
        self.assertIn('/data/models/nemotron/v2/nim-cache/ngc/hub/model/blobs/abc', output.call_args[0][0])
        file = S3File('dli-lms', 'data/c-fx-17-v2-models/rt-embed/v1/embed-cache/triton/model/video.engine',
                      False, rel='data/c-fx-17-v2-models/')
        with patch('loader.os.path.exists', return_value=True), patch('builtins.print') as output:
            loader.get_data_locations = lambda: [file]
            loader.load()
        self.assertIn('/data/models/rt-embed/v1/embed-cache/triton/model/video.engine', output.call_args[0][0])
        file = S3File('dli-lms', 'data/c-fx-17-v2-models/rt-cv/v1/cv-cache/model.engine',
                      False, rel='data/c-fx-17-v2-models/')
        with patch('loader.os.path.exists', return_value=True), patch('builtins.print') as output:
            loader.get_data_locations = lambda: [file]
            loader.load()
        self.assertIn('/data/models/rt-cv/v1/cv-cache/model.engine', output.call_args[0][0])

    def test_explicit_course_keys_keep_their_folder(self):
        # A non-recursive data_sources line for a nested key must land under its
        # folder, not at /data/<basename>, or the recursive course line downloads
        # it again and a stray copy sits at the volume root.
        manifest = {'version': 'v1', 'directory': 'model', 'files': [{'name': 'config.json'}]}
        keys = ['data/c-fx-17-v2/search-videos/normal_eval.mp4',
                'data/c-fx-17-v2/warehouse/w1.mp4',
                'data/c-fx-17-v2/traffic_short.mp4',
                'data/c-fx-17-v2-docs/course-docs/index.html',
                'data/c-fx-17-v2-models/rt-vlm/v1/model/config.json']
        files = [SimpleNamespace(bucket='dli-lms', key=k, filename=k.rsplit('/', 1)[1]) for k in keys]
        self.assertEqual([course.destination(f) for f in course.select_files(files, manifest)],
                         [('', 'search-videos/normal_eval.mp4'),
                          ('', 'warehouse/w1.mp4'),
                          ('', 'traffic_short.mp4'),
                          ('', 'index.html'),            # docs: the loader's own relpath (basename here)
                          ('/models', 'config.json')])   # models: the loader's own relpath

    @NEEDS_REAL_LOADER
    def test_real_loader_explicit_key_path(self):
        # VERIFY ON DEVBOX: S3File's constructor signature for a non-recursive line
        # (no rel=) inside nvdli/s3_data_loader:latest.
        from loader import S3File
        from unittest.mock import patch
        file = S3File('dli-lms', 'data/c-fx-17-v2/search-videos/normal_eval.mp4', False)
        with patch('loader.os.path.exists', return_value=True), patch('builtins.print') as output:
            loader = object.__new__(course.CourseLoader)
            loader.dest_dir = '/data'
            loader.get_data_locations = lambda: [file]
            loader.load()
        self.assertIn('/data/search-videos/normal_eval.mp4', output.call_args[0][0])

    def test_nothing_can_land_on_the_status_marker(self):
        # A recursive course or docs line, or an explicit key with the marker's
        # basename, would overwrite /data/.course-data-status.json.
        manifest = {'version': 'v1', 'directory': 'model', 'files': [{'name': 'config.json'}]}
        pinned = SimpleNamespace(bucket='dli-lms', key='data/c-fx-17-v2-models/rt-vlm/v1/model/config.json')
        for stray in (SimpleNamespace(bucket='dli-lms', key='data/c-fx-17-v2/.course-data-status.json',
                                      rel='data/c-fx-17-v2/'),
                      SimpleNamespace(bucket='dli-lms', key='data/c-fx-17-v2-docs/.course-data-status.json',
                                      rel='data/c-fx-17-v2-docs/'),
                      SimpleNamespace(bucket='dli-lms', key='data/c-fx-17-v2/.course-data-status.json'),
                      SimpleNamespace(bucket='other', key='x/.course-data-status.json')):
            with self.assertRaisesRegex(RuntimeError, 'status marker'):
                course.select_files([stray, pinned], manifest)
        # The same name one folder down lands elsewhere and is allowed.
        nested = SimpleNamespace(bucket='dli-lms', key='data/c-fx-17-v2/sub/.course-data-status.json',
                                 rel='data/c-fx-17-v2/')
        self.assertEqual(course.select_files([nested, pinned], manifest), [nested, pinned])


class StatusMarkerTests(unittest.TestCase):
    """The marker vss-deploy and course-prep wait on: RUNNING, then OK or FAILED."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, course.STATUS_NAME)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        import json
        with open(self.path) as stream:
            return json.load(stream)

    def leftovers(self):
        return sorted(name for name in os.listdir(self.tmp.name) if name != course.STATUS_NAME)

    def test_running_is_written_before_the_load_and_replaces_a_stale_ok(self):
        course.write_status(self.path, {'state': 'OK', 'finished': 'yesterday'})
        seen = []
        course.run(lambda: seen.append(self.read()), self.path, now=lambda: 'T0')
        self.assertEqual(seen, [{'state': 'RUNNING', 'started': 'T0'}])

    def test_ok_after_a_successful_load(self):
        course.run(lambda: None, self.path, now=lambda: 'T1')
        self.assertEqual(self.read(), {'state': 'OK', 'finished': 'T1'})
        self.assertEqual(self.leftovers(), [])

    def test_failed_records_the_error_and_reraises(self):
        def load():
            raise KeyError('Contents')
        with self.assertRaises(KeyError):
            course.run(load, self.path, now=lambda: 'T2')
        self.assertEqual(self.read(), {'state': 'FAILED', 'error': "KeyError: 'Contents'"})
        self.assertEqual(self.leftovers(), [])


if __name__ == '__main__':
    unittest.main()
