# Deployer sidecar: docker CLI + compose + git + NGC CLI, no daemon.
#
# iproute2 is required: dev-profile.sh calls `ip route get 1.1.1.1` at script
# init to derive a default host IP, before any argument is parsed.
# Talks to dind over DOCKER_HOST and runs upstream's dev-profile.sh there.
#
# docker-buildx-plugin is REQUIRED, not optional: several upstream Dockerfiles
# use `COPY --chmod=...`, which the classic builder cannot do. Without buildx the
# client silently falls back to the legacy builder and those builds fail, while
# dev-profile.sh still reports "State up completed". See AGENTS.md.
#
# Debian rather than the smaller docker:*-cli (alpine) base: the NGC CLI is a
# glibc PyInstaller bundle and will not run on musl. dev-profile.sh shells out
# to `ngc registry model download-version` for the TAO models used by the
# alerts profile, so the CLI has to be present.
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG NGC_CLI_VERSION=4.13.0

# The three Docker packages are PINNED to the versions inside the deploy image that
# brought the Berlin stack up on 2026-10-18 (Docker CLI 29.9.1, Compose 5.1.4,
# buildx 0.31.1; read with `docker --version`, `docker compose version`,
# `docker buildx version` in that image). An unpinned install on 2026-10-21 got
# buildx 0.38.1, whose Bake entitlement check refuses the host-network builds of four
# upstream helper images ("additional privileges requested: pass --allow=network.host")
# and fails the deploy. Re-pin only after a full devbox deploy with the new versions.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git bash jq unzip procps iproute2 util-linux \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
       https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "docker-ce-cli=5:29.9.1-1~debian.12~bookworm" \
        "docker-compose-plugin=5.1.4-1~debian.12~bookworm" \
        "docker-buildx-plugin=0.31.1-1~debian.12~bookworm" \
    && rm -rf /var/lib/apt/lists/*

# NGC CLI, used by dev-profile.sh to pull TAO/perception model weights.
RUN curl -fsSL -o /tmp/ngccli.zip \
      "https://api.ngc.nvidia.com/v2/resources/nvidia/ngc-apps/ngc_cli/versions/${NGC_CLI_VERSION}/files/ngccli_linux.zip" \
    && unzip -q /tmp/ngccli.zip -d /opt \
    && chmod +x /opt/ngc-cli/ngc \
    && ln -s /opt/ngc-cli/ngc /usr/local/bin/ngc \
    && rm -f /tmp/ngccli.zip

COPY deploy-vss.sh   /deploy/deploy-vss.sh
COPY wait-for-vss.sh /deploy/wait-for-vss.sh
COPY restore-rt-vlm-model.sh /deploy/restore-rt-vlm-model.sh
COPY rt-vlm-checkpoint.json /deploy/rt-vlm-checkpoint.json
COPY restore-nemotron-cache.sh /deploy/restore-nemotron-cache.sh
COPY nemotron-checkpoint.json /deploy/nemotron-checkpoint.json
COPY restore-rt-embed-cache.sh /deploy/restore-rt-embed-cache.sh
COPY rt-embed-checkpoint.json /deploy/rt-embed-checkpoint.json
COPY restore-rt-cv-cache.sh /deploy/restore-rt-cv-cache.sh
COPY rt-cv-checkpoint.json /deploy/rt-cv-checkpoint.json
RUN chmod +x /deploy/*.sh

ENTRYPOINT ["/deploy/deploy-vss.sh"]
