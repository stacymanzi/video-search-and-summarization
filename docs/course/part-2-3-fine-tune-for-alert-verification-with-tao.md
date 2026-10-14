# Part 2.3 (Optional): Fine-Tune for Alert Verification With TAO

:::{note}
**This walkthrough is optional.** It repeats the workflow of Notebook 1.3 —
zero-shot evaluation, LoRA fine-tuning, evaluation and merge — but driven by
natural-language prompts to a coding agent rather than notebook cells. Part 2.4
uses a course-provided fine-tuned checkpoint, so you can skip this section and
return to it later.

The same agent-driven workflow is demonstrated in the video tutorial
[Post-train Cosmos 3 with TAO agent skills](https://youtu.be/9AQkVbx3fKA),
which also covers **AutoML**: letting the agent search the hyperparameter space
for you instead of tuning learning rate, rank and batch size by hand.
:::

Part 2.1 left you with a working pipeline whose verifier — the stock Cosmos
Reason 3 Nano — confirms most real events but not all of them. Part 2.2 gave you
the data to fix that. Here you fine-tune the verifier with LoRA, and you do it
without writing a single container command: you tell a coding agent what you
want, and the [NVIDIA TAO Skill Bank](https://github.com/NVIDIA-TAO/tao-skill-bank)
teaches it how TAO trains, evaluates and merges Cosmos Reason.

You measure the base model on held-out clips, fine-tune, measure again on the
same clips, and merge the adapter into the plain checkpoint that Part 2.4
deploys.

```{nvlearning-meta}
- **Level:** All levels — no prior VLM or Cosmos experience needed
- **Duration:** About 30 minutes, dominated by one training wait
- **Video tutorial:** <https://youtu.be/9AQkVbx3fKA>
- **Agent:** Codex with the TAO Skill Bank
- **Working directory:** `$DLI_BASE/part2`
```

## Learning Objectives

```{nvlearning-objectives}
- **Install** the TAO Skill Bank into a coding agent.
- **Run** a zero-shot evaluation of Cosmos Reason 3 Nano and read what kind of gap it shows.
- **Review** the training plan the agent proposes before approving the launch.
- **Fine-tune** Cosmos Reason 3 Nano with LoRA and evaluate the result on the same clips.
- **Merge** the adapter into a checkpoint that VSS can serve.
```

## Before You Begin

The lab environment is already loaded: the TAO container image, the base
checkpoint and the dataset are in place, and the prompts below assume that. The
Skill Bank itself is installed in Step 2, straight from GitHub. On a fresh machine none of this is a blocker — given an NGC
key and a Hugging Face token, the coding agent with the TAO skills pulls the
image, downloads the model and prepares the checkpoint itself. The only
difference is time.

| Asset | Where |
|---|---|
| TAO Cosmos-RL container | `nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl`, present in the Docker daemon |
| Base checkpoint | `$DLI_BASE/models/Cosmos3-Nano-VLM/` (17 GB) |
| Dataset | `$DLI_BASE/dataset/` — `train/` 92 clips, `val/` 128 clips, `dataset_info.json` |
| Course-provided fine-tuned checkpoint | `$LAB_DATA_DIR/models/Cosmos3-Nano-VLM-lora/` — the fallback Part 2.4 deploys if you skip this walkthrough |

The base checkpoint is Cosmos Reason 3 Nano already converted to Qwen3-VL
safetensors, so no conversion runs here. When that converted checkpoint is not
present, the agent downloads the omni-modal `nvidia/Cosmos3-Nano` from Hugging
Face and converts it to Qwen3-VL safetensors itself before training; the skill
owns that step.

Everything under `$LAB_DATA_DIR` is visible at the same path from your terminal
and from the Docker daemon (which runs in a separate `dind` container, reached
through `DOCKER_HOST`). That is the only tree the daemon can bind-mount, which is
why the workspace lives there: anything written elsewhere is invisible to the
container.

### The Recipe

The configuration used for every number on this page. Step 5 gives it to the
agent explicitly.

| Setting | Value |
|---|---|
| Method | LoRA, supervised fine-tuning (SFT) |
| Adapter targets | `q_proj`, `v_proj` |
| LoRA rank / alpha | 16 / 32 |
| Epochs | 3 |
| Batch size | 4 |
| Learning rate | 1e-4 |
| Frames sampled per clip | 8 |
| Training annotations | `train/annotations_augmented.json` — originals plus fog, rain and night re-renders |
| GPUs | 0 and 1, one FSDP shard each |

### Expected Times

Measured on the course instance (2 × H100) with the prompts on this page. The
"whole step" column includes the agent's planning, the container run and the
report.

| Step | Container | Whole step |
|---|---|---|
| 4 Zero-shot evaluation | ~1 min | ~4 min |
| 5 Plan, review and fine-tune | ~4.5 min | ~8 min |
| 6 Fine-tuned evaluation | ~1.5 min | ~4 min |
| 7 Merge and verify | ~2 min | ~4 min |

About 20 minutes end to end. The prompts are written to keep the agent's share
of that small: sparser prompts measured roughly twice as long, almost all of it
agent time (see the note under Step 4).

### Three Things Worth Knowing Before You Prompt

The Skill Bank encodes production defaults, and this lab sits outside three of
them. The prompts below handle each; this is why.

**The patch embedding.** Cosmos-RL's default Conv3D video patch embedding has no
cuDNN engine on this machine's driver and fails on the first clip with
`GET was unable to find an engine to execute this computation`. The `linear`
implementation is numerically equivalent and works everywhere, so the prompts
ask for it. Left to discover this on its own, the agent does get there, but only after
several retries.

**GPU memory and disk.** The Cosmos-RL skill requires at least 256 GB of GPU
memory in total and 384 GiB of free disk before it will launch SFT — sized for
fine-tuning an 8B model on a full dataset, where 8 × H100 is the
recommendation. This machine has two H100s, about 190 GB. The job here is 92
clips for three epochs and fits comfortably; the prompts say so, and the agent
proceeds. The disk figure deserves respect even so: each full training
checkpoint Cosmos-RL writes is about **66 GB**, and it retains two. A run that
fills the disk fails while saving a checkpoint, with the rather opaque message
`basic_ios::clear: iostream error`. Step 8 removes these checkpoints once the
adapter has been merged.

**Running as root.** The TAO Docker skill refuses to run a container as `root`
when it writes to a bind mount. The reason is practical: a container running as
root writes root-owned files onto your disk, and afterwards you — a normal user
— cannot delete or modify them. The skill's fix is to run the container as your
own user id instead. In this lab that protection is not needed: the lab itself runs as
root in a single-user container, so there is no other user whose files could be
affected. The prompts tell the agent this, and it runs the container as root
without the extra ownership step.

## Step 1: Free the GPUs

Part 2.2 left model endpoints running — Nemotron and, if you did Track A,
Cosmos. Training needs both GPUs empty. In a JupyterLab terminal:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
```

Stop whatever is listed:

```bash
docker stop $(docker ps -q)
```

Rerun `nvidia-smi`. **Expected:** both GPUs under 1 GB used.

:::{warning}
`docker stop` is sufficient. Please avoid `-v` on a compose down and
`docker system prune`: the image cache is what allows every later step to fit
its time slot.
:::

## Step 2: Install the TAO Skill Bank

The Skill Bank ships a one-line Codex installer. It registers the skills as a
plugin and installs the TAO agent identity so every Codex session knows to use
them.

```bash
curl -fsSL https://raw.githubusercontent.com/NVIDIA-TAO/tao-skill-bank/main/scripts/install-codex-agents.sh | bash
```

**Expected:** about 5 seconds, ending with `Installed TAO agent identity` and
`Done. Launch 'codex' from any directory to use the TAO skill bank.`

## Step 3: Start the Agent

A new Codex session in a new directory. The Part 2.1 session lives in
`/opt/vss` with the VSS skills; this one gets its own workspace.

```bash
mkdir -p $DLI_BASE/part2 && cd $DLI_BASE/part2
codex
```

Leave the approval policy at its default. Every `docker run` the agent proposes
stops for your approval, and the command it shows you is worth reading.

## Step 4: Zero-Shot Evaluation

Paste into Codex:

```text
Using the TAO skills, evaluate the base Cosmos 3 Nano checkpoint at
$DLI_BASE/models/Cosmos3-Nano-VLM on the validation split in
$DLI_BASE/dataset/val, writing everything under the current directory.

Use these verified settings and do not probe for them:
- image: nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl (already present, never pull)
- GPUs: 0 and 1
- run as root, with -w /results and --shm-size=32g --ipc=host
- env: TAO_API_JOB_ID=<job name>, TAO_API_RESULTS_DIR=/results, HF_HUB_OFFLINE=1
- mounts: checkpoint at /ptm (read-only), val split at /val (read-only), results directory at /results
- command: cosmos-rl-evaluate --config /results/config.toml
- spec: start from /workspace/lab-3-vlm-pt/configs/eval_config.toml and set only
  model.model_name=/ptm, model.base_model_path=/ptm, model.enable_lora=false,
  dataset.annotation_path=/val/annotations_augmented.json,
  dataset.system_prompt from dataset_info.json, num_gpus=2
- no credentials are needed

Launch it, then wait for the container to exit with docker wait - do not read
or stream the log while it runs. Then score results.json: each answer counts as
the class it names (collision, stalled or none; anything else is none), and
collision and stalled together are the positive class. Report macro-F1 over
positive vs none, precision, recall, clips detected per class, and accuracy per
capture condition.

Then tell me in two sentences what kind of gap this is.
```

The bullet list is what makes this prompt fast. Every setting in it
is something the agent would otherwise discover by reading skill files, probing
the image, or launching a container that fails and retrying — and in our measured
runs that discovery cost 5 to 14 minutes on a one-minute evaluation. Pointing at
the validated spec that ships with the lab spares it rebuilding one from the
skill's template — whose placeholder values fail on the first launch, and which
defaults to CPU video decoding (`torchvision`) where the shipped spec uses the
GPU decoder; in our measurement that difference alone made training three times
slower (20 minutes instead of 6.5). Telling
the agent *not* to read the log until the container exits removes the other
large cost: streaming a training or evaluation log and reasoning about every
chunk.

**Expected (measured):** the agent shows you one `docker run`, waits for your
approval, and evaluates for about a minute.

| Metric | Value |
|---|---|
| Precision | 1.000 |
| Recall | 0.500 |
| Macro-F1 | 0.733 |
| `collision` detected | 32/32 |
| `stalled` detected | 0/32 |

The same score appears in every condition. Look at the raw answers too: the base
model drops the `LABEL=` prefix on most of them and just says `none` or
`collision`. Fine-tuning fixes the format along with the content.

:::{important}
Perfect precision with recall 0.500 and `stalled` at 0/32 means the base model
never predicts one of the two classes. Precision 1.000 is not a sign of strength
in this case; it is the score a model earns by answering only the cases it is
already confident about. Because the result is identical across original, fog,
rain and night, this is a **class gap rather than a weather gap** — and a class
the model never predicts is something training addresses far more effectively
than prompting.
:::

## Step 5: Plan, Review and Launch the Fine-Tuning Run

One prompt covers the plan, the review and the launch. The agent stops once —
at the `docker run` approval — and that pause is where you read the plan.

```text
Fine-tune the same checkpoint with LoRA on the training split in
$DLI_BASE/dataset/train (annotations_augmented.json), validating on
the val split, on both GPUs, with outputs under the current directory.

Settings, all verified for this environment:
- spec: start from /workspace/lab-3-vlm-pt/configs/train_config.toml
- LoRA rank 16, alpha 32, on q_proj and v_proj
- 3 epochs; train_batch_per_replica 4 with mini_batch 1; learning rate 1e-4
- nframes 8; model_max_length 40960; dp_shard_size 2
- custom.system_prompt from dataset_info.json; train and val annotation paths
- container: same as the evaluation, plus the train split mounted read-only at /train
- command: cosmos-rl --config /results/config.toml /opt/cosmos_rl/tao_sft_example.py
  (the hook is a required positional argument)
- this machine is below the skill's 256 GB GPU-memory and 384 GiB disk gates; the job
  is validated here, so proceed past both without running preflight

Then, in order:
- verify the training split: records per label, no clip in both train and val,
  every video present
- show me the launch review with a one-line reason for each setting
- launch, and wait for the container to exit with docker wait - do not read the
  log while it runs
- report the validation loss per epoch, the epoch selected as best, and the
  adapter path
```

**Expected:** 92 training records — 40 collision, 36 stalled, 16 none — from 46
source clips, no overlap with val, all videos present; a settings table with the
agent's reasoning; then the `docker run` approval prompt, about 4.5 minutes of
training, three validation-loss lines, and the adapter under
`.../safetensors/epoch_N/`.

Two of the checks it runs are worth understanding, because both produce
misleading errors when violated:

- Batch size must be **divisible by** the micro-batch. Otherwise: an immediate
  `AssertionError` on every rank.
- Batch size must be **no larger than** `samples ÷ GPUs` — here 92 ÷ 2 = 46.
  Otherwise the run completes *zero* training steps and then crashes saving a
  checkpoint with `'NoneType' object has no attribute 'state_dict'` — an error
  that appears to concern checkpointing but is caused by the batch size.

The recipe is spelled out because it is known to work on this dataset in this
time slot. You can instead drop the settings paragraph and ask the agent to
choose the LoRA and optimizer settings itself, explaining each choice; that is
the better first move on a dataset you have not trained on before. Expect it to
take longer to converge on a good configuration, though — in our test the
agent's own first choice (batch 1, a higher learning rate, adapters on every
projection) trained unstably and had to be replaced with the recipe above.

:::{tip}
Ask the agent what it would change. The learning rate is two orders of
magnitude above the skill's default of `1e-6`, and a well-briefed agent will
point that out. The value is appropriate for 92 clips over a few epochs; on a
large dataset it would need to be tuned, which is the subject of "Beyond This
Lab".
:::

:::{only} internal
While it trains, the instructor narrates: adapters on the attention projections
only, frozen base weights, one FSDP shard per GPU, validation at the end of every
epoch, a 15 MB adapter instead of a 17 GB model. Note that only epochs 2 and 3
remain on disk — checkpoint retention keeps two.
:::

## Step 6: Evaluate the Fine-Tuned Model

Paste into Codex:

```text
Evaluate the best adapter on the same validation clips with the same container
settings and scoring as the zero-shot run (model.model_name = the adapter folder,
model.enable_lora = true, model.base_model_path = /ptm). Wait for the container
with docker wait; do not read the log while it runs.

Show me before and after side by side: the metrics, how many clips of each class
were detected, and accuracy per condition. List every clip that is still wrong.

Then tell me what changed and what gap remains.
```

**Expected (measured):** about 1.5 minutes.

| Metric | Base | Fine-tuned | Change |
|---|---|---|---|
| Macro-F1 | 0.733 | 0.961 | +0.228 |
| Precision | 1.000 | 0.928 | −0.072 |
| Recall | 0.500 | 1.000 | +0.500 |
| `stalled` detected | 0/32 | 32/32 | +32 |

Clean clips are perfect; fog, rain and night trail slightly. Every remaining
error is a *normal* clip in bad weather that the model now flags as an anomaly.

:::{note}
**Precision fell and the model got better.** The base model made 32 predictions
and got all 32 right. The fine-tuned model makes about 69 and gets 64 right:
it recovered every one of the 32 missed anomalies and picked up a few false
alarms doing it. Precision dropped because the denominator doubled, not because
the model became worse at anything. Whether the trade is worthwhile depends on
the application; for alert verification, where a miss is an unreviewed collision
and a false alarm costs an operator a few seconds, recovering 32 misses for a
handful of false alarms is a clear improvement.
:::

The class gap is closed, and a small weather gap has appeared where there was
none before. That residual is the specification for the next data run — exactly
what Part 2.2 generates. This is the loop closing on itself.

## Step 7: Merge for Deployment

VSS serves a standard Hugging Face checkpoint rather than a base model with a
separate adapter, and the Cosmos 3 Reasoner NIM has the same requirement.
Merging the adapter into the base weights is therefore the standard final step
of a Cosmos fine-tuning run.

Part 2.4 deploys whatever is at `$LAB_DATA_DIR/models/Cosmos3-Nano-VLM-lora`.

Paste into Codex:

```text
Merge the best adapter into the base weights and write the merged checkpoint to
$LAB_DATA_DIR/models/Cosmos3-Nano-VLM-lora. If a checkpoint already exists there,
leave it in place and skip the merge.

Evaluate the checkpoint at that path on the same clips, mounted at /ptm with
enable_lora false, waiting with docker wait as before, and confirm it scores
identically to the adapter run.

Report its size and shard count.
```

**Expected:** about 1 minute to merge, 1.5 to evaluate; 17 GB in 4 shards;
metrics identical to Step 6.

## Step 8: Clean Up

Training left two full checkpoints of about 66 GB each under the run directory.
The merged checkpoint and the small adapter are all that later steps need.

```text
Delete the full training checkpoints under the run directory, keeping the
adapter safetensors, the evaluation results and the merged checkpoint.

Show me the disk space before and after.
```

```{nvlearning-checkpoint} Checkpoint 3
- Part 2.2 endpoints stopped; both GPUs free.
- TAO Skill Bank installed into Codex (installer ended with `Done`).
- Zero-shot baseline measured: precision 1.000, recall 0.500, `stalled` 0/32, the same in every condition.
- Training split verified and the launch plan reviewed before approval.
- LoRA fine-tuning completed; evaluated on the same clips: recall 1.000, macro-F1 up by about 0.23.
- You can explain why precision fell while the model improved.
- The checkpoint at the path Part 2.4 deploys scores identically to the adapter.
- Full training checkpoints removed; adapter, results and merged checkpoint kept.
```

## Troubleshooting

| It reports | What is actually true |
|---|---|
| The installer does not end with `Done`, or Codex does not behave as the TAO agent | The installer needs network access to GitHub. Rerun it; if it still fails, ask a TA. |
| Agent asks for `NGC_KEY`, `HF_TOKEN`, or which `model_type` the checkpoint is | Expected — see the answer table under Step 4: no credentials, checkpoint is `qwen3_vl`. |
| Agent stops on the 256 GB GPU-memory or 384 GiB disk gate | Expected. Tell it the job is validated on this machine class and to proceed; see "Three Things Worth Knowing Before You Prompt". |
| Agent refuses to run the container as root | Tell it the lab itself runs as root, so there is no ownership mismatch to protect against. |
| `ERROR: This container was built for NVIDIA Driver Release 595.45 or later` | A version notice printed at container start, not a failure. Judge the run by its exit code and outputs. |
| Job recorded as `ERROR` before model loading: "requires `TAO_API_JOB_ID`" or "could not write outputs as UID 1000" | Two mandatory settings from the Step 4 prompt were not applied: run as root, and set `TAO_API_JOB_ID` to the results directory. The agent usually proposes a corrected launch itself; approve it. |
| Container exits 1 immediately, before loading anything | Its working directory is not writable. The agent should set it to the results mount (`-w /results`). |
| A GPU has less than 60 GB free, or training OOMs | Something from Part 2.2 or 2.1 is still running. `docker ps` shows it; `docker stop` it. |
| Training crashes saving a checkpoint with `'NoneType' object has no attribute 'state_dict'` | Zero training steps ran — batch size exceeded samples per GPU. See Step 5. |
| Training exits at once with `ModuleNotFoundError: No module named 'cosmos_rl'` | The training hook was run as a script. It is the final positional argument to `cosmos-rl --config <spec> <hook.py>`, not a program of its own. The agent usually catches this itself and retries. |
| Training fails while saving a checkpoint with `RuntimeError: basic_ios::clear: iostream error` | The disk is full. Each full checkpoint is ~66 GB and two are kept. Free space (old runs' `checkpoints/` trees are the usual culprit; the small `safetensors/` adapters are what you need) and rerun. |
| Training fails with `FileNotFoundError: .rank_0_complete` | Checkpoint retention was set to 1. It must stay at 2. |
| The `best` link points at a `step_N` directory that does not exist | Known Cosmos-RL behaviour: epoch-based saves write `epoch_N` while `best` records a step. The agent resolves it to the retained epoch folder. |
| Merged checkpoint scores differently from the adapter | Do not deploy it. Have the agent rerun the merge and compare `merge_info.json` with the adapter's config. |

## Beyond This Lab

### One Configuration Is Not a Tuned Configuration

You ran one LoRA configuration and gained +0.228 macro-F1. That is a large gain
from a single run, and it is unlikely to be the best available. The NVIDIA
reference workflow for this model follows the fine-tuning prompt with a second
one that hands hyperparameter search to
[TAO AutoML](https://docs.nvidia.com/tao/tao-toolkit/latest/text/automl/automl.html):

```text
Run an AutoML sweep to improve the LoRA result. Let TAO choose suitable search
strategies and tune the important training hyperparameters.

Optimize validation accuracy and summarize the best models.
```

In the [Post-train Cosmos 3 with TAO agent skills](https://github.com/NVIDIA-TAO/tao-tutorials/blob/main/tutorials/tao_agent_skills_examples/post_train_cosmos3/post_train_cosmos3_lora.md)
tutorial, on the Woven Traffic Safety benchmark, one LoRA run took accuracy from 54% to
87%, and a 43-trial AutoML sweep reached 93% — in about 19.5 hours and 170
GPU-hours, which is why it is out of scope for a 30-minute slot. One finding
from that sweep is more important than the headline number: the trial with the
**lowest validation loss was not the one with the highest accuracy**. Optimize
the metric you care about, and have the agent report both.

### Why LoRA and Not Full Fine-Tuning

The same sweep was run with full-parameter SFT. The best SFT trial reached the
same 93% as the best LoRA trial, at roughly seven times the GPU-hours per trial.
On this class of task LoRA matches full fine-tuning at a fraction of the cost,
which is why it is the default here and in the reference workflow.

### Other Platforms

The lab is a single Docker daemon, so the agent used the Docker platform skill.
The same model skill runs unchanged on SLURM, Kubernetes and Brev through their
platform skills; the prompts in this walkthrough work as written on any of them.

:::{only} internal
**Shot list.**

- The installer's `Done. Launch 'codex' from any directory` line.
- The agent's launch review before the training `docker run`.
- Zero-shot score table: precision 1.000, recall 0.500, `stalled` 0/32.
- Training log: three validation-loss lines and the safetensors export.
- Before/after score table: 0.733 to 0.961; `stalled` 32/32.
- Per-condition table: clean perfect, fog, rain and night slightly lower.
- Merge summary: 17 GB, 4 shards, "scores identically to the adapter".

**Time budget (measured on the course instance).** Free GPUs 2, install 1,
zero-shot 4, plan+review+train 8, evaluate 4, merge 4, clean up 1 = 24 min,
leaving slack in a 30-minute slot.
:::

## What's Next

You have a merged checkpoint scoring 0.961 macro-F1 on held-out clips. That is a
model-level number. In
[Part 2.4: Deploy Fine-Tuned VSS for Alert Verification](part-2-4-deploy-fine-tuned-vss)
you put it back into the pipeline and find out what it is worth at the system
level.
