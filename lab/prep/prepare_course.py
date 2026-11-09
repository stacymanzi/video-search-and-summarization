#!/usr/bin/env python3
"""Launch-time preparation for course 1, run once by the `course-prep` service.

Location: /dli/prep/prepare_course.py in the lab image (lab/Dockerfile
`COPY lab/prep /dli/prep`; BUILD.md 5a decision 6), deliberately outside
/dli/task. /dli/task is a bind mount in development and the `task` named
volume in production, and either hides a script kept under it, which used to
cost an override-only bind mount. The compose entrypoint is
`python3 /dli/prep/prepare_course.py`. course_setup comes from /dli/task/part1
through the image's PYTHONPATH; the insert below only serves the
COURSE_PREP_TASK_DIR hook.

What it prepares: the search collection of notebook 4 (RT-Embed: Video
Search), the four clips listed in /dli/task/search-videos.json. Contract:
CONTRACTS.md section 6. `main()` does, in order:

  1. Write /dli/task/data/course_prep_status.json with state RUNNING and every
     clip pending.
  2. Wait for the S3 loader's marker /dli/task/data/.course-data-status.json
     to read OK (cap COURSE_DATA_TIMEOUT_S), with the status file's `detail`
     saying "waiting for S3 course data". Compose does not hold this service
     until s3_data_loader exits (production launch fix 3, after tag v0.0.4).
     A FAILED loader or the cap marks every clip failed with the reason.
  3. Wait for http://nginx/agent/health, bounded, printing elapsed time and the
     deployer's status tail. A deployer failure or the cap marks every clip
     failed with the first line of the reason. The full reason, with the
     deployer's status tail, goes to /dli/task/data/course_prep.log.
  4. Ingest the four search clips through course_setup.ingest_file on their
     VIOS anchor from the manifest (2025-01-02), writing the registry
     /dli/task/data/search_sources.json after every clip. A clip the
     registry already names is reused only while VIOS lists its sensor and
     RT-Embed's index still holds records for it (see ingest()).
  5. Wait until every clip's Cosmos-Embed1 records cover its timeline, so that
     OK means "searchable", not "accepted for processing".
  6. Write the status file with state OK, or FAILED and a non-zero exit.

Second entry point (BUILD.md 5a decision 10): `prepare_search_collection()`,
the instructor's by-hand path for a devbox without the service. The
instructor-only cell at the end of notebook 4 (`05-instructor-prep`) imports
this module so the instructor can call this function. It runs steps 3 to 6
through the same code as `main()`, so the registry and the status file have one
format whichever path prepared the machine. Importable from any lab kernel as
`import prepare_course` (/dli/prep is on the image's PYTHONPATH).

Every step runs or raises. There is no prepared-output mode and no banner that
lets anyone continue past a failure: the onboarding notebook
(part1/00-onboarding.ipynb) and notebook 4 read the status file and raise
until it says OK. Re-running after a failure is safe: a registry entry whose
sensor is still in VIOS and still has Cosmos-Embed1 records is reused, never
uploaded again. When Elasticsearch has deleted those records (its index
lifecycle policy deletes the mdx-* indices after a set age), the clip is
uploaded again as a new VIOS source and the registry names the new sensor.
"""
import json
import pathlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_TASK_DIR = "/dli/task"
SEARCH_MANIFEST_NAME = "search-videos.json"     # stays as in Berlin (CONTRACTS.md section 7)
# COURSE_PREP_TASK_DIR is a test hook only: it points a run at a scratch tree
# with its own part1/course_setup.py. In the image it is unset.
TASK_DIR = os.environ.get("COURSE_PREP_TASK_DIR", DEFAULT_TASK_DIR)

