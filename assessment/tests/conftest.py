"""Shared harness for the offline grader tests (pytest).

Each test builds a throwaway ``/dli``-like tree under ``tmp_path`` from one
fixture case: the learner's ``work/assessment/answers.json``, course-prep's
``data/search_sources.json`` (from ``world["registry"]``), a results
directory and an answer-key directory holding the fixture key (never the
real one). It binds a ``common.Context`` to that tree
with the fake transport from ``fakes.py`` and grades. Nothing here touches
the network.

Run from the repo root: ``python3 -I -B -m pytest task1/assessment/tests -q -p no:cacheprovider``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import pytest

ASSESSMENT_DIR = Path(__file__).resolve().parents[1]
FIXTURES = ASSESSMENT_DIR / "fixtures"
if str(ASSESSMENT_DIR) not in sys.path:
    sys.path.insert(0, str(ASSESSMENT_DIR))

from grader import common  # noqa: E402
from tests.fakes import FakeHttp, load_world  # noqa: E402


def fixture_cases():
    """(case name, case dir) for every case under fixtures/cases/."""
    return [(case.name, case) for case in sorted((FIXTURES / "cases").iterdir()) if (case / "expect.json").is_file()]


def build_context(tmp_path: Path, case_dir: Optional[Path] = None, services: str = "up", world: Optional[dict] = None,
                  rubric: Optional[dict] = None, answer_key: Optional[Path] = FIXTURES / "answer_key.json",
                  registry: bool = True) -> common.Context:
    """A Context over a fresh tree. ``case_dir`` supplies answers.json (raw bytes, so a broken file stays broken)."""
    world = load_world() if world is None else world
    root = tmp_path / "dli"
    answers_dir = root / "task" / "work" / "assessment"
    data_dir = root / "task" / "data"
    results = root / "assessment_results"
    keys = root / "assessment" / "grader" / "answer_keys"
    for folder in (answers_dir, data_dir, results, keys):
        folder.mkdir(parents=True, exist_ok=True)
    source = (case_dir or FIXTURES / "pass") / "answers.json"
    if source.is_file():
        shutil.copyfile(source, answers_dir / "answers.json")
    if answer_key is not None:
        shutil.copyfile(answer_key, keys / "part1.json")
    if registry:
        (data_dir / "search_sources.json").write_text(json.dumps(world["registry"], indent=2) + "\n", encoding="utf-8")
    return common.Context(
        answers_file=answers_dir / "answers.json",
        results_dir=results,
        answer_keys_dir=keys,
        ground_truth_file=common.GROUND_TRUTH_PATH,
        registry_file=data_dir / "search_sources.json",
        rubric=rubric or common.load_rubric(),
        http=FakeHttp(world, down=(services == "down")),
        now=common.utc_now,
    )


def failed_ids(marker: dict):
    return {item["id"] for item in marker["checks"] if not item["passed"]}


def assert_clean(ctx: common.Context, marker: dict) -> None:
    """Every failure is a deliberate one: no grader bug and no request the fake does not cover."""
    assert not ctx.http.errors, ctx.http.errors
    bugs = [item for item in marker["checks"]
            if "The grader hit a bug" in item["detail"] or "The grader crashed" in item["detail"]]
    assert not bugs, common.summarize(marker)


def assert_case(marker: dict, case_dir: Path, case_name: str) -> None:
    """The marker's overall flag is ``expect.passed`` and exactly the ids in ``expect.fails`` failed."""
    expect = json.loads((case_dir / "expect.json").read_text(encoding="utf-8"))
    assert marker["passed"] is expect["passed"], (
        f"{case_name} should {'pass' if expect['passed'] else 'fail'} ({expect['note']}):\n" + common.summarize(marker))
    assert failed_ids(marker) == set(expect["fails"]), (
        f"{case_name}: expected exactly {sorted(expect['fails'])} to fail ({expect['note']}):\n" + common.summarize(marker))


@pytest.fixture
def rubric():
    return common.load_rubric()
