#!/usr/bin/env bash
#
# Bring the VSS blueprint up INSIDE dind, then exit 0.
#
# This is the piece that replaces "a human with host access runs dev-profile.sh".
# It runs as a one-shot compose service against DOCKER_HOST=tcp://dind:2375, so
# every container it creates lands in dind's namespace, where the cgroupfs
# driver and network sysctls are already correct.
#
# Students never run this. They open JupyterLab and the stack is already up.
#
# ---------------------------------------------------------------------------
# Interface notes, verified against upstream tag v3.2.1 on 2026-08-06. These
# differ from the 3.0 era, so re-check them when bumping VSS_VERSION:
#
#   * tag format is `vX.Y.Z` (NOT `release/X.Y.Z`)
#   * the launcher lives at deploy/docker/scripts/dev-profile.sh
#     (it resolves its own repo root, so calling it by absolute path is fine)
#   * profiles are: base | lvs | search | alerts   (no warehouse/edge)
#   * hardware profiles are: H100 | L40S | RTXPRO4500BW | RTXPRO6000BW |
#     DGX-SPARK | IGX-THOR | AGX-THOR | OTHER
#   * there is NO --ngc-cli-api-key flag; the key is read from the
#     NGC_CLI_API_KEY environment variable
#   * there is NO --llm-mode/--vlm-mode. Remote is `--use-remote-llm` /
#     `--use-remote-vlm`; *shared* is DERIVED: if --llm-device-id equals
#     --vlm-device-id, upstream treats it as local_shared automatically
# ---------------------------------------------------------------------------
# Exit paths and the LAST line of /opt/vss/_course-runtime/seed-status they
# leave behind (course 1, 2026-10-20). The notebooks read that last line.
#
#   fail() anywhere             exit 1    FAILED: <plain message>
#       S3 course data wait:    FAILED: S3 course data loader failed: <error from the marker>
#                               FAILED: S3 course data not ready after <cap>s
#                               FAILED: COURSE_DATA_TIMEOUT_S must be ...
#                               FAILED: S3 course data marker ... (unreadable, or an unknown state)
#       NGC preflight (login or manifest), S3 cache verification, upstream
#       fetch, patches, dry-run, compose pull x3, compose build, course image
#       pull, image audit, dev-profile.sh up (x3 only on pull/network errors,
#       never once containers exist), zero containers after up
#   unguarded error or signal   exit rc   FAILED: seed exited <rc> while '<last RUNNING line>'; see seed.log
#       written by the EXIT trap; it never overwrites an earlier FAILED line
#   student-deploys seed done   exit 0    OK: seeded <n> images
#   readiness reached           exit 0    OK: VSS <profile> ready in <t>s (...)   [wait-for-vss.sh]
#   readiness timeout           exit 1    FAILED: readiness timeout after <cap>s; not ready: <list>   [wait-for-vss.sh]
#   waiter misconfigured        exit 1    FAILED: VSS_READY_TIMEOUT_S must be ...   [wait-for-vss.sh]
#
# wait-for-vss.sh is CALLED, never exec'd, so the trap above still runs after it.
# ---------------------------------------------------------------------------

set -euo pipefail

log()  { echo "[vss-deploy] $*"; }
# Progress/failure record for the notebooks. vss_src (/opt/vss) is mounted in this container AND in the lab, and the lab
# no longer waits for this service, so a seed that dies would otherwise be invisible to the learner (production, Sep 4:
# 50 minutes of "image not present yet" while the seed had aborted in its first minute). Each step overwrites the file;
# the notebooks' preflight cells print it when something they need is missing.
SEED_STATUS_DIR=/opt/vss/_course-runtime
SEED_STATUS_FILE="${SEED_STATUS_DIR}/seed-status"
# Exported for wait-for-vss.sh, which APPENDS its own RUNNING/OK/FAILED lines to this same file once it takes
# over (this script overwrites per step; the waiter appends). Readers take the last line as the current state.
export SEED_STATUS_FILE
status() { mkdir -p "${SEED_STATUS_DIR}" 2>/dev/null && printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "${SEED_STATUS_FILE}" 2>/dev/null || true; }
fail() { echo "[vss-deploy] FATAL: $*" >&2; status "FAILED: $*"; exit 1; }
# Everything this script prints is also appended to a log the lab can read (the vss-deploy container's own
# stdout is invisible from JupyterLab). Keys never appear in the log: nvapi- tokens and *_API_KEY assignments
# are redacted in-line before the line is written.
SEED_LOG="${SEED_STATUS_DIR}/seed.log"
mkdir -p "${SEED_STATUS_DIR}" 2>/dev/null || true
exec > >(sed -u -E 's/nvapi-[A-Za-z0-9_-]{8,}/nvapi-<redacted>/g; s/((NGC_CLI_API_KEY|NGC_API_KEY|NVIDIA_API_KEY|OPENAI_API_KEY)=)[^[:space:]]*/\1<redacted>/g' | tee -a "${SEED_LOG}") 2>&1
log "seed log: ${SEED_LOG}"

: "${VSS_VERSION:=3.2.1}"
: "${VSS_PROFILE:=search}"
# RT-VLM is not in the v3.2.1 search profile; patch_rt_vlm_into_search() adds it.
: "${VSS_ENABLE_RT_VLM:=true}"
: "${VSS_RT_VLM_DEVICE_ID:=0}"
: "${VSS_RT_VLM_PORT:=8018}"
: "${VSS_RT_VLM_MODEL_PATH:=ngc:nim/nvidia/cosmos3-super-reasoner:modelopt-fp8-final_format_fix}"
: "${VSS_RT_VLM_USE_S3_CACHE:=true}"
: "${VSS_RT_EMBED_USE_S3_CACHE:=true}"
: "${VSS_RT_CV_USE_S3_CACHE:=true}"
# Same default as task1/.env and docker-compose.yml: Cosmos 3 Super FP8 cannot
# load at the old 0.45 (see .env). Compose always passes the value; this default
# only matters when the script is run by hand.
: "${VSS_RT_VLM_GPU_MEMORY_UTILIZATION:=0.65}"
: "${VSS_NUM_STREAMS:=4}"
# smartcity = TrafficCamNet RT-DETR (traffic classes); warehouse = upstream stock.
: "${VSS_RT_CV_MODEL:=smartcity}"
# Read by dev-profile.sh from the ENVIRONMENT of the child process (it tests
# ${ENABLE_CRITIC+set}), so this must be exported, not merely set. false makes it
# write VLM_NAME_SLUG=none and skip the local VLM NIM entirely, which is what
# keeps the v3.2.1 search profile from scheduling its critic onto a nonexistent
# GPU 2 on a 2-GPU host.
: "${ENABLE_CRITIC:=false}"
export ENABLE_CRITIC
: "${HARDWARE_PROFILE:=H100}"
: "${LLM_DEVICE_ID:=1}"
: "${VLM_DEVICE_ID:=1}"
: "${USE_REMOTE_LLM:=false}"
: "${VSS_NEMOTRON_USE_S3_CACHE:=true}"
# The alerts profile REQUIRES --mode; upstream refuses to deploy without it.
# verification = CV pipeline raises alerts, VLM verifies them (what the course teaches).
: "${VSS_ALERTS_MODE:=verification}"
: "${USE_REMOTE_VLM:=false}"
: "${DOCKER_HOST:=tcp://dind:2375}"
export DOCKER_HOST
# Upstream builds four helper images (broker health check, elasticsearch, two init
# containers) whose build definitions ask for host networking. Compose >= 2.35 defaults
# to the Bake builder, which refuses that without --allow=network.host and fails the
# deploy with "additional privileges requested". The classic builder honours it, so
# pin the behaviour here instead of depending on which compose the image installed.
export COMPOSE_BAKE=false

SRC=/opt/vss
REPO_URL="https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git"
TAG="v${VSS_VERSION}"

# ---------------------------------------------------------------- preflight
log "waiting for the inner Docker daemon at ${DOCKER_HOST}"
status "RUNNING: waiting for the inner Docker daemon"
for _ in $(seq 1 90); do
    docker info >/dev/null 2>&1 && break
    sleep 2
done
docker info >/dev/null 2>&1 || fail "inner daemon never became ready"
log "inner daemon up: $(docker info --format '{{.ServerVersion}} runtime={{.DefaultRuntime}} cgroup={{.CgroupDriver}}')"

