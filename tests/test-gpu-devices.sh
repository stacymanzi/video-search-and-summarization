#!/usr/bin/env bash
#
# Verifies GPU device assignment actually does what .env claims.
#
#   ./tests/test-gpu-devices.sh
#
# Assignment is checked by GPU **UUID**, not by index. Index is unreliable:
# a container pinned to host device 1 reports it as index 0 internally, so an
# index-based check would pass even when the wrong physical GPU is attached.
#
# Claims under test:
#   1. `runtime: nvidia` + NVIDIA_VISIBLE_DEVICES pins the lab to one GPU
#   2. Pinning follows the variable (0 -> GPU0, 1 -> GPU1), verified by UUID
#   3. dind with NVIDIA_VISIBLE_DEVICES=all preserves HOST device numbering,
#      so LLM_DEVICE_ID / VLM_DEVICE_ID mean the same thing inside
#   4. An inner container pinned to device N gets host GPU N
#   5. Reference: whether `deploy:` is honored by the local compose version
#
# (4) is the load-bearing one. If dind were pinned to a GPU subset instead of
# `all`, devices would renumber from 0 and the VSS device IDs would silently
# target the wrong hardware.

set -uo pipefail

CUDA_IMG=nvidia/cuda:12.6.0-base-ubuntu24.04
DIND_IMG=${DIND_IMG:-c-fx-17-v2-task1-dind:latest}
CTR=gputest-dind
VOL=gputest-dind-vol
WORK=$(mktemp -d)

pass=0; fail=0; skip=0
ok()   { echo "  PASS  $*"; pass=$((pass+1)); }
no()   { echo "  FAIL  $*"; fail=$((fail+1)); }
note() { echo "  NOTE  $*"; }
info() { echo "[test] $*"; }

cleanup() {
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    docker volume rm -f "$VOL" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

command -v nvidia-smi >/dev/null 2>&1 || { echo "nvidia-smi not found; skipping"; exit 0; }
NGPU=$(nvidia-smi --query-gpu=uuid --format=csv,noheader | grep -c .)
[ "$NGPU" -ge 2 ] || { echo "need >=2 GPUs to test pinning (found $NGPU); skipping"; exit 0; }

UUID0=$(nvidia-smi -i 0 --query-gpu=uuid --format=csv,noheader | tr -d '\r')
UUID1=$(nvidia-smi -i 1 --query-gpu=uuid --format=csv,noheader | tr -d '\r')
info "host GPU0=$UUID0"
info "host GPU1=$UUID1"

docker image inspect "$CUDA_IMG" >/dev/null 2>&1 || docker pull -q "$CUDA_IMG" >/dev/null

# -- 1/2. lab-style pinning, the legacy form --------------------------------
info "checking lab-style pinning (runtime: nvidia + NVIDIA_VISIBLE_DEVICES)"
cat > "$WORK/lab.yml" <<'YAML'
services:
  lab:
    image: nvidia/cuda:12.6.0-base-ubuntu24.04
    runtime: nvidia
    environment:
      NVIDIA_VISIBLE_DEVICES: "${LAB_DEVICE_ID:-0}"
      NVIDIA_DRIVER_CAPABILITIES: compute,utility
    command: nvidia-smi --query-gpu=uuid --format=csv,noheader
YAML

for d in 0 1; do
    want=$([ "$d" = 0 ] && echo "$UUID0" || echo "$UUID1")
    got=$(LAB_DEVICE_ID=$d docker compose -f "$WORK/lab.yml" run --rm lab 2>/dev/null | grep GPU | tr -d '\r')
    n=$(echo "$got" | grep -c .)
    if [ "$got" = "$want" ] && [ "$n" = "1" ]; then
        ok "LAB_DEVICE_ID=$d -> exactly 1 GPU, correct device"
    else
        no "LAB_DEVICE_ID=$d -> expected 1x $want, got ($n) '$got'"
    fi
done

# -- 3/4. nested: dind preserves numbering ----------------------------------
if ! docker image inspect "$DIND_IMG" >/dev/null 2>&1; then
    note "dind image not built; skipping nested checks (run: docker-compose build dind)"
    skip=$((skip+1))
else
    info "starting dind with NVIDIA_VISIBLE_DEVICES=all"
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    docker run -d --name "$CTR" --privileged --cgroupns private \
        --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=all \
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
        -v "$VOL":/var/lib/docker "$DIND_IMG" >/dev/null

    for _ in $(seq 1 45); do docker exec "$CTR" docker info >/dev/null 2>&1 && break; sleep 2; done

    if ! docker exec "$CTR" docker info >/dev/null 2>&1; then
        no "dind never became ready"
    else
        inner0=$(docker exec "$CTR" nvidia-smi -i 0 --query-gpu=uuid --format=csv,noheader 2>/dev/null | tr -d '\r')
        inner1=$(docker exec "$CTR" nvidia-smi -i 1 --query-gpu=uuid --format=csv,noheader 2>/dev/null | tr -d '\r')
        if [ "$inner0" = "$UUID0" ] && [ "$inner1" = "$UUID1" ]; then
            ok "dind preserves host device numbering (0->GPU0, 1->GPU1)"
        else
            no "dind renumbered devices; VSS device IDs would target wrong GPUs"
        fi

        docker exec "$CTR" docker pull -q "$CUDA_IMG" >/dev/null 2>&1
        for d in 0 1; do
            want=$([ "$d" = 0 ] && echo "$UUID0" || echo "$UUID1")
            got=$(docker exec "$CTR" docker run --rm --gpus "\"device=$d\"" "$CUDA_IMG" \
                    nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | grep GPU | tr -d '\r')
            if [ "$got" = "$want" ]; then
                ok "inner container device=$d -> host GPU$d"
            else
                no "inner container device=$d -> expected $want, got '$got'"
            fi
        done
    fi
fi

# -- 5. reference: is `deploy:` honored here? -------------------------------
info "reference check: is the deploy: directive honored by this compose?"
cat > "$WORK/dep.yml" <<'YAML'
services:
  gpudev:
    image: nvidia/cuda:12.6.0-base-ubuntu24.04
    command: nvidia-smi --query-gpu=uuid --format=csv,noheader
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["1"]
              capabilities: [gpu]
YAML
got=$(docker compose -f "$WORK/dep.yml" run --rm gpudev 2>/dev/null | grep GPU | tr -d '\r')
if [ "$got" = "$UUID1" ]; then
    note "deploy.resources.reservations.devices IS honored by $(docker compose version --short 2>/dev/null)."
    note "We still use runtime: nvidia anyway: it is the team convention"
    note "(api-endpoint-template, c-fx-17-v1, s-fx-36-v1), it also works on legacy"
    note "version 2.3 tooling in case the CI builder differs, and an ignored"
    note "deploy: fails SILENTLY by handing the lab every GPU."
else
    note "deploy.resources.reservations.devices is IGNORED here, which confirms the"
    note "decision to use runtime: nvidia + NVIDIA_VISIBLE_DEVICES."
fi

echo
echo "======================================"
echo "  passed: $pass   failed: $fail   skipped: $skip"
echo "======================================"
[ "$fail" -eq 0 ]
