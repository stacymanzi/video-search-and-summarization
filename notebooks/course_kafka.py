"""Bounded, read-only Kafka retrieval for the course's shared mdx-raw topic.

No consumer group commits, producer changes, or assumptions about source partitions.
"""
from collections import Counter
import subprocess
import time
from uuid import uuid4


def source_payloads(source, frame_type, *, max_messages=50000, batch_size=1000,
                    timeout_s=180):
    """Find a source in retained partition snapshots, searching backward in batches.

    frame_type is the deployment's Protobuf Frame class. Returned values remain
    bytes so the assessment still asks the learner to decode and check them.
    Budgets bound records examined and wall time, including subprocess startup.
    """
    if not source or min(max_messages, batch_size, timeout_s) <= 0:
        raise ValueError('Source and positive search budgets are required.')
    deadline = time.monotonic() + timeout_s
    seen = Counter()
    examined = 0

    def run(args, label):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f'Kafka search for {source!r} exceeded {timeout_s}s; '
                               f'examined {examined} messages; sources={dict(seen)}')
        try:
            result = subprocess.run(['docker', 'exec', 'kafka', *args],
                                    capture_output=True, timeout=min(40, left))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f'{label} timed out searching for {source!r}; '
                               f'examined {examined}; sources={dict(seen)}') from exc
        if result.returncode:
            raise RuntimeError(f'{label} failed (exit {result.returncode}): '
                               + result.stderr.decode(errors='replace')[-500:])
        return result.stdout

    def offsets(at):
        raw = run(['kafka-get-offsets', '--bootstrap-server', 'localhost:9092',
                   '--topic', 'mdx-raw', '--time', str(at)], f'Kafka offsets {at}')
        result = {}
        for line in raw.decode().splitlines():
            topic, partition, offset = line.rsplit(':', 2)
            if topic != 'mdx-raw':
                raise RuntimeError(f'Unexpected Kafka offset topic: {topic!r}')
            result[int(partition)] = int(offset)
        if not result:
            raise RuntimeError('mdx-raw has no partitions; check Kafka and RT-CV readiness.')
        return result

    starts = offsets(-2)  # earliest retained offsets, which need not be zero
    cursors = offsets(-1)  # fixed end snapshot: ongoing live traffic cannot extend the search
    if starts.keys() != cursors.keys():
        raise RuntimeError('Kafka partitions changed during the snapshot; rerun retrieval.')
    while any(cursors[p] > starts[p] for p in cursors) and examined < max_messages:
        for partition in sorted(cursors):
            end = cursors[partition]
            count = min(batch_size, end - starts[partition], max_messages - examined)
            if count <= 0:
                continue
            begin = end - count
            marker = ('<<course-' + uuid4().hex + '>>').encode()
            raw = run(['kafka-console-consumer', '--bootstrap-server', 'localhost:9092',
                       '--topic', 'mdx-raw', '--partition', str(partition),
                       '--offset', str(begin), '--max-messages', str(count),
                       '--timeout-ms', '10000', '--formatter-property',
                       'line.separator=' + marker.decode()],
                      f'Kafka partition {partition}, offsets [{begin}, {end})')
            parts = raw.split(marker)
            if parts[-1] or len(parts) - 1 != count:
                raise RuntimeError(f'Incomplete Kafka read for partition {partition}, '
                                   f'offsets [{begin}, {end}): expected {count}, '
                                   f'got {len(parts) - 1} framed messages. '
                                   'Retention may have advanced; rerun retrieval.')
            matches = []
            for payload in parts[:-1]:
                message = frame_type()
                try:
                    message.ParseFromString(payload)
                except Exception as exc:
                    raise RuntimeError(f'Invalid Frame in partition {partition}; check the VSS Protobuf schema.') from exc
                seen[message.sensorId] += 1
                examined += 1
                if message.sensorId == source:
                    matches.append(payload)
            cursors[partition] = begin
            if matches:
                print(f'Found {len(matches)} messages for {source!r} in partition {partition}; '
                      f'examined {examined} retained messages.')
                return matches
    reason = 'message budget reached' if examined >= max_messages else 'retained snapshot exhausted'
    raise RuntimeError(f'No Kafka frames for {source!r}: {reason}; examined {examined}; '
                       f'sources={dict(seen)}; earliest offsets={starts}; remaining ends={cursors}. '
                       'Check the RT-CV source name and Kafka retention. Elasticsearch can retain '
                       'frames after Kafka expires them. No producers were stopped.')