# Bridge two naming conventions, rather than forcing either side to change:
#   NGC_API_KEY:       the DLI team convention. It is what secrets.env declares,
#                      what nim-embedded-template uses, and what c-fx-17-v1 and
#                      s-fx-36-v1 both map through the GitLab secrets manager.
#   NGC_CLI_API_KEY:   what upstream's dev-profile.sh reads from the environment.
: "${NGC_CLI_API_KEY:=${NGC_API_KEY:-}}"
: "${NGC_CLI_ORG:=nvidia}"
export NGC_CLI_API_KEY NGC_CLI_ORG
[ -n "${NGC_CLI_API_KEY}" ] || \
    fail "no NGC credential found. Expected NGC_API_KEY (or NGC_CLI_API_KEY) from ./.env-dev in development, or /dli/secrets/.env in production."

# A dry-run writes the real key to generated.env in the shared vss_src volume.
# Scrub it on every exit, including student cache-seeding mode and failures.
cleanup_credentials() {
    local generated
    for generated in "${SRC}"/deploy/docker/developer-profiles/dev-profile-*/generated.env; do
        [ -f "${generated}" ] || continue
        sed -i -E \
            's/^(NGC_CLI_API_KEY|NGC_API_KEY|NVIDIA_API_KEY|OPENAI_API_KEY)=.*/\1=<redacted-by-course>/' \
            "${generated}"
    done
    docker logout nvcr.io >/dev/null 2>&1 || true
}
# A failure that escapes fail() (`set -e` on an unguarded command, a killed process) must not leave the
# status file frozen on RUNNING, or the notebooks tell the learner to wait forever. Production, 2026-09-15:
# `compose pull` died within a minute of 00:58 and the status still read RUNNING at 01:20 with an idle daemon.
on_exit() {
    rc=$?
    if [ "${rc}" -ne 0 ] && ! grep -q ' FAILED' "${SEED_STATUS_FILE}" 2>/dev/null; then
        status "FAILED: seed exited ${rc} while '$(tail -n 1 "${SEED_STATUS_FILE}" 2>/dev/null | sed 's/^[^ ]* //')'; see ${SEED_LOG}"
    fi
    cleanup_credentials
}
trap on_exit EXIT

# ------------------------------------------------------------ NGC preflight
# Fail in the first minute, not after forty (course 1 blocker B1). Two checks, both with the injected key
# and neither echoing it (the key travels on stdin; nothing below prints it, and the tee above redacts
# key-shaped strings regardless):
#
#   1. `docker login nvcr.io`: the dind daemon validates the key against the registry and the deployer
#      keeps the credential for the compose pull below. dev-profile.sh logs in again on its own later.
#   2. one manifest read of the VSS agent image. `docker manifest inspect` is a client-side GET of the
#      manifest from this container; no layer is pulled. A key that logs in but cannot see this image lacks
#      the vss-core entitlement, the failure that used to surface as a 403 in the middle of a 93 GB pull.
#      The course needs vss-core plus the Nemotron NIM (nim/nvidia/nvidia-nemotron-nano-9b-v2); this
#      probes the first, the compose pull retries cover the second.
#
# VERIFY ON DEVBOX: docker login through DOCKER_HOST=tcp://dind:2375 stores the credential client-side, and
# docker manifest inspect reaches nvcr.io from the vss-deploy container (same egress the git fetch uses).
status "RUNNING: NGC preflight (nvcr.io login and one manifest check)"
if ! login_out=$(printf '%s' "${NGC_CLI_API_KEY}" | docker login nvcr.io --username '$oauthtoken' --password-stdin 2>&1); then
    printf '%s\n' "${login_out}" | sed 's/^/[vss-deploy]   /'
    fail "nvcr.io rejected the NGC key (docker login failed). Check the key behind NGC_API_KEY in /dli/secrets/.env (production) or ./.env-dev (development); nothing was pulled."
fi
log "nvcr.io login succeeded"
NGC_PREFLIGHT_IMAGE="${NGC_PREFLIGHT_IMAGE:-nvcr.io/nvidia/vss-core/vss-agent:${VSS_VERSION}}"
if ! manifest_out=$(timeout 120 docker manifest inspect "${NGC_PREFLIGHT_IMAGE}" 2>&1 >/dev/null); then
    printf '%s\n' "${manifest_out}" | head -5 | sed 's/^/[vss-deploy]   /'
    fail "the NGC key logged in but cannot read ${NGC_PREFLIGHT_IMAGE}: the key's org lacks the vss-core entitlement (the course needs vss-core and the Nemotron NIM), the tag is gone from NGC, or nvcr.io is unreachable from this container. Nothing was pulled."
fi
log "NGC preflight passed: ${NGC_PREFLIGHT_IMAGE} is visible to this key"

# ---------------------------------------------------------- readiness gate
# CALLED, never exec'd (course 1 blocker B2): exec replaced this shell, so a waiter timeout skipped on_exit
# and left the status file on RUNNING while the daemon sat idle. The waiter appends its own RUNNING/OK/FAILED
# lines to SEED_STATUS_FILE and exits 0 only when the stack answers; its cap is VSS_READY_TIMEOUT_S (default
# 3600 s, passed through the vss-deploy environment). Used by the healthy-stack short-circuit and the deploy
# path alike, so this is the only place the script can end with OK in pre-deploy mode.
readiness_gate() {
    local cap="${VSS_READY_TIMEOUT_S:-3600}" rc=0
    log "waiting for readiness (cap ${cap}s; a cold start with TensorRT engine builds takes 15-20 minutes)"
    status "RUNNING: waiting for readiness (cap ${cap}s)"
    set +e
    /deploy/wait-for-vss.sh
    rc=$?
    set -e
    if [ "${rc}" -ne 0 ]; then
        log "readiness gate failed (exit ${rc}); the waiter's FAILED line is in ${SEED_STATUS_FILE} and the final container state is above in ${SEED_LOG}"
        exit "${rc}"
    fi
    log "VSS ${VSS_PROFILE} is ready; seed complete"
    exit 0
}

# ------------------------------------------------------- S3 course data wait
# Compose no longer holds this container until s3_data_loader exits (production launch fix 3, after tag v0.0.4:
# that gate kept `docker compose up -d` open for the whole ~80 GB download). Wait here instead, before anything
# reads /models or /dli/task/data and before any upstream teardown. load-course-data.py writes the marker
# atomically: RUNNING at start (replacing an earlier run's OK), then OK, or FAILED with the error. Absent or
# RUNNING keeps waiting; the cap is COURSE_DATA_TIMEOUT_S (default 5400 s, passed through the vss-deploy
# environment). The deploy image has jq.
COURSE_DATA_STATUS=/dli/task/data/.course-data-status.json
COURSE_DATA_TIMEOUT_S=${COURSE_DATA_TIMEOUT_S:-5400}
if ! [[ "${COURSE_DATA_TIMEOUT_S}" =~ ^[0-9]+$ ]] || [ "${COURSE_DATA_TIMEOUT_S}" -le 0 ]; then
    fail "COURSE_DATA_TIMEOUT_S must be a positive integer number of seconds, got '${COURSE_DATA_TIMEOUT_S}'"
fi
wait_for_course_data() {
    local start=${SECONDS} elapsed state next_report=0
    while :; do
        elapsed=$(( SECONDS - start ))
        state=""
        if [ -f "${COURSE_DATA_STATUS}" ]; then
            state=$(jq -er '.state' "${COURSE_DATA_STATUS}" 2>/dev/null) \
                || fail "S3 course data marker ${COURSE_DATA_STATUS} is not JSON with a state field"
        fi
        case "${state}" in
            OK)
                log "S3 course data ready after ${elapsed}s"
                return 0 ;;
            FAILED)
                fail "S3 course data loader failed: $(jq -r '.error // "no error recorded"' "${COURSE_DATA_STATUS}")" ;;
            ""|RUNNING) ;;
            *)
                fail "S3 course data marker ${COURSE_DATA_STATUS} has an unknown state '${state}'" ;;
        esac
        if [ "${elapsed}" -ge "${COURSE_DATA_TIMEOUT_S}" ]; then
            fail "S3 course data not ready after ${COURSE_DATA_TIMEOUT_S}s"
        fi
        if [ "${elapsed}" -ge "${next_report}" ]; then
            status "RUNNING: waiting for S3 course data (${elapsed}s)"
            log "RUNNING: waiting for S3 course data (${elapsed}s)"
            next_report=$(( next_report + 60 ))
        fi
        sleep 10
    done
}
wait_for_course_data

