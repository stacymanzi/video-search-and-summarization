"""Consistent finite-video detection snapshots for tracklet exercises."""
import time
from course_setup import es, source_filter


def detection_snapshot(index, source, *, stable_s=30, timeout_s=1200, every_s=10,
                       max_frames=2000):
    """Return all source frames after counts stay nonzero and unchanged for stable_s.

    This is a bounded quiescence check, not a backend completion signal. A count
    change during the fetch invalidates the snapshot and restarts the wait.
    Fail rather than silently truncate when the teaching clip exceeds max_frames.
    """
    if min(stable_s, timeout_s, every_s, max_frames) <= 0:
        raise ValueError('Snapshot wait intervals, timeout and frame limit must be positive.')
    query = source_filter(source)
    start = time.monotonic()
    last, unchanged_since = None, start
    while True:
        now = time.monotonic()
        count = es('POST', f'/{index}/_count', json={'query': query})['count']
        if count != last or count == 0:
            last, unchanged_since = count, now
        if count > max_frames:
            raise RuntimeError(f'{source}: {count} frames exceeds the snapshot limit {max_frames}; '
                               'use a paginated query before analyzing the whole video.')
        if count > 0 and now - unchanged_since >= stable_s:
            result = es('POST', f'/{index}/_search', json={
                'query': query, 'size': max_frames, 'track_total_hits': True,
                'sort': [{'timestamp': 'asc'}],
                '_source': {'excludes': ['objects.embedding']}})
            total = result['hits']['total']
            total = total['value'] if isinstance(total, dict) else total
            hits = result['hits']['hits']
            after = es('POST', f'/{index}/_count', json={'query': query})['count']
            if count == total == len(hits) == after:
                print(f'{source}: snapshot of {count} frames after {stable_s}s of stable counts.')
                return hits
            last, unchanged_since = after, time.monotonic()
        elapsed = time.monotonic() - start
        if elapsed >= timeout_s:
            raise TimeoutError(f'{source} in {index}: no stable complete snapshot after '
                               f'{timeout_s}s (last count {last}); check RT-CV and Logstash.')
        print(f'{source}: {count} frames, waiting for {stable_s}s without new records '
              f'({elapsed:.0f}s elapsed).', flush=True)
        time.sleep(min(every_s, timeout_s - elapsed))
