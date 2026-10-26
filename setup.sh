#!/bin/bash
# setup.sh — One-time bootstrap for VSS Smart Cities labs.
#
# On Brev:     called automatically as root at instance creation.
#              Credentials come from Brev secrets (NGC_CLI_API_KEY, HF_TOKEN).
# Standalone:  run as a regular user with sudo; prompts for credentials if unset.
#
# Usage (standalone):
#   bash setup.sh [BASE_DIR]
#
# BASE_DIR defaults to /home/ubuntu/models-and-assets (root) or ~/models-and-assets (user).
# Re-running is safe: every step is skipped if already complete.

set -euo pipefail

# ── Mode detection ─────────────────────────────────────────────────────────────
IS_ROOT=0; [[ "$(id -u)" == "0" ]] && IS_ROOT=1

UBUNTU_HOME="/home/ubuntu"
if [[ $IS_ROOT == 1 ]]; then
    DEFAULT_BASE="$UBUNTU_HOME/models-and-assets"
    DEFAULT_COURSE="$UBUNTU_HOME/vss-smart-cities-course"
else
    DEFAULT_BASE="$HOME/models-and-assets"
    DEFAULT_COURSE="$HOME/vss-smart-cities-course"
fi

BASE_DIR="${1:-$DEFAULT_BASE}"
DATA_DIR="$BASE_DIR/data"
HF_HOME="$DATA_DIR/hf_home"
COSMOS_REPO_DIR="$BASE_DIR/cosmos-framework"
TAO_SKILL_BANK_DIR="$DATA_DIR/lab3/tao-skill-bank"
LAB3_DIR="$DATA_DIR/lab3"
COURSE_DIR="$DEFAULT_COURSE"
BREV_VENV="/home/ubuntu/.venv"

# Container images
NIM_COSMOS_IMAGE="nvcr.io/nim/nvidia/cosmos3-reasoner:1.7"
NIM_NEMOTRON_IMAGE="nvcr.io/nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:1.7.0-variant"
TAO_IMAGE="nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl"
TAO_SKILL_BANK_TAG="7.2.0"

# HuggingFace model revisions
COSMOS3_NANO_REPO="nvidia/Cosmos3-Nano"
COSMOS3_NANO_REVISION="7a312c868bcce8e40b3eb40861300a9d0ba3fde1"
QWEN_IMAGE_EDIT_REPO="Qwen/Qwen-Image-Edit"
QWEN_IMAGE_EDIT_REVISION="ac7f9318f633fc4b5778c59367c8128225f1e3de"
COSMOS_GUARDRAIL_REPO="nvidia/Cosmos-Guardrail1"
COSMOS_GUARDRAIL_REVISION="d6d4bfa899a71454a700907664f3e88f503950cf"
QWEN_GUARD_REPO="Qwen/Qwen3Guard-Gen-0.6B"
WAN_VAE_REPO="Wan-AI/Wan2.2-TI2V-5B"
WAN_VAE_REVISION="921dbaf3f1674a56f47e83fb80a34bac8a8f203e"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[init]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── 1. Credentials (collect first so the rest runs unattended) ─────────────────
if [[ -z "${NGC_CLI_API_KEY:-}" ]]; then
    echo ""
    echo "  NGC API key required. Generate at: https://ngc.nvidia.com/setup/api-key"
    read -r -s -p "  Paste NGC API key: " NGC_CLI_API_KEY
    echo ""
    [[ -n "$NGC_CLI_API_KEY" ]] || error "NGC_CLI_API_KEY was empty."
    export NGC_CLI_API_KEY
fi
if [[ -z "${HF_TOKEN:-}" ]]; then
    echo ""
    echo "  HuggingFace token required. Generate at: https://huggingface.co/settings/tokens"
    read -r -s -p "  Paste HuggingFace token: " HF_TOKEN
    echo ""
    [[ -n "$HF_TOKEN" ]] || error "HF_TOKEN was empty."
    export HF_TOKEN
fi
info "Credentials set."

# ── 2. System packages (root only) ─────────────────────────────────────────────
if [[ $IS_ROOT == 1 ]]; then
    info "System packages..."
    apt-get update -qq
    apt-get install -y -q git git-lfs curl ffmpeg libx11-6 libxcb1 libxext6 libgl1 \
        libglib2.0-0 libsm6 libxrender1 tree wget socat software-properties-common
    git lfs install --skip-smudge
