"""Offline tests for the Part 1 assessment: quiz.py, its two key files and the notebook's answers.json.

Run from the repo root:  python3 -I task1/tests/test_quiz.py   (pytest works too)

Standard library only. Nothing here needs the stack: quiz._http is replaced by a fake VIOS,
RT-Embed and Elasticsearch, and the notebook dry run replaces course_setup, labtools, cleanup,
requests, the Protobuf module and the docker/ffmpeg subprocesses. The dry run fills every FIXME with
its solution, runs the code cells in order and checks that every task passes and that answers.json
has exactly the shape the grader reads (task1/assessment/grader/schemas/answers.schema.json).
Two more tests keep quiz.py in step with the real course_setup.py (the T5 search) and with the
grader's rubric.json (every number a check uses).
"""
from __future__ import annotations

import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True   # loading quiz.py and the scripts by path must leave no __pycache__ in the lab content

TASK1 = Path(__file__).resolve().parents[1]
PART1 = TASK1 / "lab" / "task" / "part1"
QUIZ_PATH = PART1 / "quiz.py"
COURSE_SETUP = PART1 / "course_setup.py"
NOTEBOOK = PART1 / "assessment.ipynb"
HASHED_KEY = PART1 / "assessment_key.json"
PLAIN_KEY = TASK1 / "assessment" / "grader" / "answer_keys" / "part1.json"
RUBRIC = TASK1 / "assessment" / "grader" / "rubric.json"
MAKE_KEY = TASK1 / "scripts" / "make_quiz_key.py"

QIDS = [f"p1-q{n}" for n in range(1, 9)]
TIDS = [f"p1-t{n}" for n in range(1, 8)]
BARE_FIXME = re.compile(r'''(?<![\w"'])FIXME(?![\w"'])''')
ANCHOR = datetime(2025, 1, 1, tzinfo=timezone.utc)


