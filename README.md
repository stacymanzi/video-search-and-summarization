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
git clone -b vss-smart-cities-course https://github.com/stacymanzi/video-search-and-summarization.git
cd video-search-and-summarization

# 2. Set credentials
export NGC_CLI_API_KEY=<your-ngc-key>   # https://ngc.nvidia.com/setup/api-key
export HF_TOKEN=<your-hf-token>          # https://huggingface.co/settings/tokens

# 3. Run the one-time setup script (~1–2 hours, downloads models + containers)
bash setup.sh ~/vss-labs

# 4. Copy .env.example to .env and set LAB_GPUS to your available GPU indices
cp .env.example .env

# 5. Launch the lab container
docker compose up -d

# 6. Open JupyterLab at http://localhost and run the notebooks in order:
#    lab-1-vlm/lab_1.ipynb → lab-2-sdg/lab_2.ipynb → lab-3-vlm-pt/lab_3.ipynb
```

The setup script clones the [Cosmos Framework](https://github.com/NVIDIA/cosmos-framework),
downloads all HuggingFace model weights, and pulls the required `nvcr.io` containers.

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
