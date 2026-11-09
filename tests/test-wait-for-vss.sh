#!/usr/bin/env bash
#
# Offline test of deploy/wait-for-vss.sh: no daemon, no network, no stack.
#
#   ./tests/test-wait-for-vss.sh
#
# A stub `docker` on PATH answers `ps -a --format {{.Names}}` and
# `inspect -f <fmt> <name>` from a scenario file of `name|health|state` lines
# (a container absent from the file is absent from the daemon), and a stub
# `curl` exits with CURL_RC. The waiter runs with VSS_READY_POLL_S=1 and a cap
# of two seconds, so a failing scenario costs about two seconds.
#
# Scenarios, each checked on the exit
# code and on the LAST line of the status file, which is what notebooks 00 and
# 01 read; every scenario also checks that the deployer's own line above the
# waiter's survives (the waiter appends, never truncates):
#   1. everything ready                      -> OK: VSS search ready ...     exit 0
#   2. one container starting, one absent    -> FAILED: ... logstash(absent) vss-rtvi-vlm(starting)
#   3. all settled but the agent API is down -> FAILED: ... VSS-Agent-API(not answering at ...)
#   4. daemon unreachable                    -> FAILED: ... docker daemon unreachable: ...
#   5. VSS_READY_TIMEOUT_S=abc               -> FAILED: VSS_READY_TIMEOUT_S must be ... and no RUNNING line
#   6. VSS_ENABLE_RT_VLM=false, no vlm       -> OK (vss-rtvi-vlm not required)
#   7. VSS_READY_POLL_S=abc                  -> FAILED: VSS_READY_POLL_S must be ...

set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
WAITER=${WAITER:-${HERE}/../deploy/wait-for-vss.sh}
[ -f "${WAITER}" ] || { echo "waiter not found: ${WAITER}"; exit 2; }

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"

cat > "${WORK}/bin/docker" <<'STUB'
#!/usr/bin/env bash
# Stub daemon for test-wait-for-vss.sh. WAIT_TEST_SCENARIO: name|health|state per
# line. WAIT_TEST_DAEMON_DOWN=1 makes every call fail like an unreachable daemon.
if [ "${WAIT_TEST_DAEMON_DOWN:-0}" = 1 ]; then
    echo "Cannot connect to the Docker daemon at tcp://dind:2375. Is the docker daemon running?" >&2
    exit 1
fi
case "${1:-}" in
    ps)
        cut -d'|' -f1 "${WAIT_TEST_SCENARIO}"
        ;;
    inspect)
        line=$(grep "^${4}|" "${WAIT_TEST_SCENARIO}") || { echo "Error: No such object: ${4}" >&2; exit 1; }
        case "${3}" in
            *Health*) printf '%s\n' "${line}" | cut -d'|' -f2 ;;
            *)        printf '%s\n' "${line}" | cut -d'|' -f3 ;;
        esac
        ;;
    *)
        exit 0
        ;;
esac
STUB
cat > "${WORK}/bin/curl" <<'STUB'
#!/usr/bin/env bash
exit "${CURL_RC:-0}"
STUB
chmod +x "${WORK}/bin/docker" "${WORK}/bin/curl"
export PATH="${WORK}/bin:${PATH}"

# The search profile's required set, all healthy (the ones with a healthcheck)
# or plain running, plus an unrequired kibana so the container count is 12.
cat > "${WORK}/ready.txt" <<'SCENARIO'
vss-agent|healthy|running
vss-rtvi-cv|healthy|running
vss-rtvi-embed|healthy|running
vss-rtvi-vlm|healthy|running
vss-behavior-analytics||running
vss-vios-ingress|healthy|running
vss-vios-sensor||running
vss-vios-streamprocessing||running
elasticsearch|healthy|running
kafka|healthy|running
logstash||running
kibana||running
SCENARIO
# vlm still starting, logstash gone entirely.
grep -v '^logstash|' "${WORK}/ready.txt" | sed 's/^vss-rtvi-vlm|healthy|running$/vss-rtvi-vlm|starting|running/' > "${WORK}/pending.txt"
# No vlm at all (VSS_ENABLE_RT_VLM=false must not require it).
grep -v '^vss-rtvi-vlm|' "${WORK}/ready.txt" > "${WORK}/novlm.txt"

