#!/usr/bin/env bash
#
# DinD entrypoint. Three jobs, in order:
#   1. apply the VSS network sysctls (possible here because we own the netns)
#   2. work around cgroup v2 threaded-mode for nested Docker
#   3. start dockerd and hold the container open
#
# Deliberately NOT network_mode: host. Inner containers that declare
# `network_mode: host` join THIS container's namespace, which is all they
# actually need: they address each other over localhost. Keeping the namespace
# private is what makes step 1 possible without touching the course VM.
# See ARCHITECTURE.md §4.

set -euo pipefail

log() { echo "[dind] $*"; }

# ---------------------------------------------------------------- 1. sysctls
# These are the documented VSS 3.x host prerequisites. In a private netns they
# are ours to set. Non-fatal: net.core.* namespacing is kernel-dependent, and
# the buffer tuning is a throughput optimisation, not a correctness requirement,
# at the stream counts a course uses.
log "applying VSS network sysctls in private namespace"
if sysctl -p /etc/vss-sysctl.conf >/tmp/sysctl.out 2>/tmp/sysctl.err; then
    log "all sysctls applied"
else
    log "some sysctls could not be applied (non-fatal):"
    sed 's/^/[dind]   /' /tmp/sysctl.err || true
fi
sed 's/^/[dind]   ok: /' /tmp/sysctl.out || true

# ------------------------------------------------------------- 2. cgroup v2
# In cgroup v2 a cgroup cannot hold both processes and subtree controllers.
# Move PID 1 into a child cgroup so the root can delegate to Docker's cgroups.
# Same approach as the official docker:dind image.
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    log "cgroup v2 detected, delegating subtree control"
    mkdir -p /sys/fs/cgroup/init
    echo $$ > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
    sed -e 's/ / +/g' -e 's/^/+/' /sys/fs/cgroup/cgroup.controllers \
        > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
fi

# --------------------------------------------------------------- 3. dockerd
# TCP is bound to 0.0.0.0 *within this container's private namespace*, so it is
# reachable only from the compose network, not from the course VM's NIC. That
# is a material improvement on the host-networked original, which exposed an
# unauthenticated root-equivalent Docker API on the host.
# It is still plaintext; add TLS before anything multi-tenant.
# `--tls=false` is required, not cosmetic: Docker 29.x treats an unauthenticated
# TCP bind without explicit intent as deprecated and *deliberately delays startup
# by ~15s* to make you notice. Stating intent removes the penalty. The warning is
# still correct; see ARCHITECTURE.md §8 for the TLS follow-up.
log "starting dockerd"
dockerd \
    --host=tcp://0.0.0.0:2375 \
    --host=unix:///var/run/docker.sock \
    --tls=false \
    --config-file=/etc/docker/daemon.json &
DOCKERD_PID=$!

log "waiting for dockerd to accept connections"
for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
        log "dockerd ready"
        break
    fi
    sleep 1
done

if ! docker info >/dev/null 2>&1; then
    log "FATAL: dockerd did not become ready within 60s"
    exit 1
fi

# Report GPU visibility once, so course-startup logs show it plainly.
if docker info --format '{{.DefaultRuntime}}' 2>/dev/null | grep -q nvidia; then
    log "default runtime: nvidia"
else
    log "WARNING: default runtime is not nvidia; inner containers will not see GPUs"
fi

wait "$DOCKERD_PID"
