#!/usr/bin/env bash
# brev-init.sh — One-time VM setup for the VSS Smart Cities Course on Brev.
#
# Paste this into the Brev "Setup Script" field (VM Mode).
# Runs unattended after the VM is created — no credentials needed here.
# NGC_CLI_API_KEY and HF_TOKEN are collected at the top of each lab notebook.
#
# What this does:
#   1. Install system packages + uv
#   2. Clone the course repo
#   3. Clone cosmos-framework and build its Python venv
#   4. Clone the TAO Skill Bank
#   5. Create the lab data directory structure
#   6. Write non-secret env vars to /etc/environment (picked up by Jupyter)
#
# After this completes, open JupyterLab and run the three notebooks in order.

set -euo pipefail

COURSE_REPO="https://github.com/stacymanzi/video-search-and-summarization.git"
COURSE_BRANCH="vss-smart-cities-course"
COURSE_DIR="$HOME/vss-smart-cities-course"
BASE_DIR="$HOME/vss-labs"
DATA_DIR="$BASE_DIR/data"
COSMOS_REPO_DIR="$BASE_DIR/cosmos-framework"
TAO_SKILL_BANK_DIR="$DATA_DIR/lab3/tao-skill-bank"
TAO_SKILL_BANK_TAG="7.2.0"

NIM_COSMOS_IMAGE="nvcr.io/nim/nvidia/cosmos3-reasoner:1.7"
NIM_NEMOTRON_IMAGE="nvcr.io/nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:1.7.0-variant"
TAO_IMAGE="nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl"
COSMOS3_NANO_REVISION="7a312c868bcce8e40b3eb40861300a9d0ba3fde1"
COSMOS_GUARDRAIL_REVISION="d6d4bfa899a71454a700907664f3e88f503950cf"

echo "[brev-init] === VSS Smart Cities Course — VM Setup ==="

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "[brev-init] Installing system packages..."
apt-get update -qq
apt-get install -y -q \
    git git-lfs curl ffmpeg \
    libx11-6 libxcb1 libxext6 libgl1 libglib2.0-0 libsm6 libxrender1 \
    tree wget socat

git lfs install --skip-smudge

# ---------------------------------------------------------------------------
# 2. uv (Python toolchain manager — needed for cosmos-framework venv)
# ---------------------------------------------------------------------------
if ! command -v uv &>/dev/null; then
    echo "[brev-init] Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Use the full path so sudo calls later find uv regardless of PATH stripping
UV="$HOME/.local/bin/uv"
export PATH="$HOME/.local/bin:$PATH"

# ---------------------------------------------------------------------------
# 2b. Python 3.13 (required by cosmos-framework venv)
# ---------------------------------------------------------------------------
if ! command -v python3.13 &>/dev/null; then
    echo "[brev-init] Installing Python 3.13 via deadsnakes PPA..."
    add-apt-repository ppa:deadsnakes/ppa -y
    apt-get update -qq
    apt-get install -y -q python3.13 python3.13-venv python3.13-dev
fi

# ---------------------------------------------------------------------------
# 3. Host sysctls (required by Elasticsearch / VSS in Part 2)
# ---------------------------------------------------------------------------
echo "[brev-init] Setting host sysctls..."
sysctl -w vm.max_map_count=262144  || true
sysctl -w net.core.somaxconn=65535 || true
grep -q vm.max_map_count /etc/sysctl.conf 2>/dev/null || echo "vm.max_map_count=262144" >> /etc/sysctl.conf
grep -q net.core.somaxconn /etc/sysctl.conf 2>/dev/null || echo "net.core.somaxconn=65535" >> /etc/sysctl.conf

# ---------------------------------------------------------------------------
# 4. Clone course repo
# ---------------------------------------------------------------------------
echo "[brev-init] Cloning course repo → $COURSE_DIR ..."
if [[ -d "$COURSE_DIR/.git" ]]; then
    echo "[brev-init] Already cloned — pulling."
    (cd "$COURSE_DIR" && git pull)
else
    git clone -b "$COURSE_BRANCH" "$COURSE_REPO" "$COURSE_DIR"
fi

# ---------------------------------------------------------------------------
# 5. Directory structure
# ---------------------------------------------------------------------------
echo "[brev-init] Creating data directory structure..."
mkdir -p \
    "$DATA_DIR/hf_home" \
    "$DATA_DIR/lab2-hf-cache" \
    "$DATA_DIR/cosmos3-reasoner-cache" \
    "$DATA_DIR/nemotron-nim-cache" \
    "$DATA_DIR/Part-2" \
    "$DATA_DIR/lab3/models" \
    "$DATA_DIR/lab3/dataset/train" \
    "$DATA_DIR/lab3/dataset/val" \
    "$DATA_DIR/lab3/cache" \
    "$DATA_DIR/lab3/work"

# Nemotron cache marker — notebooks check for this file before starting the NIM.
# The actual model cache is downloaded lazily when NGC credentials are available.
touch "$DATA_DIR/nemotron-nim-cache/zz_cache_complete"

