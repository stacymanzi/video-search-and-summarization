"""Offline regression checks for notebook 4's multi-video search logic and the shape of notebooks 3 and 4.

Run as a script from anywhere: python3 -I task1/tests/test_search_collection.py
Needs nothing beyond the standard library and the tree itself (no /dli/task, no network).
"""
import ast
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

TASK = Path(__file__).resolve().parents[1] / "lab/task"
PART1 = TASK / "part1"
NOTEBOOK = PART1 / "04-rt-embed-video-search.ipynb"
CAPTIONING = "03-rt-vlm-captioning-and-prompting.ipynb"
PREP_SCRIPT = Path(__file__).resolve().parents[1] / "lab/prep/prepare_course.py"   # /dli/prep/prepare_course.py in the image
DOCKERFILE = Path(__file__).resolve().parents[1] / "lab/Dockerfile"

# The RT-VLM and RT-Embed notebooks, built from the old captioning, prompt-comparison and video-search notebooks.
REVIEWED = (CAPTIONING, NOTEBOOK.name)
HEADER = '<img src="../images/nvidia_header.png"'
RETIRED_NAMES = ("Cosmos " + "Reason", "Cosmos 3 " + "Reasoner")   # the RT-VLM model is "Cosmos 3 Super Reasoner"


def cells(name):
    return json.loads((PART1 / name).read_text())["cells"]


def text(cell):
    source = cell["source"]
    return "".join(source) if isinstance(source, list) else source


def by_id(name, cell_id):
    return next(c for c in cells(name) if c.get("id") == cell_id)


class SearchCollectionTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.hits = []
        self.sources = {
            name: {"sensor_id": sid, "creation_time": "2025-01-02T00:00:00.000Z"}
            for name, sid in [("normal_eval.mp4", "source-a"), ("night_eval.mp4", "source-b")]
        }

        def api(method, path, **kwargs):
            self.requests.append((path, kwargs))
            if "generate_text_embeddings" in path:
                return {"data": [{"embeddings": [0.1] * 768}]}
            return {"hits": {"hits": self.hits}}

        self.ns = {"api": api, "sources": self.sources, "datetime": datetime,
                   "timedelta": timedelta, "INDEX": "test-index",
                   "EMBEDDING_ANCHOR": "2025-01-01T00:00:00.000Z"}
        for cell in cells(NOTEBOOK.name):
            if cell["cell_type"] == "code":
                tree = ast.parse(text(cell))
                functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name in ("search", "show_results")]
                exec(compile(ast.Module(body=functions, type_ignores=[]), str(NOTEBOOK), "exec"), self.ns)

    def hit(self, source, score=0.7):
        return {"_score": score, "_source": {"sensor": {"id": source},
                "timestamp": "2025-01-01T00:01:55.000Z", "end": "2025-01-01T00:02:00.000Z"}}

    def test_equal_times_in_different_sources_are_distinct(self):
        self.hits = [self.hit("source-a"), self.hit("source-a"), self.hit("source-b")]
        out = self.ns["search"]("collision")
        self.assertEqual([r["filename"] for r in out], ["normal_eval.mp4", "night_eval.mp4"])

    def test_playback_uses_registered_timeline(self):
        self.hits = [self.hit("source-b")]
        hit = self.ns["search"]("collision")[0]
        self.assertEqual(hit["start_seconds"], 115)
        self.assertEqual(hit["start"], "2025-01-02T00:01:55.000Z")
        self.assertEqual(hit["end"], "2025-01-02T00:02:00.000Z")
        self.assertEqual(hit["embedding_start"], "2025-01-01T00:01:55.000Z")

    def test_source_filter_is_inside_knn_not_only_postfilter(self):
        self.hits = [self.hit("source-b")]
        out = self.ns["search"]("collision", filenames=["night_eval.mp4"])
        query = self.requests[-1][1]["json"]["query"]["bool"]
        knn = query["must"][0]["nested"]["query"]["knn"]
        self.assertEqual(knn["filter"], {"terms": {"sensor.id.keyword": ["source-b"]}})
        self.assertEqual(out[0]["sensor_id"], "source-b")

    def test_a_vector_query_skips_the_text_encoder(self):
        """Section 4.10 passes a clip's vector to the same search: no text embedding, the vector goes to the kNN."""
        self.hits = [self.hit("source-a")]
        vector = [0.5] * 768
        out = self.ns["search"](vector)
        self.assertEqual([path for path, _ in self.requests], ["/es/test-index/_search"])
        knn = self.requests[-1][1]["json"]["query"]["bool"]["must"][0]["nested"]["query"]["knn"]
        self.assertEqual(knn["query_vector"], vector)
        self.assertEqual(knn["filter"], {"terms": {"sensor.id.keyword": ["source-a", "source-b"]}})
        self.assertEqual(out[0]["filename"], "normal_eval.mp4")

    def test_empty_selection_does_not_search_everything(self):
        self.assertEqual(self.ns["search"]("collision", filenames=[]), [])
        self.assertEqual(self.requests, [])

    def test_empty_results_are_supported(self):
        self.assertEqual(self.ns["search"]("collision"), [])

    def test_unknown_source_is_not_silently_ignored(self):
        with self.assertRaises(KeyError):
            self.ns["search"]("collision", filenames=["missing.mp4"])

    def test_manifest_and_notebook_structure(self):
        manifest = json.loads((TASK / "search-videos.json").read_text())
        self.assertEqual(len(manifest), 4)
        self.assertEqual(len({m["filename"] for m in manifest}), 4)
        self.assertTrue(all(180 <= m["duration_seconds"] < 181 for m in manifest))
        tagged = [c["id"] for c in cells(NOTEBOOK.name) if "search-preparation" in c.get("metadata", {}).get("tags", [])]
        self.assertEqual(tagged, ["05-collection-02", "05-collection-04"])
        for path in PART1.glob("*.ipynb"):
            for cell in json.loads(path.read_text())["cells"]:
                if cell["cell_type"] == "code":
                    ast.parse("\n".join(line for line in text(cell).splitlines() if not line.lstrip().startswith(("!", "%"))))
                    self.assertEqual(cell["outputs"], [], path.name)
                    self.assertIsNone(cell["execution_count"], path.name)

    def test_widened_replay_stays_inside_the_video(self):
        """Cell nb04-widen adds up to 3 s on each side of the top hit, never before 0 or past the video's end."""
        code = text(by_id(NOTEBOOK.name, "nb04-widen"))
        cuts, out = [], io.StringIO()

        def iso_offset(iso, seconds):
            moment = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(seconds=seconds)
            return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        course_setup = types.SimpleNamespace(iso_offset=iso_offset,
                                             clip_bytes=lambda sensor, start, end: cuts.append((sensor, start, end)) or b"")
        labtools = types.SimpleNamespace(show_video=lambda data: None)
        sources = {"normal_eval.mp4": {"duration_seconds": 180.0}}
        cases = {(1.0, 6.0): ("0.0 to 9.0 s", "2025-01-02T00:00:00.000Z", "2025-01-02T00:00:09.000Z"),
                 (50.0, 55.0): ("47.0 to 58.0 s", "2025-01-02T00:00:47.000Z", "2025-01-02T00:00:58.000Z"),
                 (174.0, 179.0): ("171.0 to 180.0 s", "2025-01-02T00:02:51.000Z", "2025-01-02T00:03:00.000Z")}
        for (start_s, end_s), (printed, start, end) in cases.items():
            with self.subTest(window=(start_s, end_s)):
                hit = {"filename": "normal_eval.mp4", "sensor_id": "source-a", "start_seconds": start_s,
                       "end_seconds": end_s, "start": iso_offset("2025-01-02T00:00:00.000Z", start_s),
                       "end": iso_offset("2025-01-02T00:00:00.000Z", end_s)}
                with contextlib.redirect_stdout(out):
                    exec(code, {"hits": [hit], "sources": sources, "course_setup": course_setup, "labtools": labtools})
                self.assertIn(printed, out.getvalue())
                self.assertEqual(cuts[-1], ("source-a", start, end))

    def test_instructor_cell_imports_the_course_prep_function_and_calls_nothing(self):
        """BUILD.md 5a decision 10: the by-hand path is course-prep's own prepare_search_collection(), imported, never run here."""
        tagged = [c for c in cells(NOTEBOOK.name) if "instructor-only" in c.get("metadata", {}).get("tags", [])]
        self.assertEqual([c["id"] for c in tagged], ["05-instructor-prep"])
        source = text(tagged[0])
        self.assertIn("import prepare_course", source)
        self.assertNotIn("def prepare_search_collection(", source, "one implementation: task1/lab/prep/prepare_course.py")
        self.assertIsNone(re.search(r"^\s*(\w+\s*=\s*)?prepare_search_collection\(", source, re.M),
                          "the instructor-only cell imports the function and calls nothing")
        self.assertNotIn("ingest_file(", text(by_id(NOTEBOOK.name, "05-collection-02")),
                         "cell 05-collection-02 uploads nothing (BUILD.md D12)")
        # The script the cell imports defines the function at module level and ships in the image at /dli/prep.
        prep = ast.parse(PREP_SCRIPT.read_text())
        self.assertIn("prepare_search_collection", [n.name for n in prep.body if isinstance(n, ast.FunctionDef)])
        self.assertIn("COPY lab/prep /dli/prep", DOCKERFILE.read_text())
        # Executed against a stand-in module, the cell binds the function and runs nothing.
        calls, fake = [], types.ModuleType("prepare_course")
        fake.__file__ = "/dli/prep/prepare_course.py"
        fake.prepare_search_collection = lambda *args, **kwargs: calls.append((args, kwargs))
        ns, out, saved_path = {}, io.StringIO(), list(sys.path)
        try:
            with fake_modules(prepare_course=fake), contextlib.redirect_stdout(out):
                exec(source, ns)
        finally:
            sys.path[:] = saved_path
        self.assertIs(ns["prepare_search_collection"], fake.prepare_search_collection)
        self.assertEqual(calls, [])
        self.assertIn("nothing was uploaded", out.getvalue())


