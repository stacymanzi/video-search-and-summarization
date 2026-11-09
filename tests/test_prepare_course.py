"""Offline tests for prepare_course.py: the S3 marker wait, the status file, when ingest() reuses a registered
sensor, the step order of main().

Run as a script from anywhere: python3 -I task1/tests/test_prepare_course.py
Needs `requests` (as the other course-prep tests do) and the tree itself; no
/dli/task, no network, no loader. The clock and sleep are injected or patched,
and VIOS and Elasticsearch are fakes, so the whole file runs in well under a second.
"""
import contextlib
import functools
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TASK1 = Path(__file__).resolve().parents[1]
PREP_SCRIPT = TASK1 / "lab/prep/prepare_course.py"   # /dli/prep/prepare_course.py in the image
MANIFEST = TASK1 / "lab/task/search-videos.json"      # /dli/task/search-videos.json in the image
SEARCH_CLIPS = [item["filename"] for item in json.loads(MANIFEST.read_text())]

# The script imports course_setup from <COURSE_PREP_TASK_DIR>/part1 at import time.
os.environ["COURSE_PREP_TASK_DIR"] = str(TASK1 / "lab/task")
_spec = importlib.util.spec_from_file_location("prepare_course", PREP_SCRIPT)
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)


class FakeTime:
    """A monotonic clock that only moves when sleep() is called; on_sleep(now) runs after each sleep."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.on_sleep = on_sleep or (lambda now: None)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep(self.now)


class WaitForCourseDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self.tmp.name) / prep.COURSE_DATA_MARKER_NAME
        self.reports = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **payload):
        self.marker.write_text(json.dumps(payload))

    def wait(self, fake, timeout_s=600):
        return prep.wait_for_course_data(self.marker, timeout_s, self.reports.append,
                                         clock=fake.clock, sleep=fake.sleep, every_s=10, report_every_s=60)

    def test_absent_then_running_then_ok(self):
        def loader(now):
            if now == 30:
                self.write(state="RUNNING", started="T0")
            if now == 130:
                self.write(state="OK", finished="T1")
        self.assertEqual(self.wait(FakeTime(loader)), 130)
        self.assertEqual(self.reports, [0, 60, 120])     # one progress line a minute while waiting

    def test_ok_already_present_returns_at_once(self):
        self.write(state="OK", finished="T1")
        self.assertEqual(self.wait(FakeTime()), 0)
        self.assertEqual(self.reports, [])

    def test_failed_raises_with_the_loader_error(self):
        def loader(now):
            if now == 20:
                self.write(state="FAILED", error="KeyError: 'Contents'")
        with self.assertRaisesRegex(RuntimeError, "^S3 course data loader failed: KeyError: 'Contents'$"):
            self.wait(FakeTime(loader))

    def test_running_past_the_cap_times_out(self):
        self.write(state="RUNNING", started="T0")
        with self.assertRaisesRegex(TimeoutError, r"^S3 course data not ready after 120s$"):
            self.wait(FakeTime(), timeout_s=120)

    def test_unreadable_or_unknown_marker_raises(self):
        self.marker.write_text("{not json")
        with self.assertRaisesRegex(RuntimeError, "is not JSON"):
            self.wait(FakeTime())
        self.write(state="DONE")
        with self.assertRaisesRegex(RuntimeError, "unknown state 'DONE'"):
            self.wait(FakeTime())
        self.marker.write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "unknown state None"):
            self.wait(FakeTime())

    def test_cap_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.wait(FakeTime(), timeout_s=0)


class StatusFileTests(unittest.TestCase):
    """The status file the onboarding notebook and notebook 4 read, while waiting and after a failure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = prep.Tree(self.tmp.name)
        self.tree.data.mkdir(parents=True)
        self.status = prep.Status(self.tree.status, ["normal_eval.mp4", "rain_eval.mp4"])
        self.marker = self.tree.data / prep.COURSE_DATA_MARKER_NAME
        self.seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def read_status(self):
        return json.loads(self.tree.status.read_text())

    def run_with(self, fake):
        wait = functools.partial(prep.wait_for_course_data, clock=fake.clock, sleep=fake.sleep)
        with mock.patch.object(prep, "wait_for_course_data", wait), mock.patch("builtins.print"):
            return prep.await_course_data(self.tree, self.status)

    def test_waiting_shows_in_the_status_file_then_clears(self):
        def loader(now):
            self.seen.append(self.read_status())
            if now >= 70:
                self.marker.write_text(json.dumps({"state": "OK"}))
        self.run_with(FakeTime(loader))
        self.assertTrue(self.seen[0]["detail"].startswith("waiting for S3 course data (0 s"))
        self.assertEqual(self.seen[0]["state"], "RUNNING")
        final = self.read_status()
        self.assertEqual((final["state"], final["detail"]), ("RUNNING", None))
        self.assertIn("S3 course data ready after 70 s", self.tree.log_file.read_text())

    def test_loader_failure_fails_every_asset_with_the_reason(self):
        self.marker.write_text(json.dumps({"state": "FAILED", "error": "ClientError: 404"}))
        with self.assertRaisesRegex(RuntimeError, "S3 course data loader failed: ClientError: 404"):
            self.run_with(FakeTime())
        final = self.read_status()
        self.assertEqual(final["state"], "FAILED")
        self.assertEqual({a["state"] for a in final["assets"].values()}, {"failed"})
        self.assertEqual({a["error"] for a in final["assets"].values()},
                         {"S3 course data loader failed: ClientError: 404"})

    def test_stack_failure_fails_every_asset_instead_of_staying_running(self):
        """A dead course-prep must not leave RUNNING behind: the notebooks would tell learners to keep waiting."""
        reason = "http://nginx/agent/health did not answer 200 within 5400 s (last: ConnectTimeout)."
        tail = "\nDeployer status tail:\nseed-status: 2026-11-02T09:40:00Z RUNNING: waiting for readiness\n  line 1\n  line 2"
        with mock.patch.object(prep, "wait_for_agent", side_effect=TimeoutError(reason + tail)), \
             mock.patch("builtins.print"):
            with self.assertRaises(TimeoutError):
                prep.await_agent(self.tree, self.status)
        final = self.read_status()
        self.assertEqual(final["state"], "FAILED")
        self.assertTrue(final["detail"].startswith(reason))
        self.assertEqual({a["state"] for a in final["assets"].values()}, {"failed"})
        self.assertIn("FAILED: " + reason + tail, self.tree.log_file.read_text())   # the log keeps the whole reason

    def test_stack_failure_keeps_one_readable_line_per_clip(self):
        """The onboarding status cell prints `detail` and every clip's `error`: the deployer's tail stays in the log."""
        first_line = "the deployer reported a failure after 1800 s, so the stack will not come up: FAILED: dev-profile.sh up"
        tail = "\nDeployer status tail:\nseed-status: 2026-11-02T09:40:00Z FAILED: dev-profile.sh up\n  line 1"
        with mock.patch.object(prep, "wait_for_agent", side_effect=RuntimeError(first_line + tail)), \
             mock.patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                prep.await_agent(self.tree, self.status)
        final = self.read_status()
        expected = f"{first_line}. The deployer's status tail is in {self.tree.log_file}."
        self.assertEqual(final["detail"], expected)
        self.assertEqual({a["error"] for a in final["assets"].values()}, {expected})
        self.assertIn(first_line + tail, self.tree.log_file.read_text())


