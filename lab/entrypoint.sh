#!/usr/bin/env bash
###############################################################################
# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
# 
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.
###############################################################################

set -euo pipefail

# HOST_IP is consumed by both the VSS skills and upstream Docker Compose. The
# outer hostname "dind" is valid here but is not resolvable by inner containers,
# so resolve it once per lab start and export its numeric IPv4 address.
dind_hostname="${DLI_DIND_HOSTNAME:-dind}"
dind_host_ip=""
for _ in {1..30}; do
    dind_host_ip="$(getent ahostsv4 "${dind_hostname}" 2>/dev/null \
        | awk 'NR == 1 { print $1; exit }' || true)"
    [ -n "${dind_host_ip}" ] && break
    sleep 1
done

[ -n "${dind_host_ip}" ] || {
    echo "ERROR: could not resolve ${dind_hostname} to IPv4" >&2
    exit 1
}

export HOST_IP="${dind_host_ip}"
echo "[lab] VSS HOST_IP=${HOST_IP}"

# The course bar's status server (lab/task/lab_home.py --serve). Started with
# the container so the bar is on every JupyterLab page from the first load:
# nginx adds the <script> that draws it (task1/nginx.conf, location /lab), the
# browser reaches the server through jupyter-server-proxy at /lab/proxy/8050/,
# and the Home notebook's first cell only refreshes it. Binds 127.0.0.1 only.
# Log: /var/log/lab_home.log. The loop restarts it if it ever exits; on a
# devbox, editing lab_home.py and running the Home cell stops the stale server
# (build id check) and this loop brings the new file up.
# `set +e` inside the subshell: it inherits this script's errexit, and the
# first non-zero exit (SIGTERM from the Home cell's build-id check, a crash)
# would otherwise end the loop before the echo runs.
( set +e
  while true; do
    python3 /dli/task/lab_home.py --serve --port 8050
    echo "[lab_home] status server exited ($?), restarting in 2 s"
    sleep 2
  done ) >>/var/log/lab_home.log 2>&1 &
lab_home_up=0
for _ in {1..20}; do
    if python3 -c 'import socket; s = socket.socket(); s.settimeout(0.5); s.connect(("127.0.0.1", 8050))' 2>/dev/null; then
        lab_home_up=1
        break
    fi
    sleep 0.5
done
if [ "${lab_home_up}" = 1 ]; then
    echo "[lab] course bar status server on 127.0.0.1:8050 (log /var/log/lab_home.log)"
else
    echo "[lab] WARNING: course bar status server did not answer on 127.0.0.1:8050 within 10 s; see /var/log/lab_home.log (JupyterLab starts anyway)"
fi

# The login token. The platform sets JUPYTER_TOKEN; on a devbox it is often
# not set at all (a non-interactive `docker-compose up` passes nothing), and
# under `set -u` a bare reference to it used to abort the container before
# JupyterLab started. Unset or empty: pass no token flag, so Jupyter generates
# a random one (login still required; `jupyter server list` prints it). An
# explicit empty token would switch authentication off, which is never wanted.
token_args=()
if [ -n "${JUPYTER_TOKEN:-}" ]; then
    token_args=(--NotebookApp.token="${JUPYTER_TOKEN}")
fi

# This example script is used as the entrypoint for our Docker container.
# The following command launches the JuptyerLab server, which is redirected by
# nginx to the course URL
exec jupyter lab \
        --ip 0.0.0.0                               `# Bind to all network interfaces` \
        --allow-root                               `# Allow running as the root user` \
        --no-browser                               `# Do not attempt to launch a browser` \
        --NotebookApp.base_url="/lab"              `# Set a base URL for the lab` \
        ${token_args[@]+"${token_args[@]}"}        `# Require the platform's token, or a generated one` \
        --NotebookApp.password=""                  `# Do not require password to access the course` \
        --notebook-dir="/dli/task"                 `# Set notebook directory`