else
    info "Checking prerequisites..."
    command -v git     >/dev/null 2>&1 || error "'git' not found."
    command -v docker  >/dev/null 2>&1 || error "'docker' not found. Install Docker + NVIDIA Container Toolkit."
    command -v nvidia-smi >/dev/null 2>&1 || error "'nvidia-smi' not found. NVIDIA driver not installed."
    command -v ffmpeg  >/dev/null 2>&1 || warn "'ffmpeg' not found. Install: sudo apt-get install -y ffmpeg"
    if ! command -v git-lfs >/dev/null 2>&1; then
        sudo apt-get install -y -q git-lfs
        git lfs install
    fi
    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
    [[ "$GPU_COUNT" -ge 2 ]] || warn "Only $GPU_COUNT GPU(s) found. Labs 2–3 require 2× H100 (≥60 GB each)."
fi

# ── 3. uv 0.11.29 + Python 3.12 ───────────────────────────────────────────────
info "uv + Python..."
# cosmos-framework pins uv to ==0.11.29; install that version directly
curl -LsSf https://astral.sh/uv/0.11.29/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
UV="$HOME/.local/bin/uv"
"$UV" python install 3.12

# Make uv visible in system PATH so cosmos-framework subprocesses find it
ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv 2>/dev/null || \
    sudo ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv 2>/dev/null || true

# ── 4. Sysctls ────────────────────────────────────────────────────────────────
info "Sysctls..."
_sysctl() {
    if [[ $IS_ROOT == 1 ]]; then sysctl -w "$1" || true;
    else sudo sysctl -w "$1" || true; fi
}
_sysctl vm.max_map_count=262144
_sysctl net.core.somaxconn=65535
grep -q vm.max_map_count /etc/sysctl.conf 2>/dev/null || echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf >/dev/null
grep -q net.core.somaxconn /etc/sysctl.conf 2>/dev/null || echo "net.core.somaxconn=65535" | sudo tee -a /etc/sysctl.conf >/dev/null

# ── 5. Directories ────────────────────────────────────────────────────────────
info "Directories..."
mkdir -p \
    "$HF_HOME" \
    "$DATA_DIR/cosmos3-reasoner-cache" \
    "$DATA_DIR/nemotron-nim-cache" \
    "$DATA_DIR/models" \
    "$DATA_DIR/vss-caches" \
    "$DATA_DIR/Part-2" \
    "$LAB3_DIR/models" \
    "$LAB3_DIR/dataset/train/videos" \
    "$LAB3_DIR/dataset/val/videos" \
    "$LAB3_DIR/cache" \
    "$LAB3_DIR/work" \
    "$LAB3_DIR/ptm_cache/huggingface" \
    "$LAB3_DIR/ptm_cache/home/.cache" \
    "$LAB3_DIR/ptm_cache/inductor" \
    "$LAB3_DIR/ptm_cache/triton"

[[ $IS_ROOT == 1 ]] && chown -R ubuntu:ubuntu "$BASE_DIR" || true

# ── 6. Course repo + media files ──────────────────────────────────────────────
info "Course repo..."
if [[ ! -d "$COURSE_DIR/.git" ]]; then
    git clone -b vss-smart-cities-course \
        https://github.com/stacymanzi/video-search-and-summarization.git "$COURSE_DIR"
fi
cp "$COURSE_DIR/data/traffic.png"       "$DATA_DIR/traffic.png"       2>/dev/null || \
    warn "traffic.png missing from course repo — copy manually to $DATA_DIR/traffic.png"
cp "$COURSE_DIR/data/traffic_short.mp4" "$DATA_DIR/traffic_short.mp4" 2>/dev/null || \
    warn "traffic_short.mp4 missing from course repo — copy manually to $DATA_DIR/traffic_short.mp4"

# ── 7. cosmos-framework ────────────────────────────────────────────────────────
info "cosmos-framework..."
if [[ ! -d "$COSMOS_REPO_DIR/.git" ]]; then
    git clone https://github.com/NVIDIA/cosmos-framework.git "$COSMOS_REPO_DIR"
