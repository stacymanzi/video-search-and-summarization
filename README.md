# VSS Smart Cities Launchable

**Build High-Accuracy Vision AI Agents for Anomaly Detection Using Synthetic Data and Fine-Tuning**

An end-to-end course on building a vision AI agent for smart-city traffic anomaly detection with NVIDIA Cosmos, the NVIDIA Blueprint for Video Search and Summarization (VSS), NVIDIA Cosmos Data Factory(CDF), and NVIDIA TAO Toolkit.

## Repository Layout

| Path | Contents |
|---|---|
| `docs/` | The course walkthrough site for Part2 and Introduction |
| `lab-1-vlm/` | Notebook 1.1 — Vision Language Models |
| `lab-2-sdg/` | Notebook 1.2 — Augmenting and Generating Datasets |
| `lab-3-vlm-pt/` | Notebook 1.3 — Fine-Tuning Vision Language Models |

## Course Structure

**Part 1 — Cosmos Basics** runs from the three lab notebooks above.

**Part 2 — Application** is four agent-driven walkthroughs, documented in `docs/course/`:

| Page | Topic |
|---|---|
| Part 2.1 | Deploy zero-shot VSS for alert verification |
| Part 2.2 | Augment the dataset with CDF (EVG and VDA) |
| Part 2.3 | Fine-tune for alert verification with TAO |
| Part 2.4 | Deploy fine-tuned VSS and compare results |

## Self-Hosted Setup

To run the labs on a fresh machine outside the NVIDIA DLI environment:

**Prerequisites**

- Linux x86_64, NVIDIA driver r580+ (CUDA 13)
- At least 2× H100 or 2× RTX Pro 6000 (≥60 GB VRAM each); 4× RTX Pro 6000 preferred
- ≥400 GB free disk, Docker + NVIDIA Container Toolkit
- `python3.13`, `uv`, `git`, `git-lfs`
- System packages: `curl ffmpeg libx11-dev tree wget`

**Steps**

```bash
# 1. Clone this repo
git clone https://github.com/stacymanzi/vss-smart-cities-launchable.git
cd vss-smart-cities-launchable

# 2. Set credentials
export NGC_CLI_API_KEY=<your-ngc-key>   # https://ngc.nvidia.com/setup/api-key
export HF_TOKEN=<your-hf-token>          # https://huggingface.co/settings/tokens

# 3. Run the one-time setup script (~1–2 hours, downloads models + containers)
bash setup.sh ~/vss-labs

# 4. Source env vars in every new terminal before opening notebooks
source ~/vss-labs/env.sh

# 5. Launch JupyterLab and run the notebooks in order:
#    lab-1-vlm/lab_1.ipynb → lab-2-sdg/lab_2.ipynb → lab-3-vlm-pt/lab_3.ipynb
```

The setup script (`setup.sh`) clones the [Cosmos Framework](https://github.com/NVIDIA/cosmos-framework),
downloads all HuggingFace model weights, pulls the required `nvcr.io` containers,
clones the TAO Skill Bank, and generates an `env.sh` that remaps all paths from
the DLI environment to your local machine.

**What still requires manual placement after setup:**

| Asset | Destination |
|---|---|
| Traffic camera image (`traffic.png`) | `~/vss-labs/data/traffic.png` |
| Short traffic clip (`traffic_short.mp4`) | `~/vss-labs/data/traffic_short.mp4` |
| Part 2 seed image + video | `~/vss-labs/data/Part-2/traffic_cam.png`, `input.mp4` |
| Lab 3 train/val video dataset | `~/vss-labs/data/lab3/dataset/train/` and `val/` |
| Lab 3 base checkpoint (optional) | `~/vss-labs/data/lab3/models/Cosmos3-Nano-VLM/` |

> For Lab 3, set `BUILD_PTM=True` in `lab_3.ipynb` to convert the base
> checkpoint from HuggingFace automatically instead of placing it manually.

## Build the Documentation

The site uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync
uv run sphinx-build -b html docs docs/_build/html
uv run python -m http.server 8000 -d docs/_build/html/
```

Then open `http://localhost:8000`.

One-command alternative that builds, serves, and opens a browser:

```bash
uv run python scripts/preview_learning_site.py
```

## Editions

The site builds in multiple editions from one source:

- `nvlearning_mode`: `course`, `lab`, or `hub`
- `nvlearning_audience`: `public` or `internal`

Facilitator material — shot lists, instructor cues, and the Part 2.3 time
budget — is gated to the `internal` audience and does **not** appear in the
default build. To produce the internal edition:

```bash
uv run sphinx-build -b html -D nvlearning_audience=internal docs docs/_build/internal
```

Add `-W --keep-going` to fail the build on warnings for release-quality output.

## Authoring Notes

- Course pages live in `docs/course/` and are listed in the `docs/index.md`
  toctree in curriculum order.
- Gate facilitator content with `:::{only} internal`. Do **not** put a section
  heading inside an `{only}` block — headings leak into non-matching builds.
  Use a bold label instead.
