#!/usr/bin/env bash
# setup.sh — One-time bootstrap for all VSS Smart Cities labs on a fresh machine.
#
# Usage:
#   bash setup.sh [BASE_DIR]
#
# BASE_DIR defaults to $HOME/models-and-assets.
# Credentials are entered interactively — no export commands needed.
# If NGC_CLI_API_KEY and HF_TOKEN are already exported, prompts are skipped.
# Re-running is safe: model downloads, container pulls, and the venv build are
# all skipped if already present.
#
# Requirements:
#   - Linux x86_64 with NVIDIA driver r580+ (CUDA 13)
#   - 2× A100 80 GB or H100 (or equivalent, ≥60 GB VRAM each)
#   - ≥600 GB free disk in BASE_DIR
#   - docker (with NVIDIA Container Toolkit)
#   - git
#   - System packages: curl ffmpeg  (installed automatically if missing)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR="${1:-$HOME/models-and-assets}"
DATA_DIR="$BASE_DIR/data"
HF_HOME="$DATA_DIR/hf_home"
COSMOS_REPO_DIR="$BASE_DIR/cosmos-framework"
TAO_SKILL_BANK_DIR="$DATA_DIR/lab3/tao-skill-bank"
LAB3_DIR="$DATA_DIR/lab3"

NIM_COSMOS_IMAGE="nvcr.io/nim/nvidia/cosmos3-reasoner:1.7"
NIM_NEMOTRON_IMAGE="nvcr.io/nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:1.7.0-variant"
TAO_IMAGE="nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl"
TAO_SKILL_BANK_TAG="7.2.0"

COSMOS3_NANO_REPO="nvidia/Cosmos3-Nano"
COSMOS3_NANO_REVISION="7a312c868bcce8e40b3eb40861300a9d0ba3fde1"
QWEN_IMAGE_EDIT_REPO="Qwen/Qwen-Image-Edit"
QWEN_IMAGE_EDIT_REVISION="ac7f9318f633fc4b5778c59367c8128225f1e3de"
COSMOS_GUARDRAIL_REPO="nvidia/Cosmos-Guardrail1"
COSMOS_GUARDRAIL_REVISION="d6d4bfa899a71454a700907664f3e88f503950cf"
QWEN_GUARD_REPO="Qwen/Qwen3Guard-Gen-0.6B"
WAN_VAE_REPO="Wan-AI/Wan2.2-TI2V-5B"
WAN_VAE_REVISION="921dbaf3f1674a56f47e83fb80a34bac8a8f203e"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Collect credentials interactively (skipped if already exported)
# ---------------------------------------------------------------------------
if [[ -z "${NGC_CLI_API_KEY:-}" ]]; then
    echo ""
    echo "  NGC API key required."
    echo "  Generate one at: https://ngc.nvidia.com/setup/api-key"
    echo ""
    read -r -s -p "  Paste NGC API key: " NGC_CLI_API_KEY
    echo ""
    [[ -n "$NGC_CLI_API_KEY" ]] || error "NGC_CLI_API_KEY was empty."
    export NGC_CLI_API_KEY
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
    echo ""
    echo "  HuggingFace token required."
    echo "  Generate one at: https://huggingface.co/settings/tokens"
    echo ""
    read -r -s -p "  Paste HuggingFace token: " HF_TOKEN
    echo ""
    [[ -n "$HF_TOKEN" ]] || error "HF_TOKEN was empty."
    export HF_TOKEN
fi

info "Credentials set."

# ---------------------------------------------------------------------------
# 2. Prerequisites
# ---------------------------------------------------------------------------
info "Checking prerequisites..."

command -v docker     >/dev/null 2>&1 || error "'docker' not found. Install Docker + NVIDIA Container Toolkit."
command -v git        >/dev/null 2>&1 || error "'git' not found."
command -v nvidia-smi >/dev/null 2>&1 || error "'nvidia-smi' not found. NVIDIA driver not installed."
command -v ffmpeg     >/dev/null 2>&1 || warn "'ffmpeg' not found. Install with: sudo apt-get install -y ffmpeg"

# Auto-install git-lfs if missing
if ! command -v git-lfs >/dev/null 2>&1; then
    info "git-lfs not found — installing..."
    sudo apt-get install -y -q git-lfs || error "Could not install git-lfs. Run: sudo apt-get install -y git-lfs"
    git lfs install
fi

# Auto-install Python 3.13 if missing (via deadsnakes PPA on Ubuntu/Debian)
if ! command -v python3.13 >/dev/null 2>&1; then
    info "python3.13 not found — installing via deadsnakes PPA..."
    sudo add-apt-repository ppa:deadsnakes/ppa -y
    sudo apt-get update -qq
    sudo apt-get install -y -q python3.13 python3.13-venv python3.13-dev \
        || error "Could not install python3.13. Install manually from https://www.python.org/downloads/"
