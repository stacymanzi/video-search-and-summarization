# task1/lab/prep: launch-time preparation

`prepare_course.py` is the only script here. The lab image copies this
directory to `/dli/prep` (`lab/Dockerfile`: `COPY lab/prep /dli/prep`) and the
`course-prep` service in `task1/docker-compose.yml` runs
`python3 /dli/prep/prepare_course.py` once per VM (BUILD.md D12, P4, 5a
decisions 6 and 10; CONTRACTS.md section 6).

It prepares one thing: the search collection of notebook 4 (RT-Embed: Video
Search), the four three-minute traffic clips listed in
`lab/task/search-videos.json`. Notebook 4 and the Part 1 assessment (T5)
search these four clips. In VSS Coding Agent Skills, a Course Docs page, the
coding agent uploads its own copies of the same files under names that start
with `agent_`, and never deletes course-prep's.

Why `/dli/prep` and not `/dli/task/prep`: `/dli/task` is bind-mounted from
`lab/task` in development and replaced by the `task` named volume in
production, and either hides a script kept under it; the old location needed
an override-only bind mount of `lab/prep`. Outside `/dli/task` the script is
image content in every configuration, nothing bind-mounts it, and CI
(`.gitlab-ci.yml`, `checks:platform`) fails if the three-file compose merge
leaves any bind mount under `/dli`. A change here takes effect on the next
image build. `lab/prep` still ships in the launch tarball (`.launchignore`
rule P9 forbids `lab/**` patterns) and is inert on the VM.

The script imports `course_setup` from `/dli/task/part1`, which the image's
`PYTHONPATH` names (`/dli/task/part1:/dli/prep`); the explicit `sys.path`
insert in the script serves only the `COURSE_PREP_TASK_DIR` hook. The same
`PYTHONPATH` makes `import prepare_course` work in any lab kernel.

## `main()`: the launch run, in order

1. status file `RUNNING`, every clip `pending`;
2. bounded wait for the S3 loader's marker
   `/dli/task/data/.course-data-status.json` (cap `COURSE_DATA_TIMEOUT_S`,
   default 5400 s); the status file's `detail` says what it waits for, and a
   `FAILED` loader or the cap fails every clip with the reason;
3. bounded wait on `http://nginx/agent/health` (cap
   `COURSE_PREP_HEALTH_TIMEOUT_S`, default 5400 s; elapsed time and the
   deployer's status tail every two minutes); a deployer failure or the cap
   fails every clip with the first line of the reason, and the full reason,
   with the deployer's status tail, goes to `course_prep.log`;
4. ingest of the four search clips through `course_setup.ingest_file` on the
   2025-01-02 anchor from the manifest, registry written after every clip;
5. one shared wait until every clip's Cosmos-Embed1 records cover its timeline
   to within 10 s (cap `COURSE_PREP_EMBED_TIMEOUT_S`, default 1800 s), so `OK`
   means searchable;
6. status file `OK`, or `FAILED` with a non-zero exit.

## `prepare_search_collection()`: the instructor's by-hand path

For a devbox without the `course-prep` service (BUILD.md 5a decision 10). The
instructor-only cell at the end of notebook 4 (`05-instructor-prep`) imports
it; the instructor then calls it from a new cell:

```python
import prepare_course
prepare_course.prepare_search_collection(task_dir="/dli/task", manifest_name="search-videos.json")
```

It runs steps 3 to 6 through the same code path as `main()`
(`prepare_clips`), so the registry and the status file have one format
whichever path prepared the machine. It does not wait for the S3 loader: on a
devbox the clips are already in `/dli/task/data/search-videos/`. The status
file it writes replaces any earlier one. It waits for the embeddings like the
launch run does: about four minutes per three-minute clip for the uploads,
then the shared embedding wait. Call it from a new notebook cell or a
terminal, never from a learner's machine.

## Outputs

All under `/dli/task/data/` (the `course_data` volume):

| File | Read by | Contents |
|---|---|---|
| `course_prep_status.json` | The onboarding notebook (`part1/00-onboarding.ipynb`, section 0.4) and notebook 4 (cell `05-collection-02`), which raise unless `state == "OK"`. The course bar (`lab_home.py` through `course_ui.course_status`), which shows RUNNING or FAILED. | `{"state", "detail", "assets": {name: {sensor_id, state, error}}, "updated"}`. Asset states: `pending`, `ingested`, `ok`, `failed`. |
| `search_sources.json` | Notebook 4, the Part 1 assessment (cells `p1-setup` and `p1-t5-todo`), `dashboard/app.py` and `cleanup.py`. `course_setup.registry_entry` reads it too. The lab context of VSS Coding Agent Skills (`docs/_static/vss-lab-context.txt`) names it as the list of course-prep's videos that the coding agent must never change or delete. | Four entries keyed by filename: `{filename, duration_seconds, creation_time, sensor_id, completed}`. |
| `course_prep.log` | Instructors. `docker compose logs course-prep` has the same lines. | Appended on every run. When the stack does not come up, this file holds the full reason with the deployer's status tail. The status file keeps only the first line. |

It writes nothing else: no file under `/dli/task` outside `data/`, and no git
repository.

It never falls back. A missing clip, a failed upload, a stack that does not come
up within the cap or embeddings that do not complete end in `FAILED` and a
traceback in the service log. Re-running is safe: a registered sensor is reused
only while VIOS still lists it and RT-Embed still holds records for it. If its
records are gone, course-prep uploads the file again (VIOS names the new copy
`<stem>_2`) and updates the registry.

Run the full launch preparation by hand on a devbox (never on a learner VM)
with `docker compose run --rm course-prep`. `COURSE_PREP_TASK_DIR` is a test
hook that points a run at a scratch tree with its own `part1/course_setup.py`;
it is unset in the image.

Offline tests (they need `requests`): `python3 -I task1/tests/test_prepare_course.py`
(fake clock, no loader, no network).
