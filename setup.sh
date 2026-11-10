#!/usr/bin/env bash
# Brev bootstrap for c-fx-17-v2 Part 1.
# Runs directly on the host as root (no Docker-in-Docker needed on Brev).
#
# Steps:
#   1. Configure host Docker daemon for cgroupfs (required by DeepStream/VSS)
#   2. Load NGC key from .env-dev
#   3. NGC docker login (nvcr.io)
#   4. Copy pre-staged course videos into the course_data Docker volume
#   5. Clone the VSS 3.2.1 upstream blueprint to /opt/vss
#   6. Apply course patches (preserve-volumes, RT-VLM into search, TrafficCamNet)
#   7. Run dev-profile.sh to deploy the full VSS search stack on the host daemon
#   8. Wait for VSS readiness
#   9. docker compose up for the 4 course services (lab, course-prep, assessment, nginx)
#
# Usage:
#   echo 'NGC_API_KEY=nvapi-...' > /path/to/repo/.env-dev
#   bash /path/to/repo/setup.sh
#
# Optional env vars:
#   BREV_ASSETS_DIR        Pre-staged assets dir (default: /home/ubuntu/course-assets)
#   VSS_FORCE_REDEPLOY     true = tear down and rebuild even if VSS is healthy
#   VSS_READY_TIMEOUT_S    Max seconds to wait for readiness (default: 3600)

set -euo pipefail
export COMPOSE_BAKE=false

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS_DIR="${BREV_ASSETS_DIR:-/home/ubuntu/course-assets}"

# Derive the compose volume name from COMPOSE_PROJECT_NAME so the volume we
# pre-populate in step 4 matches the one compose mounts (c-fx-17-v2-task1_course_data).
_cpn=$(grep '^COMPOSE_PROJECT_NAME=' "$REPO_DIR/.env" 2>/dev/null \
    | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)
COURSE_DATA_VOLUME="${_cpn:+${_cpn}_}course_data"
unset _cpn

log()  { echo "[setup] $*"; }
fail() { echo "[setup] FATAL: $*" >&2; exit 1; }

# VSS deploy parameters (match task1/.env defaults)
: "${VSS_VERSION:=3.2.1}"
: "${VSS_PROFILE:=search}"
: "${HARDWARE_PROFILE:=RTXPRO6000BW}"
: "${LLM_DEVICE_ID:=1}"
: "${VLM_DEVICE_ID:=1}"
: "${VSS_RT_VLM_DEVICE_ID:=0}"
: "${VSS_RT_VLM_PORT:=8018}"
: "${VSS_RT_VLM_MODEL_PATH:=ngc:nim/nvidia/cosmos3-super-reasoner:modelopt-fp8-final_format_fix}"
: "${VSS_RT_VLM_GPU_MEMORY_UTILIZATION:=0.65}"
: "${VSS_NUM_STREAMS:=4}"
: "${ENABLE_CRITIC:=false}"
: "${VSS_READY_TIMEOUT_S:=3600}"
export ENABLE_CRITIC
# Public port: what the BROWSER uses. 80 matches the course nginx.
# dev-profile.sh reads this from the environment and bakes it into generated.env.
export VSS_PUBLIC_PORT=80

SRC=/opt/vss
# Create early: docker bind-mounts an absent source as root-owned empty dir,
# which shadows the real checkout once it arrives. Exists before `up`.
mkdir -p "${SRC}/_course-runtime"
REPO_URL="https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git"
TAG="v${VSS_VERSION}"
SEED_STATUS_DIR="${SRC}/_course-runtime"
SEED_STATUS_FILE="${SEED_STATUS_DIR}/seed-status"
# Exported so wait-for-vss.sh can find the same file.
export SEED_STATUS_FILE

