"""Course wrapper around the platform S3 loader (nvdli/s3_data_loader).

Three jobs, all about WHERE a key lands:
  1. Route the pinned model files straight into the vss_models volume
     (/data/models) and refuse to start if any pinned file is absent from S3.
  2. Keep the folder of explicit (non-recursive) keys under the course prefix.
     The stock loader writes a non-recursive line to /data/<basename>, which would
     put search-videos/normal_eval.mp4 at /data/normal_eval.mp4 and make the later
     recursive line download it a second time to the right place.
  3. Skip the legacy rt-vlm-cache/ archive if it was copied across from an older
     course prefix.

It also writes the status marker /data/.course-data-status.json (STATUS_NAME),
which is what vss-deploy and course-prep wait on now that no compose dependency
gates them on this container: RUNNING at start (replacing any earlier marker, so
a stale OK from a previous run can never satisfy a waiter), then OK, or FAILED
with the error before the exception is re-raised and the container exits
non-zero. Every write is atomic (temp file in the same directory, os.replace).

Run by the s3_data_loader service: `python /var/load-course-data.py`.
Unit tests: tests/test-course-loader.py (inside the loader image).
"""
from datetime import datetime, timezone
import json
import os
import sys
import tempfile

sys.path.insert(0, '/dli/app')
from loader import Loader

COURSE_PREFIX = 'data/c-fx-17-v2/'
MODEL_PREFIX = 'data/c-fx-17-v2-models/'
BUCKET = 'dli-lms'
DATA_DIR = '/data'                      # course_data; /dli/task/data in lab, dind, vss-deploy and course-prep
STATUS_NAME = '.course-data-status.json'


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write_status(path, payload):
    """Replace the marker atomically: a reader sees the old file or the new one, never half of either."""
    directory = os.path.dirname(path) or '.'
    handle, temporary = tempfile.mkstemp(prefix=STATUS_NAME + '.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(handle, 'w') as stream:
            json.dump(payload, stream)
            stream.write('\n')
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def run(load, status_path, now=utc_now):
    """RUNNING, then load(), then OK; on any exception FAILED with the error, then re-raise."""
    write_status(status_path, {'state': 'RUNNING', 'started': now()})
    try:
        load()
    except BaseException as error:
        write_status(status_path, {'state': 'FAILED', 'error': type(error).__name__ + ': ' + str(error)})
        raise
    write_status(status_path, {'state': 'OK', 'finished': now()})


def landing_path(item):
    """Where under /data the stock loader would write a course-volume key, or None for the model volume.

    Mirrors S3File.download: a recursive line (rel set) keeps the key relative to
    the listed prefix; an explicit key is placed by destination().
    """
    if item.bucket == BUCKET and item.key.startswith(MODEL_PREFIX):
        return None
    rel = getattr(item, 'rel', None)
    if rel:
        return os.path.normpath(os.path.relpath(item.key, rel))
    if item.bucket == BUCKET and item.key.startswith(COURSE_PREFIX):
        return os.path.normpath(item.key[len(COURSE_PREFIX):])
    return item.key.split('/')[-1]       # S3File.filename


def select_files(files, manifest, nemotron=None, embedding=None, cv=None):
    prefix = MODEL_PREFIX + 'rt-vlm/' + manifest['version'] + '/' + manifest['directory'] + '/'
    expected = {prefix + item['name'] for item in manifest['files']}
    if nemotron is not None:
        prefix = MODEL_PREFIX + 'nemotron/' + nemotron['version'] + '/' + nemotron['directory'] + '/'
        expected.update(prefix + item['name'] for item in nemotron['files'])
    if embedding is not None:
        prefix = MODEL_PREFIX + 'rt-embed/' + embedding['version'] + '/' + embedding['directory'] + '/'
        expected.update(prefix + item['name'] for item in embedding['files'])
    if cv is not None:
        prefix = MODEL_PREFIX + 'rt-cv/' + cv['version'] + '/' + cv['directory'] + '/'
        expected.update(prefix + item['name'] for item in cv['files'])
    selected, found = [], set()
    for item in files:
        # The marker belongs to this wrapper. An S3 object that would land on it
        # could overwrite a FAILED or RUNNING marker with arbitrary content.
        if landing_path(item) == STATUS_NAME:
            raise RuntimeError(item.key + ' would be written over the loader status marker /data/'
                               + STATUS_NAME + '; rename or remove it in S3')
        if item.bucket == BUCKET and item.key.startswith(COURSE_PREFIX):
            if item.key.startswith(COURSE_PREFIX + 'rt-vlm-cache/'):
                continue  # Legacy archive: keep it in S3, never download it.
        if item.bucket == BUCKET and item.key.startswith(MODEL_PREFIX):
            if item.key not in expected:
                continue
            found.add(item.key)
        selected.append(item)
    if found != expected:
        raise RuntimeError('Pinned model files missing from S3 listing: ' + ', '.join(sorted(expected - found)))
    return selected


def destination(item):
    """(directory, relative path) for one S3File. Pure, so the tests can pin it."""
    if item.bucket == BUCKET and item.key.startswith(MODEL_PREFIX):
        return '/models', item.filename
    if item.bucket == BUCKET and item.key.startswith(COURSE_PREFIX):
        return '', item.key[len(COURSE_PREFIX):]
    return '', item.filename


class CourseLoader(Loader):
    def get_data_locations(self):
        with open('/var/rt-vlm-checkpoint.json') as stream:
            manifest = json.load(stream)
        with open('/var/nemotron-checkpoint.json') as stream:
            nemotron = json.load(stream)
        with open('/var/rt-embed-checkpoint.json') as stream:
            embedding = json.load(stream)
        with open('/var/rt-cv-checkpoint.json') as stream:
            cv = json.load(stream)
        return select_files(super().get_data_locations(), manifest, nemotron, embedding, cv)

    def load(self):
        for item in self.get_data_locations():
            subdir, filename = destination(item)
            # Explicit nested keys (search-videos/...) keep their folder; the stock
            # loader never creates it, so make it before boto3 opens its temp file.
            os.makedirs(os.path.join(self.dest_dir + subdir, os.path.dirname(filename)), exist_ok=True)
            try:
                item.download(self.dest_dir + subdir, filename)
            except Exception as error:
                # boto3's 404 does not name the key; the marker and seed-status should.
                raise RuntimeError('s3://' + item.bucket + '/' + item.key + ': '
                                   + type(error).__name__ + ': ' + str(error)) from error


if __name__ == '__main__':
    # CourseLoader() itself probes the network (instance_in_china), so it is
    # constructed inside run(), after RUNNING is on disk.
    run(lambda: CourseLoader().load(), os.path.join(DATA_DIR, STATUS_NAME))