class NotebookShapeTests(unittest.TestCase):
    """The house pattern for notebooks 3 and 4, and the cross-notebook facts the review pinned."""

    def test_template_cells(self):
        for name in REVIEWED:
            items = cells(name)
            with self.subTest(notebook=name):
                self.assertIn(HEADER, text(items[0]))
                self.assertIn(HEADER, text(items[-1]))
                ids = [c.get("id") for c in items]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_-]+", i or "") for i in ids), ids)
                body = "\n".join(text(c) for c in items if c["cell_type"] == "markdown")
                for needle in ("**Time:**", "In this notebook you will:", "## Check your understanding", "## Next"):
                    self.assertIn(needle, body)
                everything = "\n".join(text(c) for c in items)
                for retired in RETIRED_NAMES:
                    self.assertNotIn(retired, everything)

    def test_exercise_shape(self):
        """A `# TODO` FIXME cell is preceded by a **Your turn:** (or **TODO:**) markdown cell and followed by a hidden
        `# SOLUTION` cell; notebooks 3 and 4 have at least two such exercises each."""
        for name in REVIEWED:
            items = cells(name)
            exercises = 0
            for i, c in enumerate(items):
                if c["cell_type"] != "code" or not text(c).startswith("# TODO\n"):
                    continue
                exercises += 1
                with self.subTest(notebook=name, cell=c["id"]):
                    self.assertIn("FIXME", text(c))
                    self.assertEqual(items[i - 1]["cell_type"], "markdown")
                    self.assertTrue(text(items[i - 1]).startswith(("**Your turn:**", "**TODO:**")), items[i - 1]["id"])
                    solution = items[i + 1]
                    self.assertEqual(solution["cell_type"], "code")
                    self.assertTrue(text(solution).startswith("# SOLUTION"))
                    self.assertTrue(solution["metadata"].get("jupyter", {}).get("source_hidden"), "solution not collapsed")
                    self.assertNotIn("FIXME", text(solution))
            with self.subTest(notebook=name):
                self.assertGreaterEqual(exercises, 2)

    def test_03_reuses_notebook_2s_clip(self):
        """Notebook 3 registers the traffic_short clip notebook 2 added to VIOS; it never uploads a copy of its own."""
        source = text(by_id(CAPTIONING, "bootstrap-code"))
        self.assertNotIn("ingest_file(", source)
        self.assertNotIn("/agent/api/v1/videos", source)
        for found, expected in ((None, RuntimeError), ({"name": "traffic_short_1", "sensorId": "uuid-1"}, None)):
            with self.subTest(found=found):
                setup = types.ModuleType("course_setup")
                setup.find_sensor = lambda name, found=found: found if name == "traffic_short" else None
                setup.vlm_model_id = lambda: "cosmos-test"
                display = types.ModuleType("IPython.display")
                display.Markdown, display.display = str, print
                cleanup = types.ModuleType("cleanup")
                cleanup.remember = lambda *args, **kwargs: None
                ns, error = {}, None
                with fake_modules(course_setup=setup, labtools=types.ModuleType("labtools"), cleanup=cleanup,
                                  requests=types.ModuleType("requests"), IPython=types.ModuleType("IPython"),
                                  **{"IPython.display": display}), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        exec(source, ns)
                    except Exception as exc:  # noqa: BLE001 - the case asserts on it
                        error = exc
                if expected is None:
                    self.assertIsNone(error, error)
                    self.assertEqual(ns["sensor_id"], "uuid-1")
                else:
                    self.assertIsInstance(error, expected)
                    self.assertIn("notebook 2", str(error))


