#!/usr/bin/env python3
"""tasktest.py: what the DLI assessment runner executes when the learner presses ASSESS TASK.

Aggregator only. It grades nothing: it reads the marker the grader service
wrote for each assessment that ``grader/rubric.json`` lists under
``assessments`` (``/dli/assessment_results/<id>.json``), applies the
certificate rule from the same file and prints exactly one JSON object,
``{"score": 100 or 0, "message": "..."}``, like V1's tasktest.py.

The one rule implemented is ``all_listed``: every listed assessment passes.

A marker counts as PASS only when it is well formed, names its own assessment,
carries every must-check the rubric lists for it with ``passed: true``, and its
own ``passed`` flag agrees. Anything else (missing file, V1-style PASSED file,
a hand-written ``{"passed": true}``) is FAIL or NOT GRADED. The results volume
is read-only in the lab, so a marker can only come from the assessment
container; this recomputation is the second line.

Standard library only, no imports from ``grader/``: the runner must always get
its JSON, even when the grader package is broken. Run as a script, it first
drops every import path outside the interpreter's own directories:
entrypoint.assessment.sh clears PYTHONPATH, and this is the second line.
"""

import os
import sys

if __name__ == "__main__":
    # The image's PYTHONPATH names /dli/task/part1, which the lab can write, and a json.py planted there would
    # run here and print any score. Keep only the interpreter's own directories before the first import.
    # Paths are compared after resolving symbolic links, as the interpreter's prefixes often are links.
    _OWN = {os.path.realpath(d) for d in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)}
    sys.path[:] = [p for p in sys.path if p and any(os.path.realpath(p) == d or os.path.realpath(p).startswith(d + os.sep)
                                                    for d in _OWN)]

import json  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
RUBRIC_PATH = HERE / "grader" / "rubric.json"
DEFAULT_RESULTS_DIR = "/dli/assessment_results"
RULE = "all_listed"

PASS_MESSAGE = ("Your Part 1 result is recorded. Go to https://learn.nvidia.com/my-learning/ and click this "
                "course's name to see your progress.")
# Restore this one as PASS_MESSAGE once the certificate rule is settled (a certificate or a badge, Haleem).
CERTIFICATE_MESSAGE = ("Go to https://learn.nvidia.com/my-learning/ and click this course's name at the top of the "
                       "dashboard to view your certificate for the workshop.")


def evaluate_marker(path, rubric_section, assessment):
    """Return (status, failing_check_ids, note) for one marker file.

    status is "PASS", "FAIL" or "NOT GRADED".
    """
    notebook = rubric_section.get("notebook") or "the assessment notebook"
    if not path.is_file():
        return "NOT GRADED", [], f"there is no result yet. Open {notebook} and run its Submit cell."
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return "FAIL", [], f"the result is unreadable ({type(exc).__name__}). Run the Submit cell of {notebook} again."
    if not isinstance(marker, dict) or marker.get("assessment") != assessment or not isinstance(marker.get("checks"), list):
        return "FAIL", [], f"this is not a grader result for this assessment. Run the Submit cell of {notebook} again."
    by_id = {}
    for item in marker["checks"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id[item["id"]] = bool(item.get("passed"))
    failing = []
    for spec in rubric_section.get("checks", []):
        if spec.get("must", True) and not by_id.get(spec["id"], False):
            failing.append(spec["id"])
    if failing:
        return "FAIL", failing, ""
    if marker.get("passed") is not True:
        return "FAIL", [], (f"the result reports a failure although every check passed. Run the Submit cell of "
                            f"{notebook} again.")
    return "PASS", [], ""


def load_rubric(rubric_path):
    """The rubric and its assessment list; anything malformed raises ValueError."""
    rubric = json.loads(Path(rubric_path).read_text(encoding="utf-8"))
    if not isinstance(rubric, dict):
        raise ValueError("rubric.json is not a JSON object")
    rule = rubric.get("certificate")
    if rule != RULE:
        raise ValueError(f"certificate rule {rule!r} is not implemented; tasktest.py knows {RULE} only")
    assessments = rubric.get("assessments")
    if not isinstance(assessments, list) or not assessments or not all(isinstance(a, str) and a for a in assessments):
        raise ValueError("rubric has no assessments list")
    for assessment in assessments:
        if not isinstance(rubric.get(assessment), dict) or not isinstance(rubric[assessment].get("checks"), list):
            raise ValueError(f"rubric has no checks for {assessment}")
    return rubric, assessments


def runtest(results_dir=None, rubric_path=RUBRIC_PATH):
    results_dir = Path(results_dir or os.environ.get("GRADER_RESULTS_DIR", DEFAULT_RESULTS_DIR))
    try:
        rubric, assessments = load_rubric(rubric_path)
    except (OSError, ValueError) as exc:
        return {"score": 0, "message": f"The grader is misconfigured ({exc}). Tell the instructor."}

    lines = []
    passed = 0
    for assessment in assessments:
        section = rubric[assessment]
        label = section.get("label") or assessment
        status, failing, note = evaluate_marker(results_dir / f"{assessment}.json", section, assessment)
        if status == "PASS":
            passed += 1
            lines.append(f"{label} PASS")
        elif failing:
            notebook = section.get("notebook") or "the assessment notebook"
            lines.append(f"{label} FAIL: {', '.join(failing)}. Fix these checks in {notebook} and run its Submit "
                         "cell again.")
        else:
            lines.append(f"{label} {status}: {note}")
    if passed == len(assessments):
        what = "the assessment" if len(assessments) == 1 else f"all {len(assessments)} assessments"
        return {"score": 100, "message": " ".join(lines) + f". Congratulations, you passed {what}. " + PASS_MESSAGE}
    if len(assessments) == 1:
        summary = "Not passed yet. Do what the line above says, then press ASSESS TASK again."
    else:
        summary = (f"{passed} of {len(assessments)} assessments passed. Do what the lines above say, then press "
                   "ASSESS TASK again.")
    return {"score": 0, "message": "\n".join(lines) + "\n" + summary}


def main():
    """Print exactly one JSON object. A bug in this file is reported as score 0, never as a traceback
    (the runner would otherwise have nothing to parse); nothing is swallowed, the message carries it."""
    try:
        result = runtest()
    except Exception as exc:  # fail closed, visibly
        result = {"score": 0, "message": f"tasktest.py failed before it could read the results "
                                         f"({type(exc).__name__}: {exc}). Tell the instructor."}
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
