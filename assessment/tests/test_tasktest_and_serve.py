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




def graded_results(tmp_path, case=None):
    """A results dir holding the real grader's marker for the pass set (or for one fixture case)."""
    results = tmp_path / "results"
    results.mkdir()
    ctx = build_context(tmp_path / "grade", case)
    marker = grade_part1.grade(ctx)
    (results / "part1.json").write_text(json.dumps(marker))
    return results, marker
























def planted_module_dir(tmp_path):
    """A folder like /dli/task/part1 after a learner put a json.py there that prints a perfect score."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "json.py").write_text(
        "import sys\n"
        "sys.stdout.write('{\"score\": 100, \"message\": \"planted json module\"}\\n')\n"
        "sys.exit(0)\n", encoding="utf-8")
    return planted




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