# --- the notebook 4 preparation gate (BUILD.md 5a decision 10: the status file is required, no live-check path) ---

MANIFEST = json.loads((TASK / "search-videos.json").read_text())
SENSOR_IDS = ["sensor-" + Path(item["filename"]).stem for item in MANIFEST]
OK_REGISTRY = {item["filename"]: {**item, "sensor_id": sid, "completed": True} for item, sid in zip(MANIFEST, SENSOR_IDS)}


@contextlib.contextmanager
def fake_modules(**modules):
    """Put stand-in modules in sys.modules for the duration of an exec, restoring the originals afterwards."""
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload, self.status_code = payload, status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def fake_requests(sensor_ids):
    """A stand-in requests module whose every call answers with the VIOS sensor list; the gate makes one GET."""
    module = types.ModuleType("requests")
    module.calls = []

    def request(method, url, **kwargs):
        module.calls.append((method, url))
        return FakeResponse([{"sensorId": sid, "name": sid} for sid in sensor_ids])

    module.request = request
    module.get = lambda url, **kwargs: request("GET", url, **kwargs)
    module.post = lambda url, **kwargs: request("POST", url, **kwargs)
    return module


def status_payload(state):
    return {"state": state, "updated": "2026-10-20T08:00:00Z",
            "assets": {name: {"sensor_id": sid, "state": "ok" if state == "OK" else "pending", "error": None}
                       for name, sid in zip(OK_REGISTRY, SENSOR_IDS)}}