# wait_for_course_data (above) has already seen the loader's marker read OK,
# but the loader skips existing files without checksums. Validate direct model
# files before any upstream teardown. /models is the same persistent
# volume/path inside dind.
RT_VLM_LOCAL_MODEL_DIR=""
if [ "${VSS_RT_VLM_USE_S3_CACHE}" = true ] && [ "${VSS_ENABLE_RT_VLM}" = true ] && [ "${VSS_PROFILE}" = search ]; then
    expected_source=$(jq -er '.source' /deploy/rt-vlm-checkpoint.json)
    [ "${VSS_RT_VLM_MODEL_PATH}" = "$expected_source" ] || fail 'S3 checkpoint does not match VSS_RT_VLM_MODEL_PATH; update the pinned manifest or explicitly disable VSS_RT_VLM_USE_S3_CACHE'
    status 'RUNNING: verifying direct S3 RT-VLM model files (no extraction)'
    RT_VLM_LOCAL_MODEL_DIR=$(/deploy/restore-rt-vlm-model.sh) || fail 'RT-VLM checkpoint verification failed; existing VSS stack was not torn down'
    log "S3 checkpoint prepared for RT-VLM: ${RT_VLM_LOCAL_MODEL_DIR}"
fi

NEMOTRON_LOCAL_CACHE_DIR=""
if [ "${VSS_NEMOTRON_USE_S3_CACHE}" = true ] && [ "${USE_REMOTE_LLM}" = false ] && [ "${VSS_PROFILE}" = search ]; then
    status 'RUNNING: verifying direct S3 Nemotron cache files (no extraction)'
    NEMOTRON_LOCAL_CACHE_DIR=$(/deploy/restore-nemotron-cache.sh) || fail 'Nemotron cache verification failed; existing VSS stack was not torn down'
    log "S3 cache prepared for Nemotron: ${NEMOTRON_LOCAL_CACHE_DIR}"
fi

RT_EMBED_LOCAL_CACHE_DIR=""
if [ "${VSS_RT_EMBED_USE_S3_CACHE}" = true ] && [ "${VSS_ENABLE_RT_VLM}" = true ] && [ "${VSS_PROFILE}" = search ]; then
    [ "${VSS_VERSION}" = "$(jq -er '.vss_version' /deploy/rt-embed-checkpoint.json)" ] || fail 'RT-Embed engine cache does not match VSS_VERSION; refresh the manifest or disable VSS_RT_EMBED_USE_S3_CACHE'
    status 'RUNNING: verifying direct S3 RT-Embed model and engine files (no extraction)'
    RT_EMBED_LOCAL_CACHE_DIR=$(/deploy/restore-rt-embed-cache.sh) || fail 'RT-Embed cache verification failed; existing VSS stack was not torn down'
    log "S3 cache prepared for RT-Embed: ${RT_EMBED_LOCAL_CACHE_DIR}"
fi

RT_CV_LOCAL_CACHE_DIR=""
if [ "${VSS_RT_CV_USE_S3_CACHE}" = true ] && [ "${VSS_ENABLE_RT_VLM}" = true ] && [ "${VSS_PROFILE}" = search ] && [ "${VSS_RT_CV_MODEL}" = smartcity ]; then
    [ "${VSS_VERSION}" = "$(jq -er '.vss_version' /deploy/rt-cv-checkpoint.json)" ] || fail 'RT-CV cache does not match VSS_VERSION'
    status 'RUNNING: verifying direct S3 RT-CV models and engines (no extraction)'
    RT_CV_LOCAL_CACHE_DIR=$(/deploy/restore-rt-cv-cache.sh) || fail 'RT-CV cache verification failed; existing VSS stack was not torn down'
    log "S3 cache prepared for RT-CV: ${RT_CV_LOCAL_CACHE_DIR}"
fi

# ------------------------------------------------------------- idempotency
# Compose re-runs this service on every `up`, and `dev-profile.sh up` begins
# with a full `down`. Without this check, restarting the course tears down a
# perfectly healthy VSS stack and rebuilds it: about ten minutes, for nothing.
# If the core service is already healthy, skip straight to the readiness gate.
#
# Override with VSS_FORCE_REDEPLOY=true when you actually want a rebuild (after
# changing VSS_PROFILE, VSS_VERSION, or the GPU placement).
case "${VSS_PROFILE}" in
    alerts) DEFAULT_CORE=vss-agent ;;
    lvs)    DEFAULT_CORE=vss-lvs ;;
    *)      DEFAULT_CORE=vss-agent ;;
esac
CORE=${VSS_CORE_SERVICE:-${DEFAULT_CORE}}
if [ "${VSS_FORCE_REDEPLOY:-false}" != "true" ]; then
    core_health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${CORE}" 2>/dev/null || true)
    if [ "${core_health}" = "healthy" ]; then
        log "${CORE} is already healthy; skipping redeploy (set VSS_FORCE_REDEPLOY=true to rebuild)"
        readiness_gate
    fi
fi

# ------------------------------------------------------------------ source
# Clone upstream at a release tag rather than vendoring it. Vendoring is what
# left x-fx-113-v2 (3.0.0) and x-ov-59-v1 (3.1.0) frozen at pre-GA versions.
if [ ! -d "${SRC}/.git" ]; then
    log "fetching upstream blueprint @ ${TAG}"
    status "RUNNING: fetching the upstream blueprint"
    # `git clone` CANNOT be used here, and this is not a style choice.
    #
    # The course_data volume is mounted into this tree, at
    # deploy/docker/data-dir/videos/dev-profile-<profile>. Docker creates that
    # directory chain inside vss_src when dind starts (which is before this
    # runs), so /opt/vss is never empty and clone aborts with
    #   fatal: destination path '/opt/vss' already exists and is not an
    #   empty directory
    # init + fetch + checkout does the same job in a populated directory.
    #
    # Note the failure is captured rather than discarded. The previous version
    # sent stderr to /dev/null and reported "tag not found" for ANY failure,
    # which sent a cold build chasing a version that was in fact published.
    if ! git_err=$( { git init -q "${SRC}" \
            && git -C "${SRC}" remote add origin "${REPO_URL}" \
            && git -C "${SRC}" fetch -q --depth 1 origin "refs/tags/${TAG}:refs/tags/${TAG}" \
            && git -C "${SRC}" checkout -q "${TAG}"; } 2>&1 ); then
        log "could not check out ${TAG}. git said:"
        printf '%s\n' "${git_err}" | sed 's/^/[vss-deploy]   /'
        log "tags currently published upstream:"
        git ls-remote --tags "${REPO_URL}" 2>/dev/null \
            | awk '{print $2}' | sed 's|refs/tags/||; s|\^{}||' \
            | sort -u -V | tail -12 | sed 's/^/[vss-deploy]   /'
        fail "if ${TAG} is listed above, this is NOT a version problem; read git's message."
    fi
fi

DEPLOY_SH="${SRC}/deploy/docker/scripts/dev-profile.sh"
[ -x "${DEPLOY_SH}" ] || [ -f "${DEPLOY_SH}" ] || \
    fail "expected ${DEPLOY_SH} in the upstream repo; the layout changed, so re-check the interface notes at the top of this script."

# Upstream's state_down removes named volumes and prunes ALL dangling volumes.
# A course redeploy must not erase model caches or unrelated rollback volumes.
patch_preserve_volumes() {
    local script=$1
    grep -Fq 'docker compose -p mdx down' "$script" || fail 'upstream teardown interface changed'
    if ! grep -Fq '# DLI: preserve cached and rollback volumes' "$script"; then
        grep -Fq 'dangling_volumes=$(docker volume ls -q -f "dangling=true")' "$script" || fail 'upstream volume cleanup interface changed'
        sed -i \
            -e 's/docker compose -p mdx down -v --remove-orphans/docker compose -p mdx down --remove-orphans/g' \
            -e 's/(with volumes)/(preserving volumes)/g' \
            -e 's/Removing dangling docker volumes/Preserving dangling docker volumes for course rollback/g' \
            -e 's/dangling_volumes=$(docker volume ls -q -f "dangling=true")/dangling_volumes="" # DLI: preserve cached and rollback volumes/' \
            "$script"
    fi
    if grep -Eq '^[[:space:]]*docker compose.*down.*(-v|--volumes)' "$script"; then
        fail 'upstream teardown still removes volumes; refusing course redeploy'
    fi
    # VIOS metadata can survive in a named PostgreSQL volume while its clips
    # live under data-dir. Deleting just the latter leaves valid-looking sensor
    # IDs whose video retrieval fails. Preserve both sides of that relationship.
    if ! grep -Fq '# DLI: preserve course data directory' "$script"; then
        grep -Fq '$_sudo rm -rf "${data_directory}"' "$script" || fail 'upstream data cleanup interface changed'
        sed -i \
            -e 's/^[[:space:]]*\$_sudo rm -rf "${data_directory}"/      : # DLI: preserve course data directory/' \
            -e 's/Deleting data directory:/Preserving course data directory:/g' \
            -e 's/Data directory deleted/Data directory preserved/g' "$script"
    fi
    log 'course teardown safeguard active: named volumes and course video data preserved'
}
patch_preserve_volumes "$DEPLOY_SH"

