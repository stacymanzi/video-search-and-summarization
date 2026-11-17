import importlib.util
from pathlib import Path
from unittest.mock import Mock
import pytest

spec = importlib.util.spec_from_file_location('vlm_start', Path(__file__).resolve().parents[1] / 'deployment/startup/start-vlm-after-kafka.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_waits_for_broker_and_topic():
    empty, ready = Mock(), Mock()
    empty.partitions_for.return_value = None
    ready.partitions_for.return_value = {0}
    factory = Mock(side_effect=[ConnectionError(), empty, ready])
    module.wait_for_kafka(factory, ['broker:9092'], 'captions', clock=lambda: 0, sleep=Mock())
    assert factory.call_count == 3
    empty.close.assert_called_once()
    ready.close.assert_called_once()


def test_timeout_prevents_start():
    with pytest.raises(RuntimeError, match='refusing to start'):
        module.wait_for_kafka(Mock(side_effect=ConnectionError()), ['broker:9092'], 'captions',
                              timeout=1, clock=Mock(side_effect=[0, 2]))
