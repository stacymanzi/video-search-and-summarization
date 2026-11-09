"""Contracts between the grader and the files around it: the rubric, the schemas, the keys, the
ground truth, the fixtures, the container entrypoint, and the requests the assessment notebook sends.
The check ids, the marker and the answers.json fields are names the assessment notebook, part1/quiz.py,
course_ui and the course docs depend on."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from grader import common, grade_part1, serve
from tests.conftest import ASSESSMENT_DIR, FIXTURES

ENTRYPOINT = ASSESSMENT_DIR.parent / "entrypoint.assessment.sh"
ANSWER_KEY = common.ANSWER_KEYS_DIR / "part1.json"
LAB_PART1 = ASSESSMENT_DIR.parent / "lab" / "task" / "part1"   # absent inside the assessment image

# The check ids and their level: T1, T2, T4, the T5 search and the questions decide.
EXPECTED_CHECKS = {
    "part1.video_in_vios": True,
    "part1.detections_indexed": True,
    "part1.kafka_decoded": False,
    "part1.captions_indexed": True,
    "part1.search_matches_reference": True,
    "part1.search_hit_overlaps_collision": False,
    "part1.appearance_match": False,
    "part1.throughput_estimate": False,
    "part1.questions": True,
}
# answers.json as the task cells write it; t6.query_frame_id and t6.query_frame_time are optional extras.
EXPECTED_ANSWERS = {
    "t1": {"vios_sensor_id", "vios_name"},
    "t2": {"frames", "top_type"},
    "t3": {"sensor_id", "frame_id", "timestamp"},
    "t4": {"file_id", "chunks_10", "chunks_0", "frame_counts_8"},
    "t5": {"query", "file", "start_s", "end_s"},
    "t6": {"query_object_id", "answer_object_id"},
    "t7": {"latency_ms", "requests_per_day", "busy_seconds_per_day"},
}
# sha256 of s3://dli-lms/data/c-fx-17-v2/ground_truth/search_videos.json as uploaded on 2026-11-02.
GROUND_TRUTH_SHA256 = "61af5f2feaf5827df9c7e8c29557747096751ac019404b699f72c69e6b788a56"


def test_rubric_lists_part1_with_the_expected_checks(rubric):
    assert rubric["certificate"] == "all_listed"
    assert common.assessment_ids(rubric) == ["part1"]
    section = rubric["part1"]
    assert {spec["id"]: spec["must"] for spec in section["checks"]} == EXPECTED_CHECKS
    assert section["pass_rule"] == "all_must"
    assert section["clip_stem"] == "rain_check"
    assert section["notebook"] == "part1/assessment.ipynb"
    assert section["answers_file"] == common.DEFAULT_ANSWERS_FILE
    assert all(spec.get("text", "").strip() for spec in section["checks"])
    tasks = [spec["task"] for spec in section["checks"]]
    assert tasks == ["p1-t1", "p1-t2", "p1-t3", "p1-t4", "p1-t5", "p1-t5", "p1-t6", "p1-t7", "questions"]


def test_search_files_are_the_labelled_clips(rubric):
    """T5 searches exactly the four clips the ground truth labels, in the order the notebook lists them."""
    labelled = [entry["file"] for entry in json.loads(common.GROUND_TRUTH_PATH.read_text(encoding="utf-8"))]
    assert rubric["part1"]["search_files"] == ["normal_eval.mp4", "rain_eval.mp4", "fog_eval.mp4", "night_eval.mp4"]
    assert sorted(rubric["part1"]["search_files"]) == sorted(labelled)


def test_grader_checks_follow_the_rubric_order(rubric):
    assert [check_id for check_id, _ in grade_part1.CHECKS] == [spec["id"] for spec in rubric["part1"]["checks"]]
    for _, method in grade_part1.CHECKS:
        assert callable(getattr(grade_part1.Part1, method))


def test_graders_cover_the_rubric(rubric):
    assert sorted(serve.GRADERS) == sorted(common.assessment_ids(rubric))


def test_marker_schema_names_the_rubric_assessments(rubric):
    schema = common.load_schema("marker")
    assert schema["properties"]["assessment"]["enum"] == common.assessment_ids(rubric)


def test_rubric_carries_no_answer(rubric):
    """The lab image ships rubric.json; no question id may map to a letter anywhere in it."""
    text = json.dumps(rubric)
    assert not re.search(r'"p1-q\d+"\s*:', text)
    assert "questions" not in rubric["part1"]
    if ANSWER_KEY.is_file():
        key = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))
        for explanation in key.get("explanations", {}).values():
            assert explanation not in text


def test_rubric_numbers_match_the_texts(rubric):
    """The public texts quote the numbers the grader uses."""
    numbers = rubric["part1"]["thresholds"]
    texts = {spec["id"]: spec["text"] for spec in rubric["part1"]["checks"]}
    captions = texts["part1.captions_indexed"]
    chunks = " or ".join(str(n) for n in numbers["chunks_at_10_s"])
    assert f"split the {numbers['clip_seconds']} s clip into {chunks} chunks" in captions
    assert f"it made {numbers['chunks_at_0_s']} chunk" in captions
    assert f"With {numbers['frames_per_chunk']} sampled frames, every full chunk reported frame_count {numbers['frames_per_chunk']}" in captions
    search = texts["part1.search_matches_reference"]
    assert f"within {numbers['search_slack_s']} s of the top hit's" in search
    assert search.count(common.EMBEDDING_ANCHOR[:19]) == 1
    throughput = texts["part1.throughput_estimate"]
    requests = numbers["seconds_per_day"] // numbers["chunk_duration_s"]
    assert f"t7.requests_per_day is {requests:,}" in throughput
    assert f"one {numbers['chunk_duration_s']}-second chunk every {numbers['chunk_duration_s']} seconds" in throughput
    assert f"within {round(numbers['throughput_tolerance'] * 100)} percent of {requests:,} x t7.latency_ms / 1000" in throughput
    assert "cameras" not in throughput


def test_answers_schema_has_the_expected_fields():
    schema = common.load_schema("answers")
    assert set(schema["properties"]) == {"clip_stem", "questions"} | set(EXPECTED_ANSWERS)
    for task, keys in EXPECTED_ANSWERS.items():
        assert set(schema["properties"][task]["required"]) == keys, task
    assert set(schema["properties"]["t6"]["properties"]) == EXPECTED_ANSWERS["t6"] | {"query_frame_id", "query_frame_time"}


def test_the_pass_fixture_follows_the_answers_schema():
    answers = json.loads((FIXTURES / "pass" / "answers.json").read_text(encoding="utf-8"))
    assert common.validate(answers, common.load_schema("answers")) == []
    assert set(answers) == {"clip_stem", "questions"} | set(EXPECTED_ANSWERS)


def test_ground_truth_is_the_published_file():
    data = common.GROUND_TRUTH_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == GROUND_TRUTH_SHA256
    clips = {entry["file"]: entry for entry in json.loads(data)}
    assert sorted(clips) == ["fog_eval.mp4", "night_eval.mp4", "normal_eval.mp4", "rain_eval.mp4"]
    for entry in clips.values():
        assert sorted(e["type"] for e in entry["events"]) == ["collision", "collision", "stalled", "stalled"]
    first = next(e for e in clips["rain_eval.mp4"]["events"] if e["type"] == "collision")
    assert (first["start_s"], first["end_s"]) == (19.21, 43.75)


def test_production_answer_key_has_the_expected_format(rubric):
    if not ANSWER_KEY.is_file():
        pytest.skip("grader/answer_keys/part1.json is written with the assessment notebook")
    key = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))
    assert common.answer_key_problems(key) == []
    assert list(key["questions"]) == [f"p1-q{n}" for n in range(1, 9)]
    text = rubric["part1"]["pass_rule_text"]
    assert f"at least {key['min_correct']} of the {len(key['questions'])} multiple-choice answers" in text


def test_fixture_answer_key_has_the_production_format():
    key = json.loads((FIXTURES / "answer_key.json").read_text(encoding="utf-8"))
    assert common.answer_key_problems(key) == []
    if ANSWER_KEY.is_file():
        real = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))
        assert list(key["questions"]) == list(real["questions"]) and key["min_correct"] == real["min_correct"]
        assert key["questions"] != real["questions"], "the fixture key must not copy the real letters"


def load_make_fixtures():
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixtures_are_generated():
    """The files on disk are exactly what make_fixtures.py writes; rerun it after changing the story."""
    expected = load_make_fixtures().build()
    on_disk = {p.relative_to(FIXTURES).as_posix(): p.read_text(encoding="utf-8")
               for p in FIXTURES.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    on_disk.pop("make_fixtures.py")
    on_disk.pop("README.md", None)
    assert sorted(on_disk) == sorted(expected)
    stale = [path for path, text in expected.items() if on_disk[path] != text]
    assert not stale, f"regenerate the fixtures: {stale}"


def test_fixture_dirs_survive_git():
    """task1/.gitignore drops every directory named data, models or results."""
    names = {p.name for p in FIXTURES.rglob("*") if p.is_dir()}
    assert not names & {"data", "models", "results"}


def test_entrypoint_clears_every_marker():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'rm -f "${RESULTS}"/*.json "${RESULTS}"/PASSED' in script
    assert "a1.json" not in script and "/grade/part1" in script


def test_entrypoint_starts_the_grader_without_the_learner_path():
    """The image's PYTHONPATH names /dli/task/part1, which the lab can write: neither process may inherit it."""
    lines = [line.strip() for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines()]
    assert "unset PYTHONPATH" in lines
    assert lines.index("unset PYTHONPATH") < lines.index("python3 grader/serve.py &") < lines.index("./assessment_runner &")


