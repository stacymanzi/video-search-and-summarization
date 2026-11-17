#!/usr/bin/env bash
# The assessment notebook submits to this Python HTTP grader.
set -euo pipefail
mkdir -p /dli/assessment_results
unset PYTHONPATH
exec python3 -I /dli/assessment/grader/serve.py
