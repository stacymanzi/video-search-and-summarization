"""make_fixtures.py: writes the Part 1 grader's test fixtures.

Run it after changing the story below, then rerun the tests:

    python3 -I -B task1/assessment/fixtures/make_fixtures.py
    python3 -I -B -m pytest task1/assessment/tests -q -p no:cacheprovider

Output, next to this file:

    world.json             the fake services and files: VIOS sensors and timelines, Elasticsearch documents,
                           RT-Embed's text embeddings (tests/fakes.py serves them), and course-prep's registry
                           of the search clips (tests/conftest.py writes it as search_sources.json)
    answer_key.json        a multiple-choice key in the production format; its letters are made up, so the
                           tests never depend on the real key in grader/answer_keys/part1.json
    pass/answers.json      an answers file that passes every check
    cases/<name>/          one variant each: answers.json (absent when the case has no file) and expect.json
                           {"passed", "fails" (the exact ids that fail), "services", "note"}

The story. The learner cut rain_check.mp4 (30 s) and uploaded it; VIOS lists it as rain_check_1. RT-CV wrote
12 frame records for the stem, in which car is the most frequent type. The index also holds traps that only a
correct query avoids: frames of normal_eval with more bicycles than the clip has cars, the same frame ids
0.6 s later and an object that looks exactly like track 7, and frames of a stem rain_checkpoint that a prefix
match would count. Track 7 drifts slowly in appearance, so its nearest neighbour in another frame is itself one
frame later. Object 21 in frame 6 is a twin of track 9 as seen in frame 3, so a search from frame 3 ranks
object 21 first.

RT-Embed knows four queries. Searching the four search clips for "two cars collide at a crossing" returns
rain_eval 20 to 25 s first (it overlaps the labelled collision at 19.21 to 43.75 s) and normal_eval 45 to 50 s
second. traffic_short and the learner's own rain_check match that query even better, so a search without the
four-clip filter goes wrong. "a car drives through heavy rain" returns rain_eval 55 to 60 s first, which shows
no labelled collision. "a crash on a wet road" returns rain_eval 15 to 20 s, which touches the collision, and
"a collision on a sunny day" returns normal_eval 45 to 50 s. T7: 4,200 ms per chunk gives 8,640 requests and
36,288 s of RT-VLM time per camera per day.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

HERE = Path(__file__).resolve().parent
GROUND_TRUTH = HERE.parent / "grader" / "answer_keys" / "ground_truth" / "search_videos.json"

STEM = "rain_check"
RAIN_CHECK_ID = "5b0f3c9e-1d2a-4e8b-9c7f-0a1b2c3d4e5f"
TRAFFIC_SHORT_ID = "4d3c2b1a-0f9e-4d8c-8b7a-6f5e4d3c2b1a"
SEARCH_IDS = {
    "normal_eval.mp4": "8e7a6b5c-4d3e-4f2a-9b1c-0d9e8f7a6b5c",
    "rain_eval.mp4": "2c4e6a8b-1d3f-4a5b-8c7d-9e0f1a2b3c4d",
    "fog_eval.mp4": "7f1e2d3c-4b5a-4c6d-8e9f-0a1b2c3d4e5f",
    "night_eval.mp4": "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d",
}
CAPTION_FILE_ID = "3f2a6c1e-8b4d-4f1a-9e2b-7c5d4a3b2e1f"
OTHER_FILE_ID = "6e5d4c3b-2a19-4f8e-9d7c-6b5a49382716"

FILE_ANCHOR = datetime(2025, 1, 1, tzinfo=timezone.utc)       # file analysis and the rain_check upload
SEARCH_ANCHOR = datetime(2025, 1, 2, tzinfo=timezone.utc)      # VIOS timelines of the search clips
FRAME_STEP_S = 0.25
EMBED_CHUNK_S = 5.0                                            # RT-Embed's chunk length
EMBED_MODEL = "cosmos-embed1-448p-anomaly-detection"

COLLIDE = "two cars collide at a crossing"
RAIN = "a car drives through heavy rain"
WET_CRASH = "a crash on a wet road"
SUNNY = "a collision on a sunny day"
QUERY_VECTORS = {
    COLLIDE: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    RAIN: [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
    WET_CRASH: [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    SUNNY: [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
}

NULL = object()   # with_changes: write a JSON null instead of deleting the key

ALL_CHECKS = [
    "part1.video_in_vios",
    "part1.detections_indexed",
    "part1.kafka_decoded",
    "part1.captions_indexed",
    "part1.search_matches_reference",
    "part1.search_hit_overlaps_collision",
    "part1.appearance_match",
    "part1.throughput_estimate",
    "part1.questions",
]
TASK_CHECKS = ALL_CHECKS[:-1]
T5_CHECKS = ["part1.search_matches_reference", "part1.search_hit_overlaps_collision"]


def iso(anchor: datetime, seconds: float) -> str:
    moment = anchor + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def dump(data) -> str:
    return json.dumps(data, indent=2) + "\n"


def obj(object_id: str, kind: str, vector: Optional[list], left: float) -> dict:
    record = {"id": object_id, "type": kind, "confidence": 0.9,
              "bbox": {"leftX": left, "topY": 200.0, "rightX": left + 80.0, "bottomY": 260.0}}
    if vector is not None:
        record["embedding"] = {"vector": [round(x, 4) for x in vector]}
    return record


def track9(i: int) -> list:
    return [0.0, 1.0, 0.02 * i, 0.0]


def raw_frames() -> list:
    frames = []
    for i in range(12):
        objects = [obj("7", "car", [1.0, 0.02 * i, 0.0, 0.0], 100.0 + 10 * i)]
        if 3 <= i <= 11:
            objects.append(obj("9", "car", track9(i), 400.0))
        if 5 <= i <= 8:
            objects.append(obj("12", "person", [0.0, 0.0, 1.0, 0.02 * i], 700.0))
        if i == 2:
            objects.append(obj("15", "car", None, 900.0))          # too small for an embedding
        if i == 6:
            objects.append(obj("21", "car", track9(3), 600.0))     # looks exactly like track 9 in frame 3
        frames.append({"version": "4.0", "id": str(i), "sensorId": STEM,
                       "timestamp": iso(FILE_ANCHOR, i * FRAME_STEP_S), "objects": objects})
    # Other stems reuse the frame ids 0 to 3, 0.6 s later than rain_check's frames with the same id.
    for i in range(3):   # another clip: more bicycles than rain_check has cars, and a twin of track 7
        objects = [obj(str(100 + n), "bicycle", [0.0, 0.0, 0.0, 1.0], 50.0 * n) for n in range(10)]
        objects.append(obj("1", "car", [1.0, 0.0, 0.0, 0.0], 800.0))
        frames.append({"version": "4.0", "id": str(i), "sensorId": "normal_eval",
                       "timestamp": iso(FILE_ANCHOR, i * FRAME_STEP_S + 0.6), "objects": objects})
    for i in range(4):   # a stem that only a prefix match would confuse with rain_check
        objects = [obj(str(200 + n), "person", [0.0, 0.0, 1.0, 0.0], 50.0 * n) for n in range(10)]
        frames.append({"version": "4.0", "id": str(i), "sensorId": "rain_checkpoint",
                       "timestamp": iso(FILE_ANCHOR, i * FRAME_STEP_S + 0.6), "objects": objects})
    return frames


def embed_chunk(sensor_id: str, start_s: float, vector: list) -> dict:
    """One RT-Embed chunk in mdx-embed-filtered-2025-01-01: the VIOS sensor id, its window and its vector."""
    return {"sensor": {"id": sensor_id}, "timestamp": iso(FILE_ANCHOR, start_s),
            "end": iso(FILE_ANCHOR, start_s + EMBED_CHUNK_S), "llm": {"visionEmbeddings": [{"vector": vector}]}}


def embed_chunks() -> list:
    return [
        # Better matches for the collision query than any search-clip window: the four-clip filter keeps them out.
        embed_chunk(TRAFFIC_SHORT_ID, 0.0, [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        embed_chunk(RAIN_CHECK_ID, 5.0, [0.99, 0.0, 0.0, 0.0, 0.14, 0.0]),
        # The four search clips.
        embed_chunk(SEARCH_IDS["rain_eval.mp4"], 15.0, [0.5, 0.0, 0.86, 0.0, 0.0, 0.0]),
        embed_chunk(SEARCH_IDS["rain_eval.mp4"], 20.0, [0.95, 0.05, 0.05, 0.0, 0.3, 0.0]),
        embed_chunk(SEARCH_IDS["rain_eval.mp4"], 55.0, [0.0, 1.0, 0.0, 0.0, 0.0, 0.0]),
        embed_chunk(SEARCH_IDS["normal_eval.mp4"], 45.0, [0.8, 0.0, 0.0, 0.6, 0.0, 0.0]),
        embed_chunk(SEARCH_IDS["fog_eval.mp4"], 30.0, [0.3, 0.3, 0.3, 0.3, 0.0, 0.8]),
        embed_chunk(SEARCH_IDS["night_eval.mp4"], 80.0, [0.4, 0.0, 0.0, 0.0, 0.0, 0.9]),
    ]


def world() -> dict:
    truth = {Path(entry["file"]).name: entry for entry in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))}
    sensors = [{"sensorId": RAIN_CHECK_ID, "name": "rain_check_1"},
               {"sensorId": TRAFFIC_SHORT_ID, "name": "traffic_short_1"}]
    timelines = {RAIN_CHECK_ID: [{"startTime": iso(FILE_ANCHOR, 0), "endTime": iso(FILE_ANCHOR, 30)}],
                 TRAFFIC_SHORT_ID: [{"startTime": iso(FILE_ANCHOR, 0), "endTime": iso(FILE_ANCHOR, 16)}]}
    registry = {}
    for filename, sensor_id in SEARCH_IDS.items():
        sensors.append({"sensorId": sensor_id, "name": Path(filename).stem + "_1"})
        timelines[sensor_id] = [{"startTime": iso(SEARCH_ANCHOR, 0), "endTime": iso(SEARCH_ANCHOR, truth[filename]["duration_s"])}]
        registry[filename] = {"filename": filename, "duration_seconds": truth[filename]["duration_s"],
                              "creation_time": iso(SEARCH_ANCHOR, 0), "sensor_id": sensor_id, "completed": True}
    caption_index = "default_" + CAPTION_FILE_ID.replace("-", "_")
    captions = [{"text": f"Chunk {n}: cars drive through a wet intersection.",
                 "metadata": {"source": "N/A", "content_metadata": {"chunkIdx": n}}} for n in range(3)]
    return {
        "vios": {"sensors": sensors, "timelines": timelines},
        "rtvi_embed": {"model": EMBED_MODEL, "text_embeddings": QUERY_VECTORS},
        "es": {
            "mdx-raw-2025-01-01": raw_frames(),
            "mdx-embed-filtered-2025-01-01": embed_chunks(),
            caption_index: captions,
        },
        "registry": registry,
    }


def answer_key() -> dict:
    letters = ["C", "A", "D", "B", "A", "C", "D", "B"]
    return {"version": 1, "questions": {f"p1-q{n}": letter for n, letter in enumerate(letters, start=1)}, "min_correct": 6}


def pass_answers() -> dict:
    return {
        "clip_stem": STEM,
        "t1": {"vios_sensor_id": RAIN_CHECK_ID, "vios_name": "rain_check_1"},
        "t2": {"frames": 12, "top_type": "car"},
        "t3": {"sensor_id": STEM, "frame_id": "4", "timestamp": "2025-01-01T00:00:01.000000000Z"},
        "t4": {"file_id": CAPTION_FILE_ID, "chunks_10": 3, "chunks_0": 1, "frame_counts_8": [8, 8, 8]},
        "t5": {"query": COLLIDE, "file": "rain_eval.mp4", "start_s": 20.0, "end_s": 25.0},
        "t6": {"query_object_id": "7", "answer_object_id": "7"},
        "t7": {"latency_ms": 4200.0, "requests_per_day": 8640, "busy_seconds_per_day": 36288.0},
        "questions": dict(answer_key()["questions"]),
    }


def with_changes(changes: Dict[str, object]) -> dict:
    """The pass answers with some keys replaced (dotted paths; None deletes the key, NULL writes null)."""
    answers = copy.deepcopy(pass_answers())
    for path, value in changes.items():
        *parents, last = path.split(".")
        target = answers
        for part in parents:
            target = target[part]
        if value is None:
            target.pop(last, None)
        else:
            target[last] = None if value is NULL else value
    return answers


def case(name, note, passed, fails, answers="pass", services="up"):
    """answers: "pass" for the pass set, a dict of changes, a raw string, or None for no file."""
    if answers == "pass":
        text = dump(pass_answers())
    elif isinstance(answers, dict):
        text = dump(with_changes(answers))
    else:
        text = answers
    return name, text, {"passed": passed, "fails": sorted(fails), "services": services, "note": note}


def questions_with(correct: int, answered: int) -> dict:
    """``answered`` answers, the first ``correct`` of them right and the rest wrong."""
    key = answer_key()["questions"]
    out = {}
    for n, (question, letter) in enumerate(key.items()):
        if n >= answered:
            break
        out[question] = letter if n < correct else ("A" if letter != "A" else "B")
    return out


def cases() -> list:
    return [
        # the answers file itself
        case("no_answers_file", "the learner never ran a task cell: every check fails", False, ALL_CHECKS, answers=None),
        case("answers_not_json", "a half-written file: every check fails", False, ALL_CHECKS, answers="{\"clip_stem\": \"rain_che\n"),
        case("answers_not_an_object", "a list instead of an object: every check fails", False, ALL_CHECKS, answers="[]\n"),
        case("other_clip", "answers for another clip: every task check fails, the questions are still graded", False,
             TASK_CHECKS, answers={"clip_stem": "normal_eval"}),
        case("task_missing", "T4 never ran", False, ["part1.captions_indexed"], answers={"t4": None}),
        case("task_wrong_shape", "a count written as a string", False, ["part1.detections_indexed"],
             answers={"t2.frames": "12"}),
        # T1
        case("upload_never_ran", "the T1 check ran before the upload: no sensor id", False, ["part1.video_in_vios"],
             answers={"t1.vios_sensor_id": NULL, "t1.vios_name": ""}),
        case("unknown_sensor", "a sensor id VIOS does not list", False, ["part1.video_in_vios"],
             answers={"t1.vios_sensor_id": "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"}),
        case("sensor_of_another_clip", "a real sensor, but not the uploaded clip", False, ["part1.video_in_vios"],
             answers={"t1.vios_sensor_id": SEARCH_IDS["normal_eval.mp4"], "t1.vios_name": "normal_eval_1"}),
        case("vios_name_is_the_stem", "the file stem instead of the name VIOS shows", False, ["part1.video_in_vios"],
             answers={"t1.vios_name": STEM}),
        # T2
        case("count_still_growing", "counted before RT-CV finished", False, ["part1.detections_indexed"],
             answers={"t2.frames": 9}),
        case("wrong_top_type", "person is not the most frequent type", False, ["part1.detections_indexed"],
             answers={"t2.top_type": "person"}),
        case("top_type_any_case", "the type name is compared without case", True, [], answers={"t2.top_type": "Car"}),
        # T3 (should)
        case("kafka_message_of_another_clip", "the first message on the topic was another clip's", True,
             ["part1.kafka_decoded"], answers={"t3.sensor_id": "normal_eval"}),
        case("kafka_wrong_timestamp", "frame 4 with frame 8's timestamp, 1 s off", True, ["part1.kafka_decoded"],
             answers={"t3.timestamp": "2025-01-01T00:00:02.000Z"}),
        case("kafka_unknown_frame", "a frame id RT-CV never wrote for this clip", True, ["part1.kafka_decoded"],
             answers={"t3.frame_id": "99"}),
        case("kafka_timestamp_of_other_clip", "frame 2 of normal_eval is at 1.1 s; rain_check's frame 2 is at 0.5 s",
             True, ["part1.kafka_decoded"], answers={"t3.frame_id": "2", "t3.timestamp": "2025-01-01T00:00:01.100Z"}),
        # T4
        case("wrong_chunk_count", "five chunks at chunk_duration 10", False, ["part1.captions_indexed"],
             answers={"t4.chunks_10": 5}),
        case("four_chunks_at_10", "four chunks at chunk_duration 10: the cut ended with a short extra chunk", True, [],
             answers={"t4.chunks_10": 4, "t4.frame_counts_8": [8, 8, 8, 8]}),
        case("four_chunks_short_tail", "the cut ran 0.2 s past 30 s: the fourth chunk saw only 2 frames", True, [],
             answers={"t4.chunks_10": 4, "t4.frame_counts_8": [8, 8, 8, 2]}),
        case("frames_not_sampled", "the frames setting was not sent: one frame per chunk", False, ["part1.captions_indexed"],
             answers={"t4.frame_counts_8": [1, 1, 1]}),
        case("frame_count_not_reported", "RT-VLM left frame_count out, so the notebook saved nulls", False,
             ["part1.captions_indexed"], answers={"t4.frame_counts_8": [None, None, None]}),
        case("short_chunk_first", "a short chunk can only be the last one", False, ["part1.captions_indexed"],
             answers={"t4.chunks_10": 4, "t4.frame_counts_8": [2, 8, 8, 8]}),
        case("no_caption_index", "a file id with no caption index", False, ["part1.captions_indexed"],
             answers={"t4.file_id": OTHER_FILE_ID}),
        case("file_id_wildcard", "a wildcard would count every caption index", False, ["part1.captions_indexed"],
             answers={"t4.file_id": "*"}),
        # T5: the search decides, the labelled collision is reported
        case("window_not_the_top_hit", "normal_eval 45 to 50 s is the second hit for the query, not the first", False,
             ["part1.search_matches_reference"],
             answers={"t5.file": "normal_eval.mp4", "t5.start_s": 45.0, "t5.end_s": 50.0}),
        case("top_hit_misses_collision", "the top hit of a rain query shows no labelled collision: reported only", True,
             ["part1.search_hit_overlaps_collision"], answers={"t5.query": RAIN, "t5.start_s": 55.0, "t5.end_s": 60.0}),
        case("window_misses_collision", "a window that is neither the top hit nor a labelled collision", False,
             T5_CHECKS, answers={"t5.start_s": 55.0, "t5.end_s": 60.0}),
        case("window_rounded", "the top hit's times rounded by 0.05 s", True, [],
             answers={"t5.start_s": 20.05, "t5.end_s": 24.95}),
        case("window_half_a_second_off", "0.5 s from the top hit is more than the 0.1 s slack", False,
             ["part1.search_matches_reference"], answers={"t5.start_s": 20.5, "t5.end_s": 25.5}),
        case("window_on_the_vios_timeline", "seconds measured from the wrong anchor run past the end of the clip",
             False, T5_CHECKS, answers={"t5.start_s": 86420.0, "t5.end_s": 86425.0}),
        case("window_backwards", "the end before the start", False, T5_CHECKS,
             answers={"t5.start_s": 25.0, "t5.end_s": 20.0}),
        case("window_before_the_clip", "an index time measured from the VIOS anchor, a day too early", False,
             T5_CHECKS, answers={"t5.start_s": -86380.0, "t5.end_s": -86375.0}),
        case("own_clip_not_in_collection", "a window of the learner's own clip, not of the search clips", False,
             T5_CHECKS, answers={"t5.file": "rain_check.mp4", "t5.start_s": 0.0, "t5.end_s": 5.0}),
        case("query_not_saved", "answers.json from before the query was saved", False, T5_CHECKS,
             answers={"t5.query": None}),
        case("window_touching_collision", "15 to 20 s, the top hit of its query, shares 0.79 s with the collision at "
             "19.21 s", True, [], answers={"t5.query": WET_CRASH, "t5.start_s": 15.0, "t5.end_s": 20.0}),
        case("other_search_clip_by_stem", "normal_eval named without .mp4, 45 to 50 s, the top hit of its query and "
             "inside its first collision", True, [],
             answers={"t5.query": SUNNY, "t5.file": "normal_eval", "t5.start_s": 45.0, "t5.end_s": 50.0}),
        # T6 (should)
        case("same_track_without_frame", "track 9's own id; the grader searches from frame 3 and finds object 21, "
             "but the answers do not say which frame the learner used", True, [],
             answers={"t6.query_object_id": "9", "t6.answer_object_id": "9"}),
        case("same_track_with_frame", "with the frame given, the search is exact: object 21 ranks first", True,
             ["part1.appearance_match"],
             answers={"t6.query_object_id": "9", "t6.answer_object_id": "9", "t6.query_frame_id": "3"}),
        case("nearest_with_frame", "object 21, the closest object to track 9 in frame 3", True, [],
             answers={"t6.query_object_id": "9", "t6.answer_object_id": "21", "t6.query_frame_id": "3"}),
        case("own_track_is_nearest", "from frame 0, track 7's nearest other frame is frame 1; normal_eval's twin of "
             "track 7 must stay out of the search", True, [],
             answers={"t6.query_object_id": "7", "t6.answer_object_id": "7", "t6.query_frame_id": "0"}),
        case("nearest_with_frame_time", "the frame named by its timestamp, as the notebook knows it", True, [],
             answers={"t6.query_object_id": "9", "t6.answer_object_id": "21",
                      "t6.query_frame_time": "2025-01-01T00:00:00.750Z"}),
        case("same_track_with_frame_time", "with the frame time given, object 21 ranks first", True,
             ["part1.appearance_match"],
             answers={"t6.query_object_id": "9", "t6.answer_object_id": "9",
                      "t6.query_frame_time": "2025-01-01T00:00:00.750Z"}),
        case("query_object_without_embedding", "object 15 is too small to carry an embedding", True,
             ["part1.appearance_match"], answers={"t6.query_object_id": "15", "t6.answer_object_id": "15"}),
        # T7 (should)
        case("throughput_off", "86,400 busy seconds, a whole day, where 8,640 x 4,200 ms gives 36,288", True,
             ["part1.throughput_estimate"], answers={"t7.busy_seconds_per_day": 86400.0}),
        case("requests_per_day_wrong", "86,400 is the seconds in a day, not the requests", True,
             ["part1.throughput_estimate"], answers={"t7.requests_per_day": 86400}),
        case("requests_per_day_as_float", "86400 / 10 is 8640.0 in Python: still 8,640 requests", True, [],
             answers={"t7.requests_per_day": 8640.0}),
        case("busy_seconds_within_tolerance", "40,000 s is within 15 percent of 36,288", True, [],
             answers={"t7.busy_seconds_per_day": 40000.0}),
        case("latency_zero", "no latency measured", True, ["part1.throughput_estimate"], answers={"t7.latency_ms": 0}),
        # questions
        case("five_questions_right", "five of eight is below the key's min_correct", False, ["part1.questions"],
             answers={"questions": questions_with(correct=5, answered=8)}),
        case("six_questions_right", "six right, one wrong, one unanswered is enough", True, [],
             answers={"questions": questions_with(correct=6, answered=7)}),
        case("lower_case_letters", "letters are compared without case", True, [],
             answers={"questions": {q: letter.lower() for q, letter in answer_key()["questions"].items()}}),
        # should-level checks never decide
        case("should_checks_only", "T3, the T5 overlap, T6 and T7 fail; the result is still a pass", True,
             ["part1.kafka_decoded", "part1.search_hit_overlaps_collision", "part1.appearance_match",
              "part1.throughput_estimate"],
             answers={"t3.sensor_id": "normal_eval", "t5.query": RAIN, "t5.start_s": 55.0, "t5.end_s": 60.0,
                      "t6.answer_object_id": "9", "t7.busy_seconds_per_day": 86400.0}),
        # services
        case("services_down", "every live check fails closed; the T5 overlap, T7 and the questions need no service",
             False, ["part1.video_in_vios", "part1.detections_indexed", "part1.kafka_decoded", "part1.captions_indexed",
                     "part1.search_matches_reference", "part1.appearance_match"], services="down"),
    ]


def build() -> Dict[str, str]:
    """Every fixture file, by path relative to this directory."""
    files = {"world.json": dump(world()), "answer_key.json": dump(answer_key()), "pass/answers.json": dump(pass_answers())}
    for name, text, expect in cases():
        if text is not None:
            files[f"cases/{name}/answers.json"] = text
        files[f"cases/{name}/expect.json"] = dump(expect)
    return files


def main() -> int:
    files = build()
    cases_dir = HERE / "cases"
    if cases_dir.is_dir():   # drop cases that no longer exist, so the tree matches build() exactly
        for stale in sorted(cases_dir.rglob("*"), reverse=True):
            relative = stale.relative_to(HERE).as_posix()
            if stale.is_file() and relative not in files:
                stale.unlink()
            elif stale.is_dir() and not any(stale.iterdir()):
                stale.rmdir()
    for relative, text in files.items():
        target = HERE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} fixture files under {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
