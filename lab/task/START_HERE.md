# Build and Evaluate a Visual AI Agent

Learn the NVIDIA VSS building blocks, then apply them to traffic anomalies.

This lab is Part 1 of the day. It runs one NVIDIA Video Search and Summarization (VSS) 3.2.1 stack, already
deployed for you:

- **RT-CV** (detection and tracking) runs TrafficCamNet RT-DETR.
- **RT-VLM** (captioning and prompting) runs Cosmos 3 Super Reasoner.
- **RT-Embed** (video search) runs Cosmos-Embed1.

Around them, VIOS stores and serves the video, Kafka carries RT-CV's detections, and Elasticsearch stores
detections, embeddings and captions so you can query them. You never deploy anything in this lab.

## Start here

`00_Home.ipynb` opened when the lab launched. It shows the day with links, where you are, and the state of
the lab. The course bar at the top of every page keeps that picture up to date.

After the VSS presentation, open `part1/00-onboarding.ipynb` from Home and run it before anything else. The
stack starts when the lab launches. On a fresh lab the services can take 20 minutes or more to answer. The
readiness check in onboarding waits for them and prints the deployer's status while you wait.

## The day

Times assume a 09:00 start. The instructor gives the real ones. Paths are relative to this folder.

| Clock | Min | Step | Where |
|---|---|---|---|
| 09:00 | 15 | VSS presentation | instructor |
| 09:15 | 15 | 0. Course Onboarding | `part1/00-onboarding.ipynb` |
| 09:30 | 15 | 1. Inspect VSS Deployment | `part1/01-inspect-vss-deployment.ipynb` |
| 09:45 | 40 | 2. RT-CV: Detection & Tracking | `part1/02-rt-cv-detection-and-tracking.ipynb` |
| 10:25 | 10 | Break | |
| 10:35 | 35 | 3. RT-VLM: Captioning & Prompting | `part1/03-rt-vlm-captioning-and-prompting.ipynb` |
| 11:10 | 30 | 4. RT-Embed: Video Search | `part1/04-rt-embed-video-search.ipynb` |
| 11:40 | 25 | Part 1 Assessment, on your own | `part1/assessment.ipynb` |
| 12:05 | 40 | VSS Coding Agent Skills | a page in Course Docs, and a terminal |
| 12:45 | 45 | Lunch: save your work, click STOP TASK for Part 1, then START for Part 2 | |
| 13:30 | 10 | Slides: VSS for traffic anomalies | instructor |
| 13:40 | 35 | 2.1 VSS for Traffic Anomalies: Zero Shot | Part 2 launch |
| 14:15 | 30 | 2.2 Synthetic Data Generation with PAIDF | Part 2 launch |
| 14:45 | 10 | Break | |
| 14:55 | 45 | 2.3 VLM Fine-Tuning | Part 2 launch |
| 15:40 | 40 | 2.4 VSS Fine-Tuned: Deploy and Evaluate | Part 2 launch |
| 16:20 | 30 | Part 2 Assessment | Part 2 launch |
| 16:50 | 10 | Wrap-up | instructor |
| 17:00 | | End | |

Part 1 covers the VSS building blocks one at a time, on traffic video. Part 2 puts RT-CV and RT-VLM to work on
one job: finding traffic anomalies. You deploy VSS with the coding agent and measure it. You generate a
synthetic clip with PAIDF (NVIDIA Physical AI Data Factory) and fine-tune Cosmos 3 Nano on a prepared dataset.
Then you redeploy and measure again.

## Three rules in this lab

1. **Call every service through `nginx`.** Your notebooks and terminal run in the lab container. The VSS
   services run in their own containers behind a proxy named `nginx`, so every address starts with
   `http://nginx/`. `localhost` does not reach them. In your laptop's browser, use the lab's own address
   instead.
2. **Check what the coding agent does.** In VSS Coding Agent Skills you give the coding agent (Codex) Full
   access, so it runs commands without asking you first. Before the first task, it reads the lab's rules from a
   context file. After each task, read the requests it sent and the results it reports.
3. **Leave the VSS stack running.** It is already deployed. Never stop, restart or redeploy it, and never let
   the coding agent do it.

To check the three services yourself, open a terminal (**File > New > Terminal**) and run:

```bash
curl -s -o /dev/null -w "RT-CV    %{http_code}\n" http://nginx/rtvi-cv/api/v1/ready
curl -s -o /dev/null -w "RT-VLM   %{http_code}\n" http://nginx/rtvi-vlm/v1/health/ready
curl -s -o /dev/null -w "RT-Embed %{http_code}\n" http://nginx/rtvi-embed/v1/ready
```

Each line ends in `200` once that service is ready. Anything else, such as `502`, usually means it is still starting. If it stays that way, the readiness check in onboarding shows whether the deployment failed.

