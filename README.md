# VSS Foundations: Part 1 DLI Lab

Part 1 of the NVIDIA DLI course "Build and Evaluate a Visual AI Agent". Learners use the VSS 3.2.1
building blocks one at a time: RT-CV (detection and tracking), RT-VLM (captioning and prompting),
and RT-Embed (video search). The stack is pre-deployed at launch; learners never deploy.

## What runs

| Service | Role |
|---|---|
| `dind` | Privileged Docker daemon running the full inner VSS stack |
| `vss-deploy` | One-shot: NGC login, pulls images, downloads models, deploys VSS 3.2.1 search profile |
| `lab` | JupyterLab with notebooks 0-4 and the Part 1 assessment |
| `course-prep` | Waits for VSS health, ingests the four search clips, writes `search_sources.json` |
| `assessment` | Grader: Go runner on 8080, Python grader on 8081 |
| `nginx` | Reverse proxy on port 80 with the course bar |
| `s3_data_loader` | Local copy of pre-staged videos into the course data volume |

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

`setup.sh` installs the `docker-compose` v1 shim, verifies the video assets, builds the images,
and launches the stack. Cold first deploy takes 60-90 minutes (NGC image pull + model weight
download + TensorRT engine build). After that, snapshot the Brev volume to reuse across instances.

Watch progress:

```bash
cd /home/ubuntu/vss-foundations
docker-compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.brev.yml \
  logs -f vss-deploy course-prep
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

- **Never** run `docker-compose down -v`. The `-v` flag destroys the `dind_storage` (~93 GB) and
  `vss_models` (~60 GB) volumes. Use `docker-compose down` only.
- The Course Docs tile serves a stub page until the external docs site is configured.
- `BREV_ASSETS_DIR` defaults to `/home/ubuntu/course-assets`. Set it as an env var to use a
  different path.