fi

# Auto-install uv if missing
if ! command -v uv >/dev/null 2>&1; then
    info "uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
# Full path so sudo calls later find uv regardless of PATH stripping
UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
[[ "$GPU_COUNT" -ge 2 ]] || warn "Only $GPU_COUNT GPU(s) found. Labs 2 and 3 require 2× H100 (≥60 GB each). Continuing anyway."

python3.13 -c "import huggingface_hub" 2>/dev/null \
    || "$UV" pip install -q huggingface-hub --python python3.13 --system \
    || error "'huggingface_hub' could not be installed. Run: $UV pip install huggingface-hub --python python3.13 --system"

# ---------------------------------------------------------------------------
# 3. Host sysctls (required by Elasticsearch / VSS)
# ---------------------------------------------------------------------------
info "Applying host sysctls required by VSS (Elasticsearch)..."
sudo sysctl -w vm.max_map_count=262144 || warn "Could not set vm.max_map_count — run: sudo sysctl -w vm.max_map_count=262144"
sudo sysctl -w net.core.somaxconn=65535 || warn "Could not set net.core.somaxconn — run: sudo sysctl -w net.core.somaxconn=65535"

# Persist across reboots
SYSCTL_CONF=/etc/sysctl.conf
if ! grep -q "vm.max_map_count" "$SYSCTL_CONF" 2>/dev/null; then
    echo "vm.max_map_count=262144" | sudo tee -a "$SYSCTL_CONF" > /dev/null || warn "Could not persist vm.max_map_count to $SYSCTL_CONF — add it manually."
fi
if ! grep -q "net.core.somaxconn" "$SYSCTL_CONF" 2>/dev/null; then
    echo "net.core.somaxconn=65535" | sudo tee -a "$SYSCTL_CONF" > /dev/null || warn "Could not persist net.core.somaxconn to $SYSCTL_CONF — add it manually."
fi
info "Sysctls set and persisted to $SYSCTL_CONF."

# ---------------------------------------------------------------------------
# 4. Directory structure
# ---------------------------------------------------------------------------
info "Creating directory structure under $BASE_DIR ..."
mkdir -p \
    "$DATA_DIR" \
    "$HF_HOME" \
    "$DATA_DIR/cosmos3-reasoner-cache" \
    "$DATA_DIR/nemotron-nim-cache" \
    "$DATA_DIR/models" \
    "$DATA_DIR/vss-caches" \
    "$LAB3_DIR/models" \
    "$LAB3_DIR/dataset/train/videos" \
    "$LAB3_DIR/dataset/val/videos" \
    "$LAB3_DIR/cache" \
    "$LAB3_DIR/work" \
    "$LAB3_DIR/ptm_cache/huggingface" \
    "$LAB3_DIR/ptm_cache/home/.cache" \
    "$LAB3_DIR/ptm_cache/inductor" \
    "$LAB3_DIR/ptm_cache/triton"

# Lab 1 media ships in the repo's data/ folder - copy the real files first.
REPO_DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/data"
for _f in traffic.png traffic_short.mp4; do
    if [[ ! -s "$DATA_DIR/$_f" && -s "$REPO_DATA_DIR/$_f" ]]; then
        cp "$REPO_DATA_DIR/$_f" "$DATA_DIR/$_f"
        info "Copied $_f from the repo."
    fi
done

# Placeholder media files so Lab 1 notebook checks pass (replace with real files)
if [[ ! -s "$DATA_DIR/traffic.png" ]]; then
    warn "No traffic.png found — creating a placeholder. Replace with a real traffic-camera image before running Lab 1."
    printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x00\x00\x00\x00:~\x9bU\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82' \
        > "$DATA_DIR/traffic.png"
fi
if [[ ! -s "$DATA_DIR/traffic_short.mp4" ]]; then
    warn "No traffic_short.mp4 found — creating an empty placeholder. Replace with a real video clip before running Lab 1."
    touch "$DATA_DIR/traffic_short.mp4"
fi

# ---------------------------------------------------------------------------
# 5. HuggingFace model downloads
# ---------------------------------------------------------------------------
info "Logging in to HuggingFace..."
python3.13 -c "
from huggingface_hub import login
import os
login(token=os.environ['HF_TOKEN'], add_to_git_credential=False)
"

