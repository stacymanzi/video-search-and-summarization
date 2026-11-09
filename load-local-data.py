"""Local data loader for Brev deployments (replaces the S3 loader).

Copies pre-staged course assets from ASSETS_DIR into the course_data volume,
then writes the same .course-data-status.json marker that vss-deploy and
course-prep poll. No S3 or AWS credentials needed.

Expected layout on the host (mounted at /brev-assets inside the container):
  /brev-assets/videos/traffic_short.mp4
  /brev-assets/videos/search-videos/normal_eval.mp4
  /brev-assets/videos/search-videos/rain_eval.mp4
  /brev-assets/videos/search-videos/fog_eval.mp4
  /brev-assets/videos/search-videos/night_eval.mp4
  /brev-assets/course-docs/          (built Sphinx site)

ground_truth/search_videos.json is copied from the repo image at GROUND_TRUTH_SRC.
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone

ASSETS_DIR = '/brev-assets'
DATA_DIR = '/data'
STATUS_NAME = '.course-data-status.json'
GROUND_TRUTH_SRC = '/repo/assessment/grader/answer_keys/ground_truth/search_videos.json'

VIDEO_FILES = [
    ('videos/traffic_short.mp4', 'traffic_short.mp4'),
    ('videos/search-videos/normal_eval.mp4', 'search-videos/normal_eval.mp4'),
    ('videos/search-videos/rain_eval.mp4', 'search-videos/rain_eval.mp4'),
    ('videos/search-videos/fog_eval.mp4', 'search-videos/fog_eval.mp4'),
    ('videos/search-videos/night_eval.mp4', 'search-videos/night_eval.mp4'),
]


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write_status(payload):
    path = os.path.join(DATA_DIR, STATUS_NAME)
    directory = DATA_DIR
    handle, tmp = tempfile.mkstemp(prefix=STATUS_NAME + '.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(handle, 'w') as f:
            json.dump(payload, f)
            f.write('\n')
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def copy_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        print(f'  skip (exists): {dst}')
        return
    print(f'  {src} -> {dst}')
    shutil.copy2(src, dst)


def load():
    print('Copying videos...')
    for src_rel, dst_rel in VIDEO_FILES:
        src = os.path.join(ASSETS_DIR, src_rel)
        dst = os.path.join(DATA_DIR, dst_rel)
        if not os.path.exists(src):
            raise FileNotFoundError(f'Missing asset: {src}')
        copy_file(src, dst)

    print('Copying course-docs site...')
    docs_src = os.path.join(ASSETS_DIR, 'course-docs')
    docs_dst = os.path.join(DATA_DIR, 'course-docs')
    if os.path.exists(docs_dst):
        print(f'  skip (exists): {docs_dst}')
    elif os.path.isdir(docs_src):
        shutil.copytree(docs_src, docs_dst)
        print(f'  copied {docs_src} -> {docs_dst}')
    else:
        print(f'  no course-docs asset found -- writing stub so the tile does not error')
        os.makedirs(docs_dst, exist_ok=True)
        with open(os.path.join(docs_dst, 'index.html'), 'w') as f:
            f.write('<html><body><p>Course Docs site not yet deployed.</p></body></html>\n')

    print('Copying ground_truth...')
    gt_dst = os.path.join(DATA_DIR, 'ground_truth', 'search_videos.json')
    if not os.path.exists(gt_dst):
        if not os.path.exists(GROUND_TRUTH_SRC):
            raise FileNotFoundError(f'Missing ground truth: {GROUND_TRUTH_SRC}')
        copy_file(GROUND_TRUTH_SRC, gt_dst)

    print('Done.')


if __name__ == '__main__':
    write_status({'state': 'RUNNING', 'started': utc_now()})
    try:
        load()
    except BaseException as error:
        write_status({'state': 'FAILED', 'error': type(error).__name__ + ': ' + str(error)})
        raise
    write_status({'state': 'OK', 'finished': utc_now()})
