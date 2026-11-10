# VSS Foundations

Learn how to build a Visual AI Agent using NVIDIA Video Search and Summarization (VSS). This lab
walks you through the core VSS building blocks hands-on: detecting and tracking objects in video,
generating natural language captions from a vision-language model, and searching across a video
collection using semantic embeddings.

You work against a fully deployed VSS 3.2.1 stack running on your instance. Each notebook
introduces one component, shows you what it does, and gives you exercises to try it yourself.
By the end you will have searched a library of traffic videos by meaning, prompted a reasoning
model to describe events frame by frame, and built a mental model of how the pieces fit together
into a production visual AI pipeline.

## What runs

`setup.sh` deploys VSS 3.2.1 directly on the host Docker daemon, then brings up 4 course services.

**VSS stack** (started by `setup.sh` via `dev-profile.sh`, runs on host daemon):

RT-VLM (Cosmos 3 Super Reasoner FP8) · RT-CV (TrafficCamNet RT-DETR) · RT-Embed (Cosmos-Embed1) · LLM NIM (Nemotron-nano-9b) · VIOS · Kafka · Elasticsearch · Logstash · Behavior Analytics

**Course services** (started by `docker compose up`):

| Service | Role |
|---|---|
| `lab` | JupyterLab with notebooks 0-4 and the Part 1 assessment |
| `course-prep` | Waits for VSS health, ingests the four search clips, writes `search_sources.json` |
| `assessment` | Grader: Go runner on 8080, Python grader on 8081 |
| `nginx` | Reverse proxy on port 80; routes `/lab`, `/agent`, `/es`, `/vios`, etc. |

GPU layout (2x RTX Pro 6000, 96 GB each):
- GPU 0: RT-VLM (Cosmos 3 Super Reasoner FP8) + RT-CV (TrafficCamNet RT-DETR) + NVStreamer
- GPU 1: RT-Embed (Cosmos-Embed1) + LLM NIM (Nemotron-nano-9b)

## Hardware

- 2x NVIDIA RTX Pro 6000 (96 GB VRAM each)
- 500 GB disk (cold NGC pull on first deploy: ~93 GB images + ~60 GB model weights)

## Pre-stage assets

Upload these to the Brev instance at `/home/ubuntu/course-assets/` before running `setup.sh`:

```
course-assets/
  videos/
    traffic_short.mp4
    search-videos/
      normal_eval.mp4
      rain_eval.mp4
      fog_eval.mp4
      night_eval.mp4
```

## Launch

**Brev startup script** (no NGC key needed at this step):

```bash
git clone -b vss-foundations https://github.com/stacymanzi/video-search-and-summarization.git /home/ubuntu/vss-foundations
```

**After the instance is up**, SSH in and run:

```bash
echo 'NGC_API_KEY=nvapi-...' > /home/ubuntu/vss-foundations/.env-dev
bash /home/ubuntu/vss-foundations/setup.sh
```

`setup.sh` configures the host Docker daemon for cgroupfs, deploys VSS 3.2.1 directly on the
host (no Docker-in-Docker), copies the pre-staged videos, and brings up the 4 course services.
Cold first deploy takes 60-90 minutes (NGC image pull ~93 GB + model weights + TensorRT engine
build). After that, snapshot the Brev volume to reuse across instances.

Watch progress:

```bash
# VSS deploy log (written by setup.sh)
cat /opt/vss/_course-runtime/seed.log

# Course-prep progress (ingesting search clips)
docker compose -f /home/ubuntu/vss-foundations/docker-compose.yml \
               -f /home/ubuntu/vss-foundations/docker-compose.override.yml \
               logs -f course-prep
```

JupyterLab opens at `http://<instance-ip>/lab` once nginx is healthy. The onboarding notebook
(`part1/00-onboarding.ipynb`) shows a readiness check that waits for VSS to be ready.

## Notebooks

| Notebook | Topic |
|---|---|
| `lab/task/part1/00-onboarding.ipynb` | Readiness check and environment orientation |
| `lab/task/part1/01-inspect-vss-deployment.ipynb` | Inspect the running VSS containers and services |
| `lab/task/part1/02-rt-cv-detection-and-tracking.ipynb` | RT-CV: TrafficCamNet detection and tracklets |
| `lab/task/part1/03-rt-vlm-captioning-and-prompting.ipynb` | RT-VLM: Cosmos 3 Super Reasoner captions |
| `lab/task/part1/04-rt-embed-video-search.ipynb` | RT-Embed: Cosmos-Embed1 video search |
| `lab/task/part1/assessment.ipynb` | Part 1 graded assessment |

## Notes

- **Never** run `docker compose down -v` on the host. The VSS stack creates its own named volumes
  (model caches, TensorRT engines). `setup.sh` patches `dev-profile.sh` to preserve these on
  restart; `-v` bypasses that and destroys ~150 GB of downloaded data.
- To restart VSS without re-downloading everything: `VSS_FORCE_REDEPLOY=true bash setup.sh`.
- The Course Docs tile serves a stub page until the external docs site is configured.
- `BREV_ASSETS_DIR` defaults to `/home/ubuntu/course-assets`. Set it as an env var to use a
  different path.