HEALTH_URL = "http://nginx/agent/health"
HEALTH_TIMEOUT_S = int(os.environ.get("COURSE_PREP_HEALTH_TIMEOUT_S", "5400"))
HEALTH_EVERY_S = 15          # poll interval
HEALTH_REPORT_EVERY_S = 120  # how often the deployer's status tail is printed while waiting
EMBED_TIMEOUT_S = int(os.environ.get("COURSE_PREP_EMBED_TIMEOUT_S", "1800"))
EMBED_EVERY_S = 15
EMBED_TAIL_SLACK_S = 10      # notebook 4's rule: covered to within 10 s of the clip's end
# Written by task1/load-course-data.py in the s3_data_loader service: RUNNING, then OK or FAILED.
COURSE_DATA_MARKER_NAME = ".course-data-status.json"
COURSE_DATA_TIMEOUT_S = int(os.environ.get("COURSE_DATA_TIMEOUT_S", "5400"))
COURSE_DATA_EVERY_S = 10
COURSE_DATA_REPORT_EVERY_S = 60

sys.path.insert(0, str(Path(TASK_DIR) / "part1"))
import course_setup  # noqa: E402  (shared helper, CONTRACTS.md section 2)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


class Tree:
    """Every path one preparation run reads or writes, under one task directory."""

    def __init__(self, task_dir=DEFAULT_TASK_DIR, manifest_name=SEARCH_MANIFEST_NAME):
        self.task = Path(task_dir)
        self.data = self.task / "data"
        self.search_manifest = self.task / manifest_name
        self.registry = self.data / "search_sources.json"
        self.status = self.data / "course_prep_status.json"
        self.log_file = self.data / "course_prep.log"

    def log(self, message: str) -> None:
        """Print one timestamped line and append it to data/course_prep.log (learner-readable)."""
        line = f"[course-prep {utc_now()}] {message}"
        print(line, flush=True)
        with self.log_file.open("a") as handle:
            handle.write(line + "\n")


class Status:
    """The per-asset status file that the onboarding notebook and notebook 4 read (CONTRACTS.md section 6).

    `{"state": "RUNNING|OK|FAILED", "detail", "assets": {name: {sensor_id, state, error}}, "updated"}`
    with asset states pending -> ingested -> ok | failed. `detail` is what the
    run is waiting on (for example "waiting for S3 course data (120 s)"), or
    null. Written RUNNING on construction, rewritten atomically on every
    change. Both entry points use this class, so the two paths cannot drift
    apart in format.
    """

    def __init__(self, path: Path, names):
        self.path = path
        self.payload = {"state": "RUNNING", "detail": None,
                        "assets": {name: {"sensor_id": None, "state": "pending", "error": None} for name in names},
                        "updated": utc_now()}
        self.flush()

    def asset(self, name: str, state: str, sensor_id=None, error=None) -> None:
        self.payload["assets"][name] = {"sensor_id": sensor_id, "state": state, "error": error}
        self.flush()

    def detail(self, message) -> None:
        self.payload["detail"] = message
        self.flush()

    def fail_all(self, message: str) -> None:
        """Every asset not yet ok is failed with `message`, and the run is FAILED (the onboarding notebook prints it per clip)."""
        for name, asset in self.payload["assets"].items():
            if asset["state"] != "ok":
                self.payload["assets"][name] = {"sensor_id": asset["sensor_id"], "state": "failed", "error": message}
        self.payload["detail"] = message
        self.finish("FAILED")

    def finish(self, state: str) -> None:
        self.payload["state"] = state
        self.flush()

    def flush(self) -> None:
        self.payload["updated"] = utc_now()
        write_json_atomic(self.path, self.payload)


def search_assets(tree: Tree) -> list:
    """The four search clips, in manifest order (notebook 4's collection). The manifest gives each one's
    duration and VIOS anchor (creation_time)."""
    if not tree.search_manifest.is_file():
        raise FileNotFoundError(f"{tree.search_manifest} is missing; it ships in the lab image")
    manifest = json.loads(tree.search_manifest.read_text())
    return [{"filename": item["filename"],
             "path": tree.data / "search-videos" / item["filename"],
             "duration_seconds": item["duration_seconds"],
             "creation_time": item["creation_time"]} for item in manifest]


# --- S3 course data -------------------------------------------------------------

