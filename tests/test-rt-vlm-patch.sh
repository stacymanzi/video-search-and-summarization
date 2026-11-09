#!/usr/bin/env bash
#
# Validates the RT-VLM-into-search patch WITHOUT deploying anything: no NGC
# entitlement, no image pulls, no GPU. It resolves the Compose graph against a
# pristine v3.2.1 tree and asserts the result.
#
#   ./tests/test-rt-vlm-patch.sh [/path/to/upstream/checkout]
#   ./tests/test-rt-vlm-patch.sh --offline    # the appended env block only: no docker, no checkout
#
# Why this test exists
# --------------------
# The sandbox needs RT-CV, RT-VLM and RT-Embed in ONE deployment, and no stock
# v3.2.1 profile provides that:
#
#   rtvi-embed  profiles: ["bp_developer_search_2d"]
#   rtvi-vlm    profiles: [bp_wh_2d, both alerts modes, the two THOR base
#                          profiles, bp_developer_lvs_2d]     <- no search
#
# deploy/deploy-vss.sh closes the gap with an untracked compose.override.yml plus
# an appended env block. Both halves are easy to break silently:
#
#   * `profiles` REPLACES rather than appends on override merge, so dropping one
#     of the six original keys quietly removes rtvi-vlm from alerts or lvs.
#   * Compose's `include:` loads a .env from each included file's own directory,
#     so services/rtvi/rtvi-vlm/.env supplies defaults. Its KAFKA_TOPIC default
#     is mdx-vlm, which no Logstash pipeline reads, so captions would be published
#     and silently discarded while every healthcheck stayed green.
#   * The appended ELASTICSEARCH_ILM_MIN_AGE=30d works only as the LAST assignment
#     of that name: v3.2.1's search profile .env already sets 48h, and the ELK init
#     container's own default is 4h. Elasticsearch deletes every mdx-* index that
#     long after it was created, Part 1's search records with it.
#
# Claims under test:
#   1. RT-CV, RT-VLM and RT-Embed all resolve into one graph
#   2. The supporting write path (Kafka, Logstash, ES, behavior-analytics) is present
#   3. RT-VLM's caption topic is mdx-vlm-captions, NOT the mdx-vlm default
#   4. RT-VLM runs Cosmos 3 on the intended GPU and port, with a memory cap set
#   5. RT-CV is switched from warehouse RT-DETR to TrafficCamNet, consistently
#      (config, labels and the model download must all agree)
#   6. The ELK init container resolves ELASTICSEARCH_ILM_MIN_AGE=30d, so the mdx-*
#      indices last 30 days (--offline checks the env file alone, no Compose)

set -uo pipefail

