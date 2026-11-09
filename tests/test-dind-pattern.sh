#!/usr/bin/env bash
#
# Validates the architectural claims this course depends on, WITHOUT pulling the
# VSS images (which need NGC entitlement and ~30 pulls). Run this first on any
# new host or kernel before assuming the design holds.
#
#   ./tests/test-dind-pattern.sh
#
# Claims under test:
#   1. dind starts with a PRIVATE network namespace (no network_mode: host)
#   2. The VSS network sysctls we DO apply take effect inside it, without
#      touching the host, and IPv6 stays enabled (see dind/sysctl.conf)
#   3. cgroupfs cgroup driver + overlay2 + nvidia default runtime are in effect
#   4. GPUs reach containers started by the inner daemon
#   5. Inner containers on `network_mode: host` reach each other over localhost
#      ACROSS separate compose projects (the psf -> Kafka pattern)
#   6. A peer on the course network reaches inner services at dind:<port>
#   7. Nothing leaks onto the host NIC

set -uo pipefail

IMAGE=cfx17-dind-proto:test
CTR=cfx17-dind-test
NET=cfx17proto-net
VOL=cfx17-dind-storage
PROBE_PORT=19092

pass=0; fail=0
ok()   { echo "  PASS  $*"; pass=$((pass+1)); }
no()   { echo "  FAIL  $*"; fail=$((fail+1)); }
info() { echo "[test] $*"; }

cleanup() {
    docker exec "$CTR" docker rm -f projA >/dev/null 2>&1 || true
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    docker volume rm -f "$VOL" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

info "building $IMAGE"
docker build -q -t "$IMAGE" "$(dirname "$0")/../dind" >/dev/null || { echo "build failed"; exit 1; }

docker network create "$NET" >/dev/null 2>&1 || true
info "starting dind (privileged, cgroupns=private, NO host networking)"
docker run -d --name "$CTR" \
    --privileged --cgroupns private --gpus all \
    --network "$NET" -v "$VOL":/var/lib/docker \
    "$IMAGE" >/dev/null

for _ in $(seq 1 60); do docker exec "$CTR" docker info >/dev/null 2>&1 && break; sleep 2; done
docker exec "$CTR" docker info >/dev/null 2>&1 || { echo "dind never became ready"; docker logs "$CTR"; exit 1; }

D="docker exec $CTR"

# -- 1/2. sysctls, and host isolation ----------------------------------------
info "checking sysctls"
in_tcpr=$($D sysctl -n net.ipv4.tcp_rmem 2>/dev/null | tr -s '[:space:]' ' ')

# IPv6 must remain ENABLED inside dind. Upstream's ingress chain resolves
# localhost to ::1 first (nginx :30888 -> envoy :10000 -> backend :30001), so
# disabling it (which the VSS docs tell you to do on a host) makes every
# /vst/api/* path fail with "Cannot assign requested address [::1]:30001".
# See dind/sysctl.conf for why those three lines are deliberately omitted.
in_v6=$($D sysctl -n net.ipv6.conf.lo.disable_ipv6 2>/dev/null)
[ "$in_v6" = "0" ] && ok "IPv6 left enabled on lo inside dind (required by the VIOS ingress chain)" \
                   || no "IPv6 disabled on lo; this breaks /vst/api/* (see dind/sysctl.conf)"
has_v6=$($D sh -c 'ip -6 addr show lo 2>/dev/null | grep -c "::1/128"' 2>/dev/null)
[ "${has_v6:-0}" -ge 1 ] && ok "::1 present on lo (envoy upstreams resolve)" || no "::1 missing on lo"
[ "${in_tcpr% }" = "4096 87380 16777216" ] && ok "net.ipv4.tcp_rmem applied inside dind" || no "tcp_rmem not applied (got: $in_tcpr)"

# net.core.* is NOT network-namespaced on current kernels. Expected to fail;
# recorded explicitly so a future kernel change is noticed rather than assumed.
in_core=$($D sysctl -n net.core.rmem_max 2>/dev/null)
host_core=$(sysctl -n net.core.rmem_max 2>/dev/null)
if [ "$in_core" = "5242880" ]; then
    ok "net.core.rmem_max applied inside dind (kernel now namespaces it; update ARCHITECTURE.md §5)"
else
    echo "  KNOWN net.core.rmem_max NOT namespaced (dind=$in_core host=$host_core); needs platform-level provisioning"
fi

# -- 3. daemon configuration -------------------------------------------------
info "checking inner daemon configuration"
cfg=$($D docker info --format '{{.DefaultRuntime}}|{{.CgroupDriver}}|{{.Driver}}' 2>/dev/null)
[ "${cfg%%|*}" = "nvidia" ] && ok "default runtime = nvidia" || no "default runtime = ${cfg%%|*}"
echo "$cfg" | grep -q '|cgroupfs|' && ok "cgroup driver = cgroupfs (VSS prerequisite)" || no "cgroup driver != cgroupfs ($cfg)"
[ "${cfg##*|}" = "overlay2" ] && ok "storage driver = overlay2" || no "storage driver = ${cfg##*|}"

# -- 4. GPU passthrough ------------------------------------------------------
info "checking GPU passthrough into inner containers (pulls a CUDA base image)"
gpus=$($D docker run --rm nvidia/cuda:12.6.0-base-ubuntu24.04 \
        nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -c . || echo 0)
[ "$gpus" -ge 1 ] && ok "$gpus GPU(s) visible to inner containers" || no "no GPUs inside inner containers"

# -- 5/6/7. networking -------------------------------------------------------
info "checking cross-project localhost + peer reachability"
$D docker rm -f projA >/dev/null 2>&1 || true
$D docker run -d --name projA --network host busybox:1.37.0 \
    sh -c "while true; do echo MDX-EVENT-OK | nc -l -p $PROBE_PORT -w 1; done" >/dev/null
sleep 3

r=$($D docker run --rm --network host busybox:1.37.0 sh -c "nc -w 3 127.0.0.1 $PROBE_PORT" 2>/dev/null | tr -d '\r\n')
[ "$r" = "MDX-EVENT-OK" ] && ok "cross-project localhost reachability (the psf->Kafka pattern)" || no "localhost reachability failed (got: '$r')"

r=$(docker run --rm --network "$NET" busybox:1.37.0 sh -c "nc -w 3 $CTR $PROBE_PORT" 2>/dev/null | tail -1 | tr -d '\r\n')
[ "$r" = "MDX-EVENT-OK" ] && ok "peer reaches inner service at dind:<port> (nginx/lab path)" || no "peer reachability failed (got: '$r')"

if ss -ltn 2>/dev/null | grep -q ":$PROBE_PORT "; then
    no "inner port LEAKED onto the host NIC"
else
    ok "inner ports confined to dind's namespace (nothing on host NIC)"
fi

echo
echo "======================================"
echo "  passed: $pass   failed: $fail"
echo "======================================"
[ "$fail" -eq 0 ]