# ---------------------------------------------------------------- validate
VALID_PROFILES="base lvs search alerts"
case " ${VALID_PROFILES} " in
    *" ${VSS_PROFILE} "*) ;;
    *) fail "VSS_PROFILE='${VSS_PROFILE}' is not valid at ${TAG}. Choose one of: ${VALID_PROFILES}" ;;
esac

# ------------------------------------------------------- RT-VLM into search
# The sandbox needs RT-CV, RT-VLM and RT-Embed in ONE deployment. At v3.2.1 no
# stock profile does that:
#
#   rtvi-embed  profiles: ["bp_developer_search_2d"]
#   rtvi-vlm    profiles: ["bp_wh_2d", "bp_developer_alerts_2d_vlm",
#                          "bp_developer_alerts_2d_cv",
#                          "bp_developer_base_2d_IGX-THOR",
#                          "bp_developer_base_2d_AGX-THOR",
#                          "bp_developer_lvs_2d"]
#
# The intersection is empty. `search` therefore gives RT-CV + RT-Embed and a
# separate cosmos3-reasoner NIM critic, which has no stream ingestion, no
# chunking and no Kafka publishing, so nothing it produces reaches Elasticsearch.
#
# Two changes are needed, and BOTH are required; the profiles edit alone gives a
# container that cannot start.
#
#   1. compose.override.yml:      activate rtvi-vlm under bp_developer_search_2d
#   2. RT-VLM env in the profile: dev-profile.sh's RT-VLM setup (its
#                                   alerts/lvs/THOR-base branch) never runs for
#                                   `search`, so nothing is computed for us and
#                                   dev-profile-search/.env mentions no RTVI_VLM_*
#                                   variable at all.
#
# We do NOT edit rtvi-vlm-docker-compose.yml: it is shared with bp_wh_2d, both
# alerts modes, lvs, the THOR base profiles and the industry profiles. Compose
# auto-loads compose.override.yml from its working directory, and dev-profile.sh
# does `cd deploy/docker` before invoking compose, so an override file is picked
# up with no change to upstream's script (which has no -f option).
patch_rt_vlm_into_search() {
    local docker_dir="${SRC}/deploy/docker"
    local override="${docker_dir}/compose.override.yml"
    local profile_env="${docker_dir}/developer-profiles/dev-profile-${VSS_PROFILE}/.env"
    local model_path="${VSS_RT_VLM_MODEL_PATH}" offline=0

    [ -f "${profile_env}" ] || fail "expected ${profile_env}; upstream layout changed at ${TAG}"

    # `profiles` is NOT in Compose's concatenating-sequence set (ports, expose,
    # external_links, dns, dns_search, tmpfs), so this REPLACES the list rather
    # than appending to it, so every original value must be restated.
    cat > "${override}" <<'OVERRIDE'
# Generated by task1/deploy/deploy-vss.sh. Do not edit by hand.
# Activates rtvi-vlm under the search profile. See deploy-vss.sh for why.
services:
  rtvi-embed:
    environment:
      # Video embeddings do not need the optional proprietary codec installer.
      INSTALL_PROPRIETARY_CODECS: "false"
OVERRIDE
    if [ -n "${RT_EMBED_LOCAL_CACHE_DIR:-}" ]; then
        local embed_manifest=${RT_EMBED_CHECKPOINT_MANIFEST:-/deploy/rt-embed-checkpoint.json}
        local embed_model embed_image
        embed_model=$(jq -er '.model_directory' "$embed_manifest")
        embed_image=$(jq -er '.image' "$embed_manifest")
        cat >> "${override}" <<EOF
      MODEL_PATH: /course-models/${embed_model}
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
      VLM_BATCH_SIZE: "64"
      COSMOS_EMBED1_TRT_PRECISION: fp16
      COSMOS_EMBED1_TRT_EXTRA_ARGS: ""
    image: ${embed_image}
    volumes:
      - type: bind
        source: ${RT_EMBED_LOCAL_CACHE_DIR}/checkpoint/${embed_model}
        target: /course-models/${embed_model}
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: ${RT_EMBED_LOCAL_CACHE_DIR}/triton
        target: /tmp/triton_model_repo
        bind:
          create_host_path: false
EOF
        log 'RT-Embed uses verified direct-file S3 checkpoint and H100 NVL engines'
    fi
    cat >> "${override}" <<'OVERRIDE'
  rtvi-vlm:
    environment:
      # The course uses video-only captioning; skip startup codec installation.
      INSTALL_PROPRIETARY_CODECS: "false"
      HF_HUB_OFFLINE: "${COURSE_RT_VLM_OFFLINE:-0}"
      TRANSFORMERS_OFFLINE: "${COURSE_RT_VLM_OFFLINE:-0}"
    profiles:
      - bp_wh_2d
      - bp_developer_alerts_2d_vlm
      - bp_developer_alerts_2d_cv
      - bp_developer_base_2d_IGX-THOR
      - bp_developer_base_2d_AGX-THOR
      - bp_developer_lvs_2d
      - bp_developer_search_2d
OVERRIDE
    if [ -n "${RT_VLM_LOCAL_MODEL_DIR:-}" ]; then
        model_path="/course-models/${RT_VLM_LOCAL_MODEL_DIR##*/}"
        offline=1
        cat >> "${override}" <<EOF
    volumes:
      - type: bind
        source: ${RT_VLM_LOCAL_MODEL_DIR}
        target: ${model_path}
        bind:
          create_host_path: false
EOF
    fi
    if [ -n "${NEMOTRON_LOCAL_CACHE_DIR:-}" ]; then
        # Preserve NIM's native cache layout and normal profile selection. Pin
        # the image to the exact version whose model artifacts were cached.
        local nim_image nim_service
        nim_image=$(jq -er '.image' "${NEMOTRON_CHECKPOINT_MANIFEST:-/deploy/nemotron-checkpoint.json}")
        for nim_service in nvidia-nemotron-nano-9b-v2 nvidia-nemotron-nano-9b-v2-shared-gpu; do
            cat >> "${override}" <<EOF
  ${nim_service}:
    image: ${nim_image}
    volumes:
      - type: bind
        source: ${NEMOTRON_LOCAL_CACHE_DIR}
        target: /opt/nim/.cache
        bind:
          create_host_path: false
EOF
        done
        log 'Nemotron uses the verified direct-file S3 cache (native NIM layout)'
    fi
    if [ -n "${RT_CV_LOCAL_CACHE_DIR:-}" ]; then
        local cv_image cv_service
        cv_image=$(jq -er '.image' "${RT_CV_CHECKPOINT_MANIFEST:-/deploy/rt-cv-checkpoint.json}")
        for cv_service in perception-2d-init perception-2d-fusion; do
            cat >> "${override}" <<EOF
  ${cv_service}:
    environment:
      VISION_ENCODER_MODEL: siglip_v2
      VISION_ENCODER_VERSION: v1.1
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
    volumes:
      - type: bind
        source: ${RT_CV_LOCAL_CACHE_DIR}
        target: /opt/storage
        bind:
          create_host_path: false
EOF
            if [ "$cv_service" = perception-2d-fusion ]; then
                printf '    image: %s\n' "$cv_image" >> "${override}"
            fi
        done
        log 'RT-CV uses verified direct-file S3 models and H100 NVL engines'
    fi
    log "wrote ${override} (rtvi-vlm activated under bp_developer_search_2d)"

    # Appended, not merged: dev-profile.sh copies this file verbatim to
    # generated.env on every `up`, and later assignments win in a shell-sourced
    # env file, so appending overrides anything upstream set earlier.
    cat >> "${profile_env}" <<EOF

# --- appended by the DLI course (deploy-vss.sh) ------------------------------
# RT-VLM, patched into the search profile. Upstream computes none of this for
# the search profile, and two of these are load-bearing rather than tuning:
#
#   RTVI_VLM_KAFKA_TOPIC  services/rtvi/rtvi-vlm/.env defaults this to mdx-vlm.
#                      That topic IS pre-created, but no Logstash pipeline reads
#                      it; mdx-lvs-logstash.conf consumes mdx-vlm-captions. Left
#                      at the default, captions are published and then silently
#                      discarded, so RT-VLM looks healthy and Elasticsearch stays
#                      empty. This single line is what connects Cosmos 3 to ES.
#   RTVI_VLLM_GPU_MEMORY_UTILIZATION  empty by default, which makes vLLM claim
#                      its own default fraction of the entire card and collide
#                      with the LLM NIM sharing GPU 1.
#
# On precedence, VERIFIED against v3.2.1 with 'docker compose config' rather than
# assumed: Compose's 'include:' directive loads a .env from each included file's OWN
# directory, so services/rtvi/rtvi-vlm/.env does supply defaults (including
# RTVI_VLM_PORT=8018, which is why stock search resolves cleanly despite the
# required "\${RTVI_VLM_PORT?}" interpolation). Values appended here to the
# profile env, passed as --env-file, take precedence over those directory
# defaults, confirmed by resolving KAFKA_TOPIC to mdx-vlm-captions.
RTVI_VLM_PORT=${VSS_RT_VLM_PORT}
RT_VLM_DEVICE_ID='${VSS_RT_VLM_DEVICE_ID}'
RTVI_VLM_IMAGE_TAG="${VSS_VERSION}"
RTVI_VLM_MODEL_TO_USE=cosmos-reason3
RTVI_VLM_MODEL_PATH='${model_path}'
COURSE_RT_VLM_OFFLINE=${offline}
RTVI_VLM_KAFKA_ENABLED=true
RTVI_VLM_KAFKA_TOPIC=mdx-vlm-captions
RTVI_VLM_KAFKA_INCIDENT_TOPIC=mdx-vlm-incidents
# Empty makes vLLM claim its default fraction of the whole card and collide with
# the LLM NIM sharing this GPU.
RTVI_VLLM_GPU_MEMORY_UTILIZATION='${VSS_RT_VLM_GPU_MEMORY_UTILIZATION}'
RTVI_VLM_MAX_MODEL_LEN=32768
RTVI_VLM_BASE_URL=http://\${HOST_IP}:${VSS_RT_VLM_PORT}

# This variable does not override the search profile's fixed detector batch 16
# or vision encoder batch 128; cached engines retain those upstream settings.
NUM_STREAMS=${VSS_NUM_STREAMS}

# Elasticsearch index lifetime (not an RT-VLM setting). VSS's ELK init container
# gives every mdx-* ILM policy a delete phase at this age, counted from when the
# index was CREATED: 4h by default, and v3.2.1's search profile sets 48h above.
# Part 1's indices are created at launch, when course-prep uploads the search
# videos, and later uploads write into the same mdx-*-2025-01-01 indices. 30d
# covers a class day and devboxes that run for days (a devbox lost course-prep's
# records on 2026-11-03). Part 2's AI City package uses 30d too (build.py).
ELASTICSEARCH_ILM_MIN_AGE=30d
EOF
    log "appended RT-VLM configuration to ${profile_env##*/} (device ${VSS_RT_VLM_DEVICE_ID}, port ${VSS_RT_VLM_PORT})"
    log "Elasticsearch keeps mdx-* indices for $(sed -n 's/^ELASTICSEARCH_ILM_MIN_AGE=//p' "${profile_env}" | tail -n 1) after creation (ELASTICSEARCH_ILM_MIN_AGE in dev-profile-${VSS_PROFILE}/.env)"
}