def wait_for_course_data(marker: Path, timeout_s: int, report, clock=time.monotonic, sleep=time.sleep,
                         every_s=COURSE_DATA_EVERY_S, report_every_s=COURSE_DATA_REPORT_EVERY_S) -> float:
    """Block until the S3 loader's marker reads OK; return the seconds waited.

    Pure apart from what is injected (the marker path, the clock, sleep and the
    progress callback), so the tests drive it without a loader. Absent or
    RUNNING keeps waiting and calls report(elapsed) every report_every_s.
    FAILED raises RuntimeError("S3 course data loader failed: <error>"); the
    cap raises TimeoutError("S3 course data not ready after <cap>s"). A marker
    that is not a JSON object with a known state raises: the loader writes it
    atomically, so a half-written file is not a state to wait through.
    """
    if timeout_s <= 0:
        raise ValueError(f"COURSE_DATA_TIMEOUT_S must be a positive number of seconds, got {timeout_s}")
    started = clock()
    next_report = 0.0
    while True:
        elapsed = clock() - started
        if marker.exists():
            try:
                payload = json.loads(marker.read_text())
            except ValueError as error:
                raise RuntimeError(f"S3 course data marker {marker} is not JSON: {error}") from error
            state = payload.get("state") if isinstance(payload, dict) else None
            if state == "OK":
                return elapsed
            if state == "FAILED":
                raise RuntimeError(f"S3 course data loader failed: {payload.get('error') or 'no error recorded'}")
            if state != "RUNNING":
                raise RuntimeError(f"S3 course data marker {marker} has an unknown state {state!r}")
        if elapsed >= timeout_s:
            raise TimeoutError(f"S3 course data not ready after {timeout_s}s")
        if elapsed >= next_report:
            report(elapsed)
            next_report += report_every_s
        sleep(every_s)


def await_course_data(tree: Tree, status: Status) -> float:
    """Step 2: wait_for_course_data with progress in the status file and the log; on failure every asset fails with the reason."""
    def report(elapsed):
        message = f"waiting for S3 course data ({elapsed:.0f} s, cap {COURSE_DATA_TIMEOUT_S} s)"
        status.detail(message)
        tree.log(message)

    try:
        waited = wait_for_course_data(tree.data / COURSE_DATA_MARKER_NAME, COURSE_DATA_TIMEOUT_S, report)
    except (RuntimeError, TimeoutError) as error:
        tree.log(f"FAILED: {error}")
        status.fail_all(str(error))
        raise
    status.detail(None)
    tree.log(f"S3 course data ready after {waited:.0f} s")
    return waited


# --- the stack -----------------------------------------------------------------

def deployer_status_line() -> str:
    """The current deployer state: the last line of the seed-status file without its timestamp, or empty."""
    path = pathlib.Path(course_setup.SEED_STATUS_FILE)
    if not path.exists():
        return ""
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1].strip()
    return last.split(" ", 1)[1] if " " in last else last


def wait_for_agent(tree: Tree) -> float:
    """Block until the VSS agent service answers /health; raise TimeoutError after HEALTH_TIMEOUT_S.

    Both errors put the reason on their first line and the deployer's status tail on the lines after it,
    so await_agent can keep the status file to one line per clip.
    """
    started = time.monotonic()
    last_report = -HEALTH_REPORT_EVERY_S
    last_reason = "not polled yet"
    while True:
        elapsed = time.monotonic() - started
        try:
            response = requests.get(HEALTH_URL, timeout=10)
            if response.status_code == 200:
                tree.log(f"{HEALTH_URL} answered 200 after {elapsed:.0f} s")
                return elapsed
            last_reason = f"HTTP {response.status_code}: {response.text[:120]!r}"
        except requests.RequestException as error:
            last_reason = type(error).__name__
        status_line = deployer_status_line()
        if status_line.startswith("FAILED"):
            raise RuntimeError(
                f"the deployer reported a failure after {elapsed:.0f} s, so the stack will not come up: "
                f"{status_line}\nDeployer status tail:\n{course_setup.status_tail(10)}")
        if elapsed - last_report >= HEALTH_REPORT_EVERY_S:
            last_report = elapsed
            tree.log(f"waiting for {HEALTH_URL} ({elapsed:.0f} s elapsed, cap {HEALTH_TIMEOUT_S} s; last: {last_reason})")
            tree.log("deployer status tail:\n" + course_setup.status_tail(5))
        if elapsed >= HEALTH_TIMEOUT_S:
            raise TimeoutError(
                f"{HEALTH_URL} did not answer 200 within {HEALTH_TIMEOUT_S} s (last: {last_reason}).\n"
                f"Deployer status tail:\n{course_setup.status_tail(10)}")
        time.sleep(HEALTH_EVERY_S)