UPSTREAM=${1:-/media/azfs/fs1/workspace/sochoa/gtc-berlin/video-search-and-summarization}
TAG=v${VSS_VERSION:-3.2.1}
HERE=$(cd "$(dirname "$0")" && pwd)
pass=0; fail=0
ok(){ printf '  \033[32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no(){ printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }

# --offline: only the env block, from the real patch_rt_vlm_into_search, appended to a
# stub search profile .env that already sets ELASTICSEARCH_ILM_MIN_AGE=48h, as v3.2.1's
# does. Compose reads an env file top to bottom and keeps the last value of a repeated
# name, so the checks read the last assignment. No docker, no checkout, no Python.
if [ "${1:-}" = "--offline" ]; then
    WORK=$(mktemp -d); trap 'rm -rf "${WORK}"' EXIT
    PENV="${WORK}/deploy/docker/developer-profiles/dev-profile-search/.env"
    mkdir -p "${PENV%/*}"
    printf '%s\n' '# Stand-in for upstream dev-profile-search/.env (v3.2.1 sets 48h here).' \
        'ELASTICSEARCH_ILM_MIN_AGE=48h' 'NUM_STREAMS=1' > "${PENV}"
    last(){ awk -v k="$1=" 'index($0, k) == 1 { v = substr($0, length(k) + 1) } END { print v }' "${PENV}"; }
    echo "offline: the env block deploy-vss.sh appends to the search profile"
    if (
        SRC="${WORK}" VSS_PROFILE=search VSS_VERSION=3.2.1 VSS_RT_VLM_PORT=8018 VSS_RT_VLM_DEVICE_ID=0
        VSS_RT_VLM_MODEL_PATH=ngc:nim/nvidia/cosmos3-super-reasoner:modelopt-fp8-final_format_fix
        VSS_RT_VLM_GPU_MEMORY_UTILIZATION=0.65 VSS_NUM_STREAMS=4
        RT_VLM_LOCAL_MODEL_DIR="" RT_EMBED_LOCAL_CACHE_DIR="" NEMOTRON_LOCAL_CACHE_DIR="" RT_CV_LOCAL_CACHE_DIR=""
        log(){ :; }; fail(){ echo "  patch function failed: $*"; exit 1; }
        eval "$(sed -n '/^patch_rt_vlm_into_search() {/,/^}$/p' "${HERE}/../deploy/deploy-vss.sh")"
        patch_rt_vlm_into_search
    ); then ok "patch_rt_vlm_into_search ran against a stub search profile"; else no "patch_rt_vlm_into_search failed"; fi
    v=$(last ELASTICSEARCH_ILM_MIN_AGE)
    [ "$v" = 30d ] && ok "ELASTICSEARCH_ILM_MIN_AGE=30d is the last assignment (upstream's 48h above it loses)" \
                   || no "last ELASTICSEARCH_ILM_MIN_AGE is '${v}', not 30d: Elasticsearch deletes Part 1's indices early"
    v=$(last RTVI_VLM_KAFKA_TOPIC)
    [ "$v" = mdx-vlm-captions ] && ok "RTVI_VLM_KAFKA_TOPIC=mdx-vlm-captions is the last assignment" \
                                || no "last RTVI_VLM_KAFKA_TOPIC is '${v}': captions would be dropped"
    bad=$(grep -nvE '^([[:space:]]*(#.*)?|[A-Za-z_][A-Za-z0-9_]*=.*)$' "${PENV}")
    [ -z "${bad}" ] && ok "every line is a comment, a blank or NAME=value" \
                    || no "lines an env file cannot hold: ${bad}"
    echo
    echo "  ${pass} passed, ${fail} failed"
    [ "${fail}" -eq 0 ] && exit 0 || exit 1
fi

command -v docker >/dev/null || { echo "docker not found"; exit 2; }
[ -d "${UPSTREAM}/.git" ] || { echo "not a git checkout: ${UPSTREAM}"; exit 2; }
git -C "${UPSTREAM}" rev-parse -q --verify "refs/tags/${TAG}" >/dev/null \
  || { echo "tag ${TAG} not present in ${UPSTREAM}"; exit 2; }

WORK=$(mktemp -d); trap 'rm -rf "${WORK}"' EXIT
echo "Resolving ${TAG} from ${UPSTREAM}"
git -C "${UPSTREAM}" archive "${TAG}" deploy/docker | tar -x -C "${WORK}"
D="${WORK}/deploy/docker"

# Apply the patch exactly as deploy-vss.sh does, by sourcing the real function
# rather than reimplementing it; otherwise this test drifts from the script.
export SRC="${WORK}" VSS_PROFILE=search VSS_VERSION=${VSS_VERSION:-3.2.1}
export VSS_RT_VLM_PORT=${VSS_RT_VLM_PORT:-8018}
export VSS_RT_VLM_DEVICE_ID=${VSS_RT_VLM_DEVICE_ID:-1}
export VSS_RT_VLM_MODEL_PATH=${VSS_RT_VLM_MODEL_PATH:-ngc:nim/nvidia/cosmos3-super-reasoner:modelopt-fp8-final_format_fix}
export VSS_RT_VLM_GPU_MEMORY_UTILIZATION=${VSS_RT_VLM_GPU_MEMORY_UTILIZATION:-0.65}
export VSS_NUM_STREAMS=${VSS_NUM_STREAMS:-4}
export VSS_RT_CV_MODEL=${VSS_RT_CV_MODEL:-smartcity}
export NEMOTRON_CHECKPOINT_MANIFEST="${HERE}/../deploy/nemotron-checkpoint.json"
export RT_EMBED_CHECKPOINT_MANIFEST="${HERE}/../deploy/rt-embed-checkpoint.json"
export RT_CV_CHECKPOINT_MANIFEST="${HERE}/../deploy/rt-cv-checkpoint.json"
log(){ :; }; fail_(){ echo "patch function failed: $*"; exit 1; }
eval "$(sed -n '/^patch_preserve_volumes() {/,/^}$/p' "${HERE}/../deploy/deploy-vss.sh" | sed 's/\bfail\b/fail_/g')"
patch_preserve_volumes "${D}/scripts/dev-profile.sh"
if grep -Eq '^[[:space:]]*\$_sudo rm -rf "\$\{data_directory\}"' "${D}/scripts/dev-profile.sh"; then no 'teardown deletes course video data'; else ok 'teardown preserves course video data'; fi
if grep -Eq '^[[:space:]]*docker compose.*down.*(-v|--volumes)' "${D}/scripts/dev-profile.sh"; then no 'teardown still removes named volumes'; else ok 'teardown preserves named volumes'; fi
if grep -Fq 'dangling_volumes=$(docker volume ls' "${D}/scripts/dev-profile.sh"; then no 'dangling-volume prune still active'; else ok 'dangling-volume prune disabled'; fi
before=$(sha256sum "${D}/scripts/dev-profile.sh")
patch_preserve_volumes "${D}/scripts/dev-profile.sh"
[ "$before" = "$(sha256sum "${D}/scripts/dev-profile.sh")" ] && ok 'teardown safeguard is idempotent' || no 'teardown safeguard changed on second application'
eval "$(sed -n '/^patch_rt_vlm_into_search() {/,/^}$/p' "${HERE}/../deploy/deploy-vss.sh" | sed 's/\bfail\b/fail_/g')"
patch_rt_vlm_into_search
eval "$(sed -n '/^patch_rt_cv_to_trafficcamnet() {/,/^}$/p' "${HERE}/../deploy/deploy-vss.sh" | sed 's/\bfail\b/fail_/g')"
patch_rt_cv_to_trafficcamnet
if [ -n "${RT_CV_TEST_CONFIG_DEST:-}" ]; then
    # Export generated configs for the separate GPU integration test; never
    # copy generated.env or credentials. Destination must be new.
    [ ! -e "$RT_CV_TEST_CONFIG_DEST" ] || exit 1
    mkdir -p "$RT_CV_TEST_CONFIG_DEST"
    cp -a "${D}/developer-profiles/dev-profile-search/video-analytics-2d-app/deepstream/configs" "$RT_CV_TEST_CONFIG_DEST/configs"
    cp "${D}/services/rtvi/rtvi-cv/ds-start.sh" "$RT_CV_TEST_CONFIG_DEST/"
    cp "${D}/developer-profiles/dev-profile-search/video-analytics-2d-app/deepstream/scripts/ds-search-start.sh" "$RT_CV_TEST_CONFIG_DEST/"
    cp "${D}/developer-profiles/dev-profile-search/video-analytics-2d-app/deepstream/scripts/download-vision-encoder.sh" "$RT_CV_TEST_CONFIG_DEST/"
fi

# dev-profile.sh normally fills these in; the values do not matter to the graph.
G="${D}/developer-profiles/dev-profile-search/generated.env"
cp "${D}/developer-profiles/dev-profile-search/.env" "${G}"
sed -i "s|^VSS_APPS_DIR=.*|VSS_APPS_DIR=${D}|; s|^VSS_DATA_DIR=.*|VSS_DATA_DIR=/tmp/vss-data|" "${G}"
echo "HOST_IP=127.0.0.1" >> "${G}"

RESOLVED="${WORK}/resolved.yml"
# Compose lets the caller's environment outrank --env-file; the claim under test is the file.
unset ELASTICSEARCH_ILM_MIN_AGE
if ! (cd "${D}" && docker compose --env-file "${G}" config) >"${RESOLVED}" 2>"${WORK}/err"; then
    echo "docker compose config failed:"; grep -v 'level=warning' "${WORK}/err" | head -5; exit 1
fi
echo

svc(){ python3 -c "
import sys,yaml; d=yaml.safe_load(open('${RESOLVED}'))
print('yes' if '$1' in d.get('services',{}) else 'no')"; }
env_of(){ python3 -c "
import sys,yaml; d=yaml.safe_load(open('${RESOLVED}'))
print(d['services']['$1']['environment'].get('$2',''))"; }

echo "1/4  all three perception services in one graph"
if [ -n "${RT_CV_LOCAL_CACHE_DIR:-}" ]; then
    python3 - "$RESOLVED" "$D" <<'PY' && ok 'RT-CV pinned image, shared direct cache, offline mode, engine path and batches' || no 'RT-CV cache wiring incorrect'
import configparser, json, os, sys, yaml
services = yaml.safe_load(open(sys.argv[1]))['services']
manifest = json.load(open(os.environ['RT_CV_CHECKPOINT_MANIFEST']))
for name in ('perception-2d-init', 'perception-2d-fusion'):
    service = services[name]
    mounts = [m for m in service['volumes'] if m['target'].rstrip('/') == '/opt/storage']
    assert len(mounts) == 1, mounts
    assert mounts[0]['type'] == 'bind'
    assert mounts[0]['source'] == os.environ['RT_CV_LOCAL_CACHE_DIR']
    assert not mounts[0].get('bind', {}).get('create_host_path', False)
    assert service['environment']['VISION_ENCODER_MODEL'] == 'siglip_v2'
    assert service['environment']['VISION_ENCODER_VERSION'] == 'v1.1'
    assert str(service['environment']['HF_HUB_OFFLINE']) == '1'
assert services['perception-2d-fusion']['image'] == manifest['image']
assert services['perception-2d-fusion']['depends_on']['perception-2d-init']['condition'] == 'service_completed_successfully'
cfg = sys.argv[2] + '/developer-profiles/dev-profile-search/video-analytics-2d-app/deepstream/configs/'
pgie = yaml.safe_load(open(cfg + 'ds-pgie-config.yml'))['property']
assert pgie['model-engine-file'] == '/opt/storage/rtdetr-its/model_epoch_035.fp16.onnx_b16_gpu0_fp16.engine'
config = configparser.ConfigParser(strict=False, interpolation=None)
config.read(cfg + 'ds-main-config.txt')
assert config['primary-gie']['batch-size'] == '16'
assert config['visionencoder']['batch-size'] == '128'
PY
fi
if [ -n "${RT_EMBED_LOCAL_CACHE_DIR:-}" ]; then
    python3 - "$RESOLVED" <<'PY' && ok 'RT-Embed pinned image, local checkpoint, offline mode and engine mount' || no 'RT-Embed cache wiring incorrect'
import json, os, sys, yaml
service = yaml.safe_load(open(sys.argv[1]))['services']['rtvi-embed']
manifest = json.load(open(os.environ['RT_EMBED_CHECKPOINT_MANIFEST']))
env = service['environment']
root = os.environ['RT_EMBED_LOCAL_CACHE_DIR']
assert service['image'] == manifest['image']
assert env['MODEL_PATH'] == '/course-models/' + manifest['model_directory']
assert str(env['HF_HUB_OFFLINE']) == str(env['TRANSFORMERS_OFFLINE']) == '1'
assert str(env['VLM_BATCH_SIZE']) == '64'
assert env['COSMOS_EMBED1_TRT_PRECISION'] == 'fp16'
assert env['COSMOS_EMBED1_TRT_EXTRA_ARGS'] == ''
mounts = {m['target']: m for m in service['volumes']}
for target, source in [(env['MODEL_PATH'], root + '/checkpoint/' + manifest['model_directory']),
                       ('/tmp/triton_model_repo', root + '/triton')]:
    assert mounts[target]['type'] == 'bind' and mounts[target]['source'] == source
    assert mounts[target].get('bind', {}).get('create_host_path', False) is False
assert mounts[env['MODEL_PATH']]['read_only'] is True
assert not mounts['/tmp/triton_model_repo'].get('read_only', False)
PY
fi
if [ -n "${NEMOTRON_LOCAL_CACHE_DIR:-}" ]; then
    python3 - "$RESOLVED" "${D}/compose.override.yml" <<'PY' && ok 'Nemotron digest and direct cache mount; dependencies unchanged' || no 'Nemotron cache wiring incorrect'
import json, os, sys, yaml
resolved = yaml.safe_load(open(sys.argv[1]))['services']
override = yaml.safe_load(open(sys.argv[2]))['services']
manifest = json.load(open(os.environ['NEMOTRON_CHECKPOINT_MANIFEST']))
for name in ('nvidia-nemotron-nano-9b-v2', 'nvidia-nemotron-nano-9b-v2-shared-gpu'):
    assert set(override[name]) == {'image', 'volumes'}, 'startup tuning/dependencies must not change'
    assert override[name]['image'] == manifest['image']
    mounts = override[name]['volumes']
    assert mounts[0]['source'] == os.environ['NEMOTRON_LOCAL_CACHE_DIR']
    assert mounts[0]['target'] == '/opt/nim/.cache'
    assert mounts[0]['bind']['create_host_path'] is False
    if name in resolved:
        mounts = [m for m in resolved[name]['volumes'] if m['target'] == '/opt/nim/.cache']
        assert len(mounts) == 1 and mounts[0]['type'] == 'bind'
        assert mounts[0]['source'] == os.environ['NEMOTRON_LOCAL_CACHE_DIR']
PY
fi
for pair in "perception-2d-fusion:RT-CV (DeepStream RT-DETR)" "rtvi-vlm:RT-VLM (Cosmos 3)" "rtvi-embed:RT-Embed"; do
    s=${pair%%:*}; label=${pair#*:}
    [ "$(svc "$s")" = yes ] && ok "${label} present as ${s}" || no "${label} MISSING (${s})"
done

echo "2/4  write path that carries metadata to Elasticsearch"
# vss-search-analytics-2d-fusion is the SOLE producer of mdx-embed-filtered; if
# it is absent, embedding search returns nothing while everything else looks fine.
for pair in "kafka:Kafka" "logstash:Logstash" "elasticsearch:Elasticsearch" \
            "vss-search-analytics-2d-fusion:behavior-analytics" "vst-ingress:VIOS ingress" "vss-agent:VSS agent"; do
    s=${pair%%:*}; label=${pair#*:}
    [ "$(svc "$s")" = yes ] && ok "${label} present" || no "${label} MISSING (${s})"
done

echo "3/4  captions actually reach Elasticsearch"
t=$(env_of rtvi-vlm KAFKA_TOPIC)
[ "$t" = "mdx-vlm-captions" ] \
    && ok "RT-VLM KAFKA_TOPIC=${t} (the topic mdx-lvs-logstash.conf consumes)" \
    || no "RT-VLM KAFKA_TOPIC=${t}: no Logstash pipeline reads this; captions would be dropped"
e=$(env_of rtvi-vlm KAFKA_ENABLED)
[ "$e" = "true" ] && ok "RT-VLM KAFKA_ENABLED=true" || no "RT-VLM KAFKA_ENABLED=${e}"

echo "4/4  RT-VLM runs Cosmos 3, placed and capped"
m=$(env_of rtvi-vlm VLM_MODEL_TO_USE)
c=$(env_of rtvi-vlm INSTALL_PROPRIETARY_CODECS)
[ "$c" = "false" ] && ok "proprietary codec installation disabled" || no "INSTALL_PROPRIETARY_CODECS=${c}"
c=$(env_of rtvi-embed INSTALL_PROPRIETARY_CODECS)
[ "$c" = "false" ] && ok "RT-Embed proprietary codec installation disabled" || no "RT-Embed INSTALL_PROPRIETARY_CODECS=${c}"
if [ -n "${RT_VLM_LOCAL_MODEL_DIR:-}" ]; then
    p=$(env_of rtvi-vlm MODEL_PATH)
    [ "$p" = "/course-models/${RT_VLM_LOCAL_MODEL_DIR##*/}" ] && ok 'restored checkpoint selected' || no 'wrong local model path'
    [ "$(env_of rtvi-vlm HF_HUB_OFFLINE)" = 1 ] && ok 'HF offline mode enabled' || no 'HF offline mode missing'
    python3 - "$RESOLVED" <<'PY' && ok 'restored model mount resolves on dind' || no 'restored model mount incorrect'
import os, sys, yaml
service = yaml.safe_load(open(sys.argv[1]))['services']['rtvi-vlm']
assert any(v.get('source') == os.environ['RT_VLM_LOCAL_MODEL_DIR'] and not v.get('read_only', False)
           and v.get('bind', {}).get('create_host_path', False) is False for v in service['volumes'])
PY
fi
[ "$m" = "cosmos-reason3" ] && ok "VLM_MODEL_TO_USE=${m}" || no "VLM_MODEL_TO_USE=${m}"
p=$(env_of rtvi-vlm MODEL_PATH)
case "$p" in *cosmos3-super-reasoner*) ok "MODEL_PATH=${p}";; *) no "MODEL_PATH=${p}";; esac
u=$(env_of rtvi-vlm VLLM_GPU_MEMORY_UTILIZATION)
[ -n "$u" ] && ok "VLLM_GPU_MEMORY_UTILIZATION=${u} (unset would collide with the LLM NIM)" \
             || no "VLLM_GPU_MEMORY_UTILIZATION is empty; vLLM would claim the whole card"
python3 - "$RESOLVED" <<'PY' && ok "device_ids and published port as configured" || no "device/port mismatch"
import sys,yaml,os
s=yaml.safe_load(open(sys.argv[1]))['services']['rtvi-vlm']
d=s['deploy']['resources']['reservations']['devices'][0]['device_ids']
p=str(s['ports'][0]['published'])
sys.exit(0 if d==[os.environ['VSS_RT_VLM_DEVICE_ID']] and p==os.environ['VSS_RT_VLM_PORT'] else 1)
PY

echo "5/5  RT-CV runs TrafficCamNet, not warehouse RT-DETR"
CFG="${D}/developer-profiles/dev-profile-search/video-analytics-2d-app/deepstream/configs"
PG="${CFG}/ds-pgie-config.yml"
grep -q 'onnx-file: /opt/storage/rtdetr-its/model_epoch_035.fp16.onnx' "${PG}" \
    && ok "pgie onnx-file points at TrafficCamNet" || no "pgie still points at the warehouse model"
grep -q 'num-detected-classes: 5' "${PG}" \
    && ok "num-detected-classes: 5 (traffic), not 7 (warehouse)" || no "class count not switched to 5"
# The warehouse and TrafficCamNet models use DIFFERENT parser entry points and
# different parser libraries; mixing them silently produces garbage detections.
grep -q 'parse-bbox-func-name: NvDsInferParseCustomDDETRTAO' "${PG}" \
    && ok "parser is NvDsInferParseCustomDDETRTAO (the DDETR/TrafficCamNet one)" \
    || no "parser not switched; warehouse uses NvDsInferParseCustomRTDETRTAO"
grep -q 'libnvds_infercustomparser_tao.so' "${PG}" \
    && ok "parser library is the _tao variant" || no "wrong parser library for this model"
grep -q 'infer-dims: 3;544;960' "${PG}" \
    && ok "infer-dims 3;544;960 (TrafficCamNet), not 3;640;640" || no "infer-dims not switched"
[ "$(wc -l < "${CFG}/rtdetr-its-labels.txt")" = 5 ] \
    && ok "label file has 5 classes" || no "label file wrong length"
grep -q 'labelfile-path: rtdetr-its-labels.txt' "${PG}" \
    && ok "pgie references the traffic label file" || no "pgie labelfile-path not updated"
# A config with no matching weights is the failure mode that looks like a broken
# container rather than a missing download, so assert the two agree.
INIT="${D}/developer-profiles/dev-profile-search/video-analytics-2d-app/deepstream/scripts/download-vision-encoder.sh"
grep -q 'trafficcamnet_transformer_lite:deployable_resnet50_v2.0' "${INIT}" \
    && ok "init container downloads TrafficCamNet weights" || no "no TrafficCamNet download step"

# Both of these were REAL failures on the first live deploy, so they are asserted
# rather than assumed.
#
# 1. `ngc registry model download-version` needs --org even though the org is
#    already the first path segment. Without it: "Missing org - If
#    Authenticated, org is also required." and RT-CV starts against a config
#    pointing at weights that never arrived.
grep -q 'download-version "${ITS_NGC}" --org nvidia' "${INIT}" \
    && ok "the download passes --org (required, and easy to omit)" \
    || no "download is missing --org; it will fail with 'Missing org'"

# 2. The upstream script exits 0 at its own idempotency marker. A block appended
#    after that runs once and can never be retried; restarting the container to
#    recover silently does nothing. Ours must sit ABOVE the marker check.
tc_line=$(grep -n 'DLI course: TrafficCamNet' "${INIT}" | head -1 | cut -d: -f1)
mk_line=$(grep -n 'if \[\[ -f "$MARKER" \]\]' "${INIT}" | head -1 | cut -d: -f1)
if [ -n "${tc_line}" ] && [ -n "${mk_line}" ] && [ "${tc_line}" -lt "${mk_line}" ]; then
    ok "download sits before the marker early-exit (line ${tc_line} < ${mk_line}), so it is retryable"
else
    no "download is after the early exit (${tc_line:-?} vs ${mk_line:-?}); it can never retry"
fi

# Swapping the detector has a downstream dependency: behaviour analytics only
# creates tracklets for classes in stateManagementFilter, shipped as ["Person"]
# for the stock warehouse model. With TrafficCamNet that yields an empty
# mdx-behavior, silently: the container stays healthy and logs a high message
# rate while emitting nothing.
ACFG="${D}/developer-profiles/dev-profile-search/video-analytics-2d-app/vss-search-analytics/configs/vss-search-analytics-kafka-config.json"
if python3 -c "
import json,sys
d=json.load(open('${ACFG}'))
v={e['name']:e['value'] for e in d['app']}
f=v.get('stateManagementFilter','')
sys.exit(0 if 'car' in f else 1)" 2>/dev/null; then
    ok "behaviour analytics filters on the TrafficCamNet classes"
else
    no "stateManagementFilter still targets warehouse classes; mdx-behavior stays empty"
fi
if python3 -c "
import json,sys
d=json.load(open('${ACFG}'))
v={e['name']:e['value'] for e in d['app']}
sys.exit(0 if int(v.get('behaviorWatermarkSec','30'))<=10 else 1)" 2>/dev/null; then
    ok "behaviour watermark lowered so short clips flush tracklets"
else
    no "behaviorWatermarkSec too high; clips shorter than it never flush"
fi
# The patch rewrites this file via mktemp+mv, which carries mktemp's 0600 onto
# the destination. vss-behavior-analytics runs as non-root and bind-mounts it,
# so 0600 makes it crash-loop on "doesn't have read access", and because that
# one container is the sole producer of BOTH mdx-behavior and mdx-embed-filtered,
# a lost mode bit silently takes out tracklets and embedding search together.
acfg_mode=$(stat -c '%a' "${ACFG}" 2>/dev/null)
if [ "${acfg_mode}" = "644" ]; then
    ok "analytics config is world-readable (${acfg_mode}); non-root container can read it"
else
    no "analytics config is ${acfg_mode:-?}, not 644; vss-behavior-analytics will crash-loop"
fi

echo "6/6  Elasticsearch keeps Part 1's indices for 30 days"
# The ELK init container (vss-elasticsearch-init) writes every mdx-* ILM policy from this
# one variable, at each deploy. What it resolves to is what Elasticsearch enforces.
if [ "$(svc elasticsearch-init-container)" = yes ]; then
    a=$(env_of elasticsearch-init-container ELASTICSEARCH_ILM_MIN_AGE)
    [ "$a" = "30d" ] && ok "elasticsearch-init-container ELASTICSEARCH_ILM_MIN_AGE=${a}" \
                     || no "elasticsearch-init-container ELASTICSEARCH_ILM_MIN_AGE=${a:-<unset>}, not 30d: Part 1's search records expire early"
else
    no "elasticsearch-init-container MISSING; nothing writes the mdx-* ILM policies"
fi

echo
echo "  ${pass} passed, ${fail} failed"
[ "${fail}" -eq 0 ]