## Sign in to the coding agent during onboarding

Onboarding has you sign in with `codex login --device-auth` in a terminal. The coding agent is then ready for
VSS Coding Agent Skills at the end of Part 1. Use the account named in the course prerequisites you were
given. The lab does not come with a coding-agent account, and nothing in Part 1 needs a key. If the site
refuses the device code, sign in at auth.openai.com in another browser tab. Open **Settings**, then
**Security**, and turn on **Enable device code authorization for Codex**. Then enter the code again. If the
sign-in still fails, raise your hand. Do not retry in a loop, and do not paste a key anywhere.

## VSS Coding Agent Skills is a page in Course Docs

The last step of Part 1 is not a notebook. Open the JupyterLab Launcher (**File > New Launcher**), click
**Course Docs**, then choose **VSS Coding Agent Skills**. The page opens in a JupyterLab tab, and you work in a
terminal next to it. There the coding agent adds the four traffic videos to VIOS, reports possible collisions
with clip links, and watches a live stream for a collision. It writes its files to `work/agent/`. If Course
Docs shows no pages, tell the instructor.

## The Part 1 assessment

The assessment is the part of Part 1 you do on your own. You get a new 30-second traffic clip. In seven short
tasks you add it to VIOS, read RT-CV's detections for it from Elasticsearch and Kafka, and caption it with
RT-VLM. You also find a car in it by its appearance, search the four traffic videos with RT-Embed, and work
out what captioning one camera for a day costs RT-VLM. Eight multiple-choice questions follow. You pass when tasks T1,
T2, T4 and T5 pass and at least 6 of the 8 answers are right. The Submit cell asks the grader to check your
work and shows each check. When it shows PASSED, click ASSESS TASK on the course page.

## Before lunch: save your work, stop this lab, then start Part 2

Part 2 runs on its own launch, a fresh machine with its own Home page. Nothing from this lab carries over, and
the course page runs only one lab at a time, so before lunch:

1. If you have not done so yet, click **ASSESS TASK** on the course page. It checks your Part 1 assessment in
   this lab, so do it before you stop the lab.
2. Save your open notebooks (**File > Save All**). Then, on `00_Home.ipynb`, run the cell under **Keep a copy
   of your work (Save HTMLs)** and download the zip it links. It holds your notebooks as HTML files. To keep
   another file, such as `dashboard/app.py` or a file in `work/agent/`, right-click it in the file browser and
   choose **Download**.
3. On the course page, click **STOP TASK** for Part 1. Part 2 cannot start while this lab runs.
4. Click **START** for Part 2. The new lab needs most of lunch to get ready, so start it before you go.
5. Go to lunch.

You sign in to the coding agent again in Part 2. Part 2 also asks for your own NGC Personal API Key in its
first step. If you do not have one yet, you can make one over lunch at [ngc.nvidia.com](https://ngc.nvidia.com).
Open **Account Settings**, then **Keys & Secrets**, and generate a Personal Key with **NGC Catalog** selected.
NGC shows the key only once, so keep it somewhere safe. The **Prerequisites** page in Course Docs shows the same
steps, with screenshots.

## What is in this folder

| Path | What it is |
|---|---|
| `00_Home.ipynb` | Home: the day with links, where you are, and Save HTMLs |
| `part1/` | the Part 1 notebooks in order (`00-onboarding` to `04-rt-embed-video-search`, then `assessment`) and the helpers they import. VSS Coding Agent Skills, the last step, is a page in Course Docs. |
| `part1/course_setup.py` | shared helpers for the service calls; every call checks the response and raises with the reason |
| `part1/labtools.py` | plots, frames and video previews; no service calls |
| `dashboard/app.py` | a starter web page for the optional dashboard task of VSS Coding Agent Skills. Run it with `python3 /dli/task/dashboard/app.py`, then open `http://<your-lab-host>/dashboard/`. The coding agent builds it out. |
| `data/traffic_short.mp4` | the 16-second clip of notebooks 2 and 3 |
| `data/search-videos/` | the four search videos, three minutes each, used from notebook 4 on. Their names (`normal`, `rain`, `fog`, `night`) give the weather or light, not what happens in them. |
| `search-videos.json` | the search videos' filenames, durations and VIOS timeline start |
| `data/search_sources.json`, `data/course_prep_status.json` | written at launch by `course-prep`: the VIOS sensor ids of the four search videos, and course-prep's progress |
| `data/course-docs/` | the Course Docs site that the Course Docs tile in the Launcher shows. It holds the VSS Coding Agent Skills page and `_static/vss-lab-context.txt`, the lab context the coding agent reads. |
| `work/` | files you create, for example `work/assessment/` in the assessment and `work/agent/` for the coding agent |
