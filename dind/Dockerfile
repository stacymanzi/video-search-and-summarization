# Docker-in-Docker sidecar for VSS 3.x.
#
# Purpose: give the course a Docker daemon it owns, so VSS's host-level
# prerequisites (cgroupfs cgroup driver, network sysctls) can be satisfied
# WITHOUT root on the DLI course VM. See ARCHITECTURE.md.
#
# Ported from x-ov-59-v1/task1/dind, with the Isaac Sim / remote-desktop
# couplings removed (that course's desktop needs NVENC, which H100 lacks).
#
# The outer container must run with:  privileged: true, gpus: all, cgroup: private
# It deliberately does NOT use network_mode: host; see entrypoint.sh.

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

# PIN THE INNER DOCKER. VSS documents a supported range of 28.3.3 to <29.5.0 and
# warns that 29.5.0+ may fail pulling NGC-hosted images. Unpinned, this image
# picked up 29.7.2 and upstream's own preflight refused to deploy onto it.
#
# This is the daemon VSS actually runs on; the HOST's version is irrelevant
# here, which is why a host-only prerequisite check missed it.
ARG DOCKER_VERSION=5:29.4.3-1~ubuntu.24.04~noble

# Docker CE: daemon + CLI + compose v2 plugin.
# Pinned below 29.5.0: VSS docs warn "Docker Engine 29.5.0+ may fail pulling
# NGC-hosted images." Validated range is 28.3.3 to <29.5.0.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg iproute2 iputils-ping jq && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
    https://download.docker.com/linux/ubuntu noble stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y \
        docker-ce="${DOCKER_VERSION}" \
        docker-ce-cli="${DOCKER_VERSION}" \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin && \
    apt-mark hold docker-ce docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

# nvidia-container-toolkit, so containers started by THIS daemon get GPUs.
RUN curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
        gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && \
    curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list && \
    apt-get update && \
    apt-get install -y nvidia-container-toolkit && \
    rm -rf /var/lib/apt/lists/*

# NOTE: upstream also ran `nvidia-ctk runtime configure --set-as-default` here.
# It is redundant: it writes /etc/docker/daemon.json, which the COPY below
# immediately overwrites. We COPY the file and skip the ctk call.

COPY daemon.json    /etc/docker/daemon.json
COPY sysctl.conf    /etc/vss-sysctl.conf
COPY entrypoint.sh  /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Inner image layers. MUST be a named volume, not a bind mount; overlay2
# will not run on top of most bind-mounted filesystems.
VOLUME /var/lib/docker

# Docker API, plaintext, reachable only on the private compose network.
# Port 2375 (not 2376) because 2376 conventionally implies TLS and this is not TLS.
EXPOSE 2375

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
