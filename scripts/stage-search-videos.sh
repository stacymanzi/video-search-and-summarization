#!/usr/bin/env bash
# Devbox preparation. Keep source media out of the lab image and Git history.
set -euo pipefail
task_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
media_dir="${1:-${task_dir}/..}"
cd "${task_dir}"
lab_id="$(docker-compose ps -q lab)"
test -n "${lab_id}" || { echo 'Start the course lab first.' >&2; exit 1; }
for name in normal_eval.mp4 rain_eval.mp4 fog_eval.mp4 night_eval.mp4; do
    test -f "${media_dir}/${name}" || { echo "Missing ${media_dir}/${name}" >&2; exit 1; }
done
# /dli/task/data is the course_data named volume, not the notebook bind mount.
docker exec "${lab_id}" mkdir -p /dli/task/data/search-videos
for name in normal_eval.mp4 rain_eval.mp4 fog_eval.mp4 night_eval.mp4; do
    if docker exec "${lab_id}" test -f "/dli/task/data/search-videos/${name}"; then
        source_hash="$(sha256sum "${media_dir}/${name}" | cut -d' ' -f1)"
        staged_hash="$(docker exec "${lab_id}" sha256sum "/dli/task/data/search-videos/${name}" | cut -d' ' -f1)"
        test "${source_hash}" = "${staged_hash}" || {
            echo "Different staged copy of ${name}; review it before replacing or re-ingesting." >&2
            exit 1
        }
    else
        docker cp "${media_dir}/${name}" "${lab_id}:/dli/task/data/search-videos/${name}"
    fi
    echo "Staged ${name}"
done
echo 'Next: docker compose run --rm course-prep   (or run the instructor-only cell at the end of notebook 05)'