def load_quiz(key_file=None):
    """A fresh copy of quiz.py. key_file=None loads the shipped hashed key, as the notebook does."""
    env = {} if key_file is None else {"ASSESSMENT_KEY": str(key_file)}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("quiz_under_test", QUIZ_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso(seconds):
    moment = ANCHOR + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class Isolated(unittest.TestCase):
    """Each test gets its own quiz module with answers.json, results.json and the work dir in a temp folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.quiz = load_quiz()
        self.quiz.WORK = self.dir
        self.quiz.RESULTS = self.dir / "results.json"
        self.quiz.ANSWERS = self.dir / "answers.json"
        self.quiz.GROUND_TRUTH = self.dir / "search_videos.json"
        self.quiz.REGISTRY = self.dir / "search_sources.json"
        self.out = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def run_quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(self.out):
            return fn(*args, **kwargs)

    def answers(self):
        return json.loads(self.quiz.ANSWERS.read_text())


# --------------------------------------------------------------------------- the generic quiz API

class QuizApiTests(Isolated):

    def test_make_key_round_trip_and_answer(self):
        self.quiz.KEY.clear()
        self.quiz.load_key({"x-q1": list(self.quiz.make_key("x-q1", "C", "Because of the reason.", 4))})
        self.assertTrue(self.run_quiet(self.quiz.answer, "x-q1", " c "))
        self.assertIn("C is correct. Because of the reason.", self.out.getvalue())
        self.assertFalse(self.run_quiet(self.quiz.answer, "x-q1", "A"))
        self.assertIn("A is not the best answer", self.out.getvalue())
        self.assertNotIn("Because of the reason.", self.out.getvalue().split("A is not")[1])
        results = json.loads(self.quiz.RESULTS.read_text())
        self.assertEqual(results["x-q1"], {"kind": "question", "answer": "A", "correct": False})
        self.assertEqual(self.answers()["questions"], {"x-q1": "A"}, "the last answer is the one saved")

    def test_invalid_letters_are_not_recorded(self):
        self.quiz.KEY.clear()
        self.quiz.load_key({"x-q1": list(self.quiz.make_key("x-q1", "B", "Why.", 4))})
        for bad in ("", "E", "AB", None):
            self.assertFalse(self.run_quiet(self.quiz.answer, "x-q1", bad))
        self.assertIn("answer with one letter, A, B, C, D", self.out.getvalue())
        self.assertFalse(self.quiz.RESULTS.exists())
        self.assertFalse(self.quiz.ANSWERS.exists())

    def test_unknown_question_raises(self):
        with self.assertRaises(KeyError):
            self.quiz.answer("p1-q99", "A")

    def test_missing_key_file_explains_itself(self):
        quiz = load_quiz(key_file=self.dir / "absent.json")
        self.assertEqual(quiz.KEY, {})
        with self.assertRaisesRegex(RuntimeError, "absent.json is missing"):
            quiz.answer("p1-q1", "A")

    def test_broken_key_file_is_reported_on_use_not_on_import(self):
        broken = self.dir / "broken.json"
        broken.write_text("{not json")
        quiz = load_quiz(key_file=broken)
        with self.assertRaisesRegex(RuntimeError, "broken.json"):
            quiz.answer("p1-q1", "A")

    def test_save_merges_and_keeps_other_keys(self):
        self.quiz.save("clip_stem", "rain_check")
        self.quiz.save("t1", {"vios_sensor_id": "s", "vios_name": "rain_check_1"})
        self.quiz.save("questions", {"p1-q1": "B"})
        self.quiz.save("questions", {"p1-q2": "D"})
        self.quiz.save("t1", {"vios_sensor_id": "s2", "vios_name": "rain_check_2"})
        self.assertEqual(self.answers(), {"clip_stem": "rain_check",
                                          "t1": {"vios_sensor_id": "s2", "vios_name": "rain_check_2"},
                                          "questions": {"p1-q1": "B", "p1-q2": "D"}})
        self.assertFalse(any(p.name.endswith(".tmp") for p in self.dir.iterdir()), "atomic write leaves no temp file")
        with self.assertRaises(ValueError):
            self.quiz.save("", 1)

    def test_task_records_pass_fail_and_exceptions(self):
        self.assertTrue(self.run_quiet(self.quiz.task, "p1-t1", "One", lambda: (True, "fine")))
        self.assertFalse(self.run_quiet(self.quiz.task, "p1-t2", "Two", lambda: (False, "not yet")))

        def broken():
            raise RuntimeError("GET http://nginx/x -> HTTP 502")

        self.assertFalse(self.run_quiet(self.quiz.task, "p1-t3", "Three", broken))
        self.assertTrue(self.run_quiet(self.quiz.task, "p1-t4", "Four", lambda: (True, "720 frame records.")))
        text = self.out.getvalue()
        self.assertIn("p1-t1 One: PASS. Fine", text, "a message reads as a sentence after PASS.")
        self.assertIn("p1-t2 Two: NOT YET. Not yet", text)
        self.assertIn("p1-t3 Three: NOT YET. RuntimeError: GET http://nginx/x -> HTTP 502", text)
        self.assertIn("p1-t4 Four: PASS. 720 frame records.", text)
        results = json.loads(self.quiz.RESULTS.read_text())
        self.assertEqual([results[k]["passed"] for k in ("p1-t1", "p1-t2", "p1-t3")], [True, False, False])
        self.assertEqual(results["p1-t2"]["message"], "Not yet", "results.json records the message as printed")

    def test_summary_counts(self):
        self.quiz.KEY.clear()
        self.quiz.load_key({"x-q1": list(self.quiz.make_key("x-q1", "A", "Why.", 4)),
                            "x-q2": list(self.quiz.make_key("x-q2", "B", "Why.", 4))})
        self.run_quiet(self.quiz.task, "p1-t1", "One", lambda: (True, ""))
        self.run_quiet(self.quiz.task, "p1-t2", "Two", lambda: (False, ""))
        self.run_quiet(self.quiz.answer, "x-q1", "A")
        self.run_quiet(self.quiz.answer, "x-q2", "A")
        counts = self.run_quiet(self.quiz.summary)
        self.assertEqual(counts, {"tasks_passed": 1, "tasks": 2, "questions_correct": 1, "questions": 2})
        self.assertIn("Tasks passed: 1 of 2. Questions correct: 1 of 2.", self.out.getvalue())

    def test_parse_time_handles_protobuf_and_elasticsearch_forms(self):
        parse = self.quiz._parse_time
        same = parse("2025-01-01T00:00:12.345Z")
        self.assertEqual(parse("2025-01-01T00:00:12.345000000Z"), same)
        self.assertEqual(parse("2025-01-01T00:00:12.345+00:00"), same)
        self.assertEqual(parse("2025-01-01T00:00:12Z"), datetime(2025, 1, 1, 0, 0, 12, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            parse("")


class HttpTests(Isolated):
    """quiz._http against a real local server: status, JSON body and no-response handling."""

    def test_status_body_and_no_response(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                status = 404 if self.path.startswith("/es/missing") else 200
                payload = json.dumps({"path": self.path, "echo": body}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.quiz.BASE = f"http://127.0.0.1:{server.server_address[1]}"
            status, data = self.quiz._http("POST", "/es/idx/_count", {"query": {"match_all": {}}})
            self.assertEqual((status, data["echo"]), (200, {"query": {"match_all": {}}}))
            self.assertEqual(self.quiz._count("missing", {"match_all": {}}), 0, "a missing index counts as 0")
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                self.quiz._json("POST", "/es/missing/_search", {})
        finally:
            server.shutdown()
            server.server_close()
        self.quiz.BASE = "http://127.0.0.1:9"     # nothing listens on the discard port
        with self.assertRaisesRegex(RuntimeError, "no response"):
            self.quiz._http("GET", "/vios/vst/api/v1/sensor/list", timeout=2)


# --------------------------------------------------------------------------- the key files

class KeyFileTests(unittest.TestCase):

    def setUp(self):
        self.plain = json.loads(PLAIN_KEY.read_text())
        self.hashed = json.loads(HASHED_KEY.read_text())

    def test_plain_key_format(self):
        self.assertEqual(self.plain["version"], 1)
        self.assertEqual(sorted(self.plain["questions"]), sorted(QIDS))
        self.assertTrue(all(letter in "ABCD" for letter in self.plain["questions"].values()))
        self.assertEqual(self.plain["min_correct"], 6)
        self.assertEqual(sorted(self.plain["explanations"]), sorted(QIDS))
        text = PLAIN_KEY.read_text()
        for needle in ("\u2014", "Cosmos " + "Reason"):   # spelled in parts so this file passes the same scans
            self.assertNotIn(needle, text)

    def test_hashed_key_matches_plain_key_for_every_question(self):
        quiz = load_quiz()
        self.assertEqual(sorted(self.hashed["questions"]), sorted(QIDS))
        for qid, letter in self.plain["questions"].items():
            with self.subTest(question=qid):
                digest, cipher, options = self.hashed["questions"][qid]
                self.assertEqual(options, 4)
                self.assertEqual(digest, hashlib.sha256(f"{qid}:{letter}".encode()).hexdigest())
                for other in "ABCD".replace(letter, ""):
                    self.assertNotEqual(digest, hashlib.sha256(f"{qid}:{other}".encode()).hexdigest())
                data = bytes.fromhex(cipher)
                text = bytes(a ^ b for a, b in zip(data, quiz._stream(qid, letter, len(data)))).decode()
                self.assertEqual(text, self.plain["explanations"][qid])
                self.assertNotIn(self.plain["explanations"][qid], HASHED_KEY.read_text())

    def test_quiz_loads_the_shipped_key_and_grades_it(self):
        quiz = load_quiz()
        self.assertEqual(quiz.KEY_FILE, HASHED_KEY)
        self.assertEqual(sorted(quiz.KEY), sorted(QIDS))
        with tempfile.TemporaryDirectory() as folder:
            quiz.RESULTS, quiz.ANSWERS = Path(folder) / "r.json", Path(folder) / "a.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                for qid, letter in self.plain["questions"].items():
                    self.assertTrue(quiz.answer(qid, letter))
                    wrong = "ABCD"[("ABCD".index(letter) + 1) % 4]
                    self.assertFalse(quiz.answer(qid, wrong))
                    self.assertTrue(quiz.answer(qid, letter))
            self.assertEqual(json.loads(quiz.ANSWERS.read_text())["questions"], self.plain["questions"])

    def test_make_quiz_key_check_passes_and_detects_drift(self):
        script = load_script(MAKE_KEY, "make_quiz_key_under_test")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(["--check"]), 0)
        with tempfile.TemporaryDirectory() as folder:
            plain = json.loads(PLAIN_KEY.read_text())
            plain["questions"]["p1-q1"] = "ABCD"[("ABCD".index(plain["questions"]["p1-q1"]) + 1) % 4]
            changed = Path(folder) / "part1.json"
            changed.write_text(json.dumps(plain))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(script.main(["--check", "--key", str(changed)]), 1)
            out = Path(folder) / "assessment_key.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(script.main(["--key", str(changed), "--out", str(out)]), 0)
            self.assertNotEqual(out.read_text(), HASHED_KEY.read_text())

    def test_make_quiz_key_rejects_a_bad_plain_key(self):
        script = load_script(MAKE_KEY, "make_quiz_key_bad_input")
        with tempfile.TemporaryDirectory() as folder:
            for broken in ({"questions": {"p1-q1": "E"}, "explanations": {"p1-q1": "x"}, "min_correct": 1},
                           {"questions": {"p1-q1": "A"}, "explanations": {}, "min_correct": 1},
                           {"questions": {"p1-q1": "A"}, "explanations": {"p1-q1": "x"}, "min_correct": 2}):
                path = Path(folder) / "bad.json"
                path.write_text(json.dumps(broken))
                with self.subTest(key=broken), self.assertRaises(ValueError):
                    script.read_plain(path)


# --------------------------------------------------------------------------- a fake deployment

class FakeStack:
    """VIOS, Elasticsearch and RT-VLM state for one learner's clip, answering quiz._http and the fake helpers."""

    STEM = "rain_check"
    SENSOR = "11111111-2222-3333-4444-555555555555"
    FILE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    SEARCH = {"normal_eval.mp4": "s-normal", "rain_eval.mp4": "s-rain", "fog_eval.mp4": "s-fog", "night_eval.mp4": "s-night"}

    def __init__(self):
        self.uploaded = False
        self.frames = 720
        self.types = [("car", 6200), ("person", 300)]
        self.vector = [0.01] * 1152
        self.query_frame = {"id": "240", "timestamp": iso(10), "sensorId": self.STEM,
                            "objects": [{"id": "7", "type": "car", "bbox": {"leftX": 10, "topY": 10, "rightX": 90, "bottomY": 60},
                                         "embedding": {"vector": self.vector}},
                                        {"id": "9", "type": "car", "bbox": {"leftX": 0, "topY": 0, "rightX": 20, "bottomY": 20},
                                         "embedding": {"vector": self.vector}}]}
        self.kafka_frame = {"id": "100", "sensorId": self.STEM, "timestamp": iso(4.167), "objects": [{"id": "3"}]}
        self.best_id = "7"
        self.captions_indexed = 3
        self.embed_top = {"sensor": {"id": "s-rain"}, "timestamp": iso(20), "end": iso(25)}   # the T5 top hit
        self.calls = []

    @staticmethod
    def registry():
        """course-prep's search_sources.json for the four search videos."""
        return {name: {"filename": name, "sensor_id": sid, "creation_time": "2025-01-02T00:00:00.000Z",
                       "duration_seconds": 180.25, "completed": True} for name, sid in FakeStack.SEARCH.items()}

    # quiz._http
    def http(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        if path == "/vios/vst/api/v1/sensor/list":
            return 200, self.sensors()
        if path == f"/vios/vst/api/v1/storage/{self.SENSOR}/timelines":
            return 200, [{"startTime": iso(0), "endTime": iso(30)}] if self.uploaded else []
        if path == "/rtvi-embed/v1/generate_text_embeddings":
            return 200, {"data": [{"embeddings": [0.1] * 768}]}
        if path.startswith("/es/"):
            return self.es(method, path[3:], body)
        return 404, {"error": path}

    def sensors(self):
        return [{"sensorId": self.SENSOR, "name": self.STEM + "_1"}] if self.uploaded else []

    def es(self, method, path, body):
        index, _, op = path.strip("/").partition("/")
        text = json.dumps(body or {})
        if op == "_count":
            if index.startswith("default_"):
                return 200, {"count": self.captions_indexed if index == "default_" + self.FILE_ID.replace("-", "_") else 0}
            if index.startswith("mdx-raw"):
                return 200, {"count": self.frames if self.uploaded and self.STEM in text else 0}
            return 404, {"error": "no such index"}
        if op != "_search":
            return 404, {"error": path}
        if index.startswith("mdx-embed-filtered"):
            return 200, {"hits": {"hits": [{"_score": 0.62, "_source": self.embed_top}]}}
        if "aggs" in (body or {}):
            buckets = [{"key": key, "doc_count": n} for key, n in self.types]
            return 200, {"aggregations": {"objects": {"types": {"buckets": buckets}}}, "hits": {"hits": []}}
        if '"knn"' in text:
            assert body["_source"] == {"excludes": ["objects.embedding"]}, body["_source"]
            frame = {"id": "241", "timestamp": iso(10.042),
                     "objects": [{"id": "9", "type": "car"}, {"id": self.best_id, "type": "car"}]}
            inner = {"objects": {"hits": {"hits": [{"_score": 0.97, "_nested": {"field": "objects", "offset": 1}}]}}}
            return 200, {"hits": {"hits": [{"_source": frame, "inner_hits": inner}]}}
        if '"range"' in text:
            return 200, {"hits": {"hits": [{"_source": {"id": "239", "timestamp": iso(9.958), "objects": []}},
                                           {"_source": self.query_frame}]}}
        if '"id.keyword"' in text:
            frame_id = body["query"]["bool"]["filter"][1]["bool"]["should"][0]["term"]["id.keyword"]
            if frame_id == self.kafka_frame["id"]:
                return 200, {"hits": {"hits": [{"_source": {"id": frame_id, "timestamp": self.kafka_frame["timestamp"],
                                                             "sensorId": self.STEM}}]}}
            return 200, {"hits": {"hits": []}}
        if '"timestamp"' in text:
            return 200, {"hits": {"hits": [{"_source": self.query_frame}]}}
        return 200, {"hits": {"hits": []}}


class ChecksTests(Isolated):
    """Each check_* against the fake stack: the pass case and the mistakes a learner makes."""

    def setUp(self):
        super().setUp()
        self.stack = FakeStack()
        self.stack.uploaded = True
        self.patch = mock.patch.object(self.quiz, "_http", self.stack.http)
        self.patch.start()
        self.quiz.save("clip_stem", "rain_check")

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def check(self, name, *args):
        return getattr(self.quiz, name)(*args)

    def test_t1(self):
        self.quiz.save("t1", {"vios_sensor_id": FakeStack.SENSOR, "vios_name": "rain_check_1"})
        passed, message = self.check("check_t1")
        self.assertTrue(passed, message)
        self.assertIn("720 frame records so far", message)
        self.quiz.save("t1", {"vios_sensor_id": "not-a-sensor", "vios_name": ""})
        self.assertFalse(self.check("check_t1")[0])
        self.quiz.save("t1", {"vios_sensor_id": FakeStack.SENSOR, "vios_name": "rain_check"})
        self.assertIn("VIOS names this source 'rain_check_1'", self.check("check_t1")[1])
        self.stack.frames = 0
        self.quiz.save("t1", {"vios_sensor_id": FakeStack.SENSOR, "vios_name": "rain_check_1"})
        passed, message = self.check("check_t1")
        self.assertTrue(passed)
        self.assertIn("did not run", message)

    def test_t1_wrong_clip_name(self):
        self.stack.STEM = "rain_check"
        self.quiz.save("clip_stem", "other_clip")
        self.quiz.save("t1", {"vios_sensor_id": FakeStack.SENSOR, "vios_name": "rain_check_1"})
        passed, message = self.check("check_t1")
        self.assertFalse(passed)
        self.assertIn("not to other_clip", message)

    def test_t2(self):
        self.quiz.save("t2", {"frames": 720, "top_type": "car"})
        self.assertTrue(self.check("check_t2")[0])
        self.quiz.save("t2", {"frames": 700, "top_type": "car"})
        self.assertIn("still", self.check("check_t2")[1])
        self.quiz.save("t2", {"frames": 1440, "top_type": "car"})
        self.assertIn("Filter on the field", self.check("check_t2")[1])
        self.quiz.save("t2", {"frames": 720, "top_type": "person"})
        self.assertFalse(self.check("check_t2")[0])
        self.quiz.save("t2", {"frames": "720", "top_type": "car"})
        self.assertFalse(self.check("check_t2")[0])

    def test_t3(self):
        frame = self.stack.kafka_frame
        self.quiz.save("t3", {"sensor_id": "rain_check", "frame_id": "100", "timestamp": frame["timestamp"]})
        self.assertTrue(self.check("check_t3")[0])
        self.quiz.save("t3", {"sensor_id": "rain_check", "frame_id": "100", "timestamp": "2025-01-01T00:00:04.167000000Z"})
        self.assertTrue(self.check("check_t3")[0], "a nanosecond Protobuf time is the same instant")
        self.quiz.save("t3", {"sensor_id": "traffic_short", "frame_id": "100", "timestamp": frame["timestamp"]})
        self.assertFalse(self.check("check_t3")[0])
        self.quiz.save("t3", {"sensor_id": "rain_check", "frame_id": "999", "timestamp": frame["timestamp"]})
        self.assertFalse(self.check("check_t3")[0])
        self.quiz.save("t3", {"sensor_id": "rain_check", "frame_id": "100", "timestamp": iso(20)})
        self.assertFalse(self.check("check_t3")[0])

    def test_t4(self):
        good = {"file_id": FakeStack.FILE_ID, "chunks_10": 3, "chunks_0": 1, "frame_counts_8": [8, 8, 8]}
        self.quiz.save("t4", good)
        passed, message = self.check("check_t4")
        self.assertTrue(passed, message)
        self.assertIn("3 chunks at 10 s", message)
        # A cut that runs a fraction of a second past 30 s adds a short fourth chunk with fewer frames.
        self.quiz.save("t4", {**good, "chunks_10": 4, "frame_counts_8": [8, 8, 8, 1]})
        passed, message = self.check("check_t4")
        self.assertTrue(passed, message)
        self.assertIn("4 chunks at 10 s", message)
        for change in ({"chunks_10": 2}, {"chunks_10": 5}, {"chunks_10": True}, {"chunks_0": 3},
                       {"frame_counts_8": [1, 1, 1]}, {"frame_counts_8": [8, 8, 1]}, {"frame_counts_8": [8, 8, 8, 9]},
                       {"frame_counts_8": [8, 8, 8, 8, 8]}, {"frame_counts_8": []}, {"frame_counts_8": "8"}):
            with self.subTest(change=change):
                self.quiz.save("t4", {**good, **change})
                self.assertFalse(self.check("check_t4")[0])
        self.quiz.save("t4", {**good, "frame_counts_8": [1, 1, 1]})
        self.assertIn("Keep the two frames fields", self.check("check_t4")[1])
        # A wrong chunk-length field leaves RT-VLM at one chunk for the whole clip.
        self.quiz.save("t4", {**good, "frame_counts_8": [8]})
        self.assertIn("Name the field that sets the chunk length", self.check("check_t4")[1])
        self.quiz.save("t4", {**good, "frame_counts_8": [None, None, None]})
        passed, message = self.check("check_t4")
        self.assertFalse(passed)
        self.assertIn("RT-VLM did not report frame_count", message)
        self.assertNotIn("Name the field", message, "a missing field is not the learner's mistake")
        self.stack.captions_indexed = 0
        self.quiz.save("t4", good)
        with mock.patch.object(self.quiz.time, "sleep"):
            passed, message = self.quiz.check_t4(wait_s=0)
        self.assertFalse(passed)
        self.assertIn("still empty", message)

    def write_truth(self):
        truth = [{"file": name, "duration_s": 180.25,
                  "events": [{"type": "collision", "start_s": 19.21, "end_s": 43.75},
                             {"type": "stalled", "start_s": 120.25, "end_s": 144.67}]}
                 for name in FakeStack.SEARCH]
        self.quiz.GROUND_TRUTH.write_text(json.dumps(truth))

    def write_registry(self):
        self.quiz.REGISTRY.write_text(json.dumps(FakeStack.registry()))

    def save_t5(self, start, end, file="rain_eval.mp4", query="two cars collide"):
        self.quiz.save("t5", {"query": query, "file": file, "start_s": start, "end_s": end})

    def test_t5_passes_on_the_reference_window_and_reports_the_overlap(self):
        self.write_truth()
        self.write_registry()
        self.save_t5(20.0, 25.0)
        passed, message = self.check("check_t5")
        self.assertTrue(passed, message)
        self.assertIn("overlaps the labelled collision at 19.2 to 43.8 s", message)
        self.save_t5(20.04, 24.96)
        self.assertTrue(self.check("check_t5")[0], "rounding to 0.01 s stays inside the slack")
        # The search sends the query as typed, and the kNN request of course_setup.knn_search for k=5.
        embedded = [body for _, path, body in self.stack.calls if path == "/rtvi-embed/v1/generate_text_embeddings"]
        self.assertEqual(embedded[-1], {"text_input": ["two cars collide"], "model": self.quiz.EMBED_MODEL})
        searched = [(path, body) for _, path, body in self.stack.calls if path.startswith("/es/mdx-embed-filtered")]
        path, body = searched[-1]
        self.assertEqual(path, "/es/mdx-embed-filtered-2025-01-01/_search")
        ids = ["s-normal", "s-rain", "s-fog", "s-night"]
        self.assertEqual(body, self.quiz._search_body([0.1] * 768, ids))
        self.assertEqual(body["size"], 5)
        knn = body["query"]["bool"]["must"][0]["nested"]["query"]["knn"]
        self.assertEqual((knn["k"], knn["num_candidates"]), (5, 40))
        self.assertEqual(knn["filter"], {"terms": {"sensor.id.keyword": ids}}, "the four search videos only")

    def test_t5_overlap_is_reported_but_does_not_decide(self):
        self.write_truth()
        self.write_registry()
        self.stack.embed_top = {"sensor": {"id": "s-night"}, "timestamp": iso(120), "end": iso(125)}
        self.save_t5(120.0, 125.0, file="night_eval.mp4")
        passed, message = self.check("check_t5")
        self.assertTrue(passed, message)
        self.assertIn("overlaps no labelled collision", message)
        self.quiz.GROUND_TRUTH.unlink()
        passed, message = self.check("check_t5")
        self.assertTrue(passed, message)
        self.assertIn("The labels file is missing", message)

    def test_t5_mistakes(self):
        self.write_truth()
        self.write_registry()
        self.save_t5(-86380.0, -86375.0)       # measured from the VIOS anchor, a day later
        passed, message = self.check("check_t5")
        self.assertFalse(passed)
        self.assertIn("the same search gives rain_eval.mp4 20.0 to 25.0 s", message)
        self.assertIn("anchor the embedding index uses", message)
        self.save_t5(21.5, 26.5)               # off by more than the slack
        self.assertFalse(self.check("check_t5")[0])
        self.save_t5(20.0, 25.0, file="fog_eval.mp4")
        passed, message = self.check("check_t5")
        self.assertFalse(passed)
        self.assertIn("not a window of fog_eval.mp4", message)
        for query in ("", "   ", None):
            with self.subTest(query=query):
                self.save_t5(20.0, 25.0, query=query)
                self.assertIn("save the query", self.check("check_t5")[1])
        self.quiz.save("t5", {"file": "rain_eval.mp4", "start_s": 20.0, "end_s": 25.0})
        self.assertIn("save the query", self.check("check_t5")[1])
        self.save_t5("20", 25.0)
        self.assertIn("must be numbers", self.check("check_t5")[1])
        self.quiz.REGISTRY.unlink()
        self.save_t5(20.0, 25.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(self.quiz.task("p1-t5", "T5", self.quiz.check_t5))
        self.assertIn("search_sources.json is missing", out.getvalue())

    def test_t6(self):
        self.quiz.save("t6", {"query_object_id": "7", "answer_object_id": "7", "query_frame_time": iso(10)})
        passed, message = self.check("check_t6")
        self.assertTrue(passed, message)
        self.assertIn("same vehicle", message)
        knn = [body for _, path, body in self.stack.calls if '"knn"' in json.dumps(body or {})][-1]
        nested = knn["query"]["nested"]
        self.assertEqual(nested["path"], "objects")
        self.assertEqual(nested["query"]["knn"]["field"], "objects.embedding.vector")
        self.assertEqual(nested["query"]["knn"]["filter"]["bool"]["must_not"], [{"term": {"timestamp": iso(10)}}])
        self.quiz.save("t6", {"query_object_id": "7", "answer_object_id": "9", "query_frame_time": iso(10)})
        self.assertFalse(self.check("check_t6")[0])
        self.quiz.save("t6", {"query_object_id": "42", "answer_object_id": "7", "query_frame_time": iso(10)})
        self.assertIn("not in the frame", self.check("check_t6")[1])
        self.quiz.save("t6", {"query_object_id": "7", "answer_object_id": "7"})
        self.assertIn("save query_frame_time", self.check("check_t6")[1])
        self.assertTrue(self.check("check_t6", iso(10))[0], "an explicit frame time still works")

    def test_t7(self):
        (self.dir / "t4_captions.json").write_text(json.dumps(
            {"chunk_duration_10": [{"vlm_latency_ms": 3000.0}, {"vlm_latency_ms": 3200.0}, {"vlm_latency_ms": 2800.0}]}))

        def save(latency, requests, busy):
            self.quiz.save("t7", {"latency_ms": latency, "requests_per_day": requests, "busy_seconds_per_day": busy})

        save(3000.0, 8640, 25920.0)
        passed, message = self.check("check_t7")
        self.assertTrue(passed, message)
        self.assertIn("8,640 requests a day at 3000 ms each: 7.2 GPU-hours a day", message)
        save(3200.0, 8640, 27648.0)
        self.assertTrue(self.check("check_t7")[0], "one chunk's latency is within 15 % of the mean")
        save(3000.0, 8640, 27000.0)
        self.assertTrue(self.check("check_t7")[0], "GPU-seconds within 15 %")
        save(3000.0, 8640.0, 25920.0)
        self.assertTrue(self.check("check_t7")[0], "8640.0 is the same count")
        save(9000.0, 8640, 77760.0)
        self.assertIn("LATENCY_MS", self.check("check_t7")[1])
        for requests in (8641, 864, 86400, "8640", None):
            with self.subTest(requests=requests):
                save(3000.0, requests, 25920.0)
                self.assertIn("REQUESTS_PER_DAY", self.check("check_t7")[1])
        for busy in (25.92, 25920000.0, 3, None, "25920"):
            with self.subTest(busy=busy):
                save(3000.0, 8640, busy)
                self.assertIn("GPU_SECONDS_PER_DAY", self.check("check_t7")[1])
        (self.dir / "t4_captions.json").unlink()
        self.assertIn("run the T4 cells first", self.check("check_t7")[1])

    def test_missing_answers_are_reported(self):
        self.quiz.ANSWERS.unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(self.quiz.task("p1-t1", "T1", self.quiz.check_t1))
        self.assertIn("no clip_stem", out.getvalue())
        self.quiz.save("clip_stem", "rain_check")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(self.quiz.task("p1-t2", "T2", self.quiz.check_t2))
        self.assertIn("no 't2' entry yet", out.getvalue())

    def test_wait_until_steady(self):
        counts = iter([0, 0, 300, 720, 720, 720, 720])
        clock = {"now": 0.0}
        with mock.patch.object(self.quiz, "_count", lambda index, query: next(counts)), \
             mock.patch.object(self.quiz.time, "monotonic", lambda: clock["now"]), \
             mock.patch.object(self.quiz.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s)):
            self.assertEqual(self.run_quiet(self.quiz.wait_until_steady, "rain_check"), 720)
        self.assertEqual(clock["now"], 50.0, "returns once the count held for 20 s")
        with mock.patch.object(self.quiz, "_count", lambda index, query: 0), \
             mock.patch.object(self.quiz.time, "monotonic", lambda: clock["now"]), \
             mock.patch.object(self.quiz.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s)), \
             self.assertRaises(TimeoutError):
            self.run_quiet(self.quiz.wait_until_steady, "rain_check", timeout_s=60)


# --------------------------------------------------------------------------- the same search and numbers elsewhere

class SameAsNotebookAndGraderTests(unittest.TestCase):
    """quiz.py repeats notebook 4's search (course_setup) and the grader's numbers (rubric.json): keep them in step."""

    def test_reference_search_is_course_setup_search(self):
        if not COURSE_SETUP.is_file():
            self.skipTest(f"{COURSE_SETUP} not found")
        with mock.patch.dict(sys.modules, {"requests": types.ModuleType("requests")}):   # course_setup imports it
            spec = importlib.util.spec_from_file_location("course_setup_for_quiz_test", COURSE_SETUP)
            cs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cs)
        quiz = load_quiz()
        for name in ("EMBED_INDEX", "EMBED_MODEL", "EMBEDDING_ANCHOR"):
            self.assertEqual(getattr(quiz, name), getattr(cs, name), name)
        sent = []

        def fake_json(method, url, timeout, **kwargs):
            sent.append((method, url, kwargs.get("json")))
            return {"data": [{"embeddings": [0.5]}]} if url.endswith("/generate_text_embeddings") else {"hits": {"hits": []}}

        ids = ["s-normal", "s-rain", "s-fog", "s-night"]
        with mock.patch.object(cs, "_json", fake_json):
            cs.text_embedding("two cars collide")
            cs.knn_search([0.5] * 768, ids, k=quiz.SEARCH_K)
        (m1, url1, body1), (m2, url2, body2) = sent
        self.assertEqual((m1, url1), ("POST", quiz.BASE + "/rtvi-embed/v1/generate_text_embeddings"))
        self.assertEqual(body1, {"text_input": ["two cars collide"], "model": quiz.EMBED_MODEL})
        self.assertEqual((m2, url2), ("POST", quiz.BASE + "/es/" + quiz.EMBED_INDEX + "/_search"))
        self.assertEqual(body2, quiz._search_body([0.5] * 768, ids))

    def test_numbers_match_the_grader_rubric(self):
        if not RUBRIC.is_file():
            self.skipTest(f"{RUBRIC} not found")
        section = json.loads(RUBRIC.read_text())["part1"]
        limits, quiz = section["thresholds"], load_quiz()
        chunks_10 = limits["chunks_at_10_s"]
        self.assertEqual(sorted(quiz.CLIP_CHUNKS_10), sorted(chunks_10 if isinstance(chunks_10, list) else [chunks_10]))
        self.assertEqual(limits["chunks_at_0_s"], 1)
        self.assertEqual(quiz.FRAMES_PER_CHUNK, limits["frames_per_chunk"])
        self.assertEqual(quiz.KAFKA_SLACK_S, limits["kafka_timestamp_slack_s"])
        self.assertEqual(quiz.CHUNK_S, limits["chunk_duration_s"])
        self.assertEqual(quiz.TOLERANCE, limits["throughput_tolerance"])
        self.assertEqual(quiz.SEARCH_K, limits.get("search_k", quiz.SEARCH_K))
        self.assertEqual(quiz.SEARCH_SLACK_S, limits.get("search_slack_s", quiz.SEARCH_SLACK_S))
        self.assertEqual(quiz.DAY_S, limits.get("seconds_per_day", quiz.DAY_S))
        self.assertEqual(list(quiz.SEARCH_FILES), section.get("search_files", list(quiz.SEARCH_FILES)))
        self.assertEqual(section["clip_stem"], "rain_check")


