#!/usr/bin/env bash
# Entrypoint of the `assessment` service: the lab image, a different process.
#
# Two listeners (task1/assessment/README.md):
#   8080  V1's Go assessment_runner. The LMS "ASSESS TASK" button calls it; it
#         executes tasktest.py, which reads the marker of every assessment
#         grader/rubric.json lists and prints one {"score","message"} JSON.
#   8081  grader/serve.py. The Submit cell of part1/assessment.ipynb POSTs to
#         http://assessment:8081/grade/part1; it runs the Part 1 grader and
#         writes /dli/assessment_results/part1.json.
# If either process exits, this script exits with its status so compose shows
# the failure (restart: unless-stopped brings it back). Nothing here falls back.
set -euo pipefail

RESULTS=/dli/assessment_results
ASSESSMENT=/dli/assessment
export GRADER_PORT=8081

# Clean start. A marker left by an earlier container would grade a learner who
# has not submitted anything in this one. The directory holds nothing but
# markers, so every JSON file goes, whatever an older course version named it.
mkdir -p "${RESULTS}"
rm -f "${RESULTS}"/*.json "${RESULTS}"/PASSED

cd "${ASSESSMENT}"
for required in assessment_runner tasktest.py grader/serve.py; do
    [ -e "${required}" ] || { echo "ERROR: ${ASSESSMENT}/${required} is missing" >&2; exit 1; }
done
chmod +x assessment_runner tasktest.py   # the runner may exec ./tasktest.py directly (shebang)

# /dli/task is writable from the lab, so nothing the grader or tasktest.py
# imports may come from it. The image's PYTHONPATH names /dli/task/part1, and
# both processes below (and every tasktest.py the runner starts) inherit this
# environment. serve.py adds /dli/assessment itself; tasktest.py needs only the
# standard library.
unset PYTHONPATH

python3 grader/serve.py &
serve_pid=$!
./assessment_runner &
runner_pid=$!
echo "[assessment] grader/serve.py pid ${serve_pid} on :${GRADER_PORT}; assessment_runner pid ${runner_pid} on :8080"

trap 'kill "${serve_pid}" "${runner_pid}" 2>/dev/null || true' EXIT

status=0
wait -n "${serve_pid}" "${runner_pid}" || status=$?
echo "ERROR: a required assessment process exited with status ${status}; stopping the container" >&2
exit "${status}"