fi
if [[ ! -x "$COSMOS_REPO_DIR/.venv/bin/python" ]]; then
    info "Building cosmos venv (~10 min)..."
    (cd "$COSMOS_REPO_DIR" && "$UV" sync --all-extras --group=cu130-train)
fi

# Pre-build hf_cli venv so guardrail download works on first torchrun
HF_CLI_DIR="$COSMOS_REPO_DIR/cosmos_framework/utils/hf_cli"
if [[ -f "$HF_CLI_DIR/pyproject.toml" && ! -d "$COSMOS_REPO_DIR/.hf-cli-venv" ]]; then
    info "Pre-building hf_cli venv..."
    (cd "$HF_CLI_DIR" && UV_OFFLINE=0 "$UV" sync --locked --no-default-groups)
fi

UV_PYTHON_PARENT=""
if [[ -L "$COSMOS_REPO_DIR/.venv/bin/python" ]]; then
    _bin="$(readlink -f "$COSMOS_REPO_DIR/.venv/bin/python")"
    UV_PYTHON_PARENT="$(dirname "$(dirname "$(dirname "$_bin")")")"
fi

# ── 8. TAO Skill Bank ──────────────────────────────────────────────────────────
info "TAO Skill Bank..."
if [[ ! -d "$TAO_SKILL_BANK_DIR/.git" ]]; then
    git clone --branch "$TAO_SKILL_BANK_TAG" --depth 1 \
        https://github.com/NVIDIA-TAO/tao-skill-bank.git "$TAO_SKILL_BANK_DIR"
fi

# ── 9. HuggingFace model downloads ─────────────────────────────────────────────
info "HuggingFace login..."
HF_HOME="$HF_HOME" "$UV" run --no-project --python 3.12 python -c "
from huggingface_hub import login
import os
login(token=os.environ['HF_TOKEN'], add_to_git_credential=False)
" 2>/dev/null || \
"$UV" pip install -q huggingface-hub --python 3.12 && \
HF_HOME="$HF_HOME" "$UV" run --no-project --python 3.12 python -c "
from huggingface_hub import login
import os
login(token=os.environ['HF_TOKEN'], add_to_git_credential=False)
"

snapshot_exists() {
    local repo="$1" rev="$2" name model_dir
    name="${repo//\//'--'}"
    model_dir="$HF_HOME/hub/models--$name/snapshots/$rev"
    [[ -d "$model_dir" ]] && [[ "$(ls -A "$model_dir" 2>/dev/null)" ]] && return 0
    model_dir="$HF_HOME/models--$name/snapshots/$rev"
    [[ -d "$model_dir" ]] && [[ "$(ls -A "$model_dir" 2>/dev/null)" ]]
}

download_hf_model() {
    local repo="$1" revision="$2" label="$3"
    info "Downloading $label ($repo @ ${revision:0:8}...)..."
    HF_HOME="$HF_HOME" "$UV" run --no-project --python 3.12 python -c "
from huggingface_hub import snapshot_download
import os
snapshot_download(
    repo_id='$repo',
    revision='${revision}' if '${revision}' else None,
    cache_dir=os.environ['HF_HOME'],
    token=os.environ['HF_TOKEN'],
)
print('  done.')
"
}

write_refs_main() {
    local repo="$1" rev="$2" name refs_dir
    [[ -z "$rev" ]] && return 0
    name="${repo//\//'--'}"
    for prefix in "hub/" ""; do
        refs_dir="$HF_HOME/${prefix}models--$name/refs"
        if [[ -d "$HF_HOME/${prefix}models--$name" ]]; then
            mkdir -p "$refs_dir"
            echo "$rev" > "$refs_dir/main"
        fi
    done
}