class PrepareClipsTests(unittest.TestCase):
    """Steps 4 to 6 with the ingest and the embedding wait replaced: per-clip states and the final state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = prep.Tree(self.tmp.name)
        self.tree.data.mkdir(parents=True)
        self.items = [{"filename": name} for name in SEARCH_CLIPS]
        self.status = prep.Status(self.tree.status, SEARCH_CLIPS)

    def tearDown(self):
        self.tmp.cleanup()

    def run_clips(self, failing_upload=None, pending_embeddings=()):
        def fake_ingest(tree, item, registry):
            if item["filename"] == failing_upload:
                raise RuntimeError("POST http://10.0.0.5:30888/api/v1/storage/file -> HTTP 507: Insufficient Storage")
            registry[item["filename"]] = {"sensor_id": "uuid-" + item["filename"], "completed": True}
            return registry[item["filename"]]["sensor_id"]

        def fake_embeddings(tree, registry, names):
            if pending_embeddings:
                raise prep.EmbeddingsIncomplete("embeddings incomplete", list(pending_embeddings))
            return {}

        with mock.patch.object(prep, "ingest", fake_ingest), \
             mock.patch.object(prep, "wait_for_embeddings", fake_embeddings), \
             mock.patch("builtins.print"):
            return prep.prepare_clips(self.tree, self.items, self.status)

    def states(self):
        status = json.loads(self.tree.status.read_text())
        return status["state"], {name: asset["state"] for name, asset in status["assets"].items()}

    def test_every_clip_searchable_is_ok(self):
        registry = self.run_clips()
        self.assertEqual(sorted(registry), sorted(SEARCH_CLIPS))
        self.assertEqual(self.states(), ("OK", {name: "ok" for name in SEARCH_CLIPS}))

    def test_one_failed_upload_fails_the_run_and_names_the_clip(self):
        with self.assertRaisesRegex(RuntimeError, "course preparation failed for fog_eval.mp4"):
            self.run_clips(failing_upload="fog_eval.mp4")
        state, assets = self.states()
        self.assertEqual(state, "FAILED")
        self.assertEqual(assets["fog_eval.mp4"], "failed")
        self.assertEqual({assets[name] for name in SEARCH_CLIPS if name != "fog_eval.mp4"}, {"ok"})

    def test_incomplete_embeddings_fail_only_the_pending_clip(self):
        with self.assertRaisesRegex(RuntimeError, "course preparation failed for night_eval.mp4"):
            self.run_clips(pending_embeddings=["night_eval.mp4"])
        state, assets = self.states()
        self.assertEqual(state, "FAILED")
        self.assertEqual(assets["night_eval.mp4"], "failed")
        self.assertEqual({assets[name] for name in SEARCH_CLIPS if name != "night_eval.mp4"}, {"ok"})


class FakeResponse:
    """The parts of a requests.Response that embedding_search() reads."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