# --------------------------------------------------------------------------- the notebook, dry run

SOLUTIONS = {
    "p1-t1-todo": ['"/agent/api/v1/videos"', '"sensorId"', '"/agent/api/v1/videos/" + SENSOR_ID + "/complete"'],
    "p1-t2-todo": ['"sensorId.keyword"', '"objects"', '"objects.type.keyword"'],
    "p1-t3-todo": ["ParseFromString", "sensorId"],
    "p1-t4-todo": ['"Did two vehicles collide in this clip? Answer yes or no."', "10", "0", '"chunk_duration"'],
    "p1-t5-todo": ['"vehicle collision"'],
    "p1-t6-todo": ['"objects.embedding.vector"', "QUERY_VECTOR"],
    "p1-t7-todo": ["sum(latencies) / len(latencies)", "24 * 60 * 60 // 10", "REQUESTS_PER_DAY * LATENCY_MS / 1000"],
}


def fill(source, values):
    parts = BARE_FIXME.split(source)
    if len(parts) != len(values) + 1:
        raise AssertionError(f"{len(parts) - 1} FIXMEs but {len(values)} solutions")
    return "".join(part + value for part, value in zip(parts, values + [""]))


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeTimestamp:
    def __init__(self, text):
        self.text = text

    def ToJsonString(self):
        return self.text