def load_lab_module(name: str):
    """A Part 1 helper module from the lab tree, for comparing the requests it sends with the grader's."""
    path = LAB_PART1 / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"{path} is not in this tree")
    spec = importlib.util.spec_from_file_location(f"lab_{name}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_t5_repeats_the_notebooks_search(monkeypatch, rubric):
    """course_setup.text_embedding and knn_search, which the T5 cell calls, send what the grader sends."""
    pytest.importorskip("requests")
    course_setup = load_lab_module("course_setup")
    assert (course_setup.EMBED_INDEX, course_setup.EMBED_MODEL, course_setup.EMBEDDING_ANCHOR) == (
        common.EMBED_INDEX, common.EMBED_MODEL, common.EMBEDDING_ANCHOR)
    sent = []
    monkeypatch.setattr(course_setup, "_json", lambda method, url, timeout, **kw: sent.append((method, url, kw["json"]))
                        or {"data": [{"embeddings": [0.5, 0.25]}]})
    assert course_setup.text_embedding("two cars collide") == [0.5, 0.25]
    assert sent == [("POST", common.RT_EMBED_URL + "/generate_text_embeddings",
                     {"text_input": ["two cars collide"], "model": common.EMBED_MODEL})]
    searched = []
    monkeypatch.setattr(course_setup, "es", lambda method, path, **kw: searched.append((method, path, kw["json"]))
                        or {"hits": {"hits": []}})
    k = rubric["part1"]["thresholds"]["search_k"]
    course_setup.knn_search([0.5, 0.25], ["id-a", "id-b"], k=k)
    assert searched == [("POST", f"/{common.EMBED_INDEX}/_search", grade_part1.embedding_search_body([0.5, 0.25], ["id-a", "id-b"], k))]


def test_t6_repeats_the_notebooks_search():
    """quiz.check_t6 and the T6 cell search with this body; the grader reads the answer the same way."""
    quiz = load_lab_module("quiz")
    vector, frame_time = [0.1, 0.2], "2025-01-01T00:00:10.000Z"
    assert quiz._object_knn("rain_check", vector, frame_time) == grade_part1.appearance_search_body("rain_check", vector, frame_time)


def test_grader_tree_keeps_the_house_rules():
    """No em dash, no retired model name, no learner-facing devbox note in anything this directory ships."""
    files = [p for p in ASSESSMENT_DIR.rglob("*")
             if p.is_file() and p.name != "assessment_runner" and "__pycache__" not in p.parts]
    files.append(ENTRYPOINT)
    retired = "Cosmos " + "Reason"
    devbox = "VERIFY ON " + "DEVBOX"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text, path
        assert retired not in text and devbox not in text, path