# ---------------------------------------------------------------------------
# 6. Clone cosmos-framework + build venv (no credentials needed)
# ---------------------------------------------------------------------------
echo "[brev-init] Cloning cosmos-framework..."
if [[ -d "$COSMOS_REPO_DIR/.git" ]]; then
    echo "[brev-init] Already cloned — skipping."
else
    git clone https://github.com/NVIDIA/cosmos-framework.git "$COSMOS_REPO_DIR"
fi

if [[ -x "$COSMOS_REPO_DIR/.venv/bin/python" ]]; then
    echo "[brev-init] cosmos-framework venv already built — skipping."
else
    echo "[brev-init] Building cosmos-framework venv (uv sync, CUDA 13.0) — this takes ~10 min..."
    (cd "$COSMOS_REPO_DIR" && uv sync --all-extras --group=cu130-train)
fi

# Resolve the uv Python parent directory (needed so the venv symlink resolves
# when started from a different working directory or subprocess).
UV_PYTHON_PARENT=""
_venv_python="$COSMOS_REPO_DIR/.venv/bin/python"
if [[ -L "$_venv_python" ]]; then
    _uv_bin="$(readlink -f "$_venv_python")"
    UV_PYTHON_PARENT="$(dirname "$(dirname "$(dirname "$_uv_bin")")")"
    echo "[brev-init] UV Python parent: $UV_PYTHON_PARENT"
fi

# ---------------------------------------------------------------------------
# 7. TAO Skill Bank (no credentials needed)
# ---------------------------------------------------------------------------
echo "[brev-init] Cloning TAO Skill Bank @ $TAO_SKILL_BANK_TAG ..."
if [[ -d "$TAO_SKILL_BANK_DIR/.git" ]]; then
    echo "[brev-init] TAO Skill Bank already present — skipping."
else
    git clone --branch "$TAO_SKILL_BANK_TAG" --depth 1 \
        https://github.com/NVIDIA-TAO/tao-skill-bank.git \
        "$TAO_SKILL_BANK_DIR"
fi

# ---------------------------------------------------------------------------
# 8. Python packages for JupyterLab on the host
# ---------------------------------------------------------------------------
echo "[brev-init] Installing Python packages..."
# Use uv pip --system so packages land in the system Python (no venv required,
# avoids the distutils error in Python 3.13, and doesn't need a separate pip).
"$UV" pip install -q --system \
    openai "diffusers>=0.33.0" transformers accelerate \
    ipywidgets tomli-w requests-unixsocket2 \
    "huggingface-hub>=0.23" hf-xet

# ---------------------------------------------------------------------------
# 9. Non-secret env vars → /etc/environment (read by PAM; available in Jupyter)
# ---------------------------------------------------------------------------
echo "[brev-init] Writing env vars to /etc/environment..."

# Remove any previous entries written by this script
sed -i '/^# VSS Smart Cities Course/,/^# end VSS/d' /etc/environment 2>/dev/null || true

cat >> /etc/environment << EOF
# VSS Smart Cities Course
VSS_LABS_DIR=$BASE_DIR
LAB_DATA_DIR=$DATA_DIR
HF_HOME=$DATA_DIR/lab2-hf-cache
HF_HUB_OFFLINE=0
TRANSFORMERS_OFFLINE=0
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
DLI_BASE=$DATA_DIR/lab3
DLI_PTM=$DATA_DIR/lab3/models/Cosmos3-Nano-VLM
DLI_CACHE=$DATA_DIR/lab3/cache
DLI_DATA=$DATA_DIR/lab3/dataset
DLI_ROOT=$DATA_DIR/lab3/work
DLI_SKILL_BANK=$TAO_SKILL_BANK_DIR
DLI_IMAGE=$TAO_IMAGE
DLI_SKILL_BANK_REF=$TAO_SKILL_BANK_TAG
DLI_GPU=1
LAB_GPUS=0,1
DOCKER_HOST=unix:///var/run/docker.sock
# end VSS
EOF

# Also export for the current shell session
set -o allexport
source /etc/environment
set +o allexport

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "[brev-init] ============================================"
echo "[brev-init]  Setup complete!"
echo "[brev-init] ============================================"
echo "[brev-init]"
echo "[brev-init]  Course files: $COURSE_DIR"
echo "[brev-init]  Data dir:     $DATA_DIR"
echo "[brev-init]"
echo "[brev-init]  Open JupyterLab and run the notebooks:"
echo "[brev-init]    1. lab-1-vlm/lab_1.ipynb"
echo "[brev-init]    2. lab-2-sdg/lab_2.ipynb"
echo "[brev-init]    3. lab-3-vlm-pt/lab_3.ipynb"
echo "[brev-init]"
echo "[brev-init]  Each notebook will ask for your NGC API key"
echo "[brev-init]  and HuggingFace token on the first run."
echo "[brev-init] ============================================"
