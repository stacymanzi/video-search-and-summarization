#!/usr/bin/env bash
#
# Audits the host against VSS 3.x's documented prerequisites.
#
#   ./tests/test-host-prereqs.sh
#
# The DLI devbox and the production course VM are the same image, so what this
# reports here is what production has. Run it on any new VM image before
# assuming the course will deploy.
#
# Sources:
#   https://docs.nvidia.com/vss/3.0.0/vssnext-docs/latest/prerequisites.html
#   https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization
#
# A FAIL here is not necessarily fatal: the whole point of the DinD design is
# that some of these are satisfied inside the container instead of on the host.
# Each check says which.

set -uo pipefail

pass=0; fail=0; warn=0
ok()   { echo "  PASS   $*"; pass=$((pass+1)); }
no()   { echo "  FAIL   $*"; fail=$((fail+1)); }
wa()   { echo "  WARN   $*"; warn=$((warn+1)); }
inf()  { echo "         $*"; }

ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

echo "=== VSS 3.x host prerequisite audit ==="
echo

# ---- OS -------------------------------------------------------------------
. /etc/os-release
if [ "${VERSION_ID:-}" = "24.04" ] || [ "${VERSION_ID:-}" = "22.04" ]; then
    ok "OS: $PRETTY_NAME"
else
    no "OS: $PRETTY_NAME (VSS documents Ubuntu 24.04, or 22.04 on x86)"
fi

# ---- GPU ------------------------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
    names=$(nvidia-smi --query-gpu=name --format=csv,noheader | sort -u | paste -sd'; ')
    n=$(nvidia-smi --query-gpu=name --format=csv,noheader | grep -c .)
    if echo "$names" | grep -qiE 'H100|L40S|RTX PRO 6000|RTXPRO6000'; then
        ok "GPU: ${n}x $names (validated for VSS 3.x)"
    else
        wa "GPU: ${n}x $names, not on the validated list (H100, L40S, RTX PRO 6000 BW)"
    fi
    [ "$n" -ge 2 ] || wa "only $n GPU(s); the default local_shared budget assumes 2 (see .env)"

    drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
    ver_ge "$drv" "580.0" && ok "driver: $drv" || no "driver: $drv (VSS documents 580.x)"
else
    no "nvidia-smi not found"
fi

# ---- container toolkit ----------------------------------------------------
tk=$(nvidia-ctk --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [ -n "$tk" ]; then
    ver_ge "$tk" "1.17.8" && ok "container toolkit: $tk" || no "container toolkit: $tk (need >= 1.17.8)"
else
    no "nvidia-container-toolkit not found"
fi

# ---- docker ---------------------------------------------------------------
dv=$(docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [ -n "$dv" ]; then
    if ver_ge "$dv" "28.3.3" && ! ver_ge "$dv" "29.5.0"; then
        ok "docker: $dv (validated range 28.3.3 to <29.5.0)"
    elif ver_ge "$dv" "29.5.0"; then
        no "docker: $dv; VSS warns 29.5.0+ may fail pulling NGC-hosted images"
    else
        wa "docker: $dv, below the 28.3.3 validated floor"
    fi
else
    no "docker not found"
fi

# The INNER daemon is what VSS actually runs on, and its version is set by
# dind/Dockerfile, not by the host. Checking only the host missed a real
# incompatibility (the image shipped 29.7.2, above VSS's <29.5.0 ceiling).
idv=$(docker exec "${DIND_CONTAINER:-c-fx-17-v2-task1-dind-1}" docker --version 2>/dev/null \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [ -n "$idv" ]; then
    if ver_ge "$idv" "28.3.3" && ! ver_ge "$idv" "29.5.0"; then
        ok "dind docker: $idv (inner daemon, in range)"
    else
        no "dind docker: $idv is OUT OF RANGE. VSS needs 28.3.3 to <29.5.0."
        inf "Pin DOCKER_VERSION in dind/Dockerfile and rebuild."
    fi
else
    inf "dind not running; inner Docker version unchecked (start it and re-run)"
fi

cv=$(docker compose version --short 2>/dev/null)
[ -n "$cv" ] && ok "docker compose: $cv" || no "docker compose plugin not found"

# ---- the nvidia runtime the compose files rely on -------------------------
if docker info --format '{{range $k,$v := .Runtimes}}{{$k}} {{end}}' 2>/dev/null | grep -qw nvidia; then
    ok "'nvidia' runtime registered (required by our runtime: nvidia services)"
else
    no "'nvidia' runtime NOT registered; lab and dind will fail to start"
fi

# ---- cgroup driver: THE one DinD exists to solve --------------------------
cd_=$(docker info --format '{{.CgroupDriver}}' 2>/dev/null)
if [ "$cd_" = "cgroupfs" ]; then
    ok "host cgroup driver: cgroupfs (matches the VSS prerequisite)"
else
    wa "host cgroup driver: $cd_; VSS 3.x requires cgroupfs"
    inf "NOT a blocker for this course: dind/daemon.json sets cgroupfs for the"
    inf "inner daemon, which is where VSS actually runs. Changing the host would"
    inf "need root on the course VM, which is the constraint this design exists"
    inf "to work around."
fi

# ---- disk -----------------------------------------------------------------
dr=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
availg=$(df -BG --output=avail "$dr" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$availg" ]; then
    if [ "$availg" -ge 500 ]; then
        ok "disk free on $dr: ${availg}G"
    elif [ "$availg" -ge 250 ]; then
        wa "disk free on $dr: ${availg}G; enough to iterate, not proven for a"
        inf "shipped course. VSS documents 1 TB and upstream's launchable relocates"
        inf "Docker storage below 350G free. dind_storage lands here too. Measure"
        inf "whether the chosen VSS profile actually fits before setting the VM spec."
    else
        no "disk free on $dr: ${availg}G, but VSS documents 1 TB; the upstream"
        inf "launchable relocates Docker storage when root has <350G free."
        inf "dind_storage (all inner VSS image layers) lands here too."
        big=$(df -BG --output=target,avail -x tmpfs -x overlay 2>/dev/null | sort -k2 -rn | head -3 | tail -2 | tr -s ' ')
        inf "largest filesystems available: $(echo "$big" | tr '\n' ' ')"
    fi
fi

# ---- RAM / CPU ------------------------------------------------------------
ramg=$(free -g | awk '/^Mem:/{print $2}')
[ "$ramg" -ge 128 ] && ok "RAM: ${ramg}G" || no "RAM: ${ramg}G (VSS documents 128 GB)"
cores=$(nproc)
[ "$cores" -ge 18 ] && ok "CPU: ${cores} cores" || no "CPU: ${cores} cores (VSS documents 18-32)"

# ---- privileged containers (the Pebble open question) ---------------------
if docker run --rm --privileged --cgroupns private busybox:1.37.0 true >/dev/null 2>&1; then
    ok "host permits privileged + cgroupns=private containers"
    inf "Not a CI/CD blocker: DLI builds with privileged: true routinely. The"
    inf "only open concern is self-paced delivery, which is a management gate."
else
    no "host refuses privileged containers; the DinD design cannot run here"
fi

echo
echo "======================================"
echo "  passed: $pass   failed: $fail   warnings: $warn"
echo "======================================"
[ "$fail" -eq 0 ]
