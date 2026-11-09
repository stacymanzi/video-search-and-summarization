#!/usr/bin/env bash
#
# Block until the VSS stack is ready, then exit 0 so `lab` can start.
# Exits non-zero on timeout, which fails course startup loudly rather than
# handing the student a JupyterLab with nothing behind it.
#
# Called by deploy-vss.sh (CALLED, never exec'd, so the deployer's EXIT trap
# still runs afterwards). Appends its own progress to the status file the
# notebooks read, one line per change or every HEARTBEAT seconds:
#
#   RUNNING: waiting <name(state)> ... <elapsed>s
#   OK: VSS <profile> ready in <t>s (<n> containers, <core> healthy, <API> answering)
#   FAILED: readiness timeout after <cap>s; not ready: <name(state)> ...
#
# deploy-vss.sh owns every line before the first RUNNING: waiting line; this
# script never truncates the file. It can also be run by hand from the lab
# terminal against DOCKER_HOST=tcp://dind:2375 (status lines then go to the
# same file through the vss_src mount).
#
# ---------------------------------------------------------------------------
# DESIGN NOTE: do not replace this with hand-written HTTP health probes.
#
# An earlier version guessed an HTTP readiness path per service. Two of five
# guesses were wrong and cost a 40-minute false failure on a stack that was
# already fully up:
#
#   :30888/vst/api/v1/ready  -> 503   (the correct VIOS path is /health)
#   :30082/v1/health/ready   -> 000   (in the lvs profile there is no VLM NIM
#                                      on 30082 at all; rtvi-vlm serves the VLM)
#
# Upstream already declares a healthcheck for each service it cares about, and
# those definitions track the profile and the version. Gate on THOSE, and keep
# exactly one HTTP check (the student-facing API), because that is what the
# notebooks call and the one thing that must genuinely answer.
# ---------------------------------------------------------------------------

set -uo pipefail

log() { echo "[wait-for-vss] $*"; }

# Shared status file; see deploy-vss.sh, which exports SEED_STATUS_FILE. Lines are appended, never
# overwritten, so the deployer's last step stays above the waiter's progress and `tail -n 1` is the current
# state. A write failure is reported once per line and does not stop the gate: the gate is the product, the
# status line is its mirror for the notebooks.
SEED_STATUS_FILE=${SEED_STATUS_FILE:-/opt/vss/_course-runtime/seed-status}
status_append() {
    mkdir -p "$(dirname "${SEED_STATUS_FILE}")" 2>/dev/null
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "${SEED_STATUS_FILE}" 2>/dev/null \
        || log "WARNING: could not append to ${SEED_STATUS_FILE}; the notebooks will not see this line: $*"
}
fail() { log "FATAL: $*"; status_append "FAILED: $*"; exit 1; }

# Readiness cap in seconds. The default, 60 min, is the slowest cold start on record for the search profile:
# rtvi-embed and rtvi-vlm each declare start_period 1200s, and RT-DETR + SigLIP2 TensorRT engines build on
# first run. Set VSS_READY_TIMEOUT_S in task1/.env (passed through the vss-deploy environment) once the
# production launches have been timed; the value must be a positive integer or the gate refuses to start.
TIMEOUT=${VSS_READY_TIMEOUT_S:-3600}
if ! [[ "${TIMEOUT}" =~ ^[0-9]+$ ]] || [ "${TIMEOUT}" -le 0 ]; then
    fail "VSS_READY_TIMEOUT_S must be a positive integer number of seconds, got '${TIMEOUT}'"
fi
# Poll interval. VSS_READY_POLL_S is a test hook for tests/test-wait-for-vss.sh (stub daemon, seconds instead of
# minutes); production leaves it unset. Validated like the cap so a typo cannot turn the loop into a busy spin.
INTERVAL=${VSS_READY_POLL_S:-15}
if ! [[ "${INTERVAL}" =~ ^[0-9]+$ ]] || [ "${INTERVAL}" -le 0 ]; then
    fail "VSS_READY_POLL_S must be a positive integer number of seconds, got '${INTERVAL}'"