if [ "${VSS_ENABLE_RT_VLM}" = "true" ] && [ "${VSS_PROFILE}" = "search" ]; then
    patch_rt_vlm_into_search
else
    log "RT-VLM patch skipped (VSS_ENABLE_RT_VLM=${VSS_ENABLE_RT_VLM}, profile=${VSS_PROFILE})"
fi

# ------------------------------------------------- RT-CV: TrafficCamNet
# The v3.2.1 search profile ships WAREHOUSE RT-DETR
# (nvidia/tao/rtdetr_2d_warehouse, 7 classes: Person, two humanoids,
# Nova_Carter, Transporter, Forklift, Pallet). On traffic footage it detects
# almost nothing but Person, which makes "look at the detection metadata" a dull
# exercise. This course is smart-city, so RT-CV runs TrafficCamNet RT-DETR
# instead (5 classes: background, bicycle, car, person, road_sign).
#
# DS_MODEL_FAMILY is NOT the knob. ds-start.sh at v3.2.1 accepts only
# rtdetr-warehouse | rtdetr-gdino | sparse4d-warehouse; there is no
# "smartcity-rtdetr" value (that belongs to the standalone
# vss-deploy-detection-tracking-2d skill, which deploys by bare `docker run` and
# is a different path entirely). rtdetr-warehouse is simply the generic
# "RT-DETR TAO" dispatch: it execs metropolis_perception_app against
# ds-main-config.txt, and WHICH model runs is decided by ds-pgie-config.yml.
# So the family stays as-is and the pgie config is swapped underneath it.
#
# Settings below are lifted from the alerts profile's own TrafficCamNet config
# (developer-profiles/dev-profile-alerts/deepstream/configs/rtdetr-960x544.txt),
# which is the proven config for this exact model. Note it differs from the
# warehouse one in more than the file path: a different parser entry point
# (NvDsInferParseCustomDDETRTAO), a different parser library (the _tao variant),
# different input dims (3;544;960, not 3;640;640), cluster-mode 4 and
# maintain-aspect-ratio 1.
patch_rt_cv_to_trafficcamnet() {
    local app="${SRC}/deploy/docker/developer-profiles/dev-profile-${VSS_PROFILE}/video-analytics-2d-app"
    local cfg="${app}/deepstream/configs"
    local init_script="${app}/deepstream/scripts/download-vision-encoder.sh"

    [ -d "${cfg}" ] || fail "expected ${cfg}; upstream layout changed at ${TAG}"
    [ -f "${init_script}" ] || fail "expected ${init_script}; upstream layout changed at ${TAG}"

    cat > "${cfg}/rtdetr-its-labels.txt" <<'LABELS'
background
bicycle
car
person
road_sign
LABELS

    # When using the validated cache, explicitly name its batch-16 engine.
    # Without model-engine-file, nvinfer rebuilds even if the derived file exists.
    cat > "${cfg}/ds-pgie-config.yml" <<'PGIE'
# Replaced by the DLI course (deploy-vss.sh): TrafficCamNet RT-DETR in place of
# upstream's warehouse RT-DETR. See deploy-vss.sh for why.
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
    if [ -n "${RT_CV_LOCAL_CACHE_DIR:-}" ]; then
        # Refuse to silently reuse an engine after upstream batch settings change.
        local section expected value
        for section in source-list streammux primary-gie visionencoder; do
            expected=16
            [ "$section" != visionencoder ] || expected=128
            value=$(awk -v group="[$section]" '
                /^\[/ { active=($0==group) }
                active && /^(max-batch-size|batch-size)=/ {split($0,a,"="); print a[2]}
            ' "${cfg}/ds-main-config.txt")
            [ "$value" = "$expected" ] || fail "RT-CV cache incompatible with ${section} batch ${value}"
        done
        sed -i '/^  onnx-file:/a\  model-engine-file: /opt/storage/rtdetr-its/model_epoch_035.fp16.onnx_b16_gpu0_fp16.engine' "${cfg}/ds-pgie-config.yml"
    fi
    log "wrote TrafficCamNet ds-pgie-config.yml + labels to ${cfg##*/}"

    # ---- downstream dependency of the detector swap ------------------------
    # Behaviour analytics only creates tracklets for object types listed in
    # stateManagementFilter, and the search profile ships it as ["Person"] to
    # match the stock WAREHOUSE detector. TrafficCamNet emits background,
    # bicycle, car, person, road_sign: on traffic footage, essentially no
    # Person at all.
    #
    # Left alone the symptom is silent and misleading: vss-behavior-analytics
    # is healthy, logs a high message rate, consumes all of mdx-raw, and emits
    # nothing. mdx-behavior never appears, so tracklets (speed, distance,
    # timeInterval) and the per-object embeddings that ride on them are simply
    # absent, with no error anywhere.
    #
    # Swapping a detector is not just a model file: anything downstream that
    # filters on class names has to move with it.
    local analytics_cfg="${app}/vss-search-analytics/configs/vss-search-analytics-kafka-config.json"
    if [ -f "${analytics_cfg}" ]; then
        # jq, not python3, because this image has no Python. Verified the hard way: an
        # earlier version used python3 here, worked when run by hand from the
        # LAB container, and killed the whole deploy with exit 127 the first
        # time it ran for real.
        local tmp_cfg
        tmp_cfg=$(mktemp)
        jq '(.app[] | select(.name=="stateManagementFilter") | .value) = "[\"car\",\"person\",\"bicycle\"]"
            | (.app[] | select(.name=="behaviorWatermarkSec")   | .value) = "5"
            | (.app[] | select(.name=="behaviorStateTimeout")   | .value) = "3"' \
            "${analytics_cfg}" > "${tmp_cfg}" \
            && mv "${tmp_cfg}" "${analytics_cfg}" \
            || { rm -f "${tmp_cfg}"; fail "could not patch ${analytics_cfg##*/}"; }
        # mktemp creates 0600 and mv carries that mode onto the destination,
        # replacing the original 0644. vss-behavior-analytics runs as non-root
        # and bind-mounts this file, so a 0600 config makes it crash-loop with
        # "doesn't have read access": healthy-looking stack, empty mdx-behavior.
        chmod 644 "${analytics_cfg}" \
            || fail "could not restore mode on ${analytics_cfg##*/}"
        log "widened behaviour-analytics stateManagementFilter to the TrafficCamNet classes"
    else
        log "WARNING: ${analytics_cfg##*/} not found; mdx-behavior will stay empty"
    fi

    # The model itself. dev-profile.sh downloads TrafficCamNet only in its ALERTS
    # branch; its search branch fetches the warehouse model. Rather than racing
    # that (search also does `rm -rf ${data_directory}/models` before its own
    # download), append to the init container's script: perception-2d-init
    # already has NGC_CLI_API_KEY, already mounts $VSS_DATA_DIR/models at
    # /opt/storage, already installs the NGC CLI, and is a
    # service_completed_successfully dependency of perception-2d-fusion, so the
    # weights are guaranteed present before RT-CV starts.
    if grep -q 'DLI course: TrafficCamNet' "${init_script}"; then
        log "TrafficCamNet download step already present in ${init_script##*/}"
        return 0
    fi

    # INSERTED, NOT APPENDED, and that distinction is load-bearing.
    #
    # download-vision-encoder.sh exits 0 at its own idempotency marker:
    #
    #     MARKER="${DEST}/.${MODEL}_${VERSION}.done"
    #     if [[ -f "$MARKER" ]]; then echo "... already present ..."; exit 0; fi
    #
    # Anything appended after that therefore runs exactly once, on the very
    # first execution, and can NEVER be retried: restarting the init container
    # short-circuits at the marker and the appended block is dead code. Found
    # the hard way: the first TrafficCamNet download failed, and restarting the
    # container to retry it silently did nothing.
    #
    # Inserting before the marker check means our block is reached on every run,
    # and it carries its own guard (skip when the ONNX already exists).
    local marker_line
    marker_line=$(grep -n 'if \[\[ -f "$MARKER" \]\]' "${init_script}" | head -1 | cut -d: -f1)
    [ -n "${marker_line}" ] || fail "could not find the marker check in ${init_script##*/}; upstream layout changed"

    local block
    block=$(mktemp)
    cat > "${block}" <<'DOWNLOAD'

# ---------------------------------------------------------------------------
# Appended by the DLI course: TrafficCamNet RT-DETR for the smart-city sandbox.
# Idempotent, and non-fatal on failure so a transient NGC error does not take
# down the whole init container; ds-pgie-config.yml points here, so a missing
# file surfaces as an RT-CV start failure with a clear log line either way.
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

  # Install the NGC CLI if it is not here yet. This block runs BEFORE the
  # script's marker early-exit (so it stays retryable), which also puts it
  # before upstream's own CLI install further down, so it cannot assume ngc
  # exists. Same install steps upstream uses, so a later `command -v ngc`
  # succeeds and upstream skips its copy.
  if ! command -v ngc >/dev/null 2>&1; then
    echo "##### NGC CLI not found, installing for TrafficCamNet... #####"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq wget unzip > /dev/null
    ( cd /tmp && wget -q https://ngc.nvidia.com/downloads/ngccli_linux.zip -O ngccli_linux.zip \
        && unzip -qo ngccli_linux.zip && chmod +x ngc-cli/ngc )
    export PATH="/tmp/ngc-cli:$PATH"
  fi
  # --org is REQUIRED even though the org is already in the model path. Without
  # it the CLI fails with "Missing org - If Authenticated, org is also required."
  # Upstream's own siglip download passes it for the same reason; the version in
  # dev-profile.sh's alerts branch gets away without it only because that script
  # runs `ngc config set` first, which the init container never does.
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
    # Splice it in above the marker check.
    sed -i "$((marker_line - 1))r ${block}" "${init_script}"
    rm -f "${block}"
    log "inserted TrafficCamNet download into ${init_script##*/} (before its early exit, line ${marker_line})"
}

if [ "${VSS_RT_CV_MODEL}" = "smartcity" ] && [ "${VSS_PROFILE}" = "search" ]; then
    patch_rt_cv_to_trafficcamnet
else
    log "RT-CV model patch skipped (VSS_RT_CV_MODEL=${VSS_RT_CV_MODEL}, profile=${VSS_PROFILE})"
fi

# Course videos are deliberately not mounted under data-dir: state_down deletes
# that tree on every deploy. After VSS is up, stage a disposable copy from the
# durable course_data volume with /usr/local/bin/stage-course-videos.

# ----------------------------------------------------------------- host-ip
# --host-ip must be an address that is routable FROM INSIDE dind, not the
# literal "localhost".
#
# Upstream mixes networking modes: redis, sdr-controller and the VIOS services
# are host-networked (i.e. they bind in dind's namespace), while sdrc-* init
# containers and rtvi-vlm sit on the mdx_default bridge. A bridge container
# that dials "localhost" reaches its OWN loopback, not dind's.
#
# That is what breaks the VIOS API. With --host-ip localhost:
#   sdrc-wait-for-redis dials localhost:6379   -> refused (redis is host-net)
#   -> the SDR controller never gets a full workload config
#   -> envoy serves stale routes and every /vst/api/* path returns 503
# while nginx's own /health still answers 200, so nothing flags it.
#
# Verified from a bridge container inside dind: "localhost" and 127.0.0.1 are
# refused; dind's eth0 address reaches redis, VST and Elasticsearch. Use eth0:
# it is the same address lab and nginx already resolve "dind" to, so one
# address is correct from both inside and outside.
# Resolve the compose name first: dind's eth0 address IS the address this network
# resolves "dind" to, and getent needs no image pull. The busybox probe stays as a
# fallback only: it is an anonymous Docker Hub pull, and Hub rate-limits every
# learner VM behind one egress IP; a throttled pull here used to end the launch.
if [ -z "${VSS_HOST_IP:-}" ]; then
    # DOCKER_HOST is tcp://dind:2375, so strip the scheme AND the port before resolving. Passing "dind:2375" to
    # getent never resolves, which silently sent every launch down the Docker Hub fallback below (seen in
    # production on Sep 4: the anonymous pull was refused, the seed aborted, and dind came up with no images).
    _dind_host="${DOCKER_HOST#tcp://}"
    _dind_host="${_dind_host%%:*}"
    VSS_HOST_IP=$(getent ahostsv4 "${_dind_host}" 2>/dev/null | awk 'NR == 1 { print $1; exit }')
    [ -n "${VSS_HOST_IP}" ] && log "host-ip resolved from the compose name '${_dind_host}'"
fi
if [ -z "${VSS_HOST_IP:-}" ]; then
    VSS_HOST_IP=$(docker run --rm --network host busybox:1.37.0 \
        sh -c "ip -4 addr show eth0 2>/dev/null | awk '/inet /{print \$2}' | cut -d/ -f1" 2>/dev/null | tr -d '\r')
fi
[ -n "${VSS_HOST_IP}" ] || fail "could not determine a routable host IP inside dind; set VSS_HOST_IP explicitly."
log "host-ip for inner services: ${VSS_HOST_IP}"
status "RUNNING: host-ip resolved (${VSS_HOST_IP}); preparing image seed"

# --external-ip is a DIFFERENT thing: what a BROWSER uses to reach the VSS UI.
# Upstream keeps them separate for good reason:
#   --host-ip      service-to-service inside dind   (172.21.0.x here)
#   --external-ip  the browser's view               (a DLI hostname)
# It does NOT need to resolve from inside the VM. DLI uses split-horizon DNS, so
# the course hostname resolves from a student's browser and not from the VM
# itself; the VM's own 10.x address is pool-internal and not browser-reachable.
# Cannot be auto-detected from in here, so it must be set in task1/.env.
if [ -n "${VSS_EXTERNAL_HOST:-}" ]; then
    log "external-ip for the UI: ${VSS_EXTERNAL_HOST}"
else
    log "WARNING: VSS_EXTERNAL_HOST is unset; VSS will fall back to an internal"
    log "         address and any UI link it generates will not open in a browser."
fi

# --------------------------------------------------- browser-facing port
# VSS_PUBLIC_PORT is the port a BROWSER uses, and it is deliberately decoupled
# from HAPROXY_PORT (the port upstream's ingress actually binds inside dind).
# Upstream uses exactly this split for Brev, where it sets VSS_PUBLIC_PORT=443
# and leaves haproxy on 7777 because an HTTPS proxy sits in front
# (deploy/docker/scripts/dev-profile.sh, the _secure_link_host branch).
#
# Our situation is the same shape with a different number: the course nginx is
# the proxy and :80 is the only port a student can reach. On a DLI VM nothing
# else is published, and some corporate networks block high ports outright, so
# a UI advertising :7777 is unreachable even though the stack is healthy.
#
# Setting this makes every NEXT_PUBLIC_* URL the UI hands the browser come out
# as http://<host>/... instead of http://<host>:7777/..., and adds <host>:80 to
# haproxy's known_host ACL. Without it the page loads and every API call fails.
export VSS_PUBLIC_PORT="${VSS_PUBLIC_PORT:-80}"
log "public port for the browser: ${VSS_PUBLIC_PORT} (haproxy still binds 7777 inside dind)"

# KNOWN UPSTREAM GAP: the agent chat endpoint is hardcoded in
# services/ui/compose.yml as http://${HOST_IP}:${VSS_AGENT_PORT:-8000}/chat/stream
# It uses HOST_IP (dind-internal, 172.21.0.x) rather than VSS_PUBLIC_HOST, and
# has no override variable. The browser therefore cannot reach it, so the chat
# sidebar is non-functional out of the box behind any reverse proxy. haproxy DOES
# route /chat, so the fix is to point those two NEXT_PUBLIC_*CHAT_COMPLETION_URL
# values at http://<public-host>/chat/stream and recreate the vss-ui container.
# Left unpatched here: it needs post-generation surgery on resolved.yml, and the
# alert-verification walkthrough does not use the chat tab.

# ------------------------------------------------ image pre-pull (both modes)
# Berlin kept the nvcr.io login, the 3x pull retry and the COURSE_EXTRA_IMAGES loop inside the
# student-deploys branch below, so the pre-deployed course (VSS_STUDENT_DEPLOYS=false, the shipped setting)
# pulled ~93 GB cold through `dev-profile.sh up` with no retry at all (course 1 blocker B3). The same seed now
# runs in both modes. The login happened once, in the NGC preflight; the credential is still held here.
# On return SEEDED_IMAGE_COUNT holds the number of image references verified present in dind.
seed_profile_images() {
    log "pre-pulling images for profile '${VSS_PROFILE}' (first run is slow; later launches reuse dind_storage)"
    status "RUNNING: pulling the VSS profile images (this is the long step)"
    # Ask upstream what this profile needs rather than hard-coding a list,
    # so it stays correct across VSS versions. --dry-run only prints the commands and writes generated.env
    # (checked in dev-profile.sh: state_down and the compose up are both print-only under dry_run), so it is
    # safe to run ahead of the real `up` in pre-deploy mode.
    cd "${SRC}/deploy/docker" 2>/dev/null || fail "upstream deploy dir missing"
    local -a DRY COMPOSE imgs
    local DRY_LOG GEN_ENV pull_attempts attempt image missing
    DRY=(up --profile "${VSS_PROFILE}" --hardware-profile "${HARDWARE_PROFILE}"
         --host-ip "${VSS_HOST_IP}" --llm-device-id "${LLM_DEVICE_ID}"
         --vlm-device-id "${VLM_DEVICE_ID}" --dry-run)
    [ "${VSS_PROFILE}" = "alerts" ] && DRY+=(--mode "${VSS_ALERTS_MODE}")
    DRY_LOG=/tmp/vss-seed-dry-run.log
    if ! bash "${DEPLOY_SH}" "${DRY[@]}" >"${DRY_LOG}" 2>&1; then
        sed 's/^/[vss-deploy]   /' "${DRY_LOG}" >&2
        fail "upstream dry-run failed; cache was not seeded"
    fi

    GEN_ENV="${SRC}/deploy/docker/developer-profiles/dev-profile-${VSS_PROFILE}/generated.env"
    [ -s "${GEN_ENV}" ] || fail "dry-run did not create ${GEN_ENV}"

    COMPOSE=(env -u HOST_IP docker compose --env-file "${GEN_ENV}")
    mapfile -t imgs < <(
        "${COMPOSE[@]}" config --images |
        sed '/^[[:space:]]*$/d' |
        sort -u
    )
    [ "${#imgs[@]}" -ge "${VSS_MIN_CACHED_IMAGES:-15}" ] ||
        fail "resolved only ${#imgs[@]} images"

    log "resolved ${#imgs[@]} profile image(s); pulling and building cache"
    # Registry token errors on nvcr.io come and go (production 2026-09-11: 403 on a model for every key for
    # hours; 2026-09-15: "unable to decode token response" on one NIM image while the rest of the org pulled
    # fine). Retry the whole pull a few times before declaring the seed failed; compose skips layers it
    # already has, so a retry only re-fetches what is missing.
    pull_attempts=3
    for attempt in $(seq 1 "${pull_attempts}"); do
        if "${COMPOSE[@]}" pull --ignore-buildable; then
            break
        fi
        [ "${attempt}" -lt "${pull_attempts}" ] ||
            fail "compose pull failed ${pull_attempts} times for profile '${VSS_PROFILE}'; the image and registry error is above in ${SEED_LOG}"
        log "compose pull attempt ${attempt} of ${pull_attempts} failed; retrying in 60 s"
        status "RUNNING: pulling the VSS profile images (retry $((attempt + 1)) of ${pull_attempts} after a registry error)"
        sleep 60
    done
    "${COMPOSE[@]}" build --pull ||
        fail "compose build failed for profile '${VSS_PROFILE}'; see ${SEED_LOG}"

    # Course-owned images outside the VSS profile (see COURSE_EXTRA_IMAGES in
    # task1/.env). Same login; verified by the loop below. Same bounded retry as
    # the profile pull: busybox comes from Docker Hub, which rate-limits the one
    # egress address a classroom shares, and nvcr.io token errors come and go.
    for image in ${COURSE_EXTRA_IMAGES:-}; do
        log "pre-pulling course image ${image}"
        status "RUNNING: pulling course image ${image}"
        for attempt in $(seq 1 "${pull_attempts}"); do
            if docker pull "${image}" >/dev/null; then
                break
            fi
            [ "${attempt}" -lt "${pull_attempts}" ] ||
                fail "could not pull ${image} after ${pull_attempts} attempts; the registry error is above in ${SEED_LOG}"
            log "pull of ${image} failed (attempt ${attempt} of ${pull_attempts}); retrying in 30 s"
            status "RUNNING: pulling course image ${image} (retry $((attempt + 1)) of ${pull_attempts} after a registry error)"
            sleep 30
        done
        imgs+=("${image}")
    done

    missing=0
    for image in "${imgs[@]}"; do
        if ! docker image inspect "${image}" >/dev/null 2>&1; then
            log "MISSING after seed: ${image}"
            missing=$((missing + 1))
        fi
    done
    [ "${missing}" -eq 0 ] || fail "${missing} image(s) still absent after cache seed"
    log "seeded ${#imgs[@]} image references"
    SEEDED_IMAGE_COUNT=${#imgs[@]}
}

# --------------------------------------------------- student-deploys mode
# The course walkthrough is "prompt agent to setup VSS for alert verification",
# so students run vss-deploy-profile themselves. Pre-deploying would remove the
# lesson. In that mode this service only warms the caches and exits, so the
# student's deploy is fast enough for a 20-minute walkthrough slot.
#
# Seeding here pulls profile images and builds local helper images into
# dind_storage. NIM weight preloading is a separate production artifact; do not
# report weights as warm merely because this image seed succeeded.
if [ "${VSS_STUDENT_DEPLOYS:-false}" = "true" ]; then
    log "student-deploys mode: seeding caches, not starting VSS"
    if [ "${VSS_SEED_CACHES:-true}" = "true" ]; then
        seed_profile_images
        status "OK: seeded ${SEEDED_IMAGE_COUNT} images"
    fi
    log "ready; students deploy with the vss-deploy-profile skill"
    exit 0
fi

# ------------------------------------------------------- pre-deploy pre-pull
# The course pre-deploys (VSS_STUDENT_DEPLOYS=false). Pull everything, with retries, BEFORE dev-profile.sh
# up, so the up itself only builds the helper images and starts containers. A registry hiccup is retried
# here instead of ending a 93 GB cold pull half-way through with no second chance.
if [ "${VSS_SEED_CACHES:-true}" = "true" ]; then
    seed_profile_images
    status "RUNNING: ${SEEDED_IMAGE_COUNT} images present in dind; starting the VSS stack"
else
    log "VSS_SEED_CACHES=false: skipping the image pre-pull; dev-profile.sh up pulls on its own and only the up itself is retried"
fi

# ------------------------------------------------------------------ deploy
if [ "${LLM_DEVICE_ID}" = "${VLM_DEVICE_ID}" ]; then
    placement="LLM+VLM co-resident on gpu${LLM_DEVICE_ID} (upstream derives local_shared)"
else
    placement="LLM=gpu${LLM_DEVICE_ID} VLM=gpu${VLM_DEVICE_ID}"
fi
log "deploying profile=${VSS_PROFILE} hardware=${HARDWARE_PROFILE} ${placement}"

ARGS=(
    up
    --profile          "${VSS_PROFILE}"
    --hardware-profile "${HARDWARE_PROFILE}"
    --host-ip          "${VSS_HOST_IP}"
    --llm-device-id    "${LLM_DEVICE_ID}"
    --vlm-device-id    "${VLM_DEVICE_ID}"
)
[ -n "${VSS_EXTERNAL_HOST:-}" ] && ARGS+=(--external-ip "${VSS_EXTERNAL_HOST}")
# --mode exists only for the alerts profile. Retained so switching VSS_PROFILE
# back to alerts for a smoke test still works.
[ "${VSS_PROFILE}" = "alerts" ] && ARGS+=(--mode "${VSS_ALERTS_MODE}")
[ "${USE_REMOTE_LLM}" = "true" ] && ARGS+=(--use-remote-llm)
[ "${USE_REMOTE_VLM}" = "true" ] && ARGS+=(--use-remote-vlm)

# NOTE: upstream's own pre-flight steps call `sudo`. Here we ARE root inside
# dind, so `sudo -n` succeeds and the full path runs. On a bare DLI VM the
# script would detect no passwordless sudo, halt, and wait for a host owner who
# never arrives. That is precisely the problem this architecture solves.
#
# Output is teed so we can inspect it: dev-profile.sh prints
# "[INFO] State up completed" and exits 0 EVEN WHEN IMAGE BUILDS FAILED.
# Observed 2026-08-06: every build failed with "--chmod requires BuildKit" and
# it still reported success, leaving zero containers running. Without the guard
# below, the only symptom is wait-for-vss.sh timing out 40 minutes later.
#
# Bounded retry (course 1 blocker B3). dev-profile.sh up is `down`, the upstream model download, a login and
# `compose up --build`. It is re-run ONLY when the log shows a pull or network error AND the daemon holds no
# container afterwards: compose pulls every image before it creates anything, so a pull failure normally
# leaves none. Once containers exist the failure is a startup problem and a blind re-run would tear down the
# evidence, so that case fails immediately with the containers left in place for inspection.
UP_ATTEMPTS=3
UP_RETRY_DELAY_S=90
UP_ERROR_RE='requires BuildKit|failed to solve|error building|Error response from daemon'
UP_RETRY_RE='unable to decode token response|error pulling image|failed to resolve reference|TLS handshake timeout|i/o timeout|connection reset by peer|unexpected EOF|dial tcp|no such host|Temporary failure in name resolution|context deadline exceeded|toomanyrequests|Service Unavailable|Bad Gateway|Gateway Time-?out|request canceled|httpReadSeeker|received unexpected HTTP status: 5[0-9][0-9]|Max retries exceeded|Read timed out|Failed to download .* from NGC'
UP_HINT="'requires BuildKit' means the deploy image is missing docker-buildx-plugin or DOCKER_BUILDKIT=1. 'failed to mount local volume' means the upstream checkout is not visible to dind at the same path; check the vss_src volume is mounted at ${SRC} in BOTH services."
LOG=/tmp/dev-profile.log
for attempt in $(seq 1 "${UP_ATTEMPTS}"); do
    status "RUNNING: dev-profile.sh up (attempt ${attempt} of ${UP_ATTEMPTS})"
    set +e
    bash "${DEPLOY_SH}" "${ARGS[@]}" 2>&1 | tee "${LOG}"
    rc=${PIPESTATUS[0]}
    set -e
    up_error=""
    if [ "${rc}" -ne 0 ]; then
        up_error="dev-profile.sh up exited ${rc}"
    elif grep -qE "${UP_ERROR_RE}" "${LOG}"; then
        up_error="deployment errors detected despite upstream reporting success"
    fi
    [ -n "${up_error}" ] || break
    log "${up_error}; last lines of ${LOG}:"
    tail -n 15 "${LOG}" | sed 's/^/[vss-deploy]   /'
    if ! created=$(docker ps -aq 2>/dev/null | wc -l | tr -d ' '); then
        fail "${up_error}, and the inner Docker daemon at ${DOCKER_HOST} no longer answers; not retrying. ${UP_HINT}"
    fi
    if [ "${created}" -gt 0 ]; then
        fail "${up_error} and ${created} container(s) now exist inside dind; not retrying, a re-run would tear them down. Inspect them from the lab (docker ps -a; docker logs <name>) and relaunch with VSS_FORCE_REDEPLOY=true. ${UP_HINT}"
    fi
    if ! grep -qE "${UP_RETRY_RE}" "${LOG}"; then
        fail "${up_error} with no pull or network error in the log, so a retry would not help. ${UP_HINT}"
    fi
    [ "${attempt}" -lt "${UP_ATTEMPTS}" ] ||
        fail "${up_error} on all ${UP_ATTEMPTS} attempts, a pull or network error each time; the registry errors are above in ${SEED_LOG}"
    log "pull or network error and no containers created; retrying dev-profile.sh up in ${UP_RETRY_DELAY_S} s (attempt $((attempt + 1)) of ${UP_ATTEMPTS})"
    status "RUNNING: dev-profile.sh up hit a pull or network error; retry $((attempt + 1)) of ${UP_ATTEMPTS} in ${UP_RETRY_DELAY_S} s"
    sleep "${UP_RETRY_DELAY_S}"
done

# ------------------------------------------------- credential containment
# dev-profile.sh writes the NGC key into
#   deploy/docker/developer-profiles/dev-profile-<profile>/generated.env
# (see its set_env_var "NGC_CLI_API_KEY" ... "true"; the "true" masks it in
# console output only; the FILE gets the real value).
#
# That path is inside /opt/vss, which is the vss_src volume, which is ALSO
# mounted in the lab container. So after a pre-deploy the course's own key is
# readable by any student with `cat`. The same key is also baked into the inner
# containers' environments, where `docker inspect` finds it.
#
# Scrub the file. The container environments cannot be scrubbed without
# recreating them, so warn plainly instead of pretending this is fully solved.
GEN_ENV="${SRC}/deploy/docker/developer-profiles/dev-profile-${VSS_PROFILE}/generated.env"
if [ -f "${GEN_ENV}" ]; then
    sed -i -E 's/^(NGC_CLI_API_KEY|NGC_API_KEY|NVIDIA_API_KEY|OPENAI_API_KEY)=.*/\1=<redacted-by-course>/' "${GEN_ENV}"
    log "scrubbed credentials from $(basename "${GEN_ENV})")"
fi
cat >&2 <<'WARN'
[vss-deploy] ---------------------------------------------------------------
[vss-deploy] WARNING: this was a COURSE-side deploy (VSS_STUDENT_DEPLOYS=false).
[vss-deploy] The course NGC key is now present in the environments of the inner
[vss-deploy] containers, which students CAN read via `docker inspect` from the
[vss-deploy] lab. generated.env has been scrubbed, but container envs cannot be.
[vss-deploy]
[vss-deploy] For a student-facing course use VSS_STUDENT_DEPLOYS=true, which
[vss-deploy] seeds caches only and lets students deploy with their own key.
[vss-deploy] ---------------------------------------------------------------
WARN

running=$(docker ps -q | wc -l | tr -d ' ')
[ "${running}" -gt 0 ] || \
    fail "dev-profile.sh reported success but no containers are running inside dind. Check ${LOG}."
log "${running} container(s) started inside dind"

# --------------------------------------------------------------- readiness
# Images were pre-pulled above, so a first run spends its 15-20 min on engine
# builds. Called, not exec'd: see readiness_gate.
readiness_gate
