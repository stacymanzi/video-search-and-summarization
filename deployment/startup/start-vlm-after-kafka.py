"""Gate every RTVI process start on Kafka, including Docker daemon restarts."""
import os
import sys
import time


def wait_for_kafka(factory, servers, topic, timeout=300, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    while True:
        producer = None
        try:
            producer = factory(bootstrap_servers=servers, api_version_auto_timeout_ms=5000,
                               request_timeout_ms=10000, max_block_ms=10000)
            if producer.partitions_for(topic):
                print(f'[course] Kafka topic {topic} is reachable; starting RTVI', flush=True)
                return
            raise RuntimeError('caption topic has no partitions yet')
        except Exception as exc:
            if clock() >= deadline:
                raise RuntimeError('Kafka not ready; refusing to start RTVI without Kafka publishing') from exc
            print(f'[course] Waiting for Kafka ({type(exc).__name__})', flush=True)
        finally:
            if producer is not None:
                producer.close(timeout=5)
        sleep(5)


def main():
    if os.environ.get('KAFKA_ENABLED', 'false').lower() == 'true':
        from kafka import KafkaProducer
        servers = os.environ['KAFKA_BOOTSTRAP_SERVERS'].split(',')
        wait_for_kafka(KafkaProducer, servers, os.environ.get('KAFKA_TOPIC', 'mdx-vlm-captions'))
    if '--check-only' not in sys.argv:
        entrypoint = os.environ.get('COURSE_RTVI_ENTRYPOINT', '/opt/nvidia/rtvi/start_rtvi_vlm.sh')
        os.execv(entrypoint, [entrypoint, *sys.argv[1:]])


if __name__ == '__main__':
    main()
