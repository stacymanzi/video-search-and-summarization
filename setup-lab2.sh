#!/usr/bin/env bash
# setup-lab2.sh — One-time setup for Lab 2 (Synthetic Data Generation with Cosmos3 I2V).
#
# Usage:
#   bash setup-lab2.sh [BASE_DIR]
#
# BASE_DIR defaults to $HOME/vss-labs.
# Reads HF_TOKEN from the environment or from .env in this script's directory.
#
# What this does:
#   1. Create lab2-hf-cache/ with the required HuggingFace model snapshots
#   2. Download: nvidia/Cosmos3-Nano, Qwen/Qwen-Image-Edit,
#                nvidia/Cosmos-Guardrail1, Qwen/Qwen3Guard-Gen-0.6B,
#                Wan-AI/Wan2.2-TI2V-5B (VAE only)
#   3. Clone NVIDIA/cosmos-framework and build its uv venv
#
# After completion run:
#   docker compose up lab nginx

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${1:-$HOME/vss-labs}"
DATA_DIR="$BASE_DIR/data"
LAB2_HF_CACHE="$DATA_DIR/hf_home"
COSMOS_REPO_DIR="$BASE_DIR/cosmos-framework"

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

info()  { echo -e "${GREEN}[setup-lab2]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Load credentials
# ---------------------------------------------------------------------------
ENV_FILE="$SCRIPT_DIR/.env"
if [[ -z "${HF_TOKEN:-}" ]] && [[ -f "$ENV_FILE" ]]; then
    HF_TOKEN="$(grep -E '^HF_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
    export HF_TOKEN
fi
[[ -n "${HF_TOKEN:-}" ]] || error "HF_TOKEN is not set. Export it or add it to .env."

info "Credentials loaded."

# ---------------------------------------------------------------------------
# 2. Find Python with huggingface_hub and ensure hf-xet is available
# ---------------------------------------------------------------------------
# The system python3 may not have huggingface_hub; find the venv that does.
PYTHON=""
for _py in \
    "$(which python3.13 2>/dev/null)" \
    "$(which python3.12 2>/dev/null)" \
    "$(which python3 2>/dev/null)"; do
    if [[ -x "${_py:-}" ]] && "${_py}" -c "import huggingface_hub" 2>/dev/null; then
        PYTHON="$_py"
        break
    fi
done
[[ -n "$PYTHON" ]] || error "No Python with huggingface_hub found. Install: pip install huggingface-hub"
info "Using Python: $PYTHON ($(\"$PYTHON\" --version 2>&1))"

"$PYTHON" -c "import hf_xet" 2>/dev/null || {
    info "Installing hf-xet for XetHub support..."
    "$PYTHON" -m pip install hf-xet --quiet || \
    error "Could not install hf-xet."
}

# ---------------------------------------------------------------------------
# 3. Create lab2-hf-cache directory
# ---------------------------------------------------------------------------
info "Creating Lab 2 HF cache at $LAB2_HF_CACHE ..."
mkdir -p "$LAB2_HF_CACHE"

# ---------------------------------------------------------------------------
# 4. Download HuggingFace models
# ---------------------------------------------------------------------------
download_hf_model() {
    local repo="$1" revision="$2" label="$3"
    info "Downloading $label ($repo @ ${revision:0:8}...)..."
    HF_HOME="$LAB2_HF_CACHE" "$PYTHON" -c "
from huggingface_hub import snapshot_download
import os, sys
snapshot_download(
    repo_id='$repo',
    revision='${revision}' if '${revision}' else None,
    cache_dir='$LAB2_HF_CACHE',
    token=os.environ['HF_TOKEN'],
)
print('  done.')
" || error "Failed to download $label"
}

# Check if already downloaded (snapshot dir must exist and be non-empty)
snapshot_exists() {
    local cache="$LAB2_HF_CACHE" repo="$1" rev="$2"
    local model_dir name
    name="${repo//\//'--'}"
    model_dir="$cache/hub/models--$name/snapshots/$rev"
    [[ -d "$model_dir" ]] && [[ "$(ls -A "$model_dir" 2>/dev/null)" ]]
}

if snapshot_exists "$COSMOS3_NANO_REPO" "$COSMOS3_NANO_REVISION"; then
    info "Cosmos3-Nano already cached — skipping."
else
    download_hf_model "$COSMOS3_NANO_REPO" "$COSMOS3_NANO_REVISION" "Cosmos3-Nano (~50 GB)"
fi

if snapshot_exists "$QWEN_IMAGE_EDIT_REPO" "$QWEN_IMAGE_EDIT_REVISION"; then
    info "Qwen-Image-Edit already cached — skipping."
else
    download_hf_model "$QWEN_IMAGE_EDIT_REPO" "$QWEN_IMAGE_EDIT_REVISION" "Qwen-Image-Edit (~57 GB)"
fi

if snapshot_exists "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION"; then
    info "Cosmos-Guardrail1 already cached — skipping."
else
    download_hf_model "$COSMOS_GUARDRAIL_REPO" "$COSMOS_GUARDRAIL_REVISION" "Cosmos-Guardrail1"
fi

if snapshot_exists "$QWEN_GUARD_REPO" ""; then
    info "Qwen3Guard-Gen-0.6B already cached — skipping."
else
    download_hf_model "$QWEN_GUARD_REPO" "" "Qwen3Guard-Gen-0.6B"
fi

if snapshot_exists "$WAN_VAE_REPO" "$WAN_VAE_REVISION"; then
    info "Wan2.2-TI2V-5B already cached — skipping."
else
    download_hf_model "$WAN_VAE_REPO" "$WAN_VAE_REVISION" "Wan2.2-TI2V-5B (VAE)"
fi

# ---------------------------------------------------------------------------
# 5. Clone cosmos-framework and build venv
# ---------------------------------------------------------------------------
info "Setting up cosmos-framework at $COSMOS_REPO_DIR ..."

if [[ -d "$COSMOS_REPO_DIR/.git" ]]; then
    warn "cosmos-framework already cloned at $COSMOS_REPO_DIR — pulling latest..."
    (cd "$COSMOS_REPO_DIR" && git pull)
else
    info "Cloning NVIDIA/cosmos-framework..."
    git clone https://github.com/NVIDIA/cosmos-framework.git "$COSMOS_REPO_DIR"
fi

if [[ -x "$COSMOS_REPO_DIR/.venv/bin/python" ]]; then
    info "cosmos-framework venv already built — skipping uv sync."
else
    command -v uv >/dev/null 2>&1 || error "'uv' not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    info "Building cosmos-framework venv (uv sync, CUDA 13.0)..."
    (
        cd "$COSMOS_REPO_DIR"
        uv sync --all-extras --group=cu130-train
    )
    info "Cosmos venv ready."
fi

# ---------------------------------------------------------------------------
# 6. Update docker-compose .env to use Lab 2 HF cache and remap GPUs
# ---------------------------------------------------------------------------
ENV_FILE="$SCRIPT_DIR/.env"
info "Updating $ENV_FILE for Lab 2..."

update_or_add() {
    local key="$1" val="$2" file="$3"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$file"
    else
        echo "${key}=${val}" >> "$file"
    fi
}

update_or_add "HF_HOME"      "$LAB2_HF_CACHE"   "$ENV_FILE"
update_or_add "COSMOS3_REPO" "$COSMOS_REPO_DIR" "$ENV_FILE"

# Resolve the uv-managed Python the cosmos-framework venv symlinks to, so the
# lab container can mount it. The symlink target is the parent bin/ directory's
# parent (i.e. the cpython-x.y.z-... install root).
_venv_python="$COSMOS_REPO_DIR/.venv/bin/python"
if [[ -L "$_venv_python" ]]; then
    _uv_python_bin="$(readlink -f "$_venv_python")"
    # Walk up to the uv python parent dir (contains cpython-x.y.z-... and version symlinks)
    _uv_python_dir="$(dirname "$(dirname "$(dirname "$_uv_python_bin")")")"
    update_or_add "UV_PYTHON_PARENT" "$_uv_python_dir" "$ENV_FILE"
    info "UV Python parent dir: $_uv_python_dir"
else
    warn "Could not resolve uv Python symlink at $_venv_python — UV_PYTHON_DIR not set."
fi
# Lab 2 needs 2 GPUs. Update LAB_GPUS to the physical indices you want to use.
# Default keeps GPUs 0 and 1; change if those are occupied on your machine.
# e.g. LAB_GPUS=2,3 if GPUs 0 and 1 are in use by another workload.
if ! grep -q "^LAB_GPUS=" "$ENV_FILE" 2>/dev/null; then
    echo "LAB_GPUS=0,1" >> "$ENV_FILE"
fi

info "Lab 2 setup complete."
info ""
info "Next steps:"
info "  1. Restart the lab container to pick up the new env vars:"
info "       docker compose restart lab"
info "  2. Open lab_2.ipynb in JupyterLab and run all cells."
