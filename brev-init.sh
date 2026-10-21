#!/bin/bash
set -euo pipefail

BASE_DIR="$HOME/vss-labs"
DATA_DIR="$BASE_DIR/data"
COSMOS_REPO_DIR="$BASE_DIR/cosmos-framework"
TAO_SKILL_BANK_DIR="$DATA_DIR/lab3/tao-skill-bank"
COURSE_DIR="$HOME/vss-smart-cities-course"

echo "[init] System packages..."
apt-get update -qq
apt-get install -y -q git git-lfs curl ffmpeg libx11-6 libxcb1 libxext6 libgl1 libglib2.0-0 libsm6 libxrender1 tree wget socat software-properties-common
git lfs install --skip-smudge

echo "[init] uv + Python 3.13..."
# cosmos-framework pins uv to ==0.11.29; install that version directly
curl -LsSf https://astral.sh/uv/0.11.29/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.13

UV="$HOME/.local/bin/uv"

echo "[init] Sysctls..."
sysctl -w vm.max_map_count=262144 || true
sysctl -w net.core.somaxconn=65535 || true
grep -q vm.max_map_count /etc/sysctl.conf 2>/dev/null || echo "vm.max_map_count=262144" >> /etc/sysctl.conf
grep -q net.core.somaxconn /etc/sysctl.conf 2>/dev/null || echo "net.core.somaxconn=65535" >> /etc/sysctl.conf

echo "[init] Directories..."
mkdir -p "$DATA_DIR/hf_home" "$DATA_DIR/lab2-hf-cache" "$DATA_DIR/cosmos3-reasoner-cache" \
         "$DATA_DIR/nemotron-nim-cache" "$DATA_DIR/Part-2" \
         "$DATA_DIR/lab3/models" "$DATA_DIR/lab3/dataset/train" "$DATA_DIR/lab3/dataset/val" \
         "$DATA_DIR/lab3/cache" "$DATA_DIR/lab3/work"
touch "$DATA_DIR/nemotron-nim-cache/zz_cache_complete"

echo "[init] Course repo..."
if [[ ! -d "$COURSE_DIR/.git" ]]; then
    git clone -b vss-smart-cities-course https://github.com/stacymanzi/video-search-and-summarization.git "$COURSE_DIR"
fi

echo "[init] cosmos-framework..."
if [[ ! -d "$COSMOS_REPO_DIR/.git" ]]; then
    git clone https://github.com/NVIDIA/cosmos-framework.git "$COSMOS_REPO_DIR"
fi
if [[ ! -x "$COSMOS_REPO_DIR/.venv/bin/python" ]]; then
    echo "[init] Building cosmos venv (takes ~10 min)..."
    (cd "$COSMOS_REPO_DIR" && "$UV" sync --all-extras --group=cu130-train)
fi

UV_PYTHON_PARENT=""
if [[ -L "$COSMOS_REPO_DIR/.venv/bin/python" ]]; then
    _bin="$(readlink -f "$COSMOS_REPO_DIR/.venv/bin/python")"
    UV_PYTHON_PARENT="$(dirname "$(dirname "$(dirname "$_bin")")")"
fi

echo "[init] TAO Skill Bank..."
if [[ ! -d "$TAO_SKILL_BANK_DIR/.git" ]]; then
    git clone --branch 7.2.0 --depth 1 https://github.com/NVIDIA-TAO/tao-skill-bank.git "$TAO_SKILL_BANK_DIR"
fi

echo "[init] Python packages (system + Brev venv)..."
"$UV" pip install -q --system openai "diffusers>=0.33.0" transformers accelerate ipywidgets tomli-w requests-unixsocket2 "huggingface-hub>=0.23" hf-xet
# Also install into Brev's managed Jupyter venv if it exists
BREV_VENV="/home/ubuntu/.venv"
if [[ -d "$BREV_VENV" ]]; then
    "$BREV_VENV/bin/python" -m ensurepip --upgrade 2>/dev/null || true
    "$BREV_VENV/bin/pip" install -q openai "diffusers>=0.33.0" transformers accelerate ipywidgets tomli-w requests-unixsocket2 "huggingface-hub>=0.23" hf-xet || true
fi

echo "[init] socat bridge (Docker unix socket → tcp://localhost:2375)..."
pkill -f "socat TCP-LISTEN:2375" 2>/dev/null || true
nohup socat TCP-LISTEN:2375,bind=127.0.0.1,fork,reuseaddr UNIX-CLIENT:/var/run/docker.sock >/dev/null 2>&1 &
echo "[init] socat started (pid $!)"

echo "[init] Environment variables..."
ENV_BLOCK="
VSS_LABS_DIR=$BASE_DIR
LAB_DATA_DIR=$DATA_DIR
HF_HOME=$DATA_DIR/lab2-hf-cache
NIM_CACHE=$DATA_DIR/cosmos3-reasoner-cache
NIM_IMAGE=nvcr.io/nim/nvidia/cosmos3-reasoner:1.7
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
COSMOS3_NANO_REVISION=7a312c868bcce8e40b3eb40861300a9d0ba3fde1
COSMOS_GUARDRAIL_REVISION=d6d4bfa899a71454a700907664f3e88f503950cf
COSMOS3_NUM_GPUS=1
COSMOS3_MASTER_ADDR=127.0.0.1
COSMOS3_VENV_PYTHON=$COSMOS_REPO_DIR/.venv/bin/python
IMAGINAIRE_HF_CLI_ENVIRONMENT=$COSMOS_REPO_DIR/.hf-cli-venv
UV_PYTHON_PARENT=$UV_PYTHON_PARENT
QWEN_GPU=0
COSMOS_GPU=1
NEMOTRON_NIM_IMAGE=nvcr.io/nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:1.7.0-variant
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
DLI_IMAGE=nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl
DLI_SKILL_BANK_REF=7.2.0
DLI_GPU=1
LAB_GPUS=0,1
DOCKER_HOST=tcp://localhost:2375
HF_HUB_OFFLINE=0
TRANSFORMERS_OFFLINE=0"

printf '%s\n' "$ENV_BLOCK" >> /etc/environment

echo "[init] Done. Open JupyterLab and run the three notebooks in order."
