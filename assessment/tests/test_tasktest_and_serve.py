"""tasktest.py (the LMS aggregator) and serve.py (the lab-facing HTTP front end), offline."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import subprocess
import sys
import threading

import pytest

from grader import common, grade_part1, serve
from tests.conftest import ASSESSMENT_DIR, FIXTURES, build_context


def load_tasktest():
    spec = importlib.util.spec_from_file_location("tasktest", ASSESSMENT_DIR / "tasktest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def graded_results(tmp_path, case=None):
    """A results dir holding the real grader's marker for the pass set (or for one fixture case)."""
    results = tmp_path / "results"
    results.mkdir()
    ctx = build_context(tmp_path / "grade", case)
    marker = grade_part1.grade(ctx)
    (results / "part1.json").write_text(json.dumps(marker))
    return results, marker


def test_pass_scores_100(tmp_path):
    results, marker = graded_results(tmp_path)
    assert marker["passed"]
    out = load_tasktest().runtest(results)
    assert out["score"] == 100
    assert out["message"].startswith("Part 1 PASS. Congratulations, you passed the assessment. Your Part 1 result is recorded.")
    assert "https://learn.nvidia.com/my-learning/" in out["message"]
    assert "certificate" not in out["message"]   # until the certificate rule is settled


def test_missing_marker_is_not_graded(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    out = load_tasktest().runtest(results)
    assert out["score"] == 0
    assert out["message"] == ("Part 1 NOT GRADED: there is no result yet. Open part1/assessment.ipynb and run its "
                              "Submit cell.\nNot passed yet. Do what the line above says, then press ASSESS TASK again.")


def test_failed_assessment_names_the_must_checks(tmp_path):
    results, marker = graded_results(tmp_path, FIXTURES / "cases" / "window_not_the_top_hit")
    assert not marker["passed"]
    out = load_tasktest().runtest(results)
    assert out["score"] == 0
    assert out["message"].startswith("Part 1 FAIL: part1.search_matches_reference. Fix these checks in "
                                     "part1/assessment.ipynb and run its Submit cell again.")


def test_should_failures_alone_still_score_100(tmp_path):
    results, marker = graded_results(tmp_path, FIXTURES / "cases" / "should_checks_only")
    assert marker["passed"] and len([c for c in marker["checks"] if not c["passed"]]) == 4
    out = load_tasktest().runtest(results)
    assert out["score"] == 100 and "Part 1 PASS" in out["message"]


def test_forged_marker_scores_0(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "part1.json").write_text(json.dumps({"passed": True}))
    (results / "PASSED").write_text("")
    out = load_tasktest().runtest(results)
    assert out["score"] == 0 and "Part 1 FAIL" in out["message"]


def test_marker_of_another_assessment_scores_0(tmp_path):
    results, marker = graded_results(tmp_path)
    marker["assessment"] = "a1"
    (results / "part1.json").write_text(json.dumps(marker))
    out = load_tasktest().runtest(results)
    assert out["score"] == 0 and "Part 1 FAIL" in out["message"]


def test_marker_passed_flag_cannot_override_checks(tmp_path):
    results, marker = graded_results(tmp_path)
    for item in marker["checks"]:
        if item["id"] == "part1.captions_indexed":
            item["passed"] = False
    (results / "part1.json").write_text(json.dumps(marker))
    out = load_tasktest().runtest(results)
    assert out["score"] == 0 and "part1.captions_indexed" in out["message"]


def test_unreadable_marker_scores_0(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "part1.json").write_text("{")
    out = load_tasktest().runtest(results)
    assert out["score"] == 0 and "unreadable" in out["message"]


@pytest.mark.parametrize("rubric", [
    {"certificate": "all_three", "assessments": ["part1"], "part1": {"checks": []}},
    {"certificate": "all_listed", "assessments": [], "part1": {"checks": []}},
    {"certificate": "all_listed", "assessments": ["part1"]},
    {"certificate": "all_listed", "part1": {"checks": []}},
])
def test_broken_rubric_is_reported_not_passed(tmp_path, rubric):
    results, _ = graded_results(tmp_path)
    path = tmp_path / "rubric.json"
    path.write_text(json.dumps(rubric))
    out = load_tasktest().runtest(results, rubric_path=path)
    assert out["score"] == 0 and "misconfigured" in out["message"]


def test_every_listed_assessment_must_pass(tmp_path):
    """all_listed with two assessments: a pass on one of them is not enough."""
    results, _ = graded_results(tmp_path)
    rubric = json.loads((ASSESSMENT_DIR / "grader" / "rubric.json").read_text())
    rubric["assessments"] = ["part1", "part2"]
    rubric["part2"] = {"label": "Part 2", "notebook": "part2/assessment.ipynb", "checks": [{"id": "part2.x", "must": True}]}
    path = tmp_path / "rubric.json"
    path.write_text(json.dumps(rubric))
    out = load_tasktest().runtest(results, rubric_path=path)
    assert out["score"] == 0
    assert "Part 1 PASS" in out["message"] and "Part 2 NOT GRADED" in out["message"] and "1 of 2" in out["message"]


def test_tasktest_prints_one_json_object(tmp_path):
    results, _ = graded_results(tmp_path)
    env = dict(os.environ, GRADER_RESULTS_DIR=str(results))
    completed = subprocess.run([sys.executable, "-I", str(ASSESSMENT_DIR / "tasktest.py")],
                               capture_output=True, text=True, env=env, check=True)
    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == load_tasktest().runtest(results)


def planted_module_dir(tmp_path):
    """A folder like /dli/task/part1 after a learner put a json.py there that prints a perfect score."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "json.py").write_text(
        "import sys\n"
        "sys.stdout.write('{\"score\": 100, \"message\": \"planted json module\"}\\n')\n"
        "sys.exit(0)\n", encoding="utf-8")
    return planted


def test_a_planted_json_module_cannot_fake_a_score(tmp_path):
    """The runner may start tasktest.py with the image's PYTHONPATH; nothing on it may be imported."""
    results = tmp_path / "results"
    results.mkdir()
    env = dict(os.environ, GRADER_RESULTS_DIR=str(results), PYTHONPATH=str(planted_module_dir(tmp_path)))
    completed = subprocess.run([sys.executable, "-B", str(ASSESSMENT_DIR / "tasktest.py")],
                               capture_output=True, text=True, env=env, check=True)
    assert "planted" not in completed.stdout
    out = json.loads(completed.stdout.strip().splitlines()[-1])
    assert out["score"] == 0 and "NOT GRADED" in out["message"]


def test_serve_ignores_a_planted_json_module(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(planted_module_dir(tmp_path)))
    completed = subprocess.run([sys.executable, "-B", str(ASSESSMENT_DIR / "grader" / "serve.py"), "--help"],
                               capture_output=True, text=True, env=env, check=True)
    assert "planted" not in completed.stdout and "usage:" in completed.stdout


@pytest.fixture
def server(tmp_path):
    ctx = build_context(tmp_path)
    httpd = serve.make_server(ctx, port=0, host="127.0.0.1")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield ctx, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=b"" if method == "POST" else None)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, payload