snapshot_exists "$COSMOS3_NANO_REPO"     "$COSMOS3_NANO_REVISION"     && info "Cosmos3-Nano cached."     || download_hf_model "$COSMOS3_NANO_REPO"     "$COSMOS3_NANO_REVISION"     "Cosmos3-Nano (~50 GB)"
snapshot_exists "$QWEN_IMAGE_EDIT_REPO"  "$QWEN_IMAGE_EDIT_REVISION"  && info "Qwen-Image-Edit cached."  || download_hf_model "$QWEN_IMAGE_EDIT_REPO"  "$QWEN_IMAGE_EDIT_REVISION"  "Qwen-Image-Edit (~57 GB)"
snapshot_exists "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION" && info "Cosmos-Guardrail1 cached." || download_hf_model "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION" "Cosmos-Guardrail1"
snapshot_exists "$QWEN_GUARD_REPO"       ""                           && info "Qwen3Guard cached."        || download_hf_model "$QWEN_GUARD_REPO"       ""                           "Qwen3Guard-Gen-0.6B"
snapshot_exists "$WAN_VAE_REPO"          "$WAN_VAE_REVISION"          && info "Wan2.2-TI2V-5B cached."   || download_hf_model "$WAN_VAE_REPO"          "$WAN_VAE_REVISION"          "Wan2.2-TI2V-5B VAE (~15 GB)"

