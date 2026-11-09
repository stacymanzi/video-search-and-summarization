#!/usr/bin/env bash
set -euo pipefail

profile="${1:-${VSS_PROFILE:-alerts}}"
source_dir=/dli/task/data
target_dir="/opt/vss/deploy/docker/data-dir/videos/dev-profile-${profile}"

mkdir -p "${target_dir}"
found=0
while IFS= read -r -d '' video; do
    install -m 0644 "${video}" "${target_dir}/$(basename "${video}")"
    found=$((found + 1))
done < <(
    find "${source_dir}" -maxdepth 1 -type f \
        \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.mov' \) -print0
)

[ "${found}" -gt 0 ] || {
    echo "No course videos found in ${source_dir}" >&2
    exit 1
}

echo "Staged ${found} video(s) in ${target_dir}"
curl -fsS -X POST http://dind:31000/vst/api/v1/sensor/scan >/dev/null ||
    echo "VST scan unavailable; run this command again after VSS is ready" >&2