fi
HEARTBEAT=120   # seconds between repeated progress lines when nothing changes: tells "slow" from "dead"
PROFILE=${VSS_PROFILE:-search}
HOST=${VSS_HOST:-dind}
case "${PROFILE}" in
    search)
        # The sandbox profile: RT-CV + RT-Embed from stock search, RT-VLM patched
        # in by deploy-vss.sh. vss-rtvi-vlm is listed conditionally because the
        # patch can be disabled with VSS_ENABLE_RT_VLM=false.
        #
        # This list is the same set notebook 01 (cell a3d3cf2d, `REQUIRED`)
        # inspects, minus nothing: keep the two in step. vss-vios-streamprocessing
        # is the VIOS component that decodes uploads into frames; without it a
        # sensor exists but no detection ever appears (course 1, P6).
        #
        # vss-behavior-analytics earns its place here: it is the SOLE producer of
        # mdx-embed-filtered (rtvi-embed -> mdx-embed -> behavior-analytics ->
        # mdx-embed-filtered). If it is down, embedding search returns nothing
        # while every other surface looks perfectly healthy.
        #
        # Kibana is deliberately NOT required: no notebook, grader or agent route
        # depends on it, and gating the launch on an exploration UI would turn a
        # Kibana restart into a FAILED course. Notebook 01 is asked to drop it
        # from its HTTP check too.
        CORE=${VSS_CORE_SERVICE:-vss-agent}
        READY_URL=${VSS_READY_URL:-http://${HOST}:${VSS_AGENT_PORT:-8000}/health}
        READY_LABEL="VSS Agent API"
        REQUIRED="vss-agent vss-rtvi-cv vss-rtvi-embed vss-behavior-analytics vss-vios-ingress vss-vios-sensor vss-vios-streamprocessing elasticsearch kafka logstash"
        [ "${VSS_ENABLE_RT_VLM:-true}" = "true" ] && REQUIRED="${REQUIRED} vss-rtvi-vlm"
        ;;
    alerts)
        CORE=${VSS_CORE_SERVICE:-vss-agent}
        READY_URL=${VSS_READY_URL:-http://${HOST}:${ALERT_BRIDGE_PORT:-9080}/api/v1/realtime}
        READY_LABEL="Alert Bridge API"
        REQUIRED="vss-agent vss-agent-ui vss-alert-bridge vss-behavior-analytics vss-rtvi-cv vss-rtvi-vlm vss-vios-ingress vss-haproxy-ingress kafka elasticsearch redis"
        ;;
    lvs)
        CORE=${VSS_CORE_SERVICE:-vss-lvs}
        READY_URL=${VSS_READY_URL:-http://${HOST}:${LVS_PORT:-38111}/v1/ready}
        READY_LABEL="LVS API"
        REQUIRED="vss-lvs"
        ;;
    *)
        CORE=${VSS_CORE_SERVICE:-vss-agent}
        READY_URL=${VSS_READY_URL:-http://${HOST}:${VSS_AGENT_PORT:-8000}/health}
        READY_LABEL="VSS Agent API"
        REQUIRED="${CORE}"
        ;;
esac

log "gate: profile=${PROFILE} core=${CORE} cap=${TIMEOUT}s required=[${REQUIRED}] api=${READY_URL}"

start=$SECONDS
last_key=""        # the pending set of the last emitted line, without the elapsed time
last_emit=-1       # SECONDS at the last emitted line, for the heartbeat
pending=""         # what is still not ready, as name(state) tokens; also the FAILED detail

while (( SECONDS - start < TIMEOUT )); do
    elapsed=$((SECONDS - start))
    # Every container the inner daemon knows about, with its health verdict.
    # Format: name|health|state   (health is empty when no healthcheck is declared)
    if ! names=$(docker ps -a --format '{{.Names}}' 2>&1); then
        snapshot=""
        daemon_error="docker daemon unreachable: $(printf '%s' "${names}" | head -1)"
    else
        daemon_error=""
        snapshot=$(printf '%s\n' "${names}" | while read -r c; do
            [ -n "$c" ] || continue
            h=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{end}}' "$c" 2>/dev/null)
            s=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
            echo "${c}|${h}|${s}"
        done)
    fi

    starting=$(echo "$snapshot" | awk -F'|' '$2=="starting"{print $1}' | tr '\n' ' ')
    unhealthy=$(echo "$snapshot" | awk -F'|' '$2=="unhealthy"{print $1}' | tr '\n' ' ')
    core_health=$(echo "$snapshot" | awk -F'|' -v c="$CORE" '$1==c{print $2}')
    core_state=$(echo "$snapshot" | awk -F'|' -v c="$CORE" '$1==c{print $3}')

    missing=""          # names only, for the log line
    missing_detail=""   # name(state), for the status line
    for c in ${REQUIRED}; do
        state=$(echo "$snapshot" | awk -F'|' -v c="$c" '$1==c{print $3}')
        if [ "$state" != "running" ]; then
            missing="${missing} ${c}"
            missing_detail="${missing_detail} ${c}(${state:-absent})"
        fi
    done
    missing=${missing# }
    missing_detail=${missing_detail# }

    # The pending set: every reason the gate is still closed, each named once.
    pending="${missing_detail}"
    for c in ${starting}; do
        case " ${missing} " in *" ${c} "*) ;; *) pending="${pending} ${c}(starting)" ;; esac
    done
    for c in ${unhealthy}; do
        case " ${missing} " in *" ${c} "*) ;; *) pending="${pending} ${c}(unhealthy)" ;; esac
    done
    if [ "${core_health}" != "healthy" ]; then
        case " ${missing} ${starting} ${unhealthy} " in
            *" ${CORE} "*) ;;
            *) pending="${pending} ${CORE}(${core_health:-${core_state:-absent}})" ;;
        esac
    fi
    # A dead daemon makes every container "absent"; name the one real cause instead of the whole list.
    [ -z "${daemon_error}" ] || pending="${daemon_error}"
    pending=${pending# }

    msg="core=${CORE}:${core_health:-${core_state:-absent}}"
    [ -n "$starting" ]  && msg="$msg starting=[${starting% }]"
    [ -n "$unhealthy" ] && msg="$msg UNHEALTHY=[${unhealthy% }]"
    [ -n "$missing" ]   && msg="$msg MISSING=[${missing}]"
    [ -n "$daemon_error" ] && msg="$msg ${daemon_error}"

    if [ "$core_health" = "healthy" ] && [ -z "$starting" ] && \
       [ -z "$unhealthy" ] && [ -z "$missing" ] && [ -z "$daemon_error" ]; then
        # Everything upstream gates on is settled. Confirm the student-facing API.
        if curl -sf --max-time 10 "$READY_URL" >/dev/null 2>&1; then
            n=$(echo "$snapshot" | grep -c .)
            log "VSS ${PROFILE} ready in ${elapsed}s: ${n} containers, core healthy, ${READY_LABEL} answering"
            status_append "OK: VSS ${PROFILE} ready in ${elapsed}s (${n} containers, ${CORE} healthy, ${READY_LABEL} answering)"
            exit 0
        fi
        pending="${READY_LABEL// /-}(not answering at ${READY_URL})"
        msg="$msg core healthy but ${READY_URL} not answering yet"
    fi

    # One line per change, and a heartbeat when nothing changes, in both the log and the status file.
    if [ "$pending" != "$last_key" ] || (( last_emit < 0 || SECONDS - last_emit >= HEARTBEAT )); then
        log "${elapsed}s: $msg"
        status_append "RUNNING: waiting ${pending} ${elapsed}s"
        last_key="$pending"
        last_emit=$SECONDS
    fi

    sleep "$INTERVAL"
done

log "TIMEOUT after ${TIMEOUT}s. Final state:"
docker ps -a --format '  {{.Names}}\t{{.Status}}' 2>&1 | sed 's/^/[wait-for-vss] /'
fail "readiness timeout after ${TIMEOUT}s; not ready: ${pending:-nothing pending (the API never answered)}"