class PreparationGateTests(unittest.TestCase):
    """Cell 05-collection-02 requires course_prep_status.json with state OK and a registry VIOS confirms."""

    def run_cell(self, status, registry, sensor_ids=SENSOR_IDS):
        code = text(by_id(NOTEBOOK.name, "05-collection-02"))
        with tempfile.TemporaryDirectory() as folder:
            task = Path(folder)
            (task / "data").mkdir()
            shutil.copy(TASK / "search-videos.json", task / "search-videos.json")
            if status is not None:
                (task / "data/course_prep_status.json").write_text(json.dumps(status))
            if registry is not None:
                (task / "data/search_sources.json").write_text(json.dumps(registry))
            patched = code.replace('Path("/dli/task")', f"Path({str(task)!r})")
            self.assertNotEqual(patched, code, "the cell no longer names /dli/task literally")
            requests_module = fake_requests(sensor_ids)
            ns, out, error = {}, io.StringIO(), None
            with fake_modules(requests=requests_module, course_setup=types.ModuleType("course_setup"),
                              labtools=types.ModuleType("labtools")), contextlib.redirect_stdout(out):
                try:
                    exec(patched, ns)
                except Exception as exc:  # noqa: BLE001 - the scenario asserts on it
                    error = exc
        return ns, out.getvalue(), error, requests_module.calls

    def test_missing_status_file_raises_even_with_a_registry(self):
        ns, out, error, calls = self.run_cell(None, OK_REGISTRY)
        self.assertIsInstance(error, FileNotFoundError)
        self.assertIn("course_prep_status.json", str(error))
        self.assertNotIn("sources", ns)
        self.assertEqual(calls, [], "nothing is probed before the status file is read")

    def test_running_and_failed_raise(self):
        for state in ("RUNNING", "FAILED"):
            with self.subTest(state=state):
                ns, out, error, calls = self.run_cell(status_payload(state), OK_REGISTRY)
                self.assertIsInstance(error, RuntimeError)
                self.assertIn(state, str(error))
                self.assertIn(f"course-prep state: {state}", out)
                self.assertNotIn("sources", ns)

    def test_ok_without_registry_raises(self):
        ns, out, error, calls = self.run_cell(status_payload("OK"), None)
        self.assertIsInstance(error, FileNotFoundError)
        self.assertIn("search_sources.json", str(error))

    def test_ok_with_matching_registry_continues(self):
        ns, out, error, calls = self.run_cell(status_payload("OK"), OK_REGISTRY)
        self.assertIsNone(error, error)
        self.assertEqual(sorted(ns["sources"]), sorted(OK_REGISTRY))
        self.assertIn("course-prep state: OK", out)
        self.assertEqual([c[0] for c in calls], ["GET"])

    def test_registry_sensor_missing_from_vios_raises(self):
        ns, out, error, calls = self.run_cell(status_payload("OK"), OK_REGISTRY, sensor_ids=SENSOR_IDS[1:])
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("not in VIOS", str(error))
        self.assertIn(MANIFEST[0]["filename"], str(error))


# --- the notebook 4 embedding wait (4.2): records that are gone stop the cell at once ---

FULL_COVERAGE = {sid: item["duration_seconds"] for item, sid in zip(MANIFEST, SENSOR_IDS)}