def records_answer(count):
    """Elasticsearch's 200 answer to the records query: `count` records for the sensor, the last ending at 175 s."""
    last = 1735689600000 + 175000 if count else None   # 2025-01-01T00:02:55Z in epoch milliseconds
    return FakeResponse(200, {"hits": {"total": {"value": count, "relation": "eq"}, "max_score": None, "hits": []},
                              "aggregations": {"last_end": {"value": last}}})


INDEX_MISSING = FakeResponse(404, {"error": {"type": "index_not_found_exception"}, "status": 404})


class FakeStack:
    """VIOS's sensor list, Elasticsearch's answers to the records query, and the uploads ingest() makes.

    `answers` maps a sensor id to the answer for its records query. Any other HTTP request fails the test:
    ingest() sends no other request, and it never deletes a VIOS source.
    """

    def __init__(self, vios, answers):
        self.vios = [dict(sensor) for sensor in vios]   # sensor/list records, in VIOS's list order
        self.answers = answers
        self.queries = []                               # (url, body) of every records query
        self.uploads = []                               # (path, name, timestamp) of every ingest_file call

    def sensors(self):
        return [dict(sensor) for sensor in self.vios]

    def post(self, url, **kwargs):
        body = kwargs["json"]
        self.queries.append((url, body))
        return self.answers[body["query"]["term"]["sensor.id.keyword"]]

    def ingest_file(self, path, name, timestamp_iso):
        """A new sensor id; VIOS names the copy <stem>_<n>, counting on from the sources it holds for the stem."""
        stem = Path(name).stem
        number = 1 + sum(sensor["name"].startswith(stem + "_") for sensor in self.vios)
        self.uploads.append((path, name, timestamp_iso))
        sensor_id = f"uuid-upload-{len(self.uploads)}"
        self.vios.append({"sensorId": sensor_id, "name": f"{stem}_{number}"})
        return sensor_id

    def other_request(self, *args, **kwargs):
        raise AssertionError(f"unexpected HTTP request {args} {kwargs}")

    def installed(self):
        """Every patch at once: course_setup's VIOS calls, the records query, and a trap for any other request."""
        patches = contextlib.ExitStack()
        patches.enter_context(mock.patch.object(prep.course_setup, "sensors", self.sensors))
        patches.enter_context(mock.patch.object(prep.course_setup, "ingest_file", self.ingest_file))
        patches.enter_context(mock.patch.object(prep.requests, "post", self.post))
        for method in ("request", "get", "put", "delete"):
            patches.enter_context(mock.patch.object(prep.requests, method, self.other_request))
        patches.enter_context(mock.patch("builtins.print"))
        return patches