pass=0; fail=0
ok() { echo "  PASS  $*"; pass=$((pass+1)); }
no() { echo "  FAIL  $*"; fail=$((fail+1)); }

DEPLOYER_LINE="2026-10-20T00:00:00Z RUNNING: waiting for readiness (cap 2s)"

# run <name> <scenario> <want_rc> <last-line regex> [NAME=VALUE ...]
run() {
    local name=$1 scenario=$2 want_rc=$3 want_re=$4; shift 4
    local status="${WORK}/${name}.status" out="${WORK}/${name}.out" rc last first
    printf '%s\n' "${DEPLOYER_LINE}" > "${status}"
    env WAIT_TEST_SCENARIO="${scenario}" SEED_STATUS_FILE="${status}" \
        VSS_PROFILE=search VSS_READY_POLL_S=1 VSS_READY_TIMEOUT_S=2 "$@" \
        bash "${WAITER}" > "${out}" 2>&1
    rc=$?
    last=$(tail -n 1 "${status}")
    first=$(head -n 1 "${status}")
    if [ "${rc}" = "${want_rc}" ]; then ok "${name}: exit ${rc}"; else no "${name}: exit ${rc}, wanted ${want_rc} (log: ${out})"; fi
    if printf '%s\n' "${last}" | grep -Eq -- "${want_re}"; then ok "${name}: last status line matches"; else no "${name}: last status line '${last}' does not match /${want_re}/"; fi
    if [ "${first}" = "${DEPLOYER_LINE}" ]; then ok "${name}: deployer's line survived (append, not truncate)"; else no "${name}: status file was truncated"; fi
    if ! grep -Eq '^[0-9T:Z-]+ (RUNNING|OK|FAILED): ' "${status}"; then no "${name}: a status line is not '<ISO> <STATE>: <text>'"; fi
    LAST_STATUS_FILE="${status}"
}

echo "[test] 1. everything ready"
run ready "${WORK}/ready.txt" 0 ' OK: VSS search ready in [0-9]+s \(12 containers, vss-agent healthy, VSS Agent API answering\)$'

echo "[test] 2. one starting, one absent"
run pending "${WORK}/pending.txt" 1 ' FAILED: readiness timeout after 2s; not ready: logstash\(absent\) vss-rtvi-vlm\(starting\)$'
grep -q ' RUNNING: waiting logstash(absent) vss-rtvi-vlm(starting) [0-9]*s$' "${LAST_STATUS_FILE}" \
    && ok "pending: progress line names the pending set" || no "pending: no RUNNING line naming the pending set"

echo "[test] 3. all settled, agent API not answering"
run apidown "${WORK}/ready.txt" 1 ' FAILED: readiness timeout after 2s; not ready: VSS-Agent-API\(not answering at http://dind:8000/health\)$' CURL_RC=7

echo "[test] 4. daemon unreachable"
run daemondown "${WORK}/ready.txt" 1 ' FAILED: readiness timeout after 2s; not ready: docker daemon unreachable: Cannot connect to the Docker daemon' WAIT_TEST_DAEMON_DOWN=1

echo "[test] 5. bad cap"
run badcap "${WORK}/ready.txt" 1 " FAILED: VSS_READY_TIMEOUT_S must be a positive integer number of seconds, got 'abc'$" VSS_READY_TIMEOUT_S=abc
# Line 1 is the deployer's own "RUNNING: waiting for readiness" line; anything the waiter appended is below it.
tail -n +2 "${LAST_STATUS_FILE}" | grep -q ' RUNNING: waiting ' && no "badcap: the loop ran despite the bad cap" || ok "badcap: refused before the loop"

echo "[test] 6. RT-VLM disabled, no vlm container"
run novlm "${WORK}/novlm.txt" 0 ' OK: VSS search ready in [0-9]+s \(11 containers, vss-agent healthy, VSS Agent API answering\)$' VSS_ENABLE_RT_VLM=false

echo "[test] 7. bad poll interval"
run badpoll "${WORK}/ready.txt" 1 " FAILED: VSS_READY_POLL_S must be a positive integer number of seconds, got 'abc'$" VSS_READY_POLL_S=abc

echo
echo "======================================"
echo "  passed: $pass   failed: $fail"
echo "======================================"
[ "$fail" -eq 0 ]