def await_agent(tree: Tree, status: Status) -> float:
    """Step 3: wait_for_agent. On failure every asset fails, so the status file never stays RUNNING after this
    service has stopped.

    The status file gets only the first line of the reason and the path of the log: the onboarding notebook
    prints `detail` and every clip's `error`, and the course bar shows `detail` as a tooltip. The full reason,
    with the deployer's status tail, goes to the log.
    """
    try:
        return wait_for_agent(tree)
    except (RuntimeError, TimeoutError) as error:
        tree.log(f"FAILED: {error}")
        reason = str(error).split("\n", 1)[0].rstrip()
        if not reason.endswith("."):
            reason += "."
        status.fail_all(f"{reason} The deployer's status tail is in {tree.log_file}.")
        raise


def load_registry(tree: Tree) -> dict:
    if not tree.registry.exists():
        return {}
    registry = json.loads(tree.registry.read_text())
    if not isinstance(registry, dict):
        raise RuntimeError(f"{tree.registry} is not a JSON object; remove it and rerun")
    return registry


def ingest(tree: Tree, asset: dict, registry: dict) -> str:
    """Ingest one clip unless the registry names a sensor that is still searchable. Returns the sensor id.

    A completed registry entry is reused only when both of these hold:

    * VIOS lists its sensor id. The match is on the id, as in notebook 4 (cell
      05-collection-02), not on find_sensor(stem): after a re-ingest VIOS lists
      the old source (<stem>_1) next to the new one (<stem>_2), and find_sensor
      returns only the first one in VIOS's list order.
    * RT-Embed's index still holds records for it, by the query that
      embedding_progress() sends. Elasticsearch deletes the mdx-* indices under
      its index lifecycle policy (VSS sets the age from ELASTICSEARCH_ILM_MIN_AGE,
      counted from the index's creation: 4h unless something sets it, 48h in
      v3.2.1's search profile, 30d from task1's deploy since 2026-11-04). So a
      later run can find the sensor in VIOS with its records gone: HTTP 404 (no
      index) or 0 records (a later upload created the index again). Waiting does
      not bring them back.

    Otherwise the file is ingested again (VIOS names the copy <stem>_2), the old
    source stays in VIOS, and the registry entry names the new sensor, so the
    embedding wait, notebook 4, the Part 1 assessment and its grader use it.
    """
    name, path = asset["filename"], asset["path"]
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing; check task1/data_sources and the S3 upload")
    entry = registry.get(name)
    if entry and entry.get("completed") and entry.get("sensor_id"):
        sensor_id = entry["sensor_id"]
        if not any(isinstance(sensor, dict) and sensor.get("sensorId") == sensor_id
                   for sensor in course_setup.sensors()):
            tree.log(f"{name}: registry names {sensor_id} but VIOS does not have it; ingesting again")
        else:
            data = embedding_search(sensor_id)
            if data is not None and data["hits"]["total"]["value"]:
                tree.log(f"{name}: already ingested as {sensor_id}; reusing")
                return sensor_id
            found = (f"HTTP 404: no index {course_setup.EMBED_INDEX}" if data is None
                     else f"0 records in {course_setup.EMBED_INDEX}")
            tree.log(f"{name}: VIOS still has {sensor_id}, but the search index has no records for it ({found}). "
                     "Elasticsearch most likely deleted them under its index lifecycle policy. "
                     "Ingesting the file again as a new VIOS source; the old one stays.")
    duration = asset["duration_seconds"]
    tree.log(f"{name}: ingesting ({duration:.1f} s of video, VIOS timeline from {asset['creation_time']}); "
             f"upload plus /complete usually takes one to three minutes")
    started = time.monotonic()
    sensor_id = course_setup.ingest_file(str(path), name, asset["creation_time"])
    registry[name] = {"filename": name,
                      "duration_seconds": duration,
                      "creation_time": asset["creation_time"],
                      "sensor_id": sensor_id,
                      "completed": True}
    write_json_atomic(tree.registry, registry)
    tree.log(f"{name}: sensor {sensor_id} after {time.monotonic() - started:.0f} s")
    return sensor_id


