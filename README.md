# VSS Foundations on Brev

Hands-on notebooks for VSS deployment, object tracking, video captioning, and
semantic video search. Open **[notebooks/00_Home.ipynb](notebooks/00_Home.ipynb)**
in Brev JupyterLab, then work through notebooks 00–04 and the assessment.

## Layout

- `notebooks/` — all notebooks, shared Python helpers, diagrams, and search manifest.
- `notebooks/data/` — uploaded videos, indexing status, registry, and event labels.
- `notebooks/work/` — student work and assessment answers.
- `deployment/` — container build, entrypoint, video preparation, and startup safeguards.
- `assessment/grader/` — notebook Submit endpoint and its grading rules.
- `tests/`, `assessment/tests/` — regression checks and fixtures.
- `setup.sh` — deploys VSS and configures the course services.
- `setup-brev-jupyter.sh` — configures host Jupyter dependencies and shared data.

Open notebooks directly from `notebooks/`; no legacy shortcut directories are needed.

## Instance requirements

The current deployment defaults target two RTX Pro 6000 GPUs with 96 GB each and
at least 500 GB disk. VSS 3.2.1 runs on the host Docker daemon: RT-CV, RT-VLM,
RT-Embed, VIOS, Kafka, Elasticsearch, Logstash, Behavior Analytics, and the LLM NIM.
The course adds an optional container JupyterLab, video preparation, a Python
assessment service, and nginx. The DLI LMS/Go assessment runner is not used.

## Before setup

Upload local assets; no S3 bucket or AWS credentials are required:

```text
/home/ubuntu/course-assets/videos/
├── traffic_short.mp4
└── search-videos/
    ├── normal_eval.mp4
    ├── rain_eval.mp4
    ├── fog_eval.mp4
    └── night_eval.mp4
```

Save `NGC_API_KEY=...` in `.env-dev` (ignored by Git). Then run:

```bash
sudo bash setup.sh
```

For a different upload directory:

```bash
sudo BREV_ASSETS_DIR=/path/to/assets bash setup.sh
```

Setup stages the inputs into `notebooks/data`, starts preparation, and configures
Brev's host JupyterLab. For an already-deployed stack, run only:

```bash
sudo bash setup-brev-jupyter.sh
```

This script defaults to `/home/ubuntu/.venv/bin/python` and
`/home/ubuntu/.local/bin/uv`; override `BREV_PYTHON` and `BREV_UV` if needed.

## Using the notebooks

Brev kernels use the proxy address in `/etc/vss-course-host.json`; container
kernels use Docker networking. Import `BASE`, `TASK_ROOT`, and `DATA_DIR` from
`course_runtime` rather than hard-coding addresses or directories.

The data folder is shared with the services. Replacing a local MP4 does not
replace its existing VIOS registration or Elasticsearch records: ingest and index
the new version explicitly. `search_sources.json` records the registered videos.
Section 4.1 checks preparation before search.

`data/ground_truth/search_videos.json` contains manually verified event timestamps
for evaluation. Empty event lists in the temporary sample mean **not annotated**;
they must not be used as evidence that there are no incidents.

RT-VLM and RT-Embed startup gates wait for Kafka before launching their models.
After restarting RT-VLM, register clips again and regenerate their captions.
A healthy HTTP endpoint alone does not prove Kafka publishing is working.

## Development checks

```bash
COURSE_BASE_URL=http://nginx PYTHONPATH=notebooks python -m pytest tests
PYTHONPATH=notebooks:assessment python -m pytest assessment/tests
```

The optional container retains internal `/dli` mount paths for compatibility;
student-facing notebook paths are rooted in `notebooks/`.
