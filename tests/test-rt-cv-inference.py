"""Offline RT-CV test inside a fresh container; writes only container-local files.

Mount generated course configs at /course-configs, the restored cache at
/opt/storage, and traffic_short.mp4 at /test-data/traffic_short.mp4. Mount the
normal ds-start.sh and ds-search-start.sh at their production paths. No Kafka
or existing model volumes are required. Retains detector/encoder batch sizes.
"""
import configparser
from pathlib import Path
import shutil
import subprocess
import time

app = Path('/opt/nvidia/deepstream/deepstream/sources/apps/sample_apps/metropolis_perception_app')
configs = Path('/opt/ds-configs-ro')
shutil.copytree('/course-configs', configs, dirs_exist_ok=True)
config = configparser.ConfigParser(strict=False, interpolation=None)
config.read(configs / 'ds-main-config.txt')
config['source-list']['num-source-bins'] = '1'
config['source-list']['list'] = 'file:///test-data/traffic_short.mp4'
config['source-list']['sensor-id-list'] = 'rt-cv-cache-test'
config['source-list']['sensor-name-list'] = 'rt-cv-cache-test'
config['source-list']['http-port'] = '19000'
config['sink0']['enable'] = '1'
config['sink1']['enable'] = '0'
config['tests']['file-loop'] = '1'
for group in ('detections', 'tracklets'):
    # A repeat must use a fresh container, not count a previous run's outputs.
    Path('/tmp/cv-' + group).mkdir()
config['application']['gie-kitti-output-dir'] = '/tmp/cv-detections'
config['application']['kitti-track-output-dir'] = '/tmp/cv-tracklets'
with (configs / 'ds-main-config.txt').open('w') as stream:
    config.write(stream, space_around_delimiters=False)
started = time.monotonic()
log = Path('/tmp/cv-cache-test.log')
with log.open('w') as output:
    proc = subprocess.Popen(['bash', str(app / 'ds-search-start.sh')], cwd=app,
                            stdout=output, stderr=subprocess.STDOUT)
    try:
        deadline = started + 120
        while time.monotonic() < deadline:
            logs = log.read_text(errors='replace')
            assert 'Engine rebuild required' not in logs, 'SigLIP attempted to rebuild'
            assert 'Trying to create engine from model files' not in logs, 'Detector attempted to rebuild'
            assert proc.poll() is None, 'CV process exited; inspect /tmp/cv-cache-test.log'
            detections = [p for p in Path('/tmp/cv-detections').glob('*') if p.stat().st_size]
            tracks = [p for p in Path('/tmp/cv-tracklets').glob('*') if p.stat().st_size]
            if (detections and tracks and 'Existing engine is compatible' in logs
                    and 'Use deserialized engine model:' in logs):
                labels = {line.split()[0] for p in detections[:10] for line in p.read_text().splitlines() if line}
                assert labels & {'car', 'person', 'bicycle'}, labels
                print('PASS: cached engines, traffic detections and tracklets after',
                      round(time.monotonic() - started, 2), 'seconds', flush=True)
                print('Detected labels:', ', '.join(sorted(labels)), flush=True)
                break
            time.sleep(2)
        else:
            raise TimeoutError('No detection/tracklet output within 120 seconds; inspect test log')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