def test_health(server):
    ctx, port = server
    status, report = request(port, "GET", "/health")
    assert status == 200, report
    assert report["status"] == "ok" and report["results_dir_writable"] and report["answers_present"]
    assert report["graders"] == ["part1"] and report["markers"] == {"part1": False}
    assert report["answer_keys_present"] == {"part1": True} and report["ground_truth_present"]
    assert report["registry_present"] and report["registry_file"] == str(ctx.registry_file)


def test_health_reports_a_missing_registry_without_degrading(server):
    """course-prep writes the registry after launch: early in the day it is missing and nothing is broken."""
    ctx, port = server
    ctx.registry_file.unlink()
    status, report = request(port, "GET", "/health")
    assert status == 200 and report["status"] == "ok" and report["registry_present"] is False


def test_health_degrades_without_an_answer_key(server):
    ctx, port = server
    (ctx.answer_keys_dir / "part1.json").unlink()
    status, report = request(port, "GET", "/health")
    assert status == 503 and report["status"] == "degraded" and "answer key missing" in report["error"]


def test_post_grade_writes_marker(server):
    ctx, port = server
    status, marker = request(port, "POST", "/grade/part1")
    assert status == 200 and marker["assessment"] == "part1" and marker["passed"] is True
    on_disk = json.loads((ctx.results_dir / "part1.json").read_text())
    assert on_disk == marker
    status, report = request(port, "GET", "/health")
    assert report["markers"]["part1"] is True


def test_post_unknown_assessment(server):
    _, port = server
    status, payload = request(port, "POST", "/grade/a1")
    assert status == 404 and "a1" in payload["error"] and "part1" in payload["error"]
    status, payload = request(port, "GET", "/grade/part1")
    assert status == 404 and "POST /grade/part1" in payload["error"]


def test_crash_leaves_failed_marker(server, monkeypatch):
    ctx, port = server

    def boom(_ctx):
        raise RuntimeError("synthetic grader bug")

    monkeypatch.setitem(serve.GRADERS, "part1", boom)
    status, marker = request(port, "POST", "/grade/part1")
    assert status == 200 and marker["passed"] is False
    assert all("synthetic grader bug" in c["detail"] for c in marker["checks"])
    assert (ctx.results_dir / "part1.json").is_file()


def test_schema_validator_basics():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"type": "array", "minItems": 1, "items": {"enum": ["x"]}}}, "additionalProperties": False}
    assert common.validate({"a": 1, "b": ["x"]}, schema) == []
    errors = common.validate({"a": 0, "b": ["y"], "c": 1}, schema)
    assert any("minimum" in e for e in errors) and any("not one of" in e for e in errors) and any("unexpected field" in e for e in errors)
    assert common.validate({"a": True}, schema)  # bool is not an integer
    assert common.validate("2025-01-02T00:00:00Z", {"type": "string", "pattern": "^[0-9]{4}-"}) == []
