# Part 2.4: Deploy Fine-Tuned VSS for Alert Verification

In Part 2.1 the traffic-tuned profile with the stock Cosmos Reason 3 Nano confirmed the collisions and rejected every stalled vehicle. Here you keep everything else fixed and swap in the checkpoint fine-tuned on the traffic dataset, then run the same evaluation video and compare.

Holding everything else constant is the whole point. The CV stages are untouched, so Behavior Analytics produces the same candidates. Alert Bridge asks the same one-label question. Any difference you measure comes from the verifier and nothing else.

```{nvlearning-meta}
- **Level:** All levels — no prior VLM or Cosmos experience needed
- **Duration:** About 25 minutes. About 10 minutes to rebuild and load the checkpoint, about 6 minutes for the evaluation video.
- **Agent:** Codex, continuing from the Part 2.1 terminal
- **Checkpoint:** `$LAB_DATA_DIR/models/Cosmos3-Nano-VLM-lora`
```

## Learning Objectives

```{nvlearning-objectives}
- **Swap** the stock Cosmos Reason 3 Nano for the fine-tuned checkpoint in the tuned profile with one build flag.
- **Run** the same evaluation video through the redeployed pipeline.
- **Compare** stock and fine-tuned verdicts on the same candidates, and on the official four-video evaluation.
- **Explain** why the model-level gain from Part 2.3 transfers to the system level here.
```

## Before You Begin

Same VM, lab container, remote Docker daemon, and environment as Part 2.1. Both GPUs must be free: the Part 2.3 fine-tune has finished and nothing else is running.

```bash
docker ps
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
```

Expected: no containers, both GPUs under 1 GB used.

The fine-tuned checkpoint is a merged LoRA (r=16, alpha=32 on q_proj and v_proj) of Cosmos Reason 3 Nano. RT-VLM serves it read-only from the course data directory and advertises the directory basename, `Cosmos3-Nano-VLM-lora`, as the model id. This walkthrough deploys the course-provided copy at `$LAB_DATA_DIR/models/Cosmos3-Nano-VLM-lora`, staged by the course loader. The checkpoint you merged in Part 2.3 lives at `$DLI_BASE/part2/merged/Cosmos3-Nano-VLM-lora`; it is the same recipe with the same basename, so you may pass that path to `--vlm-model-dir` instead and everything below works unchanged.

If the Part 2.1 terminal is still open with Codex in it, skip to Step 1. If it is gone, open a new terminal, re-create the environment and start a new Codex session. Part 2.3 installed a TAO agent identity at `~/.codex/AGENTS.md` that Codex loads into every new session; remove it so this session behaves as the VSS agent.

```bash
export AICITY_PUBLIC_URL="http://<your-lab-host>"
export AICITY_MEDIA_DIR=$LAB_DATA_DIR
export AICITY_DATA_DIR=/opt/aicity-runtime/aicity-vss-data
export AICITY_BUILD_DIR=/opt/vss/_builds/aicity-track4-alert-verification-dli-cvfix4
export NGC_CLI_ORG=nvidia
read -rsp 'NGC Personal Key: ' NGC_CLI_API_KEY && echo && export NGC_CLI_API_KEY NGC_API_KEY="$NGC_CLI_API_KEY"
rm -f ~/.codex/AGENTS.md
cd /opt/vss && codex --dangerously-bypass-approvals-and-sandbox
```

Type `/model` and choose **GPT 5.6 Sol** with **Medium** reasoning, as in Part 2.1. A new Codex session has forgotten the context. Paste this as one message and wait for "Understood":

```
Context before you begin. This is a Docker-in-Docker environment, so several things differ from a workstation:

1. The Docker daemon is NOT local. It is remote, reached via DOCKER_HOST, and is already configured for VSS. Verify it with `docker info` rather than reading /etc/docker/daemon.json or running sysctl locally - those describe this container, not the daemon. There is no sudo here and none is needed.

2. HOST_IP in this shell is the daemon's own address and is what the AI City build.py takes as --host-ip. Do NOT derive an address from this container's own interfaces or routes (`ip route get`, `hostname -I`). If you ever need to re-detect it, the correct value is:

   docker run --rm --network host busybox:1.37.0 \
     ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1

   (equivalently: getent hosts dind). It changes whenever the daemon container is recreated.

3. The browser hostname travels in AICITY_PUBLIC_URL, which is already exported, and build.py passes it as --public-url. build.py has NO --external-ip flag; the generated EXTERNAL_IP equal to HOST_IP is by design. Do not rebuild to add a flag that does not exist. If you ever need a public address, ask me for the hostname from my browser's address bar; never derive one with curl (ifconfig.me or similar), that is not what VSS needs.

4. The net.core.rmem_max / wmem_max sysctl warning is expected and safe to overrule. They are not network-namespaced and cannot be set from inside a container. Deployments succeed with the current values. Do not try to set them.

5. Everything you can see through DOCKER_HOST is a warm cache that is expensive to rebuild: about 33 images plus model weight volumes. `deploy.py down` is fine, but never pass -v to a compose down and never run `docker system prune`.

6. An idle or empty component is usually correct, not broken. RT-CV is a stream-processing worker, not an HTTP server, and processes zero sources until a stream is registered in VST. The Alert UI stays empty until a video has been processed. Check `/vst/api/v1/sensor/list` and the alert-bridge rule list before concluding something is misconfigured.

7. Files under /opt/vss/_builds, especially override.env and resolved.yml, contain the NGC key. Never print them and never grep them with context lines. Read one exact variable at a time, for example grep '^VSS_PUBLIC_HOST=' override.env. The key itself is exported in this shell; never ask me to paste it.

When something looks wrong here, ask two questions before changing any setting: which namespace am I standing in, and has this component been given work?
```

