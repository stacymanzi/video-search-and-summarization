#!/usr/bin/env bash
# setup-lab3.sh — One-time setup for Lab 3 (VLM Post-Training with LoRA).
#
# Usage:
#   bash setup-lab3.sh [BASE_DIR]
#
# BASE_DIR defaults to $HOME/vss-labs.
# Reads NGC_CLI_API_KEY and HF_TOKEN from the environment or from .env in this
# script's directory. Provide them before running if not already set:
#   export NGC_CLI_API_KEY=...
#   export HF_TOKEN=...
#
# What this does:
#   1. Pull TAO Cosmos-RL image (~41 GB)
#   2. Clone TAO Skill Bank @ 7.2.0
#   3. Download nvidia/Cosmos3-Nano from HuggingFace (~50 GB)
#   4. Convert the checkpoint to Qwen3-VL safetensors format (~17 GB output)
#
# After completion run:
#   docker compose up lab nginx
#   Open http://<server-ip> and run lab-3-vlm-pt/lab_3.ipynb

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${1:-$HOME/vss-labs}"
LAB3_DIR="$BASE_DIR/data/lab3"
HF_HOME="$BASE_DIR/data/hf_home"
SKILL_BANK_DIR="$LAB3_DIR/tao-skill-bank"
PTM_DIR="$LAB3_DIR/models/Cosmos3-Nano-VLM"
TAO_IMAGE="nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl"
COSMOS3_NANO_REPO="nvidia/Cosmos3-Nano"
COSMOS3_NANO_REVISION="7a312c868bcce8e40b3eb40861300a9d0ba3fde1"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[setup-lab3]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Load credentials from .env if not already in the environment
# ---------------------------------------------------------------------------
ENV_FILE="$SCRIPT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        key="${key// /}"
        [[ -z "${!key:-}" ]] && export "$key=$val"
    done < "$ENV_FILE"
fi

[[ -n "${NGC_CLI_API_KEY:-}" ]] || error "NGC_CLI_API_KEY not set. Add it to .env or export it."
[[ -n "${HF_TOKEN:-}" ]]        || error "HF_TOKEN not set. Add it to .env or export it."

# ---------------------------------------------------------------------------
# 1. Directory structure
# ---------------------------------------------------------------------------
info "Creating directories under $LAB3_DIR ..."
mkdir -p \
    "$PTM_DIR" \
    "$LAB3_DIR/cache" \
    "$LAB3_DIR/work" \
    "$LAB3_DIR/dataset/train/videos" \
    "$LAB3_DIR/dataset/val/videos" \
    "$HF_HOME"

# ---------------------------------------------------------------------------
# 2. Docker login + pull TAO Cosmos-RL image
# ---------------------------------------------------------------------------
info "Logging in to nvcr.io ..."
echo "$NGC_CLI_API_KEY" | docker login nvcr.io -u "\$oauthtoken" --password-stdin

info "Pulling TAO Cosmos-RL image (~41 GB) ..."
docker pull "$TAO_IMAGE"
info "TAO image pulled."

# ---------------------------------------------------------------------------
# 3. TAO Skill Bank
# ---------------------------------------------------------------------------
info "Cloning TAO Skill Bank @ 7.2.0 ..."
if [[ -d "$SKILL_BANK_DIR/.git" ]]; then
    warn "Skill Bank already cloned at $SKILL_BANK_DIR — skipping."
else
    git clone --branch 7.2.0 --depth 1 \
        https://github.com/NVIDIA-TAO/tao-skill-bank.git "$SKILL_BANK_DIR"
fi

# ---------------------------------------------------------------------------
# 4 + 5. Download Cosmos3-Nano and convert to Qwen3-VL safetensors.
#
# nvidia/Cosmos3-Nano uses HuggingFace XetHub storage. The host-side xet
# client may not be configured, so the download runs INSIDE the TAO container
# whose Python environment has xet properly set up. The same container then
# runs the conversion — one docker run for both steps.
# ---------------------------------------------------------------------------
CACHE_DIR="$LAB3_DIR/ptm_cache"
PTM_PARENT="$LAB3_DIR/models"
PTM_NAME="$(basename "$PTM_DIR")"

info "Creating conversion cache at $CACHE_DIR ..."
mkdir -p "$CACHE_DIR/huggingface" "$CACHE_DIR/home/.cache" "$CACHE_DIR/inductor" "$CACHE_DIR/triton"

info "Checking if checkpoint already converted ..."
if ls "$PTM_DIR"/*.safetensors 2>/dev/null | head -1 | grep -q .; then
    info "Checkpoint already present at $PTM_DIR — skipping download+conversion."
else
    info "Downloading nvidia/Cosmos3-Nano and converting to Qwen3-VL safetensors inside TAO container (~60 min) ..."
    docker run --rm --ipc=host --name tao-convert \
        -u "$(id -u):$(id -g)" \
        -e USER=taouser -e LOGNAME=taouser \
        -e HOME=/cache/home \
        -e XDG_CACHE_HOME=/cache/home/.cache \
        -e TORCHINDUCTOR_CACHE_DIR=/cache/inductor \
        -e TRITON_CACHE_DIR=/cache/triton \
        -e BASE_MODEL=nvidia/Cosmos3-Nano \
        -e BASE_REV="$COSMOS3_NANO_REVISION" \
        -e OUTPUT_NAME="$PTM_NAME" \
        -e CONVERT_ENTRYPOINT=cosmos_rl.model_preparation.vlm_safetensors \
        -e ARCH_MODEL=Qwen/Qwen3-VL-8B-Instruct \
        -e PYTHONUNBUFFERED=1 \
        -e HF_TOKEN="$HF_TOKEN" \
        -v "$PTM_PARENT:/output" \
        -v "$CACHE_DIR:/cache" \
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
echo "[convert] source checkpoint: $SRC"
python -m "$CONVERT_ENTRYPOINT" --checkpoint-path "$SRC" --vlm-model-name "$ARCH_MODEL" -o "/output/$OUTPUT_NAME"
'
    info "Checkpoint conversion complete. Output at $PTM_DIR"
fi

# ---------------------------------------------------------------------------
# 6. Sysctls
# ---------------------------------------------------------------------------
sudo sysctl -w vm.max_map_count=262144 || warn "Could not set vm.max_map_count"
sudo sysctl -w net.core.somaxconn=65535 || warn "Could not set net.core.somaxconn"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
info "Lab 3 setup complete!"
echo ""
echo "========================================================"
echo " Next steps:"
echo "========================================================"
echo ""
echo " 1. Ensure a dataset is at:"
echo "      $LAB3_DIR/dataset/"
echo "    Required layout:"
echo "      dataset_info.json"
echo "      train/videos/*.mp4  +  train/annotations_{original,augmented}.json"
echo "      val/videos/*.mp4    +  val/annotations_{original,augmented}.json"
echo ""
echo " 2. Start the lab container (if not already up):"
echo "      cd $SCRIPT_DIR"
echo "      docker compose up lab nginx -d"
echo ""
echo " 3. Open http://$(hostname -I | awk '{print $1}') and run:"
echo "      lab-3-vlm-pt/lab_3.ipynb"
echo "========================================================"