class FakeClock:
    """time.monotonic and time.sleep for the wait loop: a sleep moves the clock, nothing really waits."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class EmbeddingWaitTests(unittest.TestCase):
    """Cell 05-collection-04 waits while the records grow. After course-prep reported OK, no record at all for the
    four videos means Elasticsearch deleted them (its index lifecycle policy), so the cell stops at once. On the
    devbox on 2026-11-03 it waited its full 1200 s on 0 records for all four videos instead."""

    def run_wait(self, answer, state="OK"):
        """Run 4.1 as the gate tests do, then 4.2 against a stand-in Elasticsearch.

        `answer(sensor_id, poll)` gives one video's (records, seconds covered) at poll 0, 1, ...; None means the
        index is missing (404). `state` is what course-prep reported when 4.1 last read its status.
        Returns (what 4.2 printed, its error or None, the (url, sensor id) of every query, the clock).
        """
        ns, _, error, _ = PreparationGateTests.run_cell(self, status_payload("OK"), OK_REGISTRY)
        self.assertIsNone(error, error)
        self.assertEqual(ns.get("prep", {}).get("state"), "OK", "4.1 binds prep, the status 4.2 reads")
        if state != "OK":
            ns["prep"] = status_payload(state)   # as if course-prep had not said OK when 4.1 read it
        anchor = datetime.fromisoformat(ns["EMBEDDING_ANCHOR"].replace("Z", "+00:00")).timestamp()
        clock, queries = FakeClock(), []

        def post(url, **kwargs):
            sensor_id = kwargs["json"]["query"]["term"]["sensor.id.keyword"]
            queries.append((url, sensor_id))
            found = answer(sensor_id, len(clock.slept))
            if found is None:
                return FakeResponse({"error": {"type": "index_not_found_exception"}}, status_code=404)
            count, seconds = found
            last_end = (anchor + seconds) * 1000 if count else None      # epoch milliseconds, as Elasticsearch
            return FakeResponse({"hits": {"total": {"value": count}},
                                 "aggregations": {"last_end": {"value": last_end}}})

        ns["requests"] = types.SimpleNamespace(post=post)
        ns["time"] = types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        out, error = io.StringIO(), None
        with contextlib.redirect_stdout(out):
            try:
                exec(text(by_id(NOTEBOOK.name, "05-collection-04")), ns)
            except Exception as exc:  # noqa: BLE001
                error = exc
        self.assertEqual({url for url, _ in queries}, {ns["BASE"] + "/es/" + ns["INDEX"] + "/_search"})
        return out.getvalue(), error, queries, clock

    def assert_records_gone(self, out, error, queries, clock, polls):
        self.assertIsInstance(error, RuntimeError)
        for needle in ("course-prep reported OK", "records are gone", "index lifecycle policy",
                       "Call the instructor", "do not upload the videos yourself", "section 4.1"):
            self.assertIn(needle, str(error))
        self.assertEqual(clock.slept, [10] * (polls - 1), "it stops at the poll that finds nothing, without sleeping")
        self.assertEqual(sorted(sensor for _, sensor in queries), sorted(SENSOR_IDS * polls),
                         "one query per video per poll")
        self.assertNotIn("embedding records, covering", out)

    def test_no_records_after_course_prep_ok_stops_at_once(self):
        """The index is missing (404), or a later upload created it again without the four videos' records (the
        devbox case: notebook 2's upload re-created every mdx-* index)."""
        for case, answer in (("index missing", lambda sensor_id, poll: None),
                             ("no record of the four videos", lambda sensor_id, poll: (0, 0.0))):
            with self.subTest(case=case):
                out, error, queries, clock = self.run_wait(answer)
                self.assert_records_gone(out, error, queries, clock, polls=1)
                self.assertNotIn("waiting for embeddings", out)

    def test_records_deleted_during_the_wait_stop_at_the_next_poll(self):
        out, error, queries, clock = self.run_wait(lambda sensor_id, poll: (10, 50.0) if poll == 0 else None)
        self.assert_records_gone(out, error, queries, clock, polls=2)
        self.assertEqual(out.count("waiting for embeddings"), 1)

    def test_growing_records_keep_the_normal_wait(self):
        """One video has records at the first poll and the other three none yet: the cell waits, then passes."""
        def answer(sensor_id, poll):
            if poll == 0:
                return (3, 15.0) if sensor_id == SENSOR_IDS[0] else (0, 0.0)
            return (20, 100.0) if poll == 1 else (36, FULL_COVERAGE[sensor_id])

        out, error, queries, clock = self.run_wait(answer)
        self.assertIsNone(error, error)
        self.assertEqual(clock.slept, [10, 10])
        self.assertEqual(len(queries), 3 * len(SENSOR_IDS))
        self.assertEqual(out.count("waiting for embeddings"), 2)
        for item in MANIFEST:
            length = item["duration_seconds"]
            line = f"{item['filename']:18} {36:5d} embedding records, covering {length:.1f} s of {length:.1f} s"
            self.assertIn(line, out)

    def test_records_that_stop_growing_time_out_after_20_minutes(self):
        out, error, queries, clock = self.run_wait(lambda sensor_id, poll: (10, 50.0))
        self.assertIsInstance(error, TimeoutError)
        self.assertIn("still incomplete", str(error))
        self.assertEqual(sum(clock.slept), 1200)

    def test_without_course_prep_ok_an_empty_index_is_waited_for(self):
        """Only course-prep's OK proves the records existed. Without it, a missing index keeps the normal wait."""
        out, error, queries, clock = self.run_wait(
            lambda sensor_id, poll: None if poll == 0 else (36, FULL_COVERAGE[sensor_id]), state="RUNNING")
        self.assertIsNone(error, error)
        self.assertEqual(clock.slept, [10])


if __name__ == "__main__":
    unittest.main()
