#!/usr/bin/env bash
# Run after setup.sh to enable the existing Brev host JupyterLab.
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID == 0 ]] || { echo 'Run with sudo bash setup-brev-jupyter.sh'; exit 1; }
cd "$REPO_DIR"
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
    apt-get update
    apt-get install -y ffmpeg
fi
BREV_PYTHON=${BREV_PYTHON:-/home/ubuntu/.venv/bin/python}
BREV_UV=${BREV_UV:-/home/ubuntu/.local/bin/uv}
"$BREV_UV" pip install --python "$BREV_PYTHON" 'openai==1.100.2' Pillow 'protobuf>=5.27.3,<6'

lab_id=$(docker compose -f docker-compose.yml -f docker-compose.override.yml ps -q lab)
[[ -n "$lab_id" ]] || { echo 'Start the course lab container first.'; exit 1; }
port=$(docker port "$(docker compose -f docker-compose.yml -f docker-compose.override.yml ps -q nginx)" 80/tcp | head -1)
port=${port##*:}
[[ $port =~ ^[0-9]+$ ]] || { echo 'nginx has no published port'; exit 1; }
mkdir -p /dli
for name in task prep; do
    target="$REPO_DIR/lab/$name"
    if [[ -e /dli/$name || -L /dli/$name ]]; then
        [[ $(readlink -f "/dli/$name") == "$target" ]] || { echo "Conflicting /dli/$name; refusing to overwrite"; exit 1; }
    else
        ln -s "$target" "/dli/$name"
    fi
done
# Bind the existing volumes: host notebooks and containers see the SAME data.
# Persist the bind mounts for reboots; never copy or replace the course data.
for dest in /dli/task/data /dli/assessment_results; do
    source=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "'"$dest"'"}}{{.Source}}{{end}}{{end}}' "$lab_id")
    [[ -d "$source" ]] || { echo "Missing volume for $dest"; exit 1; }
    target=$(readlink -m "$dest")
    [[ "$source$target" != *[[:space:]]* ]] || { echo 'Mount paths must not contain whitespace'; exit 1; }
    mkdir -p "$target"
    opts=bind
    [[ "$dest" == /dli/assessment_results ]] && opts=bind,ro
    if ! mountpoint -q "$target"; then
        [[ -z $(ls -A "$target") ]] || { echo "$target is nonempty; refusing to hide files"; exit 1; }
        mount -o "$opts" "$source" "$target"
    fi
    entry="$source $target none $opts,nofail 0 0"
    grep -Fqx "$entry" /etc/fstab || printf '%s\n' "$entry" >> /etc/fstab
done
# The host notebook user needs to create registries and cleanup ledgers.
chown "$(stat -c '%u:%g' "$REPO_DIR/lab/task")" /dli/task/data
systemctl daemon-reload
printf '{"base_url":"http://127.0.0.1:%s"}\n' "$port" > /etc/vss-course-host.json
chmod 644 /etc/vss-course-host.json
docker exec "$(docker compose -f docker-compose.yml -f docker-compose.override.yml ps -q nginx)" nginx -t
docker exec "$(docker compose -f docker-compose.yml -f docker-compose.override.yml ps -q nginx)" nginx -s reload
echo "Brev kernel proxy: http://127.0.0.1:$port"
echo 'Restart notebook kernels and rerun from the first cell.'