class FakeFrame:
    def ParseFromString(self, payload):
        data = json.loads(payload)
        self.id, self.sensorId = data["id"], data["sensorId"]
        self.timestamp, self.objects = FakeTimestamp(data["timestamp"]), data["objects"]


class NotebookDryRun(Isolated):
    """Run every code cell of assessment.ipynb with the solutions filled in, against the fake stack."""

    def setUp(self):
        super().setUp()
        if not NOTEBOOK.is_file():
            self.skipTest(f"{NOTEBOOK} not found")
        self.nb = json.loads(NOTEBOOK.read_text())
        self.stack = FakeStack()
        self.data = self.dir / "data"
        (self.data / "search-videos").mkdir(parents=True)
        (self.data / "search-videos" / "rain_eval.mp4").write_bytes(b"source video")
        self.quiz.REGISTRY = self.data / "search_sources.json"
        self.quiz.REGISTRY.write_text(json.dumps(FakeStack.registry()))
        self.quiz.GROUND_TRUTH = self.data / "search_videos.json"
        self.quiz.GROUND_TRUTH.write_text(json.dumps([{"file": "rain_eval.mp4", "duration_s": 180.25, "events": [
            {"type": "collision", "start_s": 19.21, "end_s": 43.75}]}]))
        self.remembered = []
        self.posted = []

    # ---- fakes for the modules the notebook imports
    def course_setup(self):
        stack = self
        cs = types.ModuleType("course_setup")
        cs.BASE = "http://nginx"
        cs.EMBEDDING_ANCHOR = "2025-01-01T00:00:00.000Z"

        def parse(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        cs.iso_offset = lambda anchor, s: (parse(anchor) + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        cs.offset_seconds = lambda anchor, t: (parse(t) - parse(anchor)).total_seconds()
        cs.find_sensor = lambda stem: next((s for s in stack.stack.sensors() if s["name"].startswith(stem)), None)
        cs.sensors = stack.stack.sensors
        cs.upload_url = lambda url: url.replace(":80/", ":30888/")
        cs.source_filter = lambda name: {"bool": {"should": [{"term": {"sensorId.keyword": name}},
                                                             {"prefix": {"sensorId.keyword": name + "_"}}],
                                                  "minimum_should_match": 1}}

        def es(method, path, **kwargs):
            status, data = stack.stack.es(method, path, kwargs.get("json"))
            assert status == 200, (path, data)
            return data

        cs.es = es
        cs.vlm_model_id = lambda: "cosmos-3-super"
        cs.register_vios_window = lambda sensor, start, end, created: FakeStack.FILE_ID
        cs.caption = lambda file_id, prompt, chunk_duration=10, **kw: [
            {"content": "two cars collide", "frame_count": 1, "vlm_latency_ms": latency}
            for latency in ([3000.0, 3200.0, 2800.0] if chunk_duration == 10 else [5000.0])]
        cs.clip_bytes = lambda sensor, start, end: b"mp4"
        cs.text_embedding = lambda text: [0.1] * 768
        cs.knn_search = lambda vector, sensors, k: [
            {"_score": 0.62, "_source": {"sensor": {"id": "s-rain"}, "timestamp": "2025-01-01T00:00:20.000Z",
                                         "end": "2025-01-01T00:00:25.000Z"}}]
        return cs

    def requests_module(self):
        stack = self
        module = types.ModuleType("requests")

        def post(url, **kwargs):
            stack.posted.append(url)
            if url == "http://nginx/agent/api/v1/videos":
                return FakeResponse(200, {"url": "http://10.0.0.1:80/vst/api/v1/storage/file"})
            if url.startswith("http://10.0.0.1:30888/"):
                return FakeResponse(200, {"sensorId": FakeStack.SENSOR})
            if url == f"http://nginx/agent/api/v1/videos/{FakeStack.SENSOR}/complete":
                stack.stack.uploaded = True
                return FakeResponse(200, {"status": "ok"})
            if url == "http://nginx/rtvi-vlm/v1/generate_captions":
                body = kwargs["json"]
                frames = int(body.get("num_frames_per_second_or_fixed_frames_chunk") or 1)
                assert body.get("use_fps_for_chunking") is False
                chunks = 3 if body.get("chunk_duration") == 10 else 1   # without the field: one chunk for the clip
                return FakeResponse(200, {"chunk_responses": [
                    {"content": "two cars collide", "frame_count": frames, "vlm_latency_ms": 4000.0}
                    for _ in range(chunks)]})
            if url == "http://assessment:8081/grade/part1":
                return FakeResponse(200, {"passed": True, "checks": [], "graded_at": "now"})
            return FakeResponse(404, {"error": url})

        module.post = post
        return module

    def subprocess_run(self, args, **kwargs):
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"cut clip")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "kafka-get-offsets" in args:
            return subprocess.CompletedProcess(args, 0, "mdx-raw:0:50\nmdx-raw:1:0\nmdx-behavior:0:7\n", "")
        if "kafka-console-consumer" in args:
            mark = args[args.index("--formatter-property") + 1].split("=", 1)[1].encode()
            other = dict(self.stack.kafka_frame, sensorId="traffic_short", id="5")
            body = b"".join(json.dumps(frame).encode() + mark for frame in (other, self.stack.kafka_frame))
            return subprocess.CompletedProcess(args, 0, body, b"")
        raise AssertionError(f"unexpected subprocess {args}")

    def test_solutions_pass_and_answers_match_the_grader_schema(self):
        plain = json.loads(PLAIN_KEY.read_text())["questions"]
        modules = {
            "course_setup": self.course_setup(),
            "labtools": types.SimpleNamespace(show_video=lambda *a, **k: None, frame_from_mp4=lambda *a, **k: "frame",
                                              draw_boxes=lambda image, objects: image),
            "cleanup": types.SimpleNamespace(remember=lambda kind, rid, **kw: self.remembered.append((kind, rid))),
            "quiz": self.quiz,
            "requests": self.requests_module(),
            "schema_pb2": types.SimpleNamespace(Frame=FakeFrame),
            "course_ui": types.SimpleNamespace(results=lambda result, label: None),
        }
        clock = {"now": 0.0}
        ns = {}
        cells = [c for c in self.nb["cells"] if c["cell_type"] == "code"]
        with mock.patch.dict(sys.modules, modules), \
             mock.patch.object(self.quiz, "_http", self.stack.http), \
             mock.patch.object(subprocess, "run", self.subprocess_run), \
             mock.patch.object(self.quiz.time, "monotonic", lambda: clock["now"]), \
             mock.patch.object(self.quiz.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s)), \
             contextlib.redirect_stdout(self.out):
            for item in cells:
                source = "".join(item["source"])
                if item["id"] in SOLUTIONS:
                    source = fill(source, SOLUTIONS[item["id"]])
                if item["id"] in plain:
                    source = source.replace('""', f'"{plain[item["id"]]}"', 1)
                source = source.replace("/dli/task/data/", str(self.data) + "/")
                self.assertIsNone(BARE_FIXME.search(source), item["id"])
                exec(compile(source, item["id"], "exec"), ns)

        text = self.out.getvalue()
        for tid in TIDS:
            self.assertRegex(text, rf"{tid} [^\n]*: PASS\.", f"{tid} did not pass:\n{text}")
        self.assertNotIn("NOT YET", text)
        self.assertIn("Part 1: PASSED", text)

        answers = self.answers()
        self.assertEqual(set(answers), {"clip_stem", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "questions"})
        self.assertEqual(answers["clip_stem"], "rain_check")
        schema = {
            "t1": {"vios_sensor_id": str, "vios_name": str},
            "t2": {"frames": int, "top_type": str},
            "t3": {"sensor_id": str, "frame_id": str, "timestamp": str},
            "t4": {"file_id": str, "chunks_10": int, "chunks_0": int, "frame_counts_8": list},
            "t5": {"query": str, "file": str, "start_s": float, "end_s": float},
            "t6": {"query_object_id": str, "answer_object_id": str, "query_frame_time": str},
            "t7": {"latency_ms": float, "requests_per_day": int, "busy_seconds_per_day": float},
        }
        for key, fields in schema.items():
            with self.subTest(answer=key):
                self.assertEqual(set(answers[key]), set(fields))
                for field, kind in fields.items():
                    self.assertIsInstance(answers[key][field], kind, f"{key}.{field}")
                    self.assertNotIsInstance(answers[key][field], bool)
        self.assertTrue(all(isinstance(n, int) for n in answers["t4"]["frame_counts_8"]))
        self.assertEqual(answers["questions"], plain)
        self.assertEqual(answers["t1"], {"vios_sensor_id": FakeStack.SENSOR, "vios_name": "rain_check_1"})
        self.assertEqual(answers["t2"], {"frames": 720, "top_type": "car"})
        self.assertEqual(answers["t4"]["frame_counts_8"], [8, 8, 8])
        self.assertEqual(answers["t5"], {"query": "vehicle collision", "file": "rain_eval.mp4",
                                         "start_s": 20.0, "end_s": 25.0})
        self.assertEqual(answers["t7"], {"latency_ms": 3000.0, "requests_per_day": 8640, "busy_seconds_per_day": 25920.0})
        self.assertEqual(answers["t6"], {"query_object_id": "7", "answer_object_id": "7", "query_frame_time": iso(10)})
        self.assertEqual(sorted(kind for kind, _ in self.remembered), ["caption", "video"])
        self.assertIn("http://assessment:8081/grade/part1", self.posted)