## Step 1: Rebuild With the Fine-Tuned Checkpoint

Paste into Codex:

```
Check docker ps. If anything from the AI City project is still running, stop it first with deploy.py --build-dir $AICITY_BUILD_DIR down, without -v. Then move the existing $AICITY_BUILD_DIR aside and rebuild from /dli/task/aicity_cvfix4/README.md: run the section 5 build.py command exactly as written there (H100, --host-ip $HOST_IP, --public-url $AICITY_PUBLIC_URL, 800 by 410, the AICITY_* directories) plus --vlm-model-dir $LAB_DATA_DIR/models/Cosmos3-Nano-VLM-lora from the optional fine-tuned section. Run preflight, then up, and wait for ready. RT-VLM must advertise Cosmos3-Nano-VLM-lora. If anything fails, stop and show me the error instead of working around it. Report the exact commands you ran at the end.
```

Expected: `build.py` checks that the checkpoint is a complete merged export and readable by RT-VLM, prints `RT-VLM serves fine-tuned checkpoint ... VLM_NAME=Cosmos3-Nano-VLM-lora`, and prints the same `Cache seed` lines as in Part 2.1 with `RT-CV models/engines: already complete in VSS_DATA_DIR`. In the state initializer log the two RT-VLM volumes report `already seeded`. `up` ends with the ready message and readiness confirms the advertised model. About 10 minutes: a few minutes to rebuild, preflight and start the services, then about 5 minutes for RT-VLM to read the 17 GB checkpoint from the data volume.

## Step 2: Open the UI

Browse to `http://<your-lab-host>/alerts/`.

Rows from Part 2.1 remain under `stock-eval-01`: the state volumes survived the shutdown. New rows will arrive under the new sensor id.

## Step 3: Run the Same Evaluation Video

Paste into Codex:

```
Run README section 8 with $LAB_DATA_DIR/traffic_anomaly_dataset/eval_videos/eval_video_01.mp4, run id ft-eval-01 under /opt/aicity-runtime/aicity-eval, --vlm-model Cosmos3-Nano-VLM-lora, keeping --retain-vios-storage. Show me the run manifest counts and every verdict with its reasoning field, and compare them with the stock-eval-01 run.
```

Expected in about 6 minutes: the same four events covered, with detection-frame and candidate counts matching `stock-eval-01` or within a candidate or two of it, a Cosmos verdict per candidate, and the agent listing every verdict that changed. The collisions stay confirmed with `LABEL=collision`. The stalled vehicles, rejected with `LABEL=none` in Part 2.1, are now confirmed with `LABEL=stalled`.

The candidates are the control in this experiment. The checkpoint swap cannot change them; a missing event or a very different frame count means something else changed.

Refresh the Alerts tab and filter by sensor `ft-eval-01`. Flip between the two sensors: same clips, same candidates, different verdicts on the stopped vehicles.

## Step 4: Compare on the Official Evaluation

Course-provided results from this same pipeline on all four evaluation videos, 16 events in total: 8 collisions and 8 stalled vehicles.

| Metric | Stock Cosmos Reason 3 Nano | Fine-tuned |
|---|---|---|
| Behavior Analytics coverage | 16/16 | 16/16 |
| Collisions confirmed | 7/8 | 8/8 |
| Stalled vehicles confirmed | 0/8 | 8/8 |
| Cosmos confirmed, total | 7/16 | 16/16 |
| Confirmed false positives | 0 | 0 |

Every stalled vehicle recovered, one missed collision recovered, no regressions.