def embedding_search(sensor_id: str):
    """POST the records query for one source; the decoded answer, or None when the index does not exist (HTTP 404).

    The same query notebook 4 teaches: records in mdx-embed-filtered-2025-01-01
    carry the VIOS UUID in sensor.id.keyword and a date field `end`. Any status
    other than 200 and 404 raises. embedding_progress() and ingest() send it.
    """
    url = course_setup.ES_URL + "/" + course_setup.EMBED_INDEX + "/_search"
    response = requests.post(url, timeout=30, json={
        "size": 0,
        "query": {"term": {"sensor.id.keyword": sensor_id}},
        "aggs": {"last_end": {"max": {"field": "end"}}},
    })
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(f"POST {url} -> {response.status_code}: {response.text[:300]}")
    return response.json()


def embedding_progress(sensor_id: str) -> tuple:
    """(Cosmos-Embed1 records, seconds of timeline covered) for one source; (0, 0.0) until its index exists.

    The same query notebook 4 teaches (embedding_search): records in
    mdx-embed-filtered-2025-01-01 carry the VIOS UUID in sensor.id.keyword and
    a date field `end`.
    """
    data = embedding_search(sensor_id)
    if data is None:          # Logstash has not created the index yet
        return 0, 0.0
    total = data["hits"]["total"]["value"]
    last = data["aggregations"]["last_end"]["value"]
    # VERIFY ON DEVBOX: `end` is a date field, so the max aggregation returns epoch milliseconds.
    anchor = datetime.fromisoformat(course_setup.EMBEDDING_ANCHOR.replace("Z", "+00:00")).timestamp()
    return total, (max(0.0, last / 1000 - anchor) if last is not None else 0.0)


class EmbeddingsIncomplete(TimeoutError):
    """Raised by wait_for_embeddings with the names still pending in .pending."""

    def __init__(self, message: str, pending: list):
        super().__init__(message)
        self.pending = list(pending)


def wait_for_embeddings(tree: Tree, registry: dict, names: list) -> dict:
    """Block until every named clip's embeddings cover its timeline. Returns {name: (records, seconds)}.

    Raises EmbeddingsIncomplete (a TimeoutError) naming the clips still pending
    after EMBED_TIMEOUT_S. RT-Embed processes the clips concurrently after
    /complete, so this is one shared wait, not one per clip.
    """
    started = time.monotonic()
    while True:
        progress = {name: embedding_progress(registry[name]["sensor_id"]) for name in names}
        pending = [name for name, (count, seconds) in progress.items()
                   if not count or seconds < registry[name]["duration_seconds"] - EMBED_TAIL_SLACK_S]
        elapsed = time.monotonic() - started
        if not pending:
            tree.log(f"embeddings complete for {len(names)} clips after {elapsed:.0f} s: "
                     + ", ".join(f"{name} {count} records to {seconds:.0f} s" for name, (count, seconds) in progress.items()))
            return progress
        if elapsed >= EMBED_TIMEOUT_S:
            raise EmbeddingsIncomplete(
                f"embeddings incomplete for {pending} after {elapsed:.0f} s (cap {EMBED_TIMEOUT_S} s). "
                f"Check RT-Embed and Logstash: {course_setup.status_tail(5)}", pending)
        tree.log(f"waiting for embeddings ({elapsed:.0f} s elapsed, cap {EMBED_TIMEOUT_S} s): "
                 + ", ".join(f"{name} {count} records to {seconds:.0f} s of {registry[name]['duration_seconds']:.0f}"
                             for name, (count, seconds) in progress.items() if name in pending))
        time.sleep(EMBED_EVERY_S)


