"""Offline unit tests for task1/lab/task/part1/course_setup.py.

Run from the repo root: python3 -I task1/tests/test_course_setup.py
(as a script, like test_notebook_checks.py; under -I the CWD is not on sys.path, so
"-m unittest <path>" cannot resolve the file as a module).
Every HTTP call is intercepted at requests.request; nothing touches the network.
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

MODULE_PATH = Path(__file__).resolve().parents[1] / "lab/task/part1/course_setup.py"
_spec = importlib.util.spec_from_file_location("course_setup", MODULE_PATH)
course_setup = importlib.util.module_from_spec(_spec)
sys.modules["course_setup"] = course_setup
_spec.loader.exec_module(course_setup)


def response(status=200, body=None, text=None, content=None):
    """A stand-in for requests.Response with just the members course_setup reads."""
    fake = Mock(spec=["status_code", "json", "text", "content"])
    fake.status_code = status
    if body is not None:
        fake.json.return_value = body
        fake.text = json.dumps(body)
    else:
        fake.json.side_effect = ValueError("not JSON")
        fake.text = "" if text is None else text
    fake.content = b"" if content is None else content
    return fake


class FakeClock:
    """Deterministic time.monotonic/time.sleep for the wait loop."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class CourseSetupTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(course_setup.requests, "request")
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    # -- shared assertions ------------------------------------------------

    def assertEveryCallHasTimeout(self):
        self.assertTrue(self.request.call_args_list, "no HTTP call was made")
        for call in self.request.call_args_list:
            timeout = call.kwargs.get("timeout")
            self.assertIsNotNone(timeout, f"call without timeout: {call}")
            self.assertGreater(timeout, 0, f"non-positive timeout: {call}")

    def calls(self):
        return [(c.args[0], c.args[1]) for c in self.request.call_args_list]

    # -- upload_url -------------------------------------------------------

    def test_upload_url_rewrites_the_public_port_once(self):
        public = "http://10.0.0.5:80/api/v1/storage/file?x=:80/"
        self.assertEqual(course_setup.upload_url(public),
                         "http://10.0.0.5:30888/api/v1/storage/file?x=:80/")

    def test_upload_url_raises_without_the_public_port(self):
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.upload_url("http://vios:30888/api/v1/storage/file")
        self.assertIn("VSS_EXTERNAL_HOST", str(ctx.exception))

    # -- ingest_file ------------------------------------------------------

    def test_ingest_file_runs_the_three_call_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "rain_eval.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            self.request.side_effect = [
                response(body={"url": "http://10.0.0.5:80/api/v1/storage/file"}),
                response(body={"sensorId": "sensor-uuid-1"}),
                response(body={"status": "ok"}),
            ]
            sensor_id = course_setup.ingest_file(str(clip), "rain_eval.mp4", "2025-01-02T00:00:00")

        self.assertEqual(sensor_id, "sensor-uuid-1")
        self.assertEveryCallHasTimeout()
        first, second, third = self.request.call_args_list
        self.assertEqual(first.args, ("POST", "http://nginx/agent/api/v1/videos"))
        self.assertEqual(first.kwargs["json"], {"filename": "rain_eval.mp4"})
        self.assertEqual(second.args, ("POST", "http://10.0.0.5:30888/api/v1/storage/file"))
        self.assertEqual(second.kwargs["headers"]["nvstreamer-file-name"], "rain_eval.mp4")
        self.assertEqual(second.kwargs["headers"]["nvstreamer-is-last-chunk"], "true")
        metadata = json.loads(second.kwargs["files"]["metadata"][1])
        self.assertEqual(metadata, {"timestamp": "2025-01-02T00:00:00"})
        self.assertEqual(second.kwargs["files"]["file"][0], "rain_eval.mp4")
        self.assertEqual(third.args, ("POST", "http://nginx/agent/api/v1/videos/sensor-uuid-1/complete"))
        self.assertEqual(third.kwargs["json"], {"filename": "rain_eval.mp4"})

    def test_ingest_file_raises_on_upload_error_and_does_not_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "rain_eval.mp4"
            clip.write_bytes(b"x")
            self.request.side_effect = [
                response(body={"url": "http://10.0.0.5:80/api/v1/storage/file"}),
                response(status=507, text="Insufficient Storage " + "z" * 500),
            ]
            with self.assertRaises(RuntimeError) as ctx:
                course_setup.ingest_file(str(clip), "rain_eval.mp4", "2025-01-02T00:00:00")
        message = str(ctx.exception)
        self.assertIn("POST http://10.0.0.5:30888/api/v1/storage/file", message)
        self.assertIn("HTTP 507", message)
        self.assertIn("Insufficient Storage", message)
        self.assertEqual(self.request.call_count, 2)

    def test_ingest_file_rejects_a_missing_file_before_any_request(self):
        with self.assertRaises(FileNotFoundError):
            course_setup.ingest_file("/nonexistent/clip.mp4", "clip.mp4", "2025-01-02T00:00:00")
        self.request.assert_not_called()

    # -- find_sensor ------------------------------------------------------

    def test_find_sensor_matches_exact_name_or_underscore_suffix(self):
        sensors = [{"sensorId": "a", "name": "traffic_shortcut"},
                   {"sensorId": "b", "name": "traffic_short_1"},
                   {"sensorId": "c", "name": "traffic_short"}]
        self.request.return_value = response(body=sensors)
        self.assertEqual(course_setup.find_sensor("traffic_short")["sensorId"], "b")
        self.assertEqual(course_setup.find_sensor("traffic_shortcut")["sensorId"], "a")
        self.assertIsNone(course_setup.find_sensor("normal_eval"))
        self.assertEqual(self.calls()[0], ("GET", "http://nginx/vios/vst/api/v1/sensor/list"))
        self.assertEveryCallHasTimeout()

    def test_find_sensor_raises_when_vios_errors(self):
        self.request.return_value = response(status=502, text="Bad Gateway")
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.find_sensor("traffic_short")
        self.assertIn("GET http://nginx/vios/vst/api/v1/sensor/list -> HTTP 502: Bad Gateway", str(ctx.exception))

    # -- wait_for_detections ---------------------------------------------

    def test_wait_for_detections_prints_elapsed_every_poll_and_returns_count(self):
        clock = FakeClock()
        self.request.side_effect = [
            response(status=404, text="index_not_found_exception"),
            response(body={"count": 0}),
            response(body={"count": 385}),
        ]
        output = io.StringIO()
        with patch.object(course_setup.time, "monotonic", clock.monotonic), \
             patch.object(course_setup.time, "sleep", clock.sleep), \
             contextlib.redirect_stdout(output):
            count = course_setup.wait_for_detections("mdx-raw-2025-01-01", "traffic_short",
                                                     timeout_s=1200, every_s=10)
        self.assertEqual(count, 385)
        self.assertEqual(clock.slept, [10, 10])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("index not created yet, 0 s elapsed", lines[0])
        self.assertIn("0 so far, 10 s elapsed", lines[1])
        self.assertIn("385 frames of detections for 'traffic_short' after 20 s", lines[2])
        body = self.request.call_args_list[0].kwargs["json"]
        # The count selects the name or a re-ingest's name_<n> (notebook 2's source filter, section 2.7),
        # never the VIOS UUID.
        self.assertEqual(body, {"query": course_setup.source_filter("traffic_short")})
        self.assertEqual(body["query"]["bool"]["should"],
                         [{"term": {"sensorId.keyword": "traffic_short"}},
                          {"prefix": {"sensorId.keyword": "traffic_short_"}}])
        self.assertEqual(self.calls()[0], ("POST", "http://nginx/es/mdx-raw-2025-01-01/_count"))
        self.assertEveryCallHasTimeout()

    def test_wait_for_detections_times_out_with_index_sensor_and_last_count(self):
        clock = FakeClock()
        self.request.return_value = response(body={"count": 0})
        output = io.StringIO()
        with patch.object(course_setup.time, "monotonic", clock.monotonic), \
             patch.object(course_setup.time, "sleep", clock.sleep), \
             contextlib.redirect_stdout(output), \
             self.assertRaises(TimeoutError) as ctx:
            course_setup.wait_for_detections("mdx-raw-2025-01-01", "rain_check", timeout_s=25, every_s=10)
        message = str(ctx.exception)
        self.assertIn("mdx-raw-2025-01-01", message)
        self.assertIn("'rain_check'", message)
        self.assertIn("last count 0", message)
        self.assertIn("after 30 s", message)
        self.assertIn("print(course_setup.status_tail())", message)
        self.assertEqual(clock.slept, [10, 10, 10])
        self.assertEqual(len(output.getvalue().splitlines()), 3)

    def test_wait_for_detections_raises_on_other_es_errors(self):
        self.request.return_value = response(status=503, text="no primary shard")
        with patch.object(course_setup.time, "sleep", Mock()), self.assertRaises(RuntimeError) as ctx:
            course_setup.wait_for_detections("mdx-raw-2025-01-01", "rain_check", timeout_s=5, every_s=1)
        self.assertIn("HTTP 503: no primary shard", str(ctx.exception))

    # -- source_filter ----------------------------------------------------

    def test_source_filter_matches_exact_name_or_underscore_suffix(self):
        query = course_setup.source_filter("traffic_short")
        self.assertEqual(query, {"bool": {
            "should": [{"term": {"sensorId.keyword": "traffic_short"}},
                       {"prefix": {"sensorId.keyword": "traffic_short_"}}],
            "minimum_should_match": 1}})
        with self.assertRaises(ValueError):
            course_setup.source_filter("")
        self.request.assert_not_called()

    # -- sensors / timelines ---------------------------------------------

    def test_sensors_returns_the_raw_vios_list_and_rejects_other_shapes(self):
        listed = [{"sensorId": "a", "name": "traffic_short"}, {"sensorId": "b", "name": "normal_eval"}]
        self.request.return_value = response(body=listed)
        self.assertEqual(course_setup.sensors(), listed)
        self.assertEqual(self.calls(), [("GET", "http://nginx/vios/vst/api/v1/sensor/list")])
        self.assertEveryCallHasTimeout()
        self.request.return_value = response(body={"error": "not a list"})
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.sensors()
        self.assertIn("expected a list", str(ctx.exception))

    def test_timelines_returns_spans_and_raises_on_empty_or_malformed(self):
        spans = [{"startTime": "2025-01-02T00:00:00.000Z", "endTime": "2025-01-02T00:03:00.250Z"}]
        self.request.return_value = response(body=spans)
        self.assertEqual(course_setup.timelines("sensor-uuid"), spans)
        self.assertEqual(self.calls(), [("GET", "http://nginx/vios/vst/api/v1/storage/sensor-uuid/timelines")])
        self.assertEveryCallHasTimeout()
        self.request.return_value = response(body=[])
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.timelines("sensor-uuid")
        self.assertIn("no timeline spans", str(ctx.exception))
        self.request.return_value = response(body=[{"startTime": "2025-01-02T00:00:00.000Z"}])
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.timelines("sensor-uuid")
        self.assertIn("startTime/endTime", str(ctx.exception))
        with self.assertRaises(ValueError):
            course_setup.timelines("")
        self.assertEqual(self.request.call_count, 3)

    # -- clip_bytes -------------------------------------------------------

    def test_clip_bytes_fetches_the_video_url_through_the_proxy(self):
        self.request.side_effect = [
            response(body={"videoUrl": "http://vst:30080/api/v1/storage/clips/abc.mp4?token=1"}),
            response(content=b"\x00mp4bytes"),
        ]
        data = course_setup.clip_bytes("sensor-uuid", "2025-01-02T00:01:55.000Z", "2025-01-02T00:02:00.000Z")
        self.assertEqual(data, b"\x00mp4bytes")
        first, second = self.request.call_args_list
        self.assertEqual(first.args, ("GET", "http://nginx/vios/vst/api/v1/storage/file/sensor-uuid/url"))
        self.assertEqual(first.kwargs["params"], {"startTime": "2025-01-02T00:01:55.000Z",
                                                  "endTime": "2025-01-02T00:02:00.000Z",
                                                  "container": "mp4"})
        self.assertEqual(second.args, ("GET", "http://nginx/vios/api/v1/storage/clips/abc.mp4?token=1"))
        self.assertEveryCallHasTimeout()

    def test_clip_bytes_raises_on_an_empty_clip(self):
        self.request.side_effect = [
            response(body={"videoUrl": "http://vst:30080/api/v1/storage/clips/abc.mp4"}),
            response(content=b""),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.clip_bytes("sensor-uuid", "2025-01-01T00:01:55.000Z", "2025-01-01T00:02:00.000Z")
        self.assertIn("empty clip", str(ctx.exception))
        self.assertIn("sensor-uuid", str(ctx.exception))

    # -- iso_offset / offset_seconds -------------------------------------

    def test_iso_offset_on_both_anchors(self):
        self.assertEqual(course_setup.iso_offset("2025-01-01T00:00:00Z", 115), "2025-01-01T00:01:55.000Z")
        self.assertEqual(course_setup.iso_offset("2025-01-01T00:00:00.000Z", 6), "2025-01-01T00:00:06.000Z")
        self.assertEqual(course_setup.iso_offset("2025-01-02T00:00:00.000Z", 115.5), "2025-01-02T00:01:55.500Z")
        self.assertEqual(course_setup.iso_offset("2025-01-02T00:00:00Z", 180.25), "2025-01-02T00:03:00.250Z")
        # naive anchors (notebook 2 uploads "2025-01-01T00:00:00") are UTC
        self.assertEqual(course_setup.iso_offset("2025-01-01T00:00:00", 12), "2025-01-01T00:00:12.000Z")
        # crossing the day boundary between the two anchors, both ways
        self.assertEqual(course_setup.iso_offset("2025-01-01T23:59:59.000Z", 2), "2025-01-02T00:00:01.000Z")
        self.assertEqual(course_setup.iso_offset("2025-01-02T00:00:01.000Z", -2), "2025-01-01T23:59:59.000Z")
        # a non-UTC offset is normalised to Z
        self.assertEqual(course_setup.iso_offset("2025-01-02T02:00:00+02:00", 0), "2025-01-02T00:00:00.000Z")

    def test_iso_offset_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            course_setup.iso_offset("yesterday", 1)
        with self.assertRaises(ValueError):
            course_setup.iso_offset("2025-01-01T00:00:00Z", float("nan"))

    def test_offset_seconds_moves_an_embedding_hit_onto_the_vios_timeline(self):
        # Notebook 4's conversion: hit indexed on the 2025-01-01 anchor, source uploaded at 2025-01-02.
        hit_start, hit_end = "2025-01-01T00:01:55.000Z", "2025-01-01T00:02:00.000Z"
        anchor = course_setup.EMBEDDING_ANCHOR
        self.assertEqual(course_setup.offset_seconds(anchor, hit_start), 115.0)
        self.assertEqual(course_setup.offset_seconds(anchor, hit_end), 120.0)
        creation = "2025-01-02T00:00:00.000Z"
        self.assertEqual(course_setup.iso_offset(creation, course_setup.offset_seconds(anchor, hit_start)),
                         "2025-01-02T00:01:55.000Z")
        self.assertEqual(course_setup.iso_offset(creation, course_setup.offset_seconds(anchor, hit_end)),
                         "2025-01-02T00:02:00.000Z")
        self.assertEqual(course_setup.offset_seconds("2025-01-02T00:00:00Z", "2025-01-01T23:59:00Z"), -60.0)

    # -- es ---------------------------------------------------------------

    def test_es_passes_method_path_and_body_with_a_default_timeout(self):
        self.request.return_value = response(body={"count": 7})
        data = course_setup.es("post", "/mdx-raw-2025-01-01/_count", json={"query": {"match_all": {}}})
        self.assertEqual(data, {"count": 7})
        call = self.request.call_args
        self.assertEqual(call.args, ("POST", "http://nginx/es/mdx-raw-2025-01-01/_count"))
        self.assertEqual(call.kwargs["json"], {"query": {"match_all": {}}})
        self.assertEqual(call.kwargs["timeout"], 60)

    def test_es_honours_an_explicit_timeout_and_rejects_bad_paths(self):
        self.request.return_value = response(body={})
        course_setup.es("GET", "/_cat/indices", timeout=5)
        self.assertEqual(self.request.call_args.kwargs["timeout"], 5)
        with self.assertRaises(ValueError):
            course_setup.es("GET", "mdx-raw-2025-01-01/_count")
        with self.assertRaises(ValueError):
            course_setup.es("GET", "/es/mdx-raw-2025-01-01/_count")

    def test_es_error_names_method_url_status_and_truncated_body(self):
        long_body = "E" * 1000
        self.request.return_value = response(status=400, text=long_body)
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.es("POST", "/mdx-behavior-*/_search", json={})
        message = str(ctx.exception)
        self.assertIn("POST http://nginx/es/mdx-behavior-*/_search -> HTTP 400: ", message)
        self.assertIn("E" * 300, message)
        self.assertNotIn("E" * 301, message)

    def test_es_non_json_success_body_is_an_error(self):
        self.request.return_value = response(status=200, text="<html>proxy page</html>")
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.es("GET", "/_cluster/health")
        self.assertIn("not JSON", str(ctx.exception))
        self.assertIn("<html>proxy page</html>", str(ctx.exception))

    # -- vlm_model_id -----------------------------------------------------

    def test_vlm_model_id_reads_the_first_model(self):
        self.request.return_value = response(body={"data": [{"id": "nvidia/cosmos-3-super-fp8"}]})
        self.assertEqual(course_setup.vlm_model_id(), "nvidia/cosmos-3-super-fp8")
        self.assertEqual(self.calls(), [("GET", "http://nginx/rtvi-vlm/v1/models")])
        self.assertEveryCallHasTimeout()

    def test_vlm_model_id_raises_on_an_empty_model_list(self):
        self.request.return_value = response(body={"data": []})
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.vlm_model_id()
        self.assertIn("no models listed", str(ctx.exception))

    # -- register_vios_window --------------------------------------------

    def test_register_vios_window_posts_the_vios_url_as_a_multipart_form(self):
        self.request.side_effect = [
            response(body={"videoUrl": "http://vst:30080/api/v1/storage/clips/win.mp4"}),
            response(body={"id": "file-123"}),
        ]
        file_id = course_setup.register_vios_window("sensor-uuid", "2025-01-01T00:00:10.000Z",
                                                    "2025-01-01T00:00:16.000Z", "2025-01-01T00:00:10.000Z")
        self.assertEqual(file_id, "file-123")
        first, second = self.request.call_args_list
        self.assertEqual(first.args, ("GET", "http://nginx/vios/vst/api/v1/storage/file/sensor-uuid/url"))
        self.assertEqual(second.args, ("POST", "http://nginx/rtvi-vlm/v1/files"))
        self.assertEqual(second.kwargs["files"], {
            "purpose": (None, "vision"),
            "media_type": (None, "video"),
            "url": (None, "http://vst:30080/api/v1/storage/clips/win.mp4"),
            "creation_time": (None, "2025-01-01T00:00:10.000Z"),
        })
        self.assertEveryCallHasTimeout()

    # -- caption ----------------------------------------------------------

    def test_caption_reads_the_model_then_posts_generate_captions(self):
        chunks = [{"start_time": "2025-01-01T00:00:00.000Z", "end_time": "2025-01-01T00:00:10.000Z",
                   "content": "Two cars stopped in the intersection.", "vlm_latency_ms": 812}]
        self.request.side_effect = [
            response(body={"data": [{"id": "cosmos-3-super"}]}),
            response(body={"chunk_responses": chunks}),
        ]
        out = course_setup.caption("file-123", "What happens here?", chunk_duration=0)
        self.assertEqual(out, chunks)
        first, second = self.request.call_args_list
        self.assertEqual(first.args, ("GET", "http://nginx/rtvi-vlm/v1/models"))
        self.assertEqual(second.args, ("POST", "http://nginx/rtvi-vlm/v1/generate_captions"))
        self.assertEqual(second.kwargs["json"], {"id": "file-123", "model": "cosmos-3-super",
                                                 "prompt": "What happens here?", "chunk_duration": 0})
        self.assertEveryCallHasTimeout()

    def test_caption_sends_creation_time_only_when_given_and_raises_on_no_chunks(self):
        self.request.side_effect = [
            response(body={"data": [{"id": "cosmos-3-super"}]}),
            response(body={"chunk_responses": []}),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.caption("file-123", "Count the vehicles.", creation_time_iso="2025-01-01T00:00:00Z")
        self.assertIn("no chunk_responses for file file-123", str(ctx.exception))
        sent = self.request.call_args_list[1].kwargs["json"]
        self.assertEqual(sent["creation_time"], "2025-01-01T00:00:00Z")
        self.assertEqual(sent["chunk_duration"], 10)

    def test_caption_sends_the_frames_fields_only_when_frames_is_given(self):
        chunks = [{"start_time": "2025-01-01T00:00:00.000Z", "end_time": "2025-01-01T00:00:10.000Z",
                   "content": "Two cars collide.", "vlm_latency_ms": 900, "frame_count": 8, "input_tokens": 2100}]
        self.request.side_effect = [
            response(body={"data": [{"id": "cosmos-3-super"}]}),
            response(body={"chunk_responses": chunks}),
        ]
        out = course_setup.caption("file-123", "Describe the scene.", chunk_duration=10,
                                   creation_time_iso="2025-01-01T00:00:00.000Z", frames=8, use_fps=False)
        self.assertEqual(out, chunks)                     # returned unchanged: frame_count, input_tokens, latency
        self.assertEqual(self.request.call_args_list[1].kwargs["json"], {
            "id": "file-123", "model": "cosmos-3-super", "prompt": "Describe the scene.", "chunk_duration": 10,
            "creation_time": "2025-01-01T00:00:00.000Z",
            "num_frames_per_second_or_fixed_frames_chunk": 8, "use_fps_for_chunking": False})
        self.assertEveryCallHasTimeout()

    def test_caption_sends_frames_per_second_and_the_prompt_options_when_given(self):
        self.request.side_effect = [
            response(body={"data": [{"id": "cosmos-3-super"}]}),
            response(body={"chunk_responses": [{"content": "ok"}]}),
            response(body={"data": [{"id": "cosmos-3-super"}]}),
            response(body={"chunk_responses": [{"content": "ok"}]}),
        ]
        course_setup.caption("file-123", "Describe the scene.", frames=2.5, use_fps=True)
        sent = self.request.call_args_list[1].kwargs["json"]
        self.assertEqual(sent["num_frames_per_second_or_fixed_frames_chunk"], 2.5)
        self.assertIs(sent["use_fps_for_chunking"], True)
        self.assertNotIn("system_prompt", sent)
        self.assertNotIn("max_tokens", sent)
        course_setup.caption("file-123", "Describe the scene.", system_prompt="Answer in one sentence.",
                             max_tokens=60)
        sent = self.request.call_args_list[3].kwargs["json"]
        self.assertEqual((sent["system_prompt"], sent["max_tokens"]), ("Answer in one sentence.", 60))
        self.assertNotIn("num_frames_per_second_or_fixed_frames_chunk", sent)
        self.assertNotIn("use_fps_for_chunking", sent)
        self.assertNotIn("creation_time", sent)

    def test_caption_rejects_bad_options_before_any_request(self):
        bad = [dict(frames=0), dict(frames=-1), dict(frames=True), dict(frames="8"), dict(frames=float("nan")),
               dict(frames=8, use_fps="no"), dict(use_fps=True), dict(max_tokens=0), dict(max_tokens=2.5),
               dict(system_prompt=["not", "text"])]
        for options in bad:
            with self.subTest(options=options), self.assertRaises(ValueError):
                course_setup.caption("file-123", "Describe the scene.", **options)
        self.request.assert_not_called()

    # -- text_embedding ---------------------------------------------------

    def test_text_embedding_posts_the_model_and_returns_floats(self):
        self.request.return_value = response(body={"data": [{"embeddings": [0.1, 0.2, 3]}]})
        vector = course_setup.text_embedding("two vehicles collide at an intersection")
        self.assertEqual(vector, [0.1, 0.2, 3.0])
        self.assertTrue(all(isinstance(v, float) for v in vector))
        call = self.request.call_args
        self.assertEqual(call.args, ("POST", "http://nginx/rtvi-embed/v1/generate_text_embeddings"))
        self.assertEqual(call.kwargs["json"], {"text_input": ["two vehicles collide at an intersection"],
                                               "model": "cosmos-embed1-448p-anomaly-detection"})
        self.assertEveryCallHasTimeout()

    def test_text_embedding_raises_on_an_empty_vector(self):
        self.request.return_value = response(body={"data": [{"embeddings": []}]})
        with self.assertRaises(RuntimeError):
            course_setup.text_embedding("a polar bear in a snowstorm")

    # -- knn_search -------------------------------------------------------

    def test_knn_search_filters_inside_the_knn_and_returns_raw_hits(self):
        hits = [{"_score": 0.61, "_source": {"sensor": {"id": "source-a"},
                                              "timestamp": "2025-01-01T00:01:55.000Z",
                                              "end": "2025-01-01T00:02:00.000Z"}}]
        self.request.return_value = response(body={"hits": {"hits": hits}})
        vector = [0.5] * 768
        out = course_setup.knn_search(vector, ["source-a", "source-b"], 5)
        self.assertEqual(out, hits)
        call = self.request.call_args
        self.assertEqual(call.args, ("POST", "http://nginx/es/mdx-embed-filtered-2025-01-01/_search"))
        body = call.kwargs["json"]
        self.assertEqual(body["size"], 5)
        self.assertEqual(body["_source"], ["sensor.id", "timestamp", "end"])
        query = body["query"]["bool"]
        self.assertEqual(query["filter"], [{"terms": {"sensor.id.keyword": ["source-a", "source-b"]}}])
        nested = query["must"][0]["nested"]
        self.assertEqual(nested["path"], "llm.visionEmbeddings")
        knn = nested["query"]["knn"]
        self.assertEqual(knn["field"], "llm.visionEmbeddings.vector")
        self.assertEqual(knn["k"], 5)
        self.assertEqual(knn["num_candidates"], 40)
        self.assertEqual(knn["filter"], {"terms": {"sensor.id.keyword": ["source-a", "source-b"]}})
        self.assertEqual(len(knn["query_vector"]), 768)
        self.assertEveryCallHasTimeout()

    def test_knn_search_refuses_an_empty_source_filter(self):
        with self.assertRaises(ValueError):
            course_setup.knn_search([0.1] * 768, [], 5)
        with self.assertRaises(ValueError):
            course_setup.knn_search([0.1] * 768, ["source-a"], 0)
        self.request.assert_not_called()

    # -- status_tail ------------------------------------------------------

    def test_status_tail_returns_status_line_and_last_n_log_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "seed-status"
            log = Path(folder) / "seed.log"
            status.write_text("2026-10-20T09:12:00Z READY all services healthy\n")
            log.write_text("".join(f"line {i}\n" for i in range(1, 11)))
            with patch.object(course_setup, "SEED_STATUS_FILE", str(status)), \
                 patch.object(course_setup, "SEED_LOG_FILE", str(log)):
                text = course_setup.status_tail(3)
        lines = text.splitlines()
        self.assertEqual(lines[0], "seed-status: 2026-10-20T09:12:00Z READY all services healthy")
        self.assertEqual(lines[2:], ["  line 8", "  line 9", "  line 10"])
        self.assertNotIn("line 7", text)

    def test_status_tail_reports_a_missing_status_file(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "seed-status"
            with patch.object(course_setup, "SEED_STATUS_FILE", str(missing)), \
                 patch.object(course_setup, "SEED_LOG_FILE", str(Path(folder) / "seed.log")):
                text = course_setup.status_tail()
        self.assertTrue(text.startswith("status file not found"), text)
        self.assertIn(str(missing), text)
        with self.assertRaises(ValueError):
            course_setup.status_tail(0)

    # -- transport failures ----------------------------------------------

    def test_connection_errors_become_runtime_errors_with_method_and_url(self):
        self.request.side_effect = requests.ConnectionError("Name or service not known")
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.vlm_model_id()
        message = str(ctx.exception)
        self.assertIn("GET http://nginx/rtvi-vlm/v1/models -> no response", message)
        self.assertIn("ConnectionError", message)
        # Notebooks import the module under different names (or not at all), so the message spells out the import.
        self.assertIn("import course_setup; print(course_setup.status_tail())", message)
        self.request.side_effect = requests.Timeout("read timed out")
        with self.assertRaises(RuntimeError) as ctx:
            course_setup.text_embedding("x")
        self.assertIn("Timeout", str(ctx.exception))

    def test_no_secrets_or_fallbacks_in_the_helper_source(self):
        source = MODULE_PATH.read_text()
        self.assertNotIn("nv" + "api-", source)   # key prefix assembled so the literal never ships
        self.assertNotIn("except:", source)
        self.assertNotIn("except Exception:\n        pass", source)
        self.assertNotIn("Cosmos " + "Reason", source)   # banned model name, assembled so this file does not carry it
        self.assertNotIn("os.environ", source)



class CoursePrepGate(unittest.TestCase):
    """registry_entry / require_course_prep: a cell that reads course-prep's files must explain a
    not-yet-finished course-prep instead of raising FileNotFoundError (production, 2026-10-26), and
    point at the onboarding notebook, which shows the progress."""

    def _files(self, folder, status=None, registry=None):
        status_path = Path(folder) / "course_prep_status.json"
        registry_path = Path(folder) / "search_sources.json"
        if status is not None:
            status_path.write_text(json.dumps(status))
        if registry is not None:
            registry_path.write_text(json.dumps(registry))
        return registry_path, status_path

    def test_no_status_and_no_registry_says_course_prep_has_not_started(self):
        with tempfile.TemporaryDirectory() as folder:
            registry_path, status_path = self._files(folder)
            with self.assertRaises(RuntimeError) as ctx:
                course_setup.registry_entry("rain_eval.mp4", registry_path, status_path)
            self.assertIn("has not started", str(ctx.exception))
            self.assertIn("00-onboarding (section 0.3)", str(ctx.exception))
            self.assertNotIn("orientation", str(ctx.exception))

    def test_running_status_says_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            registry_path, status_path = self._files(folder, status={"state": "RUNNING", "assets": {}})
            with self.assertRaises(RuntimeError) as ctx:
                course_setup.registry_entry("rain_eval.mp4", registry_path, status_path)
            self.assertIn("still RUNNING", str(ctx.exception))
            self.assertIn("00-onboarding, section 0.4", str(ctx.exception))

    def test_failed_status_says_call_the_instructor(self):
        with tempfile.TemporaryDirectory() as folder:
            registry_path, status_path = self._files(folder, status={"state": "FAILED", "assets": {}})
            with self.assertRaises(RuntimeError) as ctx:
                course_setup.registry_entry("rain_eval.mp4", registry_path, status_path)
            self.assertIn("FAILED", str(ctx.exception))
            self.assertIn("call the instructor", str(ctx.exception))
            self.assertIn("00-onboarding, section 0.4", str(ctx.exception))

    def test_ok_status_without_registry_is_an_instructor_problem(self):
        with tempfile.TemporaryDirectory() as folder:
            registry_path, status_path = self._files(folder, status={"state": "OK", "assets": {}})
            with self.assertRaises(RuntimeError) as ctx:
                course_setup.registry_entry("rain_eval.mp4", registry_path, status_path)
            self.assertIn("registry was removed", str(ctx.exception))

    def test_completed_entry_is_returned(self):
        entry = {"filename": "rain_eval.mp4", "sensor_id": "abc", "creation_time": "2025-01-02T00:00:00.000Z",
                 "duration_seconds": 180.25, "completed": True}
        with tempfile.TemporaryDirectory() as folder:
            registry_path, status_path = self._files(folder, status={"state": "OK", "assets": {}},
                                                     registry={"rain_eval.mp4": entry})
            self.assertEqual(course_setup.registry_entry("rain_eval.mp4", registry_path, status_path), entry)
            self.assertEqual(course_setup.require_course_prep(status_path)["state"], "OK")

    def test_missing_clip_and_incomplete_upload_each_name_the_clip(self):
        with tempfile.TemporaryDirectory() as folder:
            registry_path, status_path = self._files(
                folder, status={"state": "OK", "assets": {}},
                registry={"fog_eval.mp4": {"sensor_id": "x", "completed": False}})
            with self.assertRaises(RuntimeError) as ctx:
                course_setup.registry_entry("rain_eval.mp4", registry_path, status_path)
            self.assertIn("rain_eval.mp4 is not in the registry", str(ctx.exception))
            self.assertIn("00-onboarding, section 0.4", str(ctx.exception))
            with self.assertRaises(RuntimeError) as ctx:
                course_setup.registry_entry("fog_eval.mp4", registry_path, status_path)
            self.assertIn("not completed", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