class IngestReuseTests(unittest.TestCase):
    """Step 4: ingest() reuses a registered sensor only while VIOS lists it and RT-Embed's index holds its records.

    Measured on c-fx-17-v2-dev (2026-11-03, docs/DEVBOX_VERIFY.md AGT-28): Elasticsearch had deleted the mdx-*
    indices under its index lifecycle policy, and notebook 2's upload had created them again without the four
    videos' records. Not measured on a devbox: a rerun of the old ingest() reuses the sensors, which VIOS still
    lists, and waits to the embedding cap (PLT1-13 checks the fix).
    """

    OLD = "uuid-normal_eval_1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        task = Path(self.tmp.name)
        shutil.copy(MANIFEST, task / MANIFEST.name)
        self.tree = prep.Tree(task)
        self.items = prep.search_assets(self.tree)
        for item in self.items:
            item["path"].parent.mkdir(parents=True, exist_ok=True)
            item["path"].write_bytes(b"clip")
        self.asset = self.items[0]   # normal_eval.mp4
        self.registry = {item["filename"]: {"filename": item["filename"],
                                            "duration_seconds": item["duration_seconds"],
                                            "creation_time": item["creation_time"],
                                            "sensor_id": f"uuid-{Path(item['filename']).stem}_1",
                                            "completed": True} for item in self.items}
        prep.write_json_atomic(self.tree.registry, self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def vios(*names):
        return [{"sensorId": "uuid-" + name, "name": name} for name in names]

    def ingest(self, stack):
        with stack.installed():
            return prep.ingest(self.tree, self.asset, self.registry)

    def log_lines(self):
        """The messages of course_prep.log, without the timestamp prefix."""
        return [line.split("] ", 1)[1] for line in self.tree.log_file.read_text().splitlines()]

    def on_disk(self):
        return json.loads(self.tree.registry.read_text())

    def assert_registry_names(self, sensor_id):
        """The registry the caller holds (the embedding wait reads it) and the file (notebook 4 reads it)."""
        expected = {"filename": "normal_eval.mp4", "duration_seconds": self.asset["duration_seconds"],
                    "creation_time": "2025-01-02T00:00:00.000Z", "sensor_id": sensor_id, "completed": True}
        self.assertEqual(self.registry["normal_eval.mp4"], expected)
        on_disk = self.on_disk()
        self.assertEqual(on_disk["normal_eval.mp4"], expected)
        self.assertEqual({name: entry["sensor_id"] for name, entry in on_disk.items() if name != "normal_eval.mp4"},
                         {"rain_eval.mp4": "uuid-rain_eval_1", "fog_eval.mp4": "uuid-fog_eval_1",
                          "night_eval.mp4": "uuid-night_eval_1"})

    def assert_ingested_again(self, stack, new, found):
        self.assertEqual(new, "uuid-upload-1")
        self.assertEqual(stack.uploads, [(str(self.asset["path"]), "normal_eval.mp4", "2025-01-02T00:00:00.000Z")])
        self.assertEqual(stack.vios, self.vios("normal_eval_1") + [{"sensorId": new, "name": "normal_eval_2"}],
                         "the copy is normal_eval_2, and the old source stays in VIOS")
        self.assert_registry_names(new)
        lines = self.log_lines()
        self.assertEqual(len(lines), 3, lines)   # the reason, then the usual two ingest lines
        self.assertEqual(lines[0],
                         f"normal_eval.mp4: VIOS still has {self.OLD}, but the search index has no records for it "
                         f"({found}). Elasticsearch most likely deleted them under its index lifecycle policy. "
                         "Ingesting the file again as a new VIOS source; the old one stays.")
        self.assertTrue(lines[1].startswith("normal_eval.mp4: ingesting ("), lines[1])
        self.assertTrue(lines[2].startswith(f"normal_eval.mp4: sensor {new} after "), lines[2])

    def test_records_present_reuse_the_registered_sensor(self):
        stack = FakeStack(self.vios("normal_eval_1"), {self.OLD: records_answer(36)})
        self.assertEqual(self.ingest(stack), self.OLD)
        self.assertEqual(stack.uploads, [])
        self.assert_registry_names(self.OLD)
        self.assertEqual(self.log_lines(), [f"normal_eval.mp4: already ingested as {self.OLD}; reusing"])
        # The query is the one embedding_progress() sends: the same URL and the same body.
        with stack.installed():
            self.assertEqual(prep.embedding_progress(self.OLD), (36, 175.0))
        self.assertEqual(len(stack.queries), 2)
        self.assertEqual(stack.queries[0], stack.queries[1])
        url, body = stack.queries[0]
        self.assertEqual(url, "http://nginx/es/mdx-embed-filtered-2025-01-01/_search")
        self.assertEqual(body["query"], {"term": {"sensor.id.keyword": self.OLD}})

    def test_index_missing_ingests_the_file_again(self):
        stack = FakeStack(self.vios("normal_eval_1"), {self.OLD: INDEX_MISSING})
        self.assert_ingested_again(stack, self.ingest(stack), "HTTP 404: no index mdx-embed-filtered-2025-01-01")

    def test_zero_records_ingest_the_file_again(self):
        """The index exists again (a later upload created it) but holds nothing for this sensor."""
        stack = FakeStack(self.vios("normal_eval_1"), {self.OLD: records_answer(0)})
        self.assert_ingested_again(stack, self.ingest(stack), "0 records in mdx-embed-filtered-2025-01-01")

    def test_a_reingested_sensor_is_reused_although_vios_lists_the_old_one_first(self):
        """No third upload on the next run: the id is looked up in the whole sensor list, not with find_sensor."""
        self.registry["normal_eval.mp4"]["sensor_id"] = "uuid-normal_eval_2"
        stack = FakeStack(self.vios("normal_eval_1", "normal_eval_2"), {"uuid-normal_eval_2": records_answer(36)})
        with stack.installed():
            self.assertEqual(prep.course_setup.find_sensor("normal_eval")["sensorId"], self.OLD)
        self.assertEqual(self.ingest(stack), "uuid-normal_eval_2")
        self.assertEqual(stack.uploads, [])
        self.assertEqual(self.log_lines(), ["normal_eval.mp4: already ingested as uuid-normal_eval_2; reusing"])

    def test_a_sensor_vios_does_not_list_is_ingested_again_without_a_records_query(self):
        stack = FakeStack(self.vios("rain_eval_1"), {})
        new = self.ingest(stack)
        self.assertEqual(stack.queries, [])
        self.assertEqual(len(stack.uploads), 1)
        self.assert_registry_names(new)
        self.assertEqual(self.log_lines()[0],
                         f"normal_eval.mp4: registry names {self.OLD} but VIOS does not have it; ingesting again")

    def test_an_elasticsearch_error_raises_and_uploads_nothing(self):
        stack = FakeStack(self.vios("normal_eval_1"), {self.OLD: FakeResponse(503, {"error": "unavailable"})})
        with self.assertRaisesRegex(RuntimeError,
                                    r"^POST http://nginx/es/mdx-embed-filtered-2025-01-01/_search -> 503: "):
            self.ingest(stack)
        self.assertEqual(stack.uploads, [])
        self.assertEqual(self.on_disk()["normal_eval.mp4"]["sensor_id"], self.OLD)

    def test_the_embedding_wait_the_status_file_and_the_registry_get_the_new_sensor(self):
        """prepare_clips() with the real ingest(): only the clip whose records are gone is uploaded again."""
        answers = {f"uuid-{Path(name).stem}_1": records_answer(36) for name in SEARCH_CLIPS}
        answers["uuid-night_eval_1"] = records_answer(0)
        stack = FakeStack(self.vios(*[f"{Path(name).stem}_1" for name in SEARCH_CLIPS]), answers)
        waited = {}

        def fake_wait(tree, registry, names):
            waited.update({name: registry[name]["sensor_id"] for name in names})
            return {}

        status = prep.Status(self.tree.status, SEARCH_CLIPS)
        with stack.installed(), mock.patch.object(prep, "wait_for_embeddings", fake_wait):
            registry = prep.prepare_clips(self.tree, self.items, status)

        expected = {name: f"uuid-{Path(name).stem}_1" for name in SEARCH_CLIPS}
        expected["night_eval.mp4"] = "uuid-upload-1"
        self.assertEqual([upload[1] for upload in stack.uploads], ["night_eval.mp4"])
        self.assertEqual(waited, expected)
        self.assertEqual({name: entry["sensor_id"] for name, entry in registry.items()}, expected)
        self.assertEqual({name: entry["sensor_id"] for name, entry in self.on_disk().items()}, expected)
        final = json.loads(self.tree.status.read_text())
        self.assertEqual(final["state"], "OK")
        self.assertEqual({name: (asset["state"], asset["sensor_id"]) for name, asset in final["assets"].items()},
                         {name: ("ok", sensor_id) for name, sensor_id in expected.items()})


class MainTests(unittest.TestCase):
    """main(): the four search clips, the step order, and nothing written outside data/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task = Path(self.tmp.name)
        shutil.copy(MANIFEST, self.task / MANIFEST.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_launch_run_prepares_the_four_search_clips(self):
        self.assertEqual(SEARCH_CLIPS, ["normal_eval.mp4", "rain_eval.mp4", "fog_eval.mp4", "night_eval.mp4"])
        items = prep.search_assets(prep.Tree(self.task))
        self.assertEqual([item["filename"] for item in items], SEARCH_CLIPS)
        for item in items:
            self.assertEqual(item["path"], self.task / "data/search-videos" / item["filename"])
            self.assertEqual(item["creation_time"], "2025-01-02T00:00:00.000Z")   # the search clips' VIOS anchor

    def test_steps_run_in_order_and_write_only_under_data(self):
        calls = []

        def step(name, result=None):
            def run(*args):
                calls.append((name, args))
                return result
            return run

        with mock.patch.object(prep, "TASK_DIR", str(self.task)), \
             mock.patch.object(prep, "await_course_data", step("data", 0.0)), \
             mock.patch.object(prep, "await_agent", step("stack", 0.0)), \
             mock.patch.object(prep, "prepare_clips", step("clips", {})), \
             mock.patch("builtins.print"):
            self.assertEqual(prep.main(), 0)

        self.assertEqual([name for name, _ in calls], ["data", "stack", "clips"])
        _, clip_args = calls[2]
        self.assertEqual([item["filename"] for item in clip_args[1]], SEARCH_CLIPS)
        written = sorted(str(path.relative_to(self.task)) for path in self.task.rglob("*") if path.is_file())
        self.assertEqual(written, ["data/course_prep.log", "data/course_prep_status.json", MANIFEST.name])
        status = json.loads((self.task / "data/course_prep_status.json").read_text())
        self.assertEqual(list(status["assets"]), SEARCH_CLIPS)

    def test_the_instructor_path_waits_for_the_stack_but_not_for_s3(self):
        calls = []
        with mock.patch.object(prep, "await_course_data", lambda *a: calls.append("data")), \
             mock.patch.object(prep, "await_agent", lambda *a: calls.append("stack")), \
             mock.patch.object(prep, "prepare_clips",
                               lambda tree, items, status: calls.append("clips") or
                               {item["filename"]: {"sensor_id": "x"} for item in items}), \
             mock.patch("builtins.print"):
            registry = prep.prepare_search_collection(task_dir=str(self.task), manifest_name=MANIFEST.name)
        self.assertEqual(calls, ["stack", "clips"])
        self.assertEqual(list(registry), SEARCH_CLIPS)


if __name__ == "__main__":
    unittest.main()