write_refs_main "$COSMOS3_NANO_REPO"     "$COSMOS3_NANO_REVISION"
write_refs_main "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION"
write_refs_main "$QWEN_GUARD_REPO" \
    "$(ls "$HF_HOME/models--${QWEN_GUARD_REPO//\//'--'}/snapshots/" 2>/dev/null | head -1)"

# ── 10. Docker login + pull containers ─────────────────────────────────────────
info "Docker login to nvcr.io..."
echo "$NGC_CLI_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin

pull_image() {
    local image="$1"
    if docker image inspect "$image" >/dev/null 2>&1; then
        info "$image already pulled — skipping."
    else
        info "Pulling $image ..."
        docker pull "$image"
    fi
}
pull_image "$NIM_COSMOS_IMAGE"
pull_image "$NIM_NEMOTRON_IMAGE"
pull_image "$TAO_IMAGE"

# ── 11. Lab 3: Cosmos3-Nano → Qwen3-VL safetensors conversion ─────────────────
PTM_DIR="$LAB3_DIR/models/Cosmos3-Nano-VLM"
if ls "$PTM_DIR"/*.safetensors 2>/dev/null | head -1 | grep -q .; then
    info "Lab 3 base checkpoint already converted — skipping."
else
    info "Converting Cosmos3-Nano → Qwen3-VL safetensors inside TAO container (~60 min)..."
    docker run --rm --ipc=host --name tao-convert \
        -u "$(id -u):$(id -g)" \
        -e USER=taouser -e LOGNAME=taouser \
        -e HOME=/cache/home \
        -e XDG_CACHE_HOME=/cache/home/.cache \
        -e TORCHINDUCTOR_CACHE_DIR=/cache/inductor \
        -e TRITON_CACHE_DIR=/cache/triton \
        -e BASE_MODEL="$COSMOS3_NANO_REPO" \
        -e BASE_REV="$COSMOS3_NANO_REVISION" \
        -e OUTPUT_NAME=Cosmos3-Nano-VLM \
        -e CONVERT_ENTRYPOINT=cosmos_rl.model_preparation.vlm_safetensors \
        -e ARCH_MODEL=Qwen/Qwen3-VL-8B-Instruct \
        -e PYTHONUNBUFFERED=1 \
        -e HF_TOKEN="$HF_TOKEN" \
        -v "$LAB3_DIR/models:/output" \
        -v "$LAB3_DIR/ptm_cache:/cache" \
        --entrypoint bash \
        "$TAO_IMAGE" \
        -lc '
set -Eeuo pipefail
export HF_HOME=/cache/huggingface
SRC="$(python - <<'"'"'PYX'"'"'
import os
from huggingface_hub import snapshot_download
print(snapshot_download(os.environ["BASE_MODEL"],
                        revision=os.environ["BASE_REV"] or None,
                        cache_dir="/cache/huggingface"))
PYX
)"
python -m "$CONVERT_ENTRYPOINT" --checkpoint-path "$SRC" --vlm-model-name "$ARCH_MODEL" -o "/output/$OUTPUT_NAME"
'
    info "Checkpoint conversion complete → $PTM_DIR"
fi

# ── 12. Python packages (Brev venv — root only) ────────────────────────────────
if [[ $IS_ROOT == 1 ]]; then
    info "Python packages (Brev venv)..."
    PKGS="openai diffusers>=0.33.0 transformers accelerate ipywidgets tomli-w \
          requests-unixsocket2 huggingface-hub>=0.23 hf-xet torchvision matplotlib pillow"
    if [[ -d "$BREV_VENV" ]]; then
        sudo -u ubuntu "$UV" pip install -q --python "$BREV_VENV/bin/python3" $PKGS
    else
        "$UV" pip install -q --system $PKGS
    fi
fi

# ── 13. socat bridge (Brev / root only) ────────────────────────────────────────
if [[ $IS_ROOT == 1 ]]; then
    info "socat bridge (systemd)..."
    cat > /etc/systemd/system/docker-tcp-bridge.service << 'SVCEOF'
[Unit]
Description=socat bridge: Docker unix socket → tcp://localhost:2375
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:2375,bind=127.0.0.1,fork,reuseaddr UNIX-CLIENT:/var/run/docker.sock
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SVCEOF
    systemctl daemon-reload
    systemctl enable --now docker-tcp-bridge
fi

# ── 14. Environment ────────────────────────────────────────────────────────────
if [[ $IS_ROOT == 1 ]]; then
    info "Writing /etc/environment..."
    printf '%s\n' "
VSS_LABS_DIR=$BASE_DIR
LAB_DATA_DIR=$DATA_DIR
HF_HOME=$HF_HOME
NIM_CACHE=$DATA_DIR/cosmos3-reasoner-cache
NIM_IMAGE=$NIM_COSMOS_IMAGE
NIM_GPU=0
NIM_PORT=30082
NIM_MODEL_SIZE=nano
NIM_READY_TIMEOUT_S=600
VLM_BASE_URL=http://localhost:30082
COURSE_VLM_BASE_URL=http://localhost:30082
COURSE_LLM_BASE_URL=http://localhost:30081
COURSE_LVS_BASE_URL=http://localhost:38111
COURSE_VIOS_BASE_URL=http://localhost:30888/vst/api
COURSE_ELK_BASE_URL=http://localhost:9200
COSMOS3_REPO=$COSMOS_REPO_DIR
COSMOS3_CHECKPOINT_PATH=Cosmos3-Nano
COSMOS3_NANO_REVISION=$COSMOS3_NANO_REVISION
COSMOS_GUARDRAIL_REVISION=$COSMOS_GUARDRAIL_REVISION
COSMOS3_NUM_GPUS=1
COSMOS3_MASTER_ADDR=127.0.0.1
COSMOS3_VENV_PYTHON=$COSMOS_REPO_DIR/.venv/bin/python
IMAGINAIRE_HF_CLI_ENVIRONMENT=$COSMOS_REPO_DIR/.hf-cli-venv
UV_PYTHON_PARENT=$UV_PYTHON_PARENT
QWEN_GPU=0
COSMOS_GPU=1
NEMOTRON_NIM_IMAGE=$NIM_NEMOTRON_IMAGE
NEMOTRON_NIM_CONTAINER=nemotron-nim
NEMOTRON_NIM_CACHE=$DATA_DIR/nemotron-nim-cache
NEMOTRON_NIM_PORT=30083
NEMOTRON_NIM_BASE_URL=http://localhost:30083
DLI_BASE=$LAB3_DIR
DLI_PTM=$LAB3_DIR/models/Cosmos3-Nano-VLM
DLI_CACHE=$LAB3_DIR/cache
DLI_DATA=$LAB3_DIR/dataset
DLI_ROOT=$LAB3_DIR/work
DLI_SKILL_BANK=$TAO_SKILL_BANK_DIR
DLI_IMAGE=$TAO_IMAGE
DLI_SKILL_BANK_REF=$TAO_SKILL_BANK_TAG
DLI_GPU=1
LAB_GPUS=0,1
DOCKER_HOST=tcp://localhost:2375
HF_HUB_OFFLINE=0
TRANSFORMERS_OFFLINE=0" >> /etc/environment
else
    # Standalone: write .env for docker compose and env.sh to source
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    info "Writing $REPO_DIR/.env ..."
    cat > "$REPO_DIR/.env" << EOF
# Generated by setup.sh — do not commit.
NGC_CLI_API_KEY=$NGC_CLI_API_KEY
HF_TOKEN=$HF_TOKEN
VSS_LABS_DIR=$BASE_DIR
UV_PYTHON_PARENT=$UV_PYTHON_PARENT
JUPYTER_TOKEN=
NGINX_PORT=80
LAB_GPUS=0,1
VSS_VERSION=3.2.1
VSS_PROFILE=alerts
HARDWARE_PROFILE=H100
NIM_MODEL_SIZE=nano
EOF

    info "Writing $BASE_DIR/env.sh ..."
    cat > "$BASE_DIR/env.sh" << EOF
# Source before running notebooks without docker compose:
#   source $BASE_DIR/env.sh
export NGC_CLI_API_KEY=$NGC_CLI_API_KEY
export HF_TOKEN=$HF_TOKEN
export VSS_LABS_DIR=$BASE_DIR
export LAB_DATA_DIR=$DATA_DIR
export HF_HOME=$HF_HOME
export HF_HUB_OFFLINE=0
export TRANSFORMERS_OFFLINE=0
export NIM_CACHE=$DATA_DIR/cosmos3-reasoner-cache
export NIM_IMAGE=$NIM_COSMOS_IMAGE
export NIM_GPU=0
export NIM_PORT=30082
export NIM_MODEL_SIZE=nano
export NIM_READY_TIMEOUT_S=600
export VLM_BASE_URL=http://localhost:30082
export COURSE_VLM_BASE_URL=http://localhost:30082
export COURSE_LLM_BASE_URL=http://localhost:30081
export COSMOS3_REPO=$COSMOS_REPO_DIR
export COSMOS3_CHECKPOINT_PATH=Cosmos3-Nano
export COSMOS3_NANO_REVISION=$COSMOS3_NANO_REVISION
export COSMOS3_NUM_GPUS=1
export COSMOS3_MASTER_ADDR=127.0.0.1
export COSMOS3_VENV_PYTHON=$COSMOS_REPO_DIR/.venv/bin/python
export IMAGINAIRE_HF_CLI_ENVIRONMENT=$COSMOS_REPO_DIR/.hf-cli-venv
export UV_PYTHON_PARENT=$UV_PYTHON_PARENT
export QWEN_GPU=0
export COSMOS_GPU=1
export NEMOTRON_NIM_IMAGE=$NIM_NEMOTRON_IMAGE
export NEMOTRON_NIM_CONTAINER=nemotron-nim
export NEMOTRON_NIM_CACHE=$DATA_DIR/nemotron-nim-cache
export NEMOTRON_NIM_PORT=30083
export NEMOTRON_NIM_BASE_URL=http://localhost:30083
export DOCKER_HOST=unix:///var/run/docker.sock
export DLI_BASE=$LAB3_DIR
export DLI_PTM=$LAB3_DIR/models/Cosmos3-Nano-VLM
export DLI_CACHE=$LAB3_DIR/cache
export DLI_DATA=$LAB3_DIR/dataset
export DLI_ROOT=$LAB3_DIR/work
export DLI_IMAGE=$TAO_IMAGE
export DLI_SKILL_BANK=$TAO_SKILL_BANK_DIR
export DLI_SKILL_BANK_REF=$TAO_SKILL_BANK_TAG
EOF
    [[ -n "$UV_PYTHON_PARENT" ]] && printf 'export UV_PYTHON_PARENT=%s\n' "$UV_PYTHON_PARENT" >> "$BASE_DIR/env.sh"
fi

# ── Done ───────────────────────────────────────────────────────────────────────
info "Done! All assets ready under $BASE_DIR"
if [[ $IS_ROOT == 0 ]]; then
    echo ""
    echo "  Next steps:"
    echo "    Option A (Docker Compose):  docker compose up -d  → http://localhost"
    echo "    Option B (bare JupyterLab): source $BASE_DIR/env.sh && jupyter lab"
    echo ""
    echo "  Run notebooks in order:"
    echo "    lab-1-vlm/lab_1.ipynb  →  lab-2-sdg/lab_2.ipynb  →  lab-3-vlm-pt/lab_3.ipynb"
fi