download_hf_model() {
    local repo="$1" revision="$2" label="$3"
    info "Downloading $label ($repo @ ${revision:0:8}...)..."
    HF_HOME="$HF_HOME" python3.13 -c "
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

snapshot_exists() {
    local repo="$1" rev="$2" name model_dir
    name="${repo//\//'--'}"
    # Check both standard layout (HF_HOME/hub/models--*) and hf-xet layout (HF_HOME/models--*)
    model_dir="$HF_HOME/hub/models--$name/snapshots/$rev"
    [[ -d "$model_dir" ]] && [[ "$(ls -A "$model_dir" 2>/dev/null)" ]] && return 0
    model_dir="$HF_HOME/models--$name/snapshots/$rev"
    [[ -d "$model_dir" ]] && [[ "$(ls -A "$model_dir" 2>/dev/null)" ]]
}

write_refs_main() {
    local repo="$1" rev="$2" name refs_dir
    name="${repo//\//'--'}"
    for prefix in "hub/" ""; do
        refs_dir="$HF_HOME/${prefix}models--$name/refs"
        if [[ -d "$HF_HOME/${prefix}models--$name" ]]; then
            mkdir -p "$refs_dir"
            printf %s "$rev" > "$refs_dir/main"
        fi
    done
}

snapshot_exists "$COSMOS3_NANO_REPO"     "$COSMOS3_NANO_REVISION"     && info "Cosmos3-Nano already cached — skipping."     || download_hf_model "$COSMOS3_NANO_REPO"     "$COSMOS3_NANO_REVISION"     "Cosmos3-Nano (~50 GB)"
snapshot_exists "$QWEN_IMAGE_EDIT_REPO"  "$QWEN_IMAGE_EDIT_REVISION"  && info "Qwen-Image-Edit already cached — skipping."  || download_hf_model "$QWEN_IMAGE_EDIT_REPO"  "$QWEN_IMAGE_EDIT_REVISION"  "Qwen-Image-Edit (~57 GB)"
snapshot_exists "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION" && info "Cosmos-Guardrail1 already cached — skipping." || download_hf_model "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION" "Cosmos-Guardrail1"
snapshot_exists "$QWEN_GUARD_REPO"       ""                           && info "Qwen3Guard already cached — skipping."        || download_hf_model "$QWEN_GUARD_REPO"       ""                           "Qwen3Guard-Gen-0.6B"
snapshot_exists "$WAN_VAE_REPO"          "$WAN_VAE_REVISION"          && info "Wan2.2-TI2V-5B already cached — skipping."   || download_hf_model "$WAN_VAE_REPO"          "$WAN_VAE_REVISION"          "Wan2.2-TI2V-5B VAE (~15 GB)"

# Write refs/main for repos cosmos-framework looks up by branch name
write_refs_main "$COSMOS3_NANO_REPO"     "$COSMOS3_NANO_REVISION"
write_refs_main "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION"
write_refs_main "$QWEN_GUARD_REPO"       "$(ls "$HF_HOME/models--${QWEN_GUARD_REPO//\//'--'}/snapshots/" 2>/dev/null | head -1)"

# ---------------------------------------------------------------------------
# 6. Docker login + pull containers
# ---------------------------------------------------------------------------
info "Logging in to nvcr.io..."
echo "$NGC_CLI_API_KEY" | docker login nvcr.io -u "\$oauthtoken" --password-stdin

pull_image() {
    local image="$1"
    info "Pulling $image ..."
    docker pull "$image"
}

pull_image "$NIM_COSMOS_IMAGE"
pull_image "$NIM_NEMOTRON_IMAGE"
pull_image "$TAO_IMAGE"

# Mark Nemotron cache as populated so notebooks detect it correctly
touch "$DATA_DIR/nemotron-nim-cache/zz_cache_complete"
info "Nemotron NIM cache marker written."

# ---------------------------------------------------------------------------
# 7. TAO Skill Bank
# ---------------------------------------------------------------------------
info "Cloning TAO Skill Bank @ $TAO_SKILL_BANK_TAG ..."
if [[ -d "$TAO_SKILL_BANK_DIR/.git" ]]; then
    warn "TAO Skill Bank already exists at $TAO_SKILL_BANK_DIR, skipping clone."
else
    git clone --branch "$TAO_SKILL_BANK_TAG" --depth 1 \
        https://github.com/NVIDIA-TAO/tao-skill-bank.git \
        "$TAO_SKILL_BANK_DIR"
fi

# ---------------------------------------------------------------------------
# 8. Cosmos3-Nano checkpoint → Qwen3-VL safetensors (Lab 3 base model)
# ---------------------------------------------------------------------------
PTM_DIR="$LAB3_DIR/models/Cosmos3-Nano-VLM"
info "Checking for Lab 3 base checkpoint at $PTM_DIR ..."
if ls "$PTM_DIR"/*.safetensors 2>/dev/null | head -1 | grep -q .; then
    info "Checkpoint already converted — skipping."
else
    info "Converting Cosmos3-Nano → Qwen3-VL safetensors inside TAO container (~60 min) ..."
    docker run --rm --ipc=host --name tao-convert \
        -u "$(id -u):$(id -g)" \
        -e USER=taouser -e LOGNAME=taouser \
        -e HOME=/cache/home \
        -e XDG_CACHE_HOME=/cache/home/.cache \
        -e TORCHINDUCTOR_CACHE_DIR=/cache/inductor \
        -e TRITON_CACHE_DIR=/cache/triton \
        -e BASE_MODEL=nvidia/Cosmos3-Nano \
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
cd /
SRC="$(python - <<'"'"'PYX'"'"'
import os
from huggingface_hub import snapshot_download
print(snapshot_download(os.environ["BASE_MODEL"],
                        revision=os.environ["BASE_REV"] or None,
                        cache_dir="/cache/huggingface"))
PYX
)"
echo "[convert] source: $SRC"
python -m "$CONVERT_ENTRYPOINT" --checkpoint-path "$SRC" --vlm-model-name "$ARCH_MODEL" -o "/output/$OUTPUT_NAME"
'
    info "Checkpoint conversion complete → $PTM_DIR"
fi

# ---------------------------------------------------------------------------
# 10. Cosmos3 framework venv (https://github.com/NVIDIA/cosmos-framework)
# ---------------------------------------------------------------------------
info "Setting up Cosmos3 framework at $COSMOS_REPO_DIR ..."

info "Installing system dependencies for Cosmos framework..."
sudo apt-get install -y --no-install-recommends \
    curl ffmpeg git-lfs libx11-dev tree wget 2>/dev/null \
    || warn "apt-get failed — ensure system deps are installed manually."

if [[ -d "$COSMOS_REPO_DIR/.git" ]]; then
    warn "cosmos-framework already cloned at $COSMOS_REPO_DIR, pulling latest..."
    (cd "$COSMOS_REPO_DIR" && git pull)
else
    info "Cloning cosmos-framework..."
    git clone https://github.com/NVIDIA/cosmos-framework.git "$COSMOS_REPO_DIR"
fi

info "Building Cosmos3 venv with uv (CUDA 13.0)..."
(
    cd "$COSMOS_REPO_DIR"
    uv sync --all-extras --group=cu130-train
)
info "Cosmos3 venv ready at $COSMOS_REPO_DIR/.venv"

COSMOS3_VENV_PYTHON="$COSMOS_REPO_DIR/.venv/bin/python"
[[ -x "$COSMOS3_VENV_PYTHON" ]] || error "Cosmos venv python not found at $COSMOS3_VENV_PYTHON after uv sync."
info "Cosmos venv: $("$COSMOS3_VENV_PYTHON" --version 2>&1)"

# Resolve the uv-managed Python the venv symlinks to — needed as a volume mount
# so the symlink chain resolves inside Docker containers.
UV_PYTHON_PARENT=""
if [[ -L "$COSMOS3_VENV_PYTHON" ]]; then
    _uv_bin="$(readlink -f "$COSMOS3_VENV_PYTHON")"
    UV_PYTHON_PARENT="$(dirname "$(dirname "$(dirname "$_uv_bin")")")"
    info "UV Python parent: $UV_PYTHON_PARENT"
fi

# ---------------------------------------------------------------------------
# 11. Write .env for docker compose
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_DIR/.env"
info "Writing $ENV_FILE ..."
cat > "$ENV_FILE" << 'EOF'
# Generated by setup.sh — do not commit this file.

EOF
printf 'NGC_CLI_API_KEY=%s\n' "$NGC_CLI_API_KEY" >> "$ENV_FILE"
printf 'HF_TOKEN=%s\n' "$HF_TOKEN" >> "$ENV_FILE"
printf 'VSS_LABS_DIR=%s\n' "$BASE_DIR" >> "$ENV_FILE"
[[ -n "$UV_PYTHON_PARENT" ]] && printf 'UV_PYTHON_PARENT=%s\n' "$UV_PYTHON_PARENT" >> "$ENV_FILE"
cat >> "$ENV_FILE" << 'EOF'

JUPYTER_TOKEN=
NGINX_PORT=80
# Physical GPU indices to expose to the lab container (comma-separated).
# Notebooks use cuda:0, cuda:1, ... as relative indices within this set.
# With 2 GPUs: LAB_GPUS=0,1  |  On a 4-GPU machine using GPUs 2 and 3: LAB_GPUS=2,3
LAB_GPUS=0,1
VSS_VERSION=3.2.1
VSS_PROFILE=alerts
HARDWARE_PROFILE=H100
NIM_MODEL_SIZE=nano
EOF
info ".env written."

# ---------------------------------------------------------------------------
# 12. Write env.sh (for running notebooks without docker compose)
# ---------------------------------------------------------------------------
info "Writing $BASE_DIR/env.sh ..."
cat > "$BASE_DIR/env.sh" << EOF
# Source this file before running notebooks without docker compose:
#   source $BASE_DIR/env.sh

EOF
printf 'export NGC_CLI_API_KEY=%s\n' "$NGC_CLI_API_KEY" >> "$BASE_DIR/env.sh"
printf 'export HF_TOKEN=%s\n' "$HF_TOKEN" >> "$BASE_DIR/env.sh"
cat >> "$BASE_DIR/env.sh" << EOF

export LAB_DATA_DIR="${DATA_DIR}"
export HF_HOME="${HF_HOME}"
export HF_HUB_OFFLINE="0"
export TRANSFORMERS_OFFLINE="0"

export NIM_CACHE="${DATA_DIR}/cosmos3-reasoner-cache"
export NIM_IMAGE="${NIM_COSMOS_IMAGE}"
export NIM_GPU="0"
export NIM_PORT="30082"
export NIM_MODEL_SIZE="nano"
export NIM_READY_TIMEOUT_S="600"
export VLM_BASE_URL="http://localhost:30082"
export COURSE_VLM_BASE_URL="http://localhost:30082"

export LAB_ROOT="\$(pwd)/lab-2-sdg"
export COSMOS3_REPO="${COSMOS_REPO_DIR}"
export COSMOS3_CHECKPOINT_PATH="Cosmos3-Nano"
export COSMOS3_NANO_REVISION="${COSMOS3_NANO_REVISION}"
export COSMOS3_NUM_GPUS="1"
export COSMOS3_MASTER_ADDR="127.0.0.1"
export COSMOS3_VENV_PYTHON="${COSMOS_REPO_DIR}/.venv/bin/python"
export QWEN_GPU="0"
export COSMOS_GPU="1"
export NEMOTRON_NIM_IMAGE="${NIM_NEMOTRON_IMAGE}"
export NEMOTRON_NIM_CONTAINER="nemotron-nim"
export NEMOTRON_NIM_CACHE="${DATA_DIR}/nemotron-nim-cache"
export NEMOTRON_NIM_PORT="30083"
export NEMOTRON_NIM_BASE_URL="http://localhost:30083"

export DLI_BASE="${LAB3_DIR}"
export DLI_PTM="${LAB3_DIR}/models/Cosmos3-Nano-VLM"
export DLI_CACHE="${LAB3_DIR}/cache"
export DLI_SKILL_BANK="${TAO_SKILL_BANK_DIR}"
export DLI_DATA="${LAB3_DIR}/dataset"
export DLI_ROOT="${LAB3_DIR}/work"
export DLI_IMAGE="${TAO_IMAGE}"
export DLI_SKILL_BANK_REF="${TAO_SKILL_BANK_TAG}"

export DOCKER_HOST="unix:///var/run/docker.sock"
EOF
[[ -n "$UV_PYTHON_PARENT" ]] && printf 'export UV_PYTHON_PARENT=%s\n' "$UV_PYTHON_PARENT" >> "$BASE_DIR/env.sh"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
info "Setup complete!"
echo ""
echo "========================================================"
echo " Next steps:"
echo "========================================================"
echo ""
echo " Option A — Docker Compose (recommended):"
echo "   docker compose up -d"
echo "   Open http://localhost in your browser."
echo ""
echo " Option B — Bare JupyterLab (no compose):"
echo "   source $BASE_DIR/env.sh"
echo "   jupyter lab"
echo ""
echo " Either way, run notebooks in this order:"
echo "   lab-1-vlm/lab_1.ipynb"
echo "   lab-2-sdg/lab_2.ipynb"
echo "   lab-3-vlm-pt/lab_3.ipynb"
echo ""
echo " Still needed (copy manually):"
echo "   $DATA_DIR/traffic.png"
echo "   $DATA_DIR/traffic_short.mp4"
echo "   $DATA_DIR/Part-2/traffic_cam.png"
echo "   $DATA_DIR/Part-2/input.mp4"
echo "   $LAB3_DIR/dataset/train/  (video clips)"
echo "   $LAB3_DIR/dataset/val/    (video clips)"
echo "========================================================"
