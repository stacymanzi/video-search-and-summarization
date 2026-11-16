"""Shared proxy address for container kernels and configured Brev host kernels."""
import json
import os
from pathlib import Path

# Host setup writes this file outside the container filesystem. Containers keep
# their Docker DNS address; host kernels use nginx's published loopback port.
_config_path = Path('/etc/vss-course-host.json')
_config = json.loads(_config_path.read_text()) if _config_path.is_file() else {}
BASE = os.environ.get('COURSE_BASE_URL', _config.get('base_url', 'http://nginx')).rstrip('/')
GRADE_URL = (BASE + '/assessment/grade/part1' if BASE != 'http://nginx'
             else 'http://assessment:8081/grade/part1')
