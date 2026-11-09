"""Run inside an isolated RT-Embed container with /test-data/traffic_short.mp4.

No external services required; never print full embedding vectors.
"""
import json
import math
import time

import requests

BASE = 'http://localhost:8000'
MODEL = 'cosmos-embed1-448p-anomaly-detection'
started = time.monotonic()
deadline = started + 300
while True:
    try:
        ready = requests.get(BASE + '/v1/ready', timeout=5).status_code == 200
    except requests.RequestException:
        ready = False
    if ready:
        break
    if time.monotonic() >= deadline:
        raise TimeoutError('RT-Embed did not become ready within 300 seconds')
    print('Waiting for RT-Embed:', round(time.monotonic() - started, 1), 'seconds', flush=True)
    time.sleep(5)
print('READY after', round(time.monotonic() - started, 1), 'seconds of polling', flush=True)

response = requests.post(BASE + '/v1/generate_text_embeddings', timeout=60,
                         json={'text_input': ['two vehicles collide at an intersection'], 'model': MODEL})
response.raise_for_status()
text_vectors = [item['embeddings'] for item in response.json()['data']]
assert len(text_vectors) == 1
assert all(len(v) == 768 and all(math.isfinite(x) for x in v) and any(x != 0 for x in v)
           for v in text_vectors)
print('PASS: text inference, one finite nonzero 768-dimensional embedding', flush=True)

with open('/test-data/traffic_short.mp4', 'rb') as video:
    response = requests.post(BASE + '/v1/files', timeout=60,
                             data={'purpose': 'vision', 'media_type': 'video',
                                   'creation_time': '2025-01-01T00:00:00.000Z'},
                             files={'file': ('traffic_short.mp4', video, 'video/mp4')})
response.raise_for_status()
file_id = response.json()['id']
try:
    response = requests.post(BASE + '/v1/generate_video_embeddings', timeout=180,
                             json={'id': file_id, 'model': MODEL, 'chunk_duration': 5,
                                   'creation_time': '2025-01-01T00:00:00.000Z'})
    response.raise_for_status()
    data = response.json()
    # Validate values recursively without assuming the service's chunk envelope.
    vectors = []
    def collect(obj):
        if isinstance(obj, dict):
            for value in obj.values():
                collect(value)
        elif isinstance(obj, list):
            if len(obj) == 768 and all(isinstance(x, (float, int)) for x in obj):
                vectors.append(obj)
            else:
                for value in obj:
                    collect(value)
    collect(data)
    assert vectors, 'No video embeddings returned; response fields: ' + ','.join(data)
    assert all(all(math.isfinite(x) for x in v) and any(x != 0 for x in v) for v in vectors)
    print('PASS: video inference,', len(vectors), 'finite nonzero 768-dimensional embeddings', flush=True)
finally:
    requests.delete(BASE + '/v1/files/' + file_id, timeout=30).raise_for_status()
print('PASS: temporary inference upload removed', flush=True)
