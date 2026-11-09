# task1/assessment: the grader

The course keeps V1's grading handshake with the LMS and runs the real checks in the `assessment`
container, outside the learner's kernel. There is one assessment, `part1`: the notebook
`task1/lab/task/part1/assessment.ipynb`. Its task cells write
`/dli/task/work/assessment/answers.json`; the grader reads that file and checks every answer again,
against VIOS, RT-Embed and Elasticsearch through `http://nginx` and against its own keys. Nothing in
the lab can write a result.

```
assessment/
  assessment_runner           V1's Go binary (github.com/NVDLI/assessment_runner), copied verbatim; port 8080
  tasktest.py                 what the runner executes: reads the markers the rubric lists, prints {"score","message"}
  grader/
    serve.py                  http.server on 8081: POST /grade/part1, GET /health
    common.py                 paths, the HTTP seam, schema validator, VIOS, Elasticsearch and RT-Embed helpers,
                              course-prep's registry, keys, markers
    grade_part1.py            the nine Part 1 checks
    rubric.json               every check id, its public text and the numbers the checks use (the lab image ships it)
    schemas/                  answers.schema.json (what the notebook writes), marker.schema.json (what serve.py writes)
    answer_keys/part1.json    the multiple-choice key, plain text (written with the assessment notebook)
    answer_keys/ground_truth/ search_videos.json, the labelled events of the four search clips, and its README
  fixtures/                   a fake world, a test key, a passing answers file and one folder per variant case
  tests/                      pytest, offline: every fixture case, tasktest.py, serve.py and the contracts
```

## 1. How the LMS calls the runner