status() {
    mkdir -p "${SEED_STATUS_DIR}" 2>/dev/null
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "${SEED_STATUS_FILE}" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 1. Configure host Docker for cgroupfs
#    DeepStream (RT-CV) requires the cgroupfs driver. Ubuntu 22 defaults to
#    systemd; this configures the host daemon and restarts it once.
# ---------------------------------------------------------------------------
log "checking Docker cgroup driver"
current_driver=$(docker info --format '{{.CgroupDriver}}' 2>/dev/null || true)
if [[ "$current_driver" != "cgroupfs" ]]; then
    log "cgroup driver is '${current_driver:-unknown}'; switching to cgroupfs"
    mkdir -p /etc/docker
    if [[ -f /etc/docker/daemon.json ]]; then
        # Preserve existing settings; merge exec-opts
        python3 - <<'PY'
import json, sys
try:
    d = json.load(open('/etc/docker/daemon.json'))
except Exception:
    d = {}
opts = d.get('exec-opts', [])
key = 'native.cgroupdriver=cgroupfs'
if key not in opts:
    opts.append(key)
d['exec-opts'] = opts
with open('/etc/docker/daemon.json.new', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
PY
        mv /etc/docker/daemon.json.new /etc/docker/daemon.json
    else
        printf '{"exec-opts": ["native.cgroupdriver=cgroupfs"]}\n' > /etc/docker/daemon.json
    fi
    systemctl restart docker
    for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
    docker info >/dev/null 2>&1 || fail "Docker daemon did not come back after cgroupfs restart"
    log "Docker restarted; cgroup driver: $(docker info --format '{{.CgroupDriver}}')"
else
    log "Docker already using cgroupfs, no restart needed"
fi

# ---------------------------------------------------------------------------
# 2. Load NGC key
# ---------------------------------------------------------------------------
if [[ -f "$REPO_DIR/.env-dev" ]]; then
    log "loading NGC key from .env-dev"
    # shellcheck disable=SC1090
    source <(grep -E '^NGC_API_KEY=' "$REPO_DIR/.env-dev")
fi
if [[ -z "${NGC_API_KEY:-}" ]]; then
    fail "NGC_API_KEY is not set and .env-dev does not exist.
  Write it first:  echo 'NGC_API_KEY=nvapi-...' > $REPO_DIR/.env-dev
  Then re-run:     bash $REPO_DIR/setup.sh"
fi
: "${NGC_CLI_API_KEY:=${NGC_API_KEY}}"
: "${NGC_CLI_ORG:=nvidia}"
export NGC_CLI_API_KEY NGC_CLI_ORG

# ---------------------------------------------------------------------------
# 3. NGC docker login
# ---------------------------------------------------------------------------
status "RUNNING: NGC login"
log "logging in to nvcr.io"
if ! printf '%s' "${NGC_CLI_API_KEY}" | \
        docker login nvcr.io --username '$oauthtoken' --password-stdin 2>&1; then
    fail "nvcr.io login failed; check the key in .env-dev"
fi
# Preflight: verify the key can see the core VSS image before pulling 93 GB.
NGC_PREFLIGHT_IMAGE="nvcr.io/nvidia/vss-core/vss-agent:${VSS_VERSION}"
log "checking NGC entitlement ($NGC_PREFLIGHT_IMAGE)"
if ! timeout 120 docker manifest inspect "$NGC_PREFLIGHT_IMAGE" >/dev/null 2>&1; then
    fail "NGC key logged in but cannot see $NGC_PREFLIGHT_IMAGE; the key lacks the vss-core entitlement"
fi
log "NGC login OK; vss-core entitlement confirmed"

# ---------------------------------------------------------------------------
# 4. Copy pre-staged course videos into the course_data Docker volume
# ---------------------------------------------------------------------------
status "RUNNING: copying course videos"
log "verifying pre-staged assets at $ASSETS_DIR"
REQUIRED_VIDEOS=(
    "videos/traffic_short.mp4"
    "videos/search-videos/normal_eval.mp4"
    "videos/search-videos/rain_eval.mp4"
    "videos/search-videos/fog_eval.mp4"
    "videos/search-videos/night_eval.mp4"
)
for rel in "${REQUIRED_VIDEOS[@]}"; do
    [[ -f "$ASSETS_DIR/$rel" ]] || \
        fail "Missing required asset: $ASSETS_DIR/$rel\nUpload course assets to $ASSETS_DIR before running setup."
done

log "copying videos into course_data volume ($COURSE_DATA_VOLUME)"
docker volume create "$COURSE_DATA_VOLUME" 2>/dev/null || true
# Use a one-shot container so volume permissions are correct.
docker run --rm \
    -v "$ASSETS_DIR:/brev-assets:ro" \
    -v "${COURSE_DATA_VOLUME}:/data" \
    -v "$REPO_DIR/assessment/grader/answer_keys/ground_truth:/gt:ro" \
    python:3.11-slim python3 -c "
import shutil, os, json, tempfile
from datetime import datetime, timezone

assets, data = '/brev-assets', '/data'

for src_rel, dst_rel in [
    ('videos/traffic_short.mp4',              'traffic_short.mp4'),
    ('videos/search-videos/normal_eval.mp4',  'search-videos/normal_eval.mp4'),
    ('videos/search-videos/rain_eval.mp4',    'search-videos/rain_eval.mp4'),
    ('videos/search-videos/fog_eval.mp4',     'search-videos/fog_eval.mp4'),
    ('videos/search-videos/night_eval.mp4',   'search-videos/night_eval.mp4'),
]:
    src = os.path.join(assets, src_rel)
    dst = os.path.join(data, dst_rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        print(f'  skip (exists): {dst}')
    else:
        shutil.copy2(src, dst)
        print(f'  copied: {dst}')

docs_dst = os.path.join(data, 'course-docs')
if not os.path.exists(docs_dst):
    docs_src = os.path.join(assets, 'course-docs')
    if os.path.isdir(docs_src):
        shutil.copytree(docs_src, docs_dst)
        print(f'  copied course-docs')
    else:
        os.makedirs(docs_dst)
        open(os.path.join(docs_dst, 'index.html'), 'w').write(
            '<html><body><p>Course Docs site not yet deployed.</p></body></html>')
        print(f'  wrote course-docs stub')

gt_dst = os.path.join(data, 'ground_truth', 'search_videos.json')
if not os.path.exists(gt_dst):
    os.makedirs(os.path.dirname(gt_dst), exist_ok=True)
    shutil.copy2('/gt/search_videos.json', gt_dst)
    print('  copied ground_truth/search_videos.json')

path = os.path.join(data, '.course-data-status.json')
_, tmp = tempfile.mkstemp(dir=data)
with open(tmp, 'w') as f:
    json.dump({'state': 'OK', 'finished': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}, f)
os.replace(tmp, path)
print('  wrote .course-data-status.json OK')
"
log "course videos ready"

# ---------------------------------------------------------------------------
# 5. Clone the VSS 3.2.1 upstream blueprint to /opt/vss
#    Uses init+fetch+checkout to work in a pre-existing directory.
# ---------------------------------------------------------------------------
if [[ ! -d "$SRC/.git" ]]; then
    status "RUNNING: cloning VSS blueprint $TAG"
    log "cloning $TAG from $REPO_URL to $SRC"
    if ! git_err=$( { git init -q "$SRC" \
            && git -C "$SRC" remote add origin "$REPO_URL" \
            && git -C "$SRC" fetch -q --depth 1 origin "refs/tags/${TAG}:refs/tags/${TAG}" \
            && git -C "$SRC" checkout -q "$TAG"; } 2>&1 ); then
        log "git error output:"
        printf '%s\n' "$git_err" | sed 's/^/  /'
        fail "could not check out $TAG from $REPO_URL"
    fi
    log "cloned $TAG to $SRC"
else
    log "VSS blueprint already present at $SRC"
fi

DEPLOY_SH="$SRC/deploy/docker/scripts/dev-profile.sh"
[[ -f "$DEPLOY_SH" ]] || fail "expected $DEPLOY_SH in the upstream repo; layout may have changed"

# ---------------------------------------------------------------------------
# 6. Apply course patches
# ---------------------------------------------------------------------------
status "RUNNING: applying course patches"

# jq is used by patch_rt_cv_to_trafficcamnet; install if missing.
if ! command -v jq >/dev/null 2>&1; then
    log "installing jq (required for behaviour-analytics config patch)"
    apt-get update -qq && apt-get install -y -qq jq
fi

# 6a. Preserve volumes: prevent dev-profile.sh 'down' from deleting model caches.
#     The same patch as in deploy-vss.sh (verified against v3.2.1).
patch_preserve_volumes() {
    local script=$1
    grep -Fq 'docker compose -p mdx down' "$script" || \
        fail 'upstream teardown interface changed (expected: docker compose -p mdx down)'
    if ! grep -Fq '# DLI: preserve cached and rollback volumes' "$script"; then
        grep -Fq 'dangling_volumes=$(docker volume ls -q -f "dangling=true")' "$script" || \
            fail 'upstream volume cleanup interface changed'
        sed -i \
            -e 's/docker compose -p mdx down -v --remove-orphans/docker compose -p mdx down --remove-orphans/g' \
            -e 's/(with volumes)/(preserving volumes)/g' \
            -e 's/Removing dangling docker volumes/Preserving dangling docker volumes for course rollback/g' \
            -e 's/dangling_volumes=$(docker volume ls -q -f "dangling=true")/dangling_volumes="" # DLI: preserve cached and rollback volumes/' \
            "$script"
    fi
    if grep -Eq '^[[:space:]]*docker compose.*down.*(-v|--volumes)' "$script"; then
        fail 'upstream teardown still removes volumes after patch; refusing to continue'
    fi
    if ! grep -Fq '# DLI: preserve course data directory' "$script"; then
        grep -Fq '$_sudo rm -rf "${data_directory}"' "$script" || \
            fail 'upstream data cleanup interface changed'
        sed -i \
            -e 's/^[[:space:]]*\$_sudo rm -rf "${data_directory}"/      : # DLI: preserve course data directory/' \
            -e 's/Deleting data directory:/Preserving course data directory:/g' \
            -e 's/Data directory deleted/Data directory preserved/g' "$script"
    fi
    log 'teardown safeguard active: named volumes and course video data preserved'
}
patch_preserve_volumes "$DEPLOY_SH"

# 6b. Activate RT-VLM under the search profile.
#     At v3.2.1 the search profile has no rtvi-vlm. This writes compose.override.yml
#     (auto-loaded by dev-profile.sh because it cd's to deploy/docker) and appends
#     the required env vars to the profile's .env.
patch_rt_vlm_into_search() {
    local docker_dir="$SRC/deploy/docker"
    local override="$docker_dir/compose.override.yml"
    local profile_env="$docker_dir/developer-profiles/dev-profile-${VSS_PROFILE}/.env"

    [[ -f "$profile_env" ]] || fail "expected $profile_env; upstream layout changed at $TAG"

    # profiles is NOT in Compose's concat set; must restate ALL original values.
    cat > "$override" <<'OVERRIDE'
# Generated by setup.sh (Brev deploy). Do not edit by hand.
# Activates rtvi-vlm under the search profile. See setup.sh for why.
services:
  rtvi-embed:
    environment:
      INSTALL_PROPRIETARY_CODECS: "false"
  rtvi-vlm:
    environment:
      INSTALL_PROPRIETARY_CODECS: "false"
      HF_HUB_OFFLINE: "0"
      TRANSFORMERS_OFFLINE: "0"
    profiles:
      - bp_wh_2d
      - bp_developer_alerts_2d_vlm
      - bp_developer_alerts_2d_cv
      - bp_developer_base_2d_IGX-THOR
      - bp_developer_base_2d_AGX-THOR
      - bp_developer_lvs_2d
      - bp_developer_search_2d
OVERRIDE
    log "wrote $override (rtvi-vlm added to bp_developer_search_2d)"

    # Appended, not merged: later assignments win in a shell-sourced env file,
    # so this overrides any upstream defaults for the same names.
    cat >> "$profile_env" <<EOF

# --- appended by setup.sh (Brev deploy) ---
# RTVI_VLM_KAFKA_TOPIC: without this, captions go to mdx-vlm (not consumed by
#   Logstash) and Elasticsearch stays empty even though RT-VLM is healthy.
# RTVI_VLLM_GPU_MEMORY_UTILIZATION: empty default lets vLLM claim its whole card
#   and collide with the LLM NIM sharing GPU ${LLM_DEVICE_ID}.
# ELASTICSEARCH_ILM_MIN_AGE: VSS default is 4h; course indices are created at
#   launch and must survive the full class day (and multi-day devboxes).
RTVI_VLM_PORT=${VSS_RT_VLM_PORT}
RT_VLM_DEVICE_ID='${VSS_RT_VLM_DEVICE_ID}'
RTVI_VLM_IMAGE_TAG="${VSS_VERSION}"
RTVI_VLM_MODEL_TO_USE=cosmos-reason3
RTVI_VLM_MODEL_PATH='${VSS_RT_VLM_MODEL_PATH}'
COURSE_RT_VLM_OFFLINE=0
RTVI_VLM_KAFKA_ENABLED=true
RTVI_VLM_KAFKA_TOPIC=mdx-vlm-captions
RTVI_VLM_KAFKA_INCIDENT_TOPIC=mdx-vlm-incidents
RTVI_VLLM_GPU_MEMORY_UTILIZATION='${VSS_RT_VLM_GPU_MEMORY_UTILIZATION}'
RTVI_VLM_MAX_MODEL_LEN=32768
RTVI_VLM_BASE_URL=http://\${HOST_IP}:${VSS_RT_VLM_PORT}
NUM_STREAMS=${VSS_NUM_STREAMS}
ELASTICSEARCH_ILM_MIN_AGE=30d
EOF
    log "appended RT-VLM config to dev-profile-${VSS_PROFILE}/.env"
}
patch_rt_vlm_into_search

# 6c. Swap RT-CV from upstream's warehouse detector to TrafficCamNet RT-DETR.
#     The search profile ships the warehouse model (7 person/vehicle warehouse classes);
#     on traffic footage it detects almost nothing. TrafficCamNet (5 traffic classes:
#     background, bicycle, car, person, road_sign) is what the course teaches.
patch_rt_cv_to_trafficcamnet() {
    local app="$SRC/deploy/docker/developer-profiles/dev-profile-${VSS_PROFILE}/video-analytics-2d-app"
    local cfg="$app/deepstream/configs"
    local init_script="$app/deepstream/scripts/download-vision-encoder.sh"
    local analytics_cfg="$app/vss-search-analytics/configs/vss-search-analytics-kafka-config.json"

    [[ -d "$cfg" ]]         || fail "expected $cfg; upstream layout changed at $TAG"
    [[ -f "$init_script" ]] || fail "expected $init_script; upstream layout changed at $TAG"

    cat > "$cfg/rtdetr-its-labels.txt" <<'LABELS'
background
bicycle
car
person
road_sign
LABELS

    # Lifted from the alerts profile's proven TrafficCamNet config.
    # Key differences from warehouse: different parser (NvDsInferParseCustomDDETRTAO),
    # different library (_tao variant), different input dims (3;544;960), cluster-mode 4.
    cat > "$cfg/ds-pgie-config.yml" <<'PGIE'
# Replaced by setup.sh: TrafficCamNet RT-DETR (smart-city) in place of upstream warehouse RT-DETR.
class-attrs-all:
  pre-cluster-threshold: 0.5
  topk: 20
property:
  cluster-mode: 4
  gie-unique-id: 1
  gpu-id: 0
  scaling-filter: 1
  infer-dims: 3;544;960
  offsets: 0;0;0
  interval: 0
  labelfile-path: rtdetr-its-labels.txt
  maintain-aspect-ratio: 1
  model-color-format: 0
  net-scale-factor: 0.00392156862745098
  network-mode: 2
  network-type: 0
  onnx-file: /opt/storage/rtdetr-its/model_epoch_035.fp16.onnx
  num-detected-classes: 5
  output-tensor-meta: 1
  output-blob-names: pred_boxes;pred_logits
  parse-bbox-func-name: NvDsInferParseCustomDDETRTAO
  strongly-typed: 1
  custom-lib-path: /opt/nvidia/deepstream/deepstream/lib/libnvds_infercustomparser_tao.so
  workspace-size: 1048576
PGIE
    log "wrote TrafficCamNet ds-pgie-config.yml + labels"

    # Widen behaviour-analytics stateManagementFilter to TrafficCamNet classes.
    # The search profile ships ["Person"]; with TrafficCamNet that is almost nothing
    # on traffic footage, so mdx-behavior stays empty with no visible error.
    if [[ -f "$analytics_cfg" ]]; then
        local tmp_cfg
        tmp_cfg=$(mktemp)
        jq '(.app[] | select(.name=="stateManagementFilter") | .value) = "[\"car\",\"person\",\"bicycle\"]"
            | (.app[] | select(.name=="behaviorWatermarkSec")   | .value) = "5"
            | (.app[] | select(.name=="behaviorStateTimeout")   | .value) = "3"' \
            "$analytics_cfg" > "$tmp_cfg" \
            && mv "$tmp_cfg" "$analytics_cfg" \
            || { rm -f "$tmp_cfg"; fail "could not patch $analytics_cfg"; }
        chmod 644 "$analytics_cfg"
        log "patched behaviour-analytics stateManagementFilter for TrafficCamNet classes"
    else
        log "WARNING: $analytics_cfg not found; mdx-behavior will stay empty"
    fi

    # Insert TrafficCamNet download BEFORE the early-exit marker.
    # MUST be before, not after: the marker short-circuits on restart, so anything
    # appended after it runs exactly once and cannot be retried.
    if grep -q 'DLI course: TrafficCamNet' "$init_script"; then
        log "TrafficCamNet download step already present in $(basename "$init_script")"
        return 0
    fi
    local marker_line
    marker_line=$(grep -n 'if \[\[ -f "\$MARKER" \]\]' "$init_script" | head -1 | cut -d: -f1)
    [[ -n "$marker_line" ]] || \
        fail "could not find the marker check in $(basename "$init_script"); upstream layout changed"

    local block
    block=$(mktemp)
    cat > "$block" <<'DOWNLOAD'

# ---------------------------------------------------------------------------
# DLI course: TrafficCamNet RT-DETR for the smart-city sandbox.
# Inserted by setup.sh before the early-exit marker so it stays retryable.
# ---------------------------------------------------------------------------
ITS_DEST="/opt/storage/rtdetr-its"
ITS_ONNX="${ITS_DEST}/model_epoch_035.fp16.onnx"
ITS_NGC="nvidia/tao/trafficcamnet_transformer_lite:deployable_resnet50_v2.0"
ITS_UNPACKED="trafficcamnet_transformer_lite_vdeployable_resnet50_v2.0"

if [[ -f "${ITS_ONNX}" ]]; then
  echo "##### TrafficCamNet already present at ${ITS_ONNX} #####"
else
  echo "##### Downloading TrafficCamNet RT-DETR (${ITS_NGC}) #####"
  mkdir -p "${ITS_DEST}"
  if ! command -v ngc >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq wget unzip > /dev/null
    ( cd /tmp && wget -q https://ngc.nvidia.com/downloads/ngccli_linux.zip -O ngccli_linux.zip \
        && unzip -qo ngccli_linux.zip && chmod +x ngc-cli/ngc )
    export PATH="/tmp/ngc-cli:$PATH"
  fi
  ( cd /tmp \
    && rm -rf "${ITS_UNPACKED}" \
    && ngc registry model download-version "${ITS_NGC}" --org nvidia --dest /tmp \
    && mv "${ITS_UNPACKED}/resnet50_trafficcamnet_rtdetr.fp16.onnx" "${ITS_ONNX}" \
    && rm -rf "${ITS_UNPACKED}" ) \
    || echo "##### WARNING: TrafficCamNet download failed; RT-CV will not start #####"
  if [[ -f "${ITS_ONNX}" ]]; then
    chown -R "${STORAGE_UID}:${STORAGE_GID}" "${ITS_DEST}"
    chmod 0777 "${ITS_DEST}"
    chmod 0644 "${ITS_ONNX}"
    echo "##### TrafficCamNet installed at ${ITS_ONNX} #####"
  fi
fi
DOWNLOAD
    sed -i "$((marker_line - 1))r ${block}" "$init_script"
    rm -f "$block"
    log "inserted TrafficCamNet download into $(basename "$init_script") (before line $marker_line)"

    # On a devbox another service (e.g. MinIO) may already hold port 9000, which
    # DeepStream's CivetWeb REST server uses. Detect the conflict and change to
    # 9002. On a fresh Brev instance port 9000 is always free; this is a no-op.
    local cv_port=9000
    if cat /proc/net/tcp /proc/net/tcp6 2>/dev/null \
            | awk -v p="$(printf '%04X' $cv_port)" '$2 ~ /:'"$(printf '%04X' $cv_port)"'$/ || $2 ~ /^[0-9A-Fa-f]*:'"$(printf '%04X' $cv_port)"'$/ {found=1} END {exit !found}'; then
        local alt_port=9002
        log "port $cv_port is in use on this host; switching RTVI_CV_PORT to $alt_port to avoid DeepStream REST conflict"
        local ds_cfg="$app/deepstream/configs"
        sed -i "s/^http-port=${cv_port}$/http-port=${alt_port}/" \
            "$ds_cfg/ds-main-config.txt" \
            "$ds_cfg/ds-main-redis-config.txt" 2>/dev/null || true
        sed -i "s|\"provisioning_address\": \"localhost:${cv_port}\"|\"provisioning_address\": \"localhost:${alt_port}\"|g" \
            "$app/sdr/docker_cluster_config.json" 2>/dev/null || true
        local env_file="$SRC/deploy/docker/developer-profiles/dev-profile-${VSS_PROFILE}/.env"
        sed -i "s/^RTVI_CV_PORT=${cv_port}$/RTVI_CV_PORT=${alt_port}/" "$env_file" 2>/dev/null || true
        log "patched DS REST port ${cv_port}→${alt_port} in configs and .env"
    fi
}
patch_rt_cv_to_trafficcamnet

# ---------------------------------------------------------------------------
# 7. Deploy VSS (dev-profile.sh up)
#    Runs directly on the host Docker daemon; no dind needed on Brev.
#    dev-profile.sh requires root (runs sudo internally for sysctl, cgroupfs).
#    It downloads all images (~93 GB on first run) and builds TensorRT engines.
# ---------------------------------------------------------------------------
VSS_HOST_IP=$(hostname -I | awk '{print $1}')
[[ -n "$VSS_HOST_IP" ]] || fail "could not determine host IP from hostname -I"
log "host IP: $VSS_HOST_IP (used by VSS for service-to-service communication)"

ARGS=(
    up
    --profile          "$VSS_PROFILE"
    --hardware-profile "$HARDWARE_PROFILE"
    --host-ip          "$VSS_HOST_IP"
    --llm-device-id    "$LLM_DEVICE_ID"
    --vlm-device-id    "$VLM_DEVICE_ID"
)

# Idempotency: skip redeploy if VSS core is already healthy.
if [[ "${VSS_FORCE_REDEPLOY:-false}" != "true" ]]; then
    core_health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' vss-agent 2>/dev/null || true)
    if [[ "$core_health" == "healthy" ]]; then
        log "vss-agent is already healthy; skipping redeploy (set VSS_FORCE_REDEPLOY=true to rebuild)"
        # Still launch course services below.
        goto_course_services=true
    fi
fi

if [[ "${goto_course_services:-false}" != "true" ]]; then
    # Remove any stopped containers whose names would conflict with the new mdx deployment.
    # On a fresh Brev instance there are none. On a devbox, old `vss`-project containers
    # from a previous deploy linger and cause "container name already in use" errors.
    old_vss=$(docker ps -aq --filter "label=com.docker.compose.project=vss" 2>/dev/null || true)
    if [[ -n "$old_vss" ]]; then
        log "removing old 'vss'-project containers to avoid name conflicts"
        # shellcheck disable=SC2086
        docker rm -f $old_vss 2>/dev/null || true
    fi

    cd "$SRC/deploy/docker"
    UP_ATTEMPTS=3
    UP_RETRY_DELAY_S=90
    UP_RETRY_RE='unable to decode token response|error pulling image|failed to resolve reference'
    UP_RETRY_RE+='|TLS handshake timeout|i/o timeout|connection reset by peer|unexpected EOF'
    UP_RETRY_RE+='|dial tcp|no such host|Temporary failure in name resolution|context deadline exceeded'
    UP_RETRY_RE+='|toomanyrequests|Service Unavailable|Bad Gateway|Gateway Time-?out'
    UP_RETRY_RE+='|received unexpected HTTP status: 5[0-9][0-9]'
    UP_RETRY_RE+='|container name.*is already in use'
    LOG=/tmp/dev-profile.log

    status "RUNNING: dev-profile.sh up (images: ~93 GB; engines: 15-20 min; allow 60-90 min total)"
    log "running dev-profile.sh up (this is the long step)"
    for attempt in $(seq 1 "$UP_ATTEMPTS"); do
        log "attempt $attempt of $UP_ATTEMPTS"
        set +e
        bash "$DEPLOY_SH" "${ARGS[@]}" 2>&1 | tee "$LOG"
        rc=${PIPESTATUS[0]}
        set -e
        up_error=""
        [[ "$rc" -ne 0 ]] && up_error="dev-profile.sh up exited $rc"
        [[ -n "$up_error" ]] || break
        log "$up_error; checking log for retryable error..."
        if ! grep -qE "$UP_RETRY_RE" "$LOG"; then
            created=$(docker ps -aq 2>/dev/null | wc -l | tr -d ' ')
            fail "$up_error with no retryable error in log ($created container(s) exist).
  Inspect with: docker ps -a; docker logs <name>
  Rebuild with: VSS_FORCE_REDEPLOY=true bash $REPO_DIR/setup.sh
  See: $LOG"
        fi
        if grep -qE 'container name.*is already in use' "$LOG"; then
            log "container name conflict detected; removing conflicting stopped containers before retry"
            docker ps -aq --filter "status=exited" --filter "label=com.docker.compose.project=vss" | xargs -r docker rm -f 2>/dev/null || true
            docker ps -aq --filter "status=created" | xargs -r docker rm -f 2>/dev/null || true
        fi
        [[ "$attempt" -lt "$UP_ATTEMPTS" ]] || \
            fail "$up_error on all $UP_ATTEMPTS attempts; registry errors above in $LOG"
        log "pull/network error detected; retrying in ${UP_RETRY_DELAY_S}s..."
        status "RUNNING: dev-profile.sh up hit a pull error; retry $((attempt+1)) of $UP_ATTEMPTS in ${UP_RETRY_DELAY_S}s"
        sleep "$UP_RETRY_DELAY_S"
    done

    running=$(docker ps -q | wc -l | tr -d ' ')
    [[ "$running" -gt 0 ]] || \
        fail "dev-profile.sh reported success but no containers are running. Check $LOG"
    log "$running VSS containers running"

    # Scrub NGC key from generated.env — readable from lab via /opt/vss bind mount.
    GEN_ENV="$SRC/deploy/docker/developer-profiles/dev-profile-${VSS_PROFILE}/generated.env"
    if [[ -f "$GEN_ENV" ]]; then
        sed -i -E \
            's/^(NGC_CLI_API_KEY|NGC_API_KEY|NVIDIA_API_KEY|OPENAI_API_KEY)=.*/\1=<redacted-by-setup>/' \
            "$GEN_ENV"
        log "scrubbed credentials from generated.env"

        # dev-profile.sh writes VSS_PUBLIC_PORT=${HAPROXY_PORT} (7777) in
        # generated.env. The course expects the STUDENT-FACING port (80, the
        # nginx reverse proxy), so override it here. This makes VST_EXTERNAL_URL
        # and VSS_AGENT_EXTERNAL_URL resolve to :80 URLs instead of :7777.
        sed -i 's/^VSS_PUBLIC_PORT=.*/VSS_PUBLIC_PORT=80/' "$GEN_ENV"
        log "set VSS_PUBLIC_PORT=80 in generated.env (overrides haproxy-internal port)"
    fi
fi

# ---------------------------------------------------------------------------
# 8. Wait for VSS readiness
#    wait-for-vss.sh was removed in v3.2.1; we poll vss-agent directly.
# ---------------------------------------------------------------------------
status "RUNNING: waiting for VSS readiness (up to ${VSS_READY_TIMEOUT_S}s)"
log "waiting for VSS stack (first run builds TensorRT engines: allow 15-20 min after images are pulled)"
_vss_deadline=$(( $(date +%s) + VSS_READY_TIMEOUT_S ))
_vss_ready=false
while [[ $(date +%s) -lt $_vss_deadline ]]; do
    _h=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' vss-agent 2>/dev/null || true)
    if [[ "$_h" == "healthy" ]]; then
        # Confirm the HTTP health endpoint also responds.
        if curl -sf --max-time 5 "http://localhost:8000/health" >/dev/null 2>&1; then
            _vss_ready=true
            break
        fi
    fi
    # Progress: print container health summary every 60 s.
    if (( $(date +%s) % 60 < 10 )); then
        log "  vss-agent health: ${_h:-not_started}; waiting..."
    fi
    sleep 10
done
[[ "$_vss_ready" == "true" ]] || \
    fail "VSS stack did not become healthy within ${VSS_READY_TIMEOUT_S}s.
  Check container logs: docker logs vss-agent
  Retry with:         VSS_FORCE_REDEPLOY=true bash $REPO_DIR/setup.sh"
log "VSS stack is ready"

# ---------------------------------------------------------------------------
# 9. Launch the 4 course services
# ---------------------------------------------------------------------------
cd "$REPO_DIR"

# Auto-detect port 80 conflict; devboxes often have another service there.
# On a fresh Brev instance port 80 is always free and this block is a no-op.
if cat /proc/net/tcp 2>/dev/null | awk '$2 ~ /00000000:0050/ {found=1} END {exit !found}'; then
    DEV_NGINX_PORT=${DEV_NGINX_PORT:-18000}
    export DEV_NGINX_PORT
    log "port 80 is already in use on this host; using DEV_NGINX_PORT=${DEV_NGINX_PORT} for nginx"
fi

log "starting course services (lab, course-prep, assessment, nginx)..."
docker compose -f docker-compose.yml -f docker-compose.override.yml build
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d

HOST_IP_DISPLAY=$(hostname -I | awk '{print $1}')
NGINX_PORT_DISPLAY=${DEV_NGINX_PORT:-80}
log ""
log "Stack is up!"
log "  JupyterLab: http://${HOST_IP_DISPLAY}${NGINX_PORT_DISPLAY:+:${NGINX_PORT_DISPLAY}}/lab"
log "  Watch course-prep:"
log "    docker compose -f $REPO_DIR/docker-compose.yml -f $REPO_DIR/docker-compose.override.yml logs -f course-prep"
log "  VSS seed log: cat $SEED_STATUS_DIR/seed.log"