def prepare_clips(tree: Tree, items: list, status: Status) -> dict:
    """Steps 4 to 6 for `items`: ingest each, one shared embedding wait, per-asset states, final state.

    Returns the registry. Any clip that fails is recorded as `failed` with the
    error, the rest continue, and the run ends with the status file at FAILED
    and a RuntimeError naming the failures; OK is written only when every clip
    is searchable. Shared by main() and prepare_search_collection().
    """
    registry = load_registry(tree)
    failures = []
    ingested = []
    for item in items:
        name = item["filename"]
        try:
            sensor_id = ingest(tree, item, registry)
        except Exception as error:  # recorded per asset; the run still ends FAILED below
            message = f"{type(error).__name__}: {error}"
            tree.log(f"{name}: FAILED ({message})")
            status.asset(name, "failed", error=message)
            failures.append(name)
            continue
        status.asset(name, "ingested", sensor_id=sensor_id)
        ingested.append(name)

    if ingested:
        try:
            wait_for_embeddings(tree, registry, ingested)
        except EmbeddingsIncomplete as error:
            pending = error.pending
            for name in pending:
                status.asset(name, "failed", sensor_id=registry[name]["sensor_id"],
                             error=f"TimeoutError: embeddings incomplete after {EMBED_TIMEOUT_S} s")
                failures.append(name)
            ingested = [name for name in ingested if name not in pending]
            tree.log(f"FAILED: {error}")
    for name in ingested:
        status.asset(name, "ok", sensor_id=registry[name]["sensor_id"])

    if failures:
        status.finish("FAILED")
        raise RuntimeError(f"course preparation failed for {', '.join(failures)}; see {tree.status} and {tree.log_file}")
    status.finish("OK")
    return registry


# --- entry points -------------------------------------------------------------------

def prepare_search_collection(task_dir=DEFAULT_TASK_DIR, manifest_name=SEARCH_MANIFEST_NAME) -> dict:
    """Instructor path (notebook 4, cell `05-instructor-prep`): prepare the search collection by hand.

    Ingests every clip in <task_dir>/<manifest_name> that is not searchable
    yet (not registered, gone from VIOS, or without records in RT-Embed's
    index: see ingest()), waits until their Cosmos-Embed1 records cover the
    timelines, and writes the same two files the launch run writes:
    data/search_sources.json (`{filename: {filename, duration_seconds,
    creation_time, sensor_id, completed}}`) and data/course_prep_status.json
    (state OK when every manifest clip is searchable; FAILED, and a
    RuntimeError, otherwise). The status file it writes replaces any earlier
    one. It does not wait for the S3 loader: on a devbox the clips are already
    in data/search-videos/. Returns the registry restricted to the manifest.
    Raises on a missing clip or any HTTP error; nothing is retried here.
    """
    tree = Tree(task_dir, manifest_name)
    tree.data.mkdir(parents=True, exist_ok=True)
    tree.log("search collection prepared by hand: prepare_search_collection()")
    items = search_assets(tree)
    status = Status(tree.status, [item["filename"] for item in items])
    tree.log(f"preparing {len(items)} clips: " + ", ".join(item["filename"] for item in items))
    await_agent(tree, status)
    registry = prepare_clips(tree, items, status)
    tree.log(f"search collection OK: {len(items)} clips registered in {tree.registry} and searchable")
    return {item["filename"]: registry[item["filename"]] for item in items}


def main() -> int:
    tree = Tree(TASK_DIR)
    tree.data.mkdir(parents=True, exist_ok=True)
    tree.log("launch-time preparation starting (course-prep service)")
    items = search_assets(tree)
    status = Status(tree.status, [item["filename"] for item in items])
    tree.log(f"preparing {len(items)} clips: " + ", ".join(item["filename"] for item in items))

    await_course_data(tree, status)
    await_agent(tree, status)
    prepare_clips(tree, items, status)
    tree.log(f"course preparation OK: {len(items)} clips registered in {tree.registry} and searchable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