class NotebookShapeTests(unittest.TestCase):
    """The ids the course path, the grader and this file rely on (PLAN section 8b)."""

    def setUp(self):
        if not NOTEBOOK.is_file():
            self.skipTest(f"{NOTEBOOK} not found")
        self.nb = json.loads(NOTEBOOK.read_text())
        self.by_id = {c["id"]: "".join(c["source"]) for c in self.nb["cells"]}

    def test_task_and_question_ids(self):
        code = "\n".join(src for c, src in zip(self.nb["cells"], self.by_id.values()) if c["cell_type"] == "code")
        self.assertEqual(sorted(set(re.findall(r'quiz\.task\("(p1-t\d)"', code))), TIDS)
        self.assertEqual(sorted(set(re.findall(r'quiz\.answer\("(p1-q\d)"', code))), QIDS)
        for qid in QIDS:
            self.assertIn(f'quiz.answer("{qid}", "")', self.by_id[qid], "answer cells ship empty")
        for tid in TIDS:
            self.assertIn(f'quiz.task("{tid}"', self.by_id[tid])

    def test_submit_cell(self):
        submit = self.by_id["p1-submit"]
        self.assertIn('"http://assessment:8081/grade/part1"', submit)
        self.assertIn("course_ui.results(", submit)

    def test_every_fixme_has_a_solution_here(self):
        for cell_id, values in SOLUTIONS.items():
            with self.subTest(cell=cell_id):
                self.assertEqual(len(BARE_FIXME.findall(self.by_id[cell_id])), len(values))
        unknown = [c["id"] for c in self.nb["cells"]
                   if c["cell_type"] == "code" and BARE_FIXME.search("".join(c["source"])) and c["id"] not in SOLUTIONS]
        self.assertEqual(unknown, [])


if __name__ == "__main__":
    unittest.main()
