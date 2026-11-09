#!/usr/bin/env bash
# Brev setup script for c-fx-17-v2 Part 1 (task1).
# Runs as root at instance creation via the Brev hook.
#
# Required Brev environment variables:
#   NGC_API_KEY       NGC Personal API Key (Catalog scope minimum)
#   BREV_ASSETS_DIR   Path to pre-staged course assets (default: /home/ubuntu/course-assets)
#                     Layout: videos/traffic_short.mp4
#                             videos/search-videos/{normal,rain,fog,night}_eval.mp4
#                             course-docs/   (built Sphinx site)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASK1="$REPO_DIR/task1"
ASSETS_DIR="${BREV_ASSETS_DIR:-/home/ubuntu/course-assets}"

# ---------------------------------------------------------------------------
# 1. Install docker-compose v1 shim (the repo's save_workspace.sh requires it)
# ---------------------------------------------------------------------------
if ! command -v docker-compose &>/dev/null; then
    echo "[setup] Installing docker-compose v1 shim..."
    curl -fsSL \
        "https://github.com/docker/compose/releases/download/v1.29.2/docker-compose-$(uname -s)-$(uname -m)" \
        -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    echo "[setup] docker-compose $(docker-compose --version)"
else
    echo "[setup] docker-compose already installed: $(docker-compose --version)"
fi

# ---------------------------------------------------------------------------
# 2. Write the NGC key into .env-dev (gitignored; consumed only by vss-deploy)
# ---------------------------------------------------------------------------
if [[ -f "$TASK1/.env-dev" ]]; then
    echo "[setup] Using existing task1/.env-dev"
elif [[ -n "${NGC_API_KEY:-}" ]]; then
    printf 'NGC_API_KEY=%s\n' "$NGC_API_KEY" > "$TASK1/.env-dev"
    echo "[setup] Wrote NGC_API_KEY to task1/.env-dev"
else
    echo "[setup] ERROR: NGC_API_KEY is not set and task1/.env-dev does not exist." >&2
    echo "[setup] Either:" >&2
    echo "[setup]   export NGC_API_KEY=nvapi-... && bash setup.sh" >&2
    echo "[setup]   echo 'NGC_API_KEY=nvapi-...' > $TASK1/.env-dev && bash setup.sh" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. Verify pre-staged assets exist before spending 60+ min on a deploy
# ---------------------------------------------------------------------------
REQUIRED_VIDEOS=(
    "$ASSETS_DIR/videos/traffic_short.mp4"
    "$ASSETS_DIR/videos/search-videos/normal_eval.mp4"
    "$ASSETS_DIR/videos/search-videos/rain_eval.mp4"
    "$ASSETS_DIR/videos/search-videos/fog_eval.mp4"
    "$ASSETS_DIR/videos/search-videos/night_eval.mp4"
)
for f in "${REQUIRED_VIDEOS[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "[setup] ERROR: Missing required asset: $f" >&2
        echo "[setup] Upload course assets to $ASSETS_DIR before running setup." >&2
        exit 1
    fi
done
if [[ ! -d "$ASSETS_DIR/course-docs" ]]; then
    echo "[setup] WARNING: No course-docs at $ASSETS_DIR/course-docs -- a stub will be written (VSS Coding Agent Skills step will not work until the real site is deployed)."
fi
echo "[setup] Required video assets present in $ASSETS_DIR"

# ---------------------------------------------------------------------------
# 4. Build images
# ---------------------------------------------------------------------------
cd "$TASK1"
echo "[setup] Building images (this takes a few minutes)..."
docker-compose \
    -f docker-compose.yml \
    -f docker-compose.override.yml \
    -f docker-compose.brev.yml \
    build

# ---------------------------------------------------------------------------
# 5. Launch the stack in the background
#    Cold NGC pull: RT-VLM (~33 GB) + RT-Embed + RT-CV + Nemotron + images (~93 GB)
#    Expect 60-90 min before the onboarding readiness check says READY.
# ---------------------------------------------------------------------------
echo "[setup] Starting the stack (cold NGC pull: allow 60-90 min before the lab is ready)..."
BREV_ASSETS_DIR="$ASSETS_DIR" docker-compose \
    -f docker-compose.yml \
    -f docker-compose.override.yml \
    -f docker-compose.brev.yml \
    up -d

echo "[setup] Stack launched. Watch progress with:"
echo "  cd $TASK1 && docker-compose logs -f vss-deploy course-prep"
echo ""
echo "[setup] JupyterLab will be at http://$(hostname -I | awk '{print $1}')/lab once the stack is healthy."
