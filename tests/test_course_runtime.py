"""Host and container kernels must select different routes without DNS guessing."""
import runpy
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'notebooks/course_runtime.py'


def test_container_defaults():
    with patch.dict('os.environ', {}, clear=True), patch.object(Path, 'is_file', return_value=False):
        config = runpy.run_path(str(MODULE))
    assert config['BASE'] == 'http://nginx'
    assert config['GRADE_URL'] == 'http://assessment:8081/grade/part1'


def test_host_config_and_override():
    for env, expected in [({}, 'http://127.0.0.1:18000'), ({'COURSE_BASE_URL': 'http://localhost:80/'}, 'http://localhost:80')]:
        with patch.dict('os.environ', env, clear=True), patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'read_text', return_value='{"base_url":"http://127.0.0.1:18000"}'):
            config = runpy.run_path(str(MODULE))
        assert config['BASE'] == expected
        assert config['GRADE_URL'] == expected + '/assessment/grade/part1'


def test_paths_follow_checkout():
    with patch.dict('os.environ', {}, clear=True), patch.object(Path, 'is_file', return_value=False):
        config = runpy.run_path(str(MODULE))
    assert config['TASK_ROOT'] == MODULE.parent
    assert config['DATA_DIR'] == MODULE.parent / 'data'
    assert config['PREP_DIR'] == MODULE.parent.parent / 'deployment/prep'