The learner presses **ASSESS TASK** in the LMS. The platform sends the job to port **8080** of the
`assessment` container, where `assessment_runner` (V1's binary, unchanged) runs `./tasktest.py`
from `/dli/assessment` and returns its single JSON object. `tasktest.py` reads the rubric's
`assessments` list (today `["part1"]`), reads `/dli/assessment_results/<id>.json` for each, applies
the certificate rule `all_listed` (every listed assessment passes) and prints

```json
{"score": 100, "message": "Part 1 PASS. Congratulations, you passed the assessment. Your Part 1 result is recorded. Go to https://learn.nvidia.com/my-learning/ ..."}
```

or `score: 0` with one line per assessment and one line on what to do, such as

```
Part 1 FAIL: part1.search_matches_reference. Fix these checks in part1/assessment.ipynb and run its Submit cell again.
Not passed yet. Do what the line above says, then press ASSESS TASK again.
```

or `Part 1 NOT GRADED: there is no result yet. Open part1/assessment.ipynb and run its Submit cell.`
followed by the same second line.

The PASS message does not mention a certificate until the course settles whether it gives a
certificate or a badge. `tasktest.py` keeps the certificate sentence as `CERTIFICATE_MESSAGE`.

A marker counts as PASS only when it names its own assessment, carries every must-check of the
rubric with `passed: true`, and its own `passed` flag agrees. A hand-written `{"passed": true}` or
a V1-style `PASSED` file scores 0. Should-checks are reported in the marker and never named in a
FAIL line. `tasktest.py` imports nothing from `grader/`, so the LMS always gets its JSON, even when
the grader package is broken (the message then says "misconfigured").

`task1/entrypoint.assessment.sh` deletes every old marker (`/dli/assessment_results/*.json` and
`PASSED`), clears `PYTHONPATH`, starts `python3 grader/serve.py` and `./assessment_runner`, and exits
when either one dies, so compose shows the failure. The image's `PYTHONPATH` names
`/dli/task/part1`, which the lab can write, so a `json.py` planted there would otherwise run inside
`tasktest.py` and print any score. As a second line, `tasktest.py` and `serve.py`, when run as
scripts, drop every import path outside the interpreter's own directories before their first
import.

## 2. How the lab reaches serve.py

`serve.py` listens on **8081** inside the `assessment` container. The lab reaches it over the
compose network as `http://assessment:8081`; nothing is published to the host and nginx has no
route for it. The Submit cell of the assessment notebook is, in essence:

```python
import requests, course_ui
r = requests.post("http://assessment:8081/grade/part1", timeout=300)
r.raise_for_status()
course_ui.results(r.json(), "Part 1")
```

The POST body is ignored: the submission is `answers.json` on disk. `GET /health` reports whether
the answers file, the ground truth, the answer key and a writable results directory are present,
which markers exist, and whether course-prep's registry (`/dli/task/data/search_sources.json`)
exists yet. It answers 503 when the answer key or the ground truth is missing (the image is broken)
or the results directory is not writable. A missing registry is reported but keeps the status `ok`,
because course-prep writes it after launch. One grading run at a time: a second POST during a run
gets 409. A grader exception still produces a marker, with every check failed and the exception in
the detail, and the traceback goes to the container log.

The `assessment_results` volume is mounted read-write in the assessment container and read-only in
the lab (`/dli/assessment_results`), so a learner can read a marker but not write one. A marker is
`{"assessment", "passed", "checks": [{"id", "must", "passed", "detail"}], "graded_at", "rubric_version"}`.

What the assessment container needs: this directory at `/dli/assessment`; `/dli/task` mounted as
the lab sees it (the answers file is under `work/assessment/`); `/dli/task/data` read-only (the
`course_data` volume, for course-prep's registry of the four search clips); network access to
`nginx`. The grader uses the standard library only. Paths can be overridden for development with
`GRADER_ANSWERS_FILE`, `GRADER_RESULTS_DIR`, `GRADER_ANSWER_KEYS_DIR`, `GRADER_GROUND_TRUTH_FILE`,
`GRADER_REGISTRY_FILE` and `GRADER_PORT`. `python3 -I grader/serve.py --grade part1` grades once
from a terminal and prints the summary.

## 3. What the Part 1 grader checks

The clip is the rubric's `clip_stem`, `rain_check`: the notebook cuts it from
`/dli/task/data/search-videos/rain_eval.mp4` (30 s from 15 s) into
`/dli/task/work/assessment/rain_check.mp4`, so no earlier step has used that stem. The grader never
takes the stem from `answers.json`; answers for another clip fail every task check.

The notebook runs its own check after every task (`part1/quiz.py`). The grader repeats them as a
second line, with the same rules and the same requests, so a task the notebook passed fails here
only when the services changed in between (for example, the clip was deleted). The one exception is
T6 when `answers.json` does not name the query frame (see the table).

| Check | Task | Level | Passes when |
|---|---|---|---|
| `part1.video_in_vios` | T1 | must | VIOS lists `t1.vios_sensor_id`, its VIOS name is `t1.vios_name` and matches `rain_check` or `rain_check_<n>`, and `GET .../storage/<id>/timelines` returns at least one segment |
| `part1.detections_indexed` | T2 | must | `t2.frames` equals `_count` on `mdx-raw-2025-01-01` with `term sensorId.keyword = rain_check` (or with notebook 2's stem-or-`rain_check_<n>` filter), and `t2.top_type` is the top bucket (ties accept either) of the nested `terms` aggregation on `objects.type.keyword` |
| `part1.kafka_decoded` | T3 | should | `t3.sensor_id` is `rain_check`, and `mdx-raw-2025-01-01` holds a `rain_check` frame with `id` (or `id.keyword`) equal to `t3.frame_id` whose `timestamp` is within `kafka_timestamp_slack_s` (0.5 s) of `t3.timestamp` |
| `part1.captions_indexed` | T4 | must | `t4.chunks_10` is 3 or 4 (`chunks_at_10_s`; a fourth chunk is a short one at the end of the cut), `t4.chunks_0` is 1, `t4.frame_counts_8` has 3 or 4 entries, 8 for each full chunk and 1 to 8 for a short fourth chunk (all `null` fails with "tell the instructor": RT-VLM did not report `frame_count`), `t4.file_id` holds only letters, digits, `-` and `_`, and `_count` on `default_<file id with _, lower case>` is at least 1 |
| `part1.search_matches_reference` | T5 | must | `t5.file` is one of the rubric's `search_files` (with or without `.mp4`), and repeating the learner's search returns that file first, with a start and an end within `search_slack_s` (0.1 s) of `t5.start_s` and `t5.end_s`. The search is the notebook's: `t5.query` through RT-Embed's `generate_text_embeddings`, then the `course_setup.knn_search` request on `mdx-embed-filtered-2025-01-01` (k 5, filtered to the four sensor ids in course-prep's registry). Times are seconds from `2025-01-01T00:00:00.000Z` |
| `part1.search_hit_overlaps_collision` | T5 | should | `0 <= t5.start_s < t5.end_s <= duration + window_slack_s` (1 s), and the window overlaps a labelled collision of that clip in `answer_keys/ground_truth/search_videos.json`. No service is called |
| `part1.appearance_match` | T6 | should | `t6.answer_object_id` is the object the notebook's T6 request ranks first: a nested kNN on `objects.embedding.vector` from the query object, filtered to `rain_check` or `rain_check_<n>` with the query frame's `timestamp` excluded, read from `inner_hits` (`_nested.offset` into `_source.objects`). The query frame is `t6.query_frame_time` or `t6.query_frame_id` when given, else the earliest frame that holds the object; without a frame, the query object's own track id is also accepted |
| `part1.throughput_estimate` | T7 | should | `t7.requests_per_day` is 8,640 (`seconds_per_day` / `chunk_duration_s`), and `t7.busy_seconds_per_day` is within `throughput_tolerance` (15 percent) of `8,640 x t7.latency_ms / 1000`, the RT-VLM seconds one camera takes per day if requests run one at a time |
| `part1.questions` | MCQ | must | at least `min_correct` (6) of the 8 answers in `questions` match `answer_keys/part1.json`, letters compared without case |

The pass rule is `all_must`: T1, T2, T4, the T5 search and the questions decide. T5 is graded on the
search itself because no measurement shows how often Cosmos-Embed1's top window over these 180 s
clips is a labelled collision, so a correct search can miss one. Whether it does is reported
(`part1.search_hit_overlaps_collision`).

The numbers the checks use (3 or 4, 1, 8, the slacks, the tolerance, 86,400 and 10) are in
`rubric.json` `part1.thresholds`, and the public texts quote them; `tests/test_contracts.py` keeps
the two in step. `part1/quiz.py` repeats the same numbers for the notebook's own checks, so change
both together.

Every live check fails closed: when a service does not answer, the check fails with a detail that
starts "A service did not answer, so this check cannot pass yet" and asks the learner to wait and
submit again. A missing task in `answers.json` fails only that task's checks. The details tell the
learner what to do next. They show the learner's own live data (sensor names, frame counts, the top
hit of their own search) but never a multiple-choice letter, the top object type or a labelled
collision window.

`answers.json`, as the task cells of part1/assessment.ipynb write it (each cell merges its own key):

```json
{"clip_stem": "rain_check",
 "t1": {"vios_sensor_id": "...", "vios_name": "rain_check_1"},
 "t2": {"frames": 0, "top_type": "car"},
 "t3": {"sensor_id": "rain_check", "frame_id": "...", "timestamp": "..."},
 "t4": {"file_id": "...", "chunks_10": 3, "chunks_0": 1, "frame_counts_8": [8, 8, 8]},
 "t5": {"query": "...", "file": "rain_eval.mp4", "start_s": 20.0, "end_s": 25.0},
 "t6": {"query_object_id": "...", "answer_object_id": "...", "query_frame_time": "..."},
 "t7": {"latency_ms": 0.0, "requests_per_day": 8640, "busy_seconds_per_day": 0.0},
 "questions": {"p1-q1": "B"}}
```

`t6.query_frame_time` or `t6.query_frame_id` (the frame the query object was taken from) is
optional; when the notebook writes one, the grader repeats the learner's T6 search exactly.
`t7.requests_per_day` may be written as `8640.0`, which is what `86400 / 10` gives in Python.
`schemas/answers.schema.json` is the machine-readable form.

## 4. Keys, ground truth and the registry

`answer_keys/part1.json` is the plain-text multiple-choice key,
`{"version": 1, "questions": {"p1-q1": "B", ...}, "min_correct": 6}` (an `explanations` map may sit
beside it). It is written with the assessment notebook; the hashed copy the notebook uses for
instant feedback is `task1/lab/task/part1/assessment_key.json`, generated by
`task1/scripts/make_quiz_key.py`. The key never goes into `rubric.json`, because the lab image
copies that file (`task1/lab/Dockerfile`).

`answer_keys/ground_truth/search_videos.json` is a byte-identical copy of the labels learners read
at `/dli/task/data/ground_truth/search_videos.json`. The grader keeps its own copy because the lab
can write to `/dli/task/data`. See `answer_keys/ground_truth/README.md` for its source.

Both ship only in the assessment image: `.launchignore` keeps `assessment/*` out of the launch
tarball, and the deployment compose mounts this directory only into the `assessment` service.

The registry, `/dli/task/data/search_sources.json`, is written by course-prep at launch:
`{<filename>: {"filename", "duration_seconds", "creation_time", "sensor_id", "completed"}}`. The T5
cell reads it to find the VIOS sensor ids of the four search clips, and the grader reads the same
file, so both search the same sensors.

## 5. Development

```bash
python3 -I -B task1/assessment/fixtures/make_fixtures.py     # after changing the fixture story
python3 -I -B -m pytest task1/assessment/tests -q -p no:cacheprovider
python3 -I -B -c "import sys; [compile(open(p).read(), p, 'exec') for p in sys.argv[1:]]" task1/assessment/tasktest.py task1/assessment/grader/*.py
```

The tests need pytest; two contract tests also load `task1/lab/task/part1/course_setup.py` (which
needs `requests`) and `quiz.py`, and skip when those files are not there. `fixtures/README.md`
describes the fake world and the cases. `tests/test_contracts.py` fails when the fixtures drift from
`make_fixtures.py`, when the rubric drifts from the check list in tests/test_contracts.py, when the
ground truth changes without its hash, or when the notebook's T5 or T6 request differs from the
grader's.

## 6. What Dana must confirm before the first production launch

1. The LMS still drives `assessment_runner` on 8080 with the `tasktest.py` contract (one JSON
   object, `score` 100 or 0), and the canonical binary is the one in V1 (`c-fx-17-v1`, built
   2026-09-15); whether the runner invokes `./tasktest.py` directly (shebang, executable bit set
   here) or through `python3`, and whether it passes its own environment on to `tasktest.py`.
2. The runner's timeout for `tasktest.py`. It reads one small file and returns in milliseconds.
3. LMS semantics for a `score` of 0 followed by 100 (retries): the learner can press ASSESS TASK
   any number of times; the course record must be updated on the first 100.
4. How CI builds and names the assessment image (`c-fx-17-v2-task1-assessment:build`), and that
   the deployment compose mounts the `task` volume and the `assessment_results` volume into it.
5. That the assessment container is on the same compose network as `nginx`, `lab` and `dind`, and
   that port 8081 may stay unpublished.
6. That `8080:8080` is published on the course VM in production and nothing else from this
   container.

## 7. Verify on a devbox

Nothing below has run against a live stack; the offline tests run the same requests against
`tests/fakes.py`.

| Where | What to check |
|---|---|
| `common.vios_timeline` | `GET /vios/vst/api/v1/storage/<sensor>/timelines` on 3.2.1 returns a JSON list of `{"startTime", "endTime"}`; any other shape is treated as a service error. |
| `part1.detections_indexed` | `_count` on `mdx-raw-2025-01-01` with `term sensorId.keyword = rain_check` equals the count the notebook's T2 cell reads, once RT-CV has finished the 30 s clip; the nested `terms` aggregation on `objects.type.keyword` returns buckets. |
| `part1.kafka_decoded` | `mdx-raw` records keep `Frame.id` as a root field `id` (searchable as `id` or `id.keyword`), and their `timestamp` equals the decoded `Frame.timestamp` within 0.5 s. |
| `part1.captions_indexed` | Record whether a 30 s registration gives 3 or 4 chunks at `chunk_duration` 10, and what `frame_count` a short last chunk reports (if it reports fewer than 8, require 8 on the first three chunks only); `num_frames_per_second_or_fixed_frames_chunk=8` with `use_fps_for_chunking=False` makes the other chunks report `frame_count` 8 on 3.2.1; the captions land in `default_<file id with _>`. |
| `part1.search_matches_reference` | The grader's embedding and kNN reproduce the notebook's top hit for the same query (the same `generate_text_embeddings` vector twice, the same top window from `mdx-embed-filtered-2025-01-01`). If approximate-search jitter shows, accept a window among the reference top 3. |
| `part1.appearance_match` | A nested `knn` on `objects.embedding.vector` with the notebook's filter (stem or `stem_<n>`, the query frame's `timestamp` excluded) works on `mdx-raw` (3.2.1); `inner_hits` returns `_nested.offset` into `_source.objects` when `_source` excludes `objects.embedding`; a `term` on `timestamp` finds the query frame; a nested `term` on `objects.id.keyword` finds a track; note the similarity the `mdx-raw` mapping sets for `objects.embedding.vector`. |
| Submit cell | Wall time of one grading run from the assessment container (it now includes one RT-Embed call), against the notebook's 300 s request timeout. |
| Mounts | `/dli/task/work/assessment/answers.json`, written in the lab, and `/dli/task/data/search_sources.json` are readable from the assessment container in production. |
| Import paths | `docker-compose exec assessment /dli/assessment/tasktest.py` prints one JSON line (the interpreter keeps its standard library under its own prefix); `/proc/<serve.py pid>/environ` has no `PYTHONPATH`; the runner starts `tasktest.py` with the entrypoint's environment. |