Why the gain transfers this time: Alert Bridge asks the verifier the exact question Lab 3 trained on, and reads the answer with the same `LABEL=` contract. In Part 2.3 the fine-tune took stalled-vehicle recall from 0 to 1 on clips; here the same weights take it from 0 to 8 out of 8 on deployed candidates. The model-level number and the system-level number line up because the deployment asks the model what it was taught. Change the question, and they would not.

The scoring command that produced the table, for reference. It needs the run directories of all four evaluation videos; the agent runs it:

```bash
cd /opt/vss && python3 experiments/aicity_track4_alert_verification/scripts/score_cosmos_super_eval.py \
  --timelines $LAB_DATA_DIR/traffic_anomaly_dataset/eval_videos/timelines.json \
  --run-dir /opt/aicity-runtime/aicity-eval/ft-eval-01 \
  --run-dir /opt/aicity-runtime/aicity-eval/ft-eval-02 \
  --run-dir /opt/aicity-runtime/aicity-eval/ft-eval-03 \
  --run-dir /opt/aicity-runtime/aicity-eval/ft-eval-04 \
  --expected-vlm-model Cosmos3-Nano-VLM-lora \
  --output /opt/aicity-runtime/aicity-eval/ft-score.json
```

## Step 5: Clean Up

Paste into Codex:

```
Stop the AI City deployment with deploy.py down (no -v) and confirm no containers remain under DOCKER_HOST.
```

```{nvlearning-checkpoint} Checkpoint 4
- Tuned profile redeployed with the fine-tuned checkpoint; RT-VLM advertised `Cosmos3-Nano-VLM-lora`.
- `eval_video_01` processed; the same candidates as the stock run; stalled vehicles now confirmed with `LABEL=stalled`.
- Stock versus fine-tuned understood: same coverage, 7 of 16 to 16 of 16 confirmed, no false positives.
- Deployment stopped without `-v`.
```

## Troubleshooting

| It reports | What is actually true |
|---|---|
| `build.py` rejects the checkpoint: missing shards or inconsistent byte counts | A copy is still in flight or incomplete. Wait for the loader, or re-copy the checkpoint, then rebuild. |
| `build.py` rejects the checkpoint: not readable by the runtime, prints a `chmod -R o+rX` command | RT-VLM runs as UID 1001. The course loader stages files as `0755`/`0644`; a hand-copied checkpoint arrives as `0750`. Run the printed command and rebuild. |
| `deploy.py down` fails with `missing regular build artifact` | The build directory was moved aside while the stack was still running. Move it back, run `down`, then move it aside again and rebuild. |
| `install.sh: installed experiment differs` | An older copy of the package is installed. Move `/opt/vss/experiments/aicity_track4_alert_verification` aside and rerun. |
| `vss-rtvi-vlm` restarting; log says `Read-only file system: .../.lock` | The model directory is not writable. The package stages a writable symlink directory for this; if you see this, the package is stale. |
| `up` or `ready`: `did not advertise exact VLM_NAME=...` | RT-VLM is still loading the 17 GB checkpoint, or the build was made without `--vlm-model-dir`. Wait for readiness to finish; if it times out, `grep '^VLM_NAME=' "$AICITY_BUILD_DIR/override.env"` must print `Cosmos3-Nano-VLM-lora`, otherwise rebuild with the flag. |
| Section 8 script: `configured Cosmos model '...' is absent from RT-VLM` | The `--vlm-model` value does not match what RT-VLM advertises. The model id is the checkpoint directory basename: pass `Cosmos3-Nano-VLM-lora`. |
| `sensor ID has stale Elasticsearch documents` or `output directory is not empty; refusing to mix runs` | A previous attempt with this run id left state behind. Rerun with a new run id such as `ft-eval-02`. |
| Compose reports a dependency `unhealthy` seconds after `up` starts | First-boot PostgreSQL race on a fresh volume. Rerun the same `up` command unchanged. |
| An event from `stock-eval-01` is missing, or the frame count is far off | Something other than the model changed. Check that the build used the same `--video-width`/`--video-height` and the same profile; the checkpoint swap alone cannot move candidates. |

:::{only} internal
**Shot list.**

- `build.py` line "RT-VLM serves fine-tuned checkpoint ... VLM_NAME=Cosmos3-Nano-VLM-lora".
- Codex summary confirming RT-VLM advertises `Cosmos3-Nano-VLM-lora`.
- Alerts UI filtered to the fine-tuned run.
- Codex's side-by-side comparison of the stock and fine-tuned verdicts.
- The stock versus fine-tuned score table.
:::

## What's Next

You have closed the loop: measured, generated, fine-tuned, redeployed, and measured again. The Conclusion recaps what you built and where to take it next.
