"""Scoped Part 1 cleanup. Never reset services, volumes, or whole ES indices.

Notebooks record resource ownership as they create it. Cleanup only removes
those resources, preserving input MP4s and the running VSS deployment.
"""
import json
import time
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import requests

LEDGER = Path('/dli/task/data/part1_resources.json')


class MetadataPending(RuntimeError):
    """A stopped producer's queued records are still reaching Elasticsearch."""


def remember(kind, resource_id, *, name='', stream_id='', ledger=LEDGER):
    if kind not in {'video', 'caption', 'live', 'nvstreamer'}:
        raise ValueError('Unknown resource kind')
    if not resource_id or not isinstance(resource_id, str):
        raise ValueError('A resource ID is required')
    data = json.loads(ledger.read_text()) if ledger.exists() else []
    entry = dict(kind=kind, id=resource_id, name=name, stream_id=stream_id)
    data = [r for r in data if (r['kind'], r['id']) != (kind, resource_id)]
    data.append(entry)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(ledger)


class Cleanup:
    def __init__(self, base='http://nginx', ledger=LEDGER, session=None):
        self.base = base.rstrip('/')
        self.ledger = ledger
        self.session = session or requests.Session()
        self.resources = json.loads(ledger.read_text()) if ledger.exists() else []
        # Validate the entire plan before sending any destructive request.
        for r in self.resources:
            if r['kind'] not in {'video', 'caption', 'live', 'nvstreamer'}:
                raise ValueError('Unknown resource kind in cleanup ledger')
            if not r['id'] or any(c in r['id'] for c in '*?/#'):
                raise ValueError('Invalid resource ID in cleanup ledger')
            if r['kind'] in {'video', 'live'} and not r['name']:
                raise ValueError('Source name missing from cleanup ledger')
            if r['kind'] == 'caption':
                UUID(r['id'])  # Caption index deletion must name exactly one UUID.

    def api(self, method, path, **kwargs):
        response = self.session.request(method, self.base + path, timeout=120, **kwargs)
        response.raise_for_status()
        data = response.json() if response.content else None
        if isinstance(data, dict) and (data.get('error_code') or
                data.get('status') in {'partial', 'failure', 'error'} or
                data.get('deleted') is False):
            raise RuntimeError(f'{method} {path}: {data}')
        return data

    def sensors(self, prefix='/vios/vst'):
        return {s['sensorId']: s for s in self.api('GET', prefix + '/api/v1/sensor/list')}

    def indices(self, pattern):
        rows = self.api('GET', '/es/_cat/indices/' + pattern,
                        params={'format': 'json', 'h': 'index',
                                'ignore_unavailable': 'true', 'allow_no_indices': 'true'})
        return [r['index'] for r in rows]

    def metadata_queries(self):
        sources = [r for r in self.resources if r['kind'] in {'video', 'live'}]
        if not sources:
            return []
        ids = sorted({r['id'] for r in sources} |
                     {r['stream_id'] for r in sources if r['stream_id']})
        names = sorted({r['name'] for r in sources})
        return [
            ('mdx-raw-*', {'terms': {'sensorId.keyword': names}}),
            ('mdx-behavior-*', {'terms': {'sensor.id.keyword': names}}),
            ('mdx-embed-*', {'terms': {'sensor.id.keyword': ids}}),
        ]

    def preview(self):
        if not self.resources:
            print('No Part 1 resources recorded. No cleanup will be performed.')
        for r in self.resources:
            print(f"{r['kind']:12} {r['name'] or r['id']}  {r['id']}")
        print('Input videos, model caches, services, and unrelated sources are preserved.')

    def remove_video(self, resource):
        # Stop RT-CV before removing its input or metadata. The agent's combined
        # delete reports "partial" when storage deletion already removed the
        # sensor, so explicitly check registration between those two operations.
        self.api('POST', '/rtvi-cv/api/v1/stream/remove',
                 headers={'x-stream-id': resource['id']}, json={
                     'key': 'sensor', 'value': {
                         'camera_id': resource['id'], 'camera_name': resource['name'],
                         'camera_url': '', 'change': 'camera_remove',
                         'metadata': {'resolution': '1920x1080', 'codec': 'h264', 'framerate': 30}},
                     'headers': {'source': 'vst'}})
        self.remove_storage(resource)
        rid = quote(resource['id'], safe='')
        if resource['id'] in self.sensors():
            self.api('DELETE', '/vios/vst/api/v1/sensor/' + rid)

    def remove_storage(self, resource):
        timelines = self.api('GET', '/vios/vst/api/v1/storage/timelines') or {}
        for stream in {resource['id'], resource['stream_id']} - {''}:
            windows = timelines.get(stream, [])
            if windows:
                self.api('DELETE', '/vios/vst/api/v1/storage/file/' + quote(stream, safe=''), params={
                    'startTime': min(w['startTime'] for w in windows),
                    'endTime': max(w['endTime'] for w in windows)})

    def run(self, *, confirm=False):
        if not confirm:
            raise ValueError('Review preview(), then call run(confirm=True).')
        if not self.resources:
            self.preview()
            return
        vios = self.sensors()
        # A name-based live delete must resolve to exactly the recorded source.
        for r in self.resources:
            if r['kind'] == 'live' and r['id'] in vios:
                matches = [s['sensorId'] for s in vios.values() if s.get('name') == r['name']]
                if matches != [r['id']]:
                    raise RuntimeError('Ambiguous live source; cleanup stopped before deletion.')

        # Stop producers and remove their recordings before deleting metadata.
        for r in sorted(self.resources, key=lambda r: r['kind'] != 'live'):
            rid = quote(r['id'], safe='')
            if r['kind'] == 'live':
                if r['id'] in vios:
                    self.api('DELETE', '/agent/api/v1/rtsp-streams/delete/' + quote(r['name'], safe=''))
                self.remove_storage(r)
            elif r['kind'] == 'video':
                self.remove_video(r)
            elif r['kind'] == 'caption':
                files = self.api('GET', '/rtvi-vlm/v1/files', params={'purpose': 'vision'})['data']
                if any(f['id'] == r['id'] for f in files):
                    # A busy-file response is an error, not a successful cleanup.
                    self.api('DELETE', '/rtvi-vlm/v1/files/' + rid)
            elif r['kind'] == 'nvstreamer':
                if r['id'] in self.sensors('/nvstreamer'):
                    if not r['stream_id']:
                        streams = self.api('GET', '/nvstreamer/api/v1/sensor/' + rid + '/streams')
                        main = [s for s in streams if s.get('isMain') and s.get('streamId')]
                        if len(main) != 1:
                            raise RuntimeError('Cannot resolve NVStreamer main stream; refusing to guess.')
                        r['stream_id'] = main[0]['streamId']
                    self.api('DELETE', '/nvstreamer/api/v1/storage/file/' + quote(r['stream_id'], safe=''))

        for r in self.resources:
            if r['kind'] == 'caption':
                index = 'default_' + str(UUID(r['id'])).replace('-', '_')
                if index in self.indices(index):
                    self.api('DELETE', '/es/' + index)

        # Kafka/analytics may still publish queued records after producers stop.
        # Remove only our records again until two spaced checks are clear.
        self.remove_metadata()
        clear_checks = 0
        for _ in range(12):
            time.sleep(5)
            try:
                self.verify()
            except MetadataPending:
                clear_checks = 0
                self.remove_metadata()
                print('Waiting for queued Part 1 metadata to drain...')
            else:
                clear_checks += 1
                if clear_checks == 2:
                    break
        else:
            raise MetadataPending('Part 1 metadata is still arriving after 60 seconds; rerun cleanup.')
        self.clear_saved_results()
        print('Cleanup complete: the resources listed above and their records were removed. VSS is still running.')
        print('Everything else, including the original MP4 files, is untouched.')

    def remove_metadata(self):
        for pattern, query in self.metadata_queries():
            for index in self.indices(pattern):
                result = self.api('POST', '/es/' + quote(index, safe='') + '/_delete_by_query',
                                  params={'refresh': 'true', 'conflicts': 'proceed'}, json={'query': query})
                # Concurrent analytics updates can conflict. Verification counts
                # remaining documents and the bounded drain loop retries them.
                if result.get('failures') or result.get('timed_out'):
                    raise RuntimeError('Metadata deletion was incomplete; rerun cleanup.')

    def verify(self):
        remaining = self.sensors()
        timelines = self.api('GET', '/vios/vst/api/v1/storage/timelines') or {}
        caption_ids = {f['id'] for f in self.api('GET', '/rtvi-vlm/v1/files',
                                                params={'purpose': 'vision'})['data']}
        for r in self.resources:
            if r['kind'] in {'video', 'live'} and r['id'] in remaining:
                raise RuntimeError('A Part 1 VIOS source is still registered.')
            if r['kind'] in {'video', 'live'} and (
                    timelines.get(r['id']) or timelines.get(r['stream_id'])):
                raise RuntimeError('Part 1 recorded footage remains in VIOS.')
            if r['kind'] == 'caption':
                if r['id'] in caption_ids:
                    raise RuntimeError('A Part 1 RT-VLM file remains.')
                index = 'default_' + str(UUID(r['id'])).replace('-', '_')
                if self.indices(index):
                    raise RuntimeError('Part 1 caption metadata remains.')
            if r['kind'] == 'nvstreamer' and r['id'] in self.sensors('/nvstreamer'):
                raise RuntimeError('A Part 1 NVStreamer source remains.')
        for pattern, query in self.metadata_queries():
            for index in self.indices(pattern):
                result = self.api('POST', '/es/' + quote(index, safe='') + '/_count', json={'query': query})
                if result['count']:
                    raise MetadataPending('Part 1 metadata remains or is still arriving. Wait, then rerun cleanup.')

    def clear_saved_results(self):
        """Invalidate only local pointers to the sources just removed."""
        ids = {r['id'] for r in self.resources if r['kind'] == 'video'}
        registry = self.ledger.parent / 'search_sources.json'
        if registry.exists():
            data = json.loads(registry.read_text())
            kept = {k: v for k, v in data.items() if v.get('sensor_id') not in ids}
            registry.write_text(json.dumps(kept, indent=2) + '\n')
        selection = self.ledger.parent / 'search_selection.json'
        if selection.exists() and json.loads(selection.read_text()).get('sensor_id') in ids:
            selection.unlink()
        pointer = Path.home() / 'sensor_id.txt'
        if pointer.exists() and pointer.read_text().strip() in ids:
            pointer.unlink()
