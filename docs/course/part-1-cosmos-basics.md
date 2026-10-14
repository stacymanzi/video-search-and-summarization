# Part 1: Cosmos Basics

Part 1 is where you work with Cosmos directly, at the model level, with nothing
in between. You deploy it, prompt it, measure it, and fine-tune it.

The vision language model you'll use throughout is **Cosmos 3 Reasoner**. It
reads a video clip and answers a question about it — and in Part 2 it becomes
the component that decides whether a traffic alert is real. Learning how it
behaves here is what makes Part 2 a deployment problem rather than a modeling
one.

The teaching content lives in the notebooks themselves. This page tells you what
to run, in what order, and what you should be able to do when you finish.

```{nvlearning-meta}
- **Level:** All levels — no prior VLM or Cosmos experience needed
- **Format:** Three Jupyter notebooks, run cell by cell
- **Primary model:** Cosmos 3 Reasoner
- **Location:** Lab notebook folders (`lab-1-vlm/`, `lab-2-sdg/`, `lab-3-vlm-pt/`)
```

## Learning Objectives

```{nvlearning-objectives}
- **Explain** the two Cosmos 3 towers — Reasoner and Generator — and which one to reach for.
- **Deploy** Cosmos 3 Reasoner and run inference on both images and video.
- **Distinguish** effective from ineffective VLM prompts, and structure model output as formatted reports, JSON, and true/false or multiple-choice answers.
- **Benchmark** a deployed VLM for latency and throughput across sampled frames, input and output sequence length, and concurrency.
- **Generate** synthetic video with the Cosmos Generator using image-to-image, image-to-video, and video-to-video workflows.
- **Organize** a dataset for VLM fine-tuning and apply LoRA supervised fine-tuning to Cosmos 3 Reasoner.
- **Evaluate** a fine-tuned VLM using appropriate task types, annotations, and metrics.
```

## The Two Cosmos Towers

Cosmos 3 is an omnimodal world model that exposes two towers. Knowing
which one a task needs is the single most useful thing to carry out of Part 1:

| Tower | Inputs | Outputs | You use it to |
|---|---|---|---|
| **Reasoner** | Text, vision | Text | Understand a scene and answer questions about it — the alert verdict in Part 2 |
| **Generator** | Text, vision, sound, action | Vision, sound, action | Generate synthetic video — the training data in Part 2.2 |

The family comes in three sizes — **Cosmos3-Super** (64B), **Cosmos3-Nano**
(16B), and **Cosmos3-Edge** (4B). This course uses the Nano size throughout,
which is what makes a full fine-tune-and-redeploy loop fit inside a lab session.

## Complete the Three Notebooks

Run them in order. Each one assumes the previous is finished.

### Notebook 1.1 — Vision Language Models

Deploy Cosmos 3 Reasoner and learn how it behaves. You'll run inference on
images and videos, see how frames become vision tokens, compare good and bad
prompts, and force structured outputs — formatted reports, JSON, true/false,
and multiple choice. You'll also compare reasoning against non-reasoning modes
and benchmark performance for latency and throughput. Tear down the deployment
at the end.

This is the Reasoner tower, and the structured-output work matters more than
it first appears: Part 2 depends on the model answering in a fixed format.

### Notebook 1.2 — Augmenting and Generating Datasets With Generative Models

Switch to the Generator tower and produce your own training data. You'll
choose a seed image — real or synthetic — and work through the configurations,
prompts, and best practices for image-to-image, image-to-video, and
video-to-video generation. You'll augment a seed image with Qwen Image Edit,
generate video from it with Cosmos, and augment existing video with
video-to-video. You'll also use an LLM inside the notebook to help write
generation prompts.

### Notebook 1.3 — Fine-Tuning Vision Language Models

Organize a dataset for VLM fine-tuning and post-train Cosmos 3 Reasoner. You'll
cover the task types worth fine-tuning on, compare fine-tuning methods — SFT,
LoRA, and RL — and work through VLM evaluation: task types, annotation types,
and metric selection. You finish by fine-tuning the model itself.

:::{important}
Part 2.3 runs the same LoRA recipe you run here — same rank, same target
modules, same hyperparameters — but through Agent Skills instead of notebook
cells. Finishing 1.3 is what makes Part 2.3 a comparison rather than a first
encounter.

Part 2.3 does **not** depend on it, though. That walkthrough is self-contained
and ships its own configuration; nothing in Part 2 reads this notebook's output
or requires it to have been run. If you fall behind or restart your instance,
you can start any Part 2 walkthrough directly.
:::

:::{only} internal
Presenter assignments for Part 1: notebook 1.1 Vision Language Models, notebook
1.2 Augmenting and Generating Datasets, notebook 1.3 Fine-tuning Vision
Language Models. The opening Physical AI, Cosmos, VSS, PAIDF, and TAO overview
is delivered as a presentation before notebook 1.1.
:::

```{nvlearning-checkpoint} Part 1 Complete
- Notebook 1.1 run end to end, with the Cosmos 3 Reasoner deployment torn down.
- Notebook 1.2 run end to end, with at least one generated video reviewed.
- Notebook 1.3 run end to end, with a fine-tuned checkpoint and its evaluation metrics.
- You can say which Cosmos tower a given task needs, and why.
- You can explain what LoRA changes and what it leaves frozen.
```

## From the Model to the Blueprint

You now know what Cosmos can do on its own. You've deployed the Reasoner tower,
shaped its answers, measured its throughput, driven the Generator tower, and
fine-tuned a checkpoint. That's the model level, and it's the right place to
start — but notice what you had to supply by hand every time: a clip, a prompt,
a place to put the answer.

Production doesn't look like that. The clip is an unbounded camera stream, the
answer has to reach other systems, and one generated video has to become a
labeled dataset. Those aren't model problems, and no amount of prompt
engineering solves them.

That's the gap the blueprints fill:

| You know how to | Part 2 shows you how to |
|---|---|
| Send a clip to Cosmos and read the answer | Run it continuously against live camera streams, store the footage, and publish verdicts to the rest of your system — with **VSS** |
| Generate a video with the Generator tower | Produce labeled, quality-checked datasets at scale — with **PAIDF** |
| Fine-tune a checkpoint in a notebook | Launch, evaluate, and merge that same fine-tune as a reproducible job — with **TAO** |

## Why Agent Skills Change the Pace

Part 2 drives all three blueprints through a coding agent and Agent Skills, and
that's not a gimmick — it's the reason the whole loop fits in a single session.

Standing up VSS by hand means reading deployment docs, picking a profile,
resolving host and external addresses, composing 31 services, registering
streams, and writing alert rules. Done manually that's days of work and a long
tail of misconfiguration. In Part 2.1 you describe what you want in a sentence
or two and the agent reads the skill to learn the rest.

The skills carry the expertise you'd otherwise have to acquire first: which
parameters matter, what a healthy deployment looks like, which failures are real
and which are expected. You stay the one making decisions — every container the
agent runs asks for your approval, and the command it shows you is worth
reading — but you skip the part where you learn a new toolchain before you can
test an idea.

That's the real payoff. Going from "I think fine-tuning would help" to a
measured answer takes an afternoon instead of a sprint, so you can afford to
actually run the loop.

## What's Next

Start with
[Part 2.1: Deploy Zero-Shot VSS for Alert Verification](part-2-1-deploy-zero-shot-vss),
where you stand up the traffic pipeline and find out where a stock deployment
falls short.
