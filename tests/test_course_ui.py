#!/usr/bin/env python3
"""course_ui.py renders plain HTML strings and reads the learner's progress; these tests check what a learner reads.

They also pin the shipped part1/course_path.json to the names other files depend on, and check the cells
task1/scripts/course_path.py generates from it.

    python3 -I task1/tests/test_course_ui.py
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

TASK1 = pathlib.Path(__file__).resolve().parents[1]
PART1 = TASK1 / "notebooks"
sys.path.insert(0, str(PART1))

import course_ui  # noqa: E402

STEPS = [
    {"id": "intro", "kind": "slides", "block": "Introduction", "minutes": 15, "title": "Slides",
     "summary": "The instructor talks."},
    {"id": "onboarding", "kind": "notebook", "block": "Introduction", "minutes": 15, "title": "0. Onboarding",
     "path": "part1/00-onboarding.ipynb", "summary": "Get ready.", "milestone_cell_id": "gate"},
    {"id": "nb2", "kind": "notebook", "block": "Part 1", "minutes": 40, "title": "2. Detection",
     "path": "part1/02.ipynb", "summary": "Detect things.", "milestone_cell_id": "ex-check"},
    {"id": "break", "kind": "break", "block": "Part 1", "minutes": 10, "title": "Break"},
    {"id": "part1", "kind": "assessment", "block": "Part 1", "minutes": 25, "title": "Part 1 Assessment",
     "path": "part1/assessment.ipynb", "summary": "On your own.", "milestone_cell_id": "submit",
     "done_when": ["the Submit cell shows PASSED"],
     "grades": "It checks the building blocks."},
    {"id": "agent", "kind": "terminal", "block": "Part 1", "minutes": 25, "title": "Coding agent",
     "path": "part1/agent.ipynb", "summary": "Ask the coding agent.", "milestone_cell_id": "agent-check",
     "evidence": ["work/agent/*.json"]},
    {"id": "lunch", "kind": "lunch", "minutes": 60, "title": "Lunch",
     "summary": "Save your work, then press the Part 2 launch button."},
    {"id": "part2", "kind": "slides", "block": "Part 2", "minutes": 210, "title": "Part 2", "summary": "On its own launch."},
]
# The last step as a Course Docs page instead of a notebook, the way the shipped path has VSS Coding Agent Skills.
DOCS_AGENT = {"id": "agent", "kind": "terminal", "block": "Part 1", "minutes": 40, "title": "Coding agent",
              "course_docs": "course/agent.md", "summary": "Ask the coding agent.",
              "evidence": ["work/agent/live_source.json"]}


def with_docs_agent():
    return [dict(DOCS_AGENT) if s["id"] == "agent" else dict(s) for s in STEPS]


def notebook(cell_id, *, execution_count=None, outputs=(), cell_type="code"):
    """A saved notebook with one cell, the way JupyterLab writes it after the cell ran (or not)."""
    cell = {"cell_type": cell_type, "id": cell_id, "metadata": {}, "source": ["print('ok')\n"]}
    if cell_type == "code":
        cell.update(execution_count=execution_count, outputs=list(outputs))
    return json.dumps({"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}, indent=1)


RAN_OK = {"execution_count": 3, "outputs": [{"output_type": "stream", "name": "stdout", "text": ["PASS\n"]}]}
RAN_ERROR = {"execution_count": 4, "outputs": [{"output_type": "error", "ename": "AssertionError", "evalue": "", "traceback": []}]}


class Renderers(unittest.TestCase):
    def test_timeline_places_windows_by_fraction(self):
        html = course_ui.timeline_html(120.0, [{"start_s": 60, "end_s": 90, "kind": "pass"}])
        self.assertIn("left:50.00%", html)
        self.assertIn("width:25.00%", html)
        self.assertIn("2:00 (120.0 s)", html)
        self.assertIn("position:relative", html)   # inline, so an untrusted notebook still draws the bar

    def test_results_card_counts_checks_and_escapes_detail(self):
        result = {"assessment": "part1", "passed": False, "graded_at": "2026-11-02T20:00:00Z", "checks": [
            {"id": "part1.video_in_vios", "must": True, "passed": True, "detail": "ok"},
            {"id": "part1.questions", "must": True, "passed": False, "detail": "5 of 8 <correct>"},
            {"id": "part1.kafka_decoded", "must": False, "passed": False, "detail": "not answered"},
        ]}
        html = course_ui.results_html(result, "Part 1 Assessment")
        self.assertIn("Part 1 Assessment: NOT PASSED", html)
        self.assertIn("1 of 3 checks passed", html)
        self.assertIn("5 of 8 &lt;correct&gt;", html)
        self.assertIn("run this cell again", html)
        self.assertIn("should", html)                # a should-check is labelled as one
        # The advice sits in the banner above the table, and only must checks decide the result.
        self.assertIn("in the table below", html)
        self.assertLess(html.index("in the table below"), html.index("<table>"))
        self.assertIn("do not stop you passing", html)
        self.assertNotIn("row above", html)
        html = course_ui.results_html({"passed": True, "checks": []}, "Part 1 Assessment")
        self.assertIn("Part 1 Assessment: PASSED", html)
        self.assertIn("result", html)
        for old in ("A1", "A2", "A3", "certificate", "all three"):
            self.assertNotIn(old, html)
        with self.assertRaises(KeyError):          # a grader response without checks is malformed
            course_ui.results_html({"passed": True}, "Part 1 Assessment")

    def test_results_label_defaults_to_the_step_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_file = pathlib.Path(tmp) / "course_path.json"
            path_file.write_text(json.dumps({"steps": STEPS}))
            html = course_ui.results_html({"assessment": "part1", "passed": True, "checks": []}, path_file=path_file)
            self.assertIn("Part 1 Assessment: PASSED", html)
            html = course_ui.results_html({"assessment": "other", "passed": False, "checks": []}, path_file=path_file)
            self.assertIn("other: NOT PASSED", html)

    def test_note_escapes_its_text(self):
        html = course_ui.note_html("<b>careful</b>", kind="fail", title="Heads up")
        self.assertIn("banner fail", html)
        self.assertIn("&lt;b&gt;careful&lt;/b&gt;", html)

    def test_the_cards_only_the_old_assessments_used_are_gone(self):
        for name in ("clip", "candidates", "verdict", "review", "checklist"):
            self.assertFalse(hasattr(course_ui, name), name)
            self.assertFalse(hasattr(course_ui, name + "_html"), name + "_html")
        source = (PART1 / "course_ui.py").read_text()
        for word in ("abstain", "A1", "certificate", "warehouse"):
            self.assertNotIn(word, source)


class ThePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.task_root = root / "task"
        (self.task_root / "part1").mkdir(parents=True)
        self.path_file = root / "course_path.json"
        self.path_file.write_text(json.dumps({"version": "test", "steps": STEPS}))
        self.results_dir = root / "results"
        self.results_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, prep_file=None):
        return course_ui.course_status(prep_file=prep_file or self.results_dir / "missing.json", results_dir=self.results_dir,
                                       task_root=self.task_root, path_file=self.path_file)

    def save_notebook(self, rel, cell_id, **cell):
        (self.task_root / rel).write_text(notebook(cell_id, **cell))

    def mark(self, assessment, passed):
        (self.results_dir / f"{assessment}.json").write_text(json.dumps({"assessment": assessment, "passed": passed}))

    def test_links_are_relative_to_the_current_folder(self):
        html = course_ui.path_html(here="part1", path_file=self.path_file)
        self.assertIn('href="00-onboarding.ipynb"', html)
        self.assertIn('href="assessment.ipynb"', html)
        html = course_ui.path_html(here=".", path_file=self.path_file)
        self.assertIn('href="part1/00-onboarding.ipynb"', html)
        self.assertIn("Part 1 &middot; 40 min", html)
        self.assertIn('<span class="muted">60 min</span>', html)     # lunch has no part: nothing dangles

    def test_fresh_lab_points_at_the_first_step(self):
        status = self.status()
        self.assertEqual(status["assessments"], {"part1": None})
        self.assertEqual(status["done"], set())
        self.assertEqual(status["current"], "onboarding")
        html = course_ui.path_html(here=".", status=status, path_file=self.path_file)
        self.assertIn('class="now"', html)
        self.assertNotIn("dot done", html)

    def test_a_milestone_that_ran_in_the_saved_notebook_moves_the_learner_forward(self):
        self.save_notebook("part1/02.ipynb", "ex-check", **RAN_OK)
        status = self.status()
        self.assertEqual(status["done"], {"onboarding", "nb2"})     # onboarding is behind nb2
        self.assertEqual(status["current"], "part1")

    def test_a_milestone_that_failed_or_never_ran_does_not(self):
        self.save_notebook("part1/02.ipynb", "ex-check", **RAN_ERROR)
        self.assertEqual(self.status()["current"], "onboarding")
        self.save_notebook("part1/02.ipynb", "ex-check")              # shipped state: no execution count
        self.assertEqual(self.status()["current"], "onboarding")
        self.save_notebook("part1/02.ipynb", "some-other-cell", **RAN_OK)
        self.assertEqual(self.status()["current"], "onboarding")

    def test_a_notebook_is_read_again_after_it_is_saved(self):
        self.save_notebook("part1/02.ipynb", "ex-check")
        self.assertEqual(self.status()["current"], "onboarding")
        self.save_notebook("part1/02.ipynb", "ex-check", **RAN_OK)
        self.assertEqual(self.status()["current"], "part1")

    def test_a_half_written_notebook_gives_no_signal_and_no_error(self):
        (self.task_root / "part1" / "02.ipynb").write_text('{"cells": [')
        self.assertEqual(self.status()["current"], "onboarding")

    def test_an_assessment_moves_only_with_its_marker(self):
        self.save_notebook("part1/assessment.ipynb", "submit", **RAN_OK)   # running Submit is not passing
        self.assertEqual(self.status()["current"], "onboarding")
        self.mark("part1", False)
        status = self.status()                               # submitted and failed: on the assessment, not ticked
        self.assertEqual(status["current"], "part1")
        self.assertEqual(status["done"], {"onboarding", "nb2"})
        self.mark("part1", True)
        status = self.status()
        self.assertEqual(status["assessments"], {"part1": True})
        self.assertEqual(status["current"], "agent")
        html = course_ui.path_html(here=".", status=status, path_file=self.path_file)
        self.assertEqual(html.count("&#10003;"), 3)         # onboarding, nb2, part1 ticked

    def test_evidence_reaches_the_last_step(self):
        (self.task_root / "work" / "agent").mkdir(parents=True)
        (self.task_root / "work" / "agent" / "collisions.json").write_text("[]")
        status = self.status()
        self.assertEqual(status["done"], {"onboarding", "nb2", "agent"})   # the assessment waits for its marker
        self.assertIsNone(status["current"])

    def test_a_course_docs_step_moves_only_with_its_evidence(self):
        self.path_file.write_text(json.dumps({"version": "test", "steps": with_docs_agent()}))
        self.mark("part1", True)
        status = self.status()
        self.assertEqual(status["current"], "agent")                      # no milestone cell can move it
        html = course_ui.path_html(here="part1", status=status, path_file=self.path_file)
        self.assertIn("<div><div>Coding agent</div>", html)               # a page in Course Docs: no link
        self.assertNotIn("agent.md", html)
        (self.task_root / "work" / "agent").mkdir(parents=True)
        (self.task_root / "work" / "agent" / "collisions.json").write_text("[]")   # not its evidence
        self.assertEqual(self.status()["current"], "agent")
        (self.task_root / "work" / "agent" / "live_source.json").write_text("{}")
        status = self.status()
        self.assertIsNone(status["current"])
        self.assertEqual(status["done"], {"onboarding", "nb2", "part1", "agent"})

    def test_mission_control_names_the_prep_state_and_the_next_step(self):
        prep = self.results_dir / "prep.json"
        prep.write_text(json.dumps({"state": "RUNNING", "detail": "waiting for embeddings"}))
        html = course_ui.mission_control_html(status=self.status(prep), path_file=self.path_file)
        self.assertIn("preparing clips", html)
        self.assertIn("waiting for embeddings", html)
        self.assertIn("Part 1 Assessment: not submitted", html)
        self.assertIn("Up next:</b> 0. Onboarding", html)
        self.assertIn("(Introduction, about 15 min)", html)
        self.assertIn("Part 1 Assessment is graded when you run its Submit cell. You can submit as often as you like.", html)
        self.assertNotIn("separate service", html)

    def test_a_corrupt_marker_raises_instead_of_hiding(self):
        (self.results_dir / "part1.json").write_text("not json")
        with self.assertRaises(ValueError):
            self.status()


class Overview(ThePath):
    """course_overview: the plain-data view of the path the course bar polls."""

    def test_numbers_only_the_counted_steps(self):
        o = course_ui.course_overview(status=self.status(), path_file=self.path_file)
        self.assertEqual(o["n"], 4)
        self.assertEqual([(s["id"], s["k"]) for s in o["steps"]],
                         [("intro", None), ("onboarding", 1), ("nb2", 2), ("break", None), ("part1", 3), ("agent", 4),
                          ("lunch", None), ("part2", None)])
        self.assertEqual(o["current"]["id"], "onboarding")
        self.assertTrue(o["steps"][1]["current"])
        self.assertEqual(o["done_count"], 0)
        self.assertEqual(o["assessments"], {"part1": None})
        self.assertEqual(o["steps"][6]["summary"], "Save your work, then press the Part 2 launch button.")
        self.assertEqual(o["version"], "test")
        json.dumps(o)                                              # it travels as JSON

    def test_states_follow_course_status(self):
        self.save_notebook("part1/02.ipynb", "ex-check", **RAN_OK)
        self.mark("part1", False)
        o = course_ui.course_overview(status=self.status(), path_file=self.path_file)
        by = {s["id"]: s for s in o["steps"]}
        self.assertTrue(by["nb2"]["done"])
        self.assertFalse(by["part1"]["done"])
        self.assertIs(by["part1"]["passed"], False)
        self.assertTrue(by["part1"]["current"])
        self.assertEqual(o["current"]["id"], "part1")
        self.assertEqual(o["done_count"], 2)
        self.assertIsNone(by["nb2"]["passed"])                      # only assessments carry a marker

    def test_home_card_when_every_step_is_reached(self):
        (self.task_root / "work" / "agent").mkdir(parents=True)
        (self.task_root / "work" / "agent" / "x.json").write_text("{}")      # the last step's evidence
        status = self.status()                               # never submitted: run Submit, not "submit again"
        self.assertIsNone(status["current"])
        html = course_ui.mission_control_html(status=status, path_file=self.path_file)
        self.assertIn("Part 1 Assessment is not passed yet.</b> Open it and run its Submit cell.", html)
        self.mark("part1", False)
        status = self.status()
        self.assertIsNone(status["current"])
        html = course_ui.mission_control_html(status=status, path_file=self.path_file)
        self.assertIn("Part 1 Assessment is not passed yet", html)
        self.assertNotIn("submit again", html)
        self.assertNotIn("Every step is done", html)
        self.mark("part1", True)
        html = course_ui.mission_control_html(status=self.status(), path_file=self.path_file)
        self.assertIn("Every step is done.", html)
        self.assertIn("Lunch is next. Save your work, then press the Part 2 launch button.", html)
        # ASSESS TASK comes before the lunch text that sends the learner to the next launch button.
        self.assertIn("Your assessment result is saved by the Submit cell.", html)
        self.assertLess(html.index("result"), html.index("Lunch is next"))

    def test_the_assessments_come_from_the_path(self):
        steps = [dict(s) for s in STEPS if s["id"] != "part1"]
        steps.insert(2, {"id": "quiz-a", "kind": "assessment", "minutes": 5, "title": "Quiz A",
                         "path": "part1/a.ipynb", "milestone_cell_id": "a"})
        steps.append({"id": "quiz-b", "kind": "assessment", "minutes": 5, "title": "Quiz B",
                      "path": "part1/b.ipynb", "milestone_cell_id": "b"})
        self.path_file.write_text(json.dumps({"steps": steps}))
        self.mark("quiz-a", True)
        o = course_ui.course_overview(status=self.status(), path_file=self.path_file)
        self.assertEqual(o["assessments"], {"quiz-a": True, "quiz-b": None})
        html = course_ui.mission_control_html(status=self.status(), path_file=self.path_file)
        self.assertIn("Quiz A: passed", html)
        self.assertIn("Quiz B: not submitted", html)
        self.assertIn("Quiz A and Quiz B are graded when you run their Submit cell", html)

    def test_a_path_without_assessments_shows_none(self):
        self.path_file.write_text(json.dumps({"steps": [s for s in STEPS if s["kind"] != "assessment"]}))
        status = self.status()
        self.assertEqual(status["assessments"], {})
        html = course_ui.mission_control_html(status=status, path_file=self.path_file)
        self.assertNotIn("graded when", html)
        (self.task_root / "work" / "agent").mkdir(parents=True)
        (self.task_root / "work" / "agent" / "x.json").write_text("{}")      # every step done
        html = course_ui.mission_control_html(status=self.status(), path_file=self.path_file)
        self.assertIn("Every step is done.", html)
        self.assertNotIn("result", html)                              # nothing to record on the platform


class ShippedPath(unittest.TestCase):
    """course_path.json against the names other files depend on (PLAN 8b) and the day's minutes."""

    def setUp(self):
        self.data = json.loads((PART1 / "course_path.json").read_text())
        self.by = {s["id"]: s for s in self.data["steps"]}

    def test_step_ids_in_order(self):
        self.assertEqual([s["id"] for s in self.data["steps"]],
                         ["intro", "onboarding", "nb1", "nb2", "break", "nb3", "nb4", "part1", "agent", "lunch", "part2"])

    def test_milestones_and_evidence(self):
        milestones = {s["id"]: s.get("milestone_cell_id") for s in self.data["steps"] if s.get("kind") in course_ui.STEP_KINDS}
        self.assertEqual(milestones, {"onboarding": "o0-gate", "nb1": "716b9cc4", "nb2": "03-ex3-check",
                                      "nb3": "04b-exercise-check", "nb4": "05-ex-perfile-check", "part1": "p1-submit",
                                      "agent": None})                  # a Course Docs page has no milestone cell
        # The file the coding agent writes once the live stream runs (the page's lab context names it).
        evidence = {s["id"]: s["evidence"] for s in self.data["steps"] if s.get("evidence")}
        self.assertEqual(evidence, {"agent": ["work/agent/live_source.json"]})

    def test_the_lunch_toast_has_a_notebook_to_fire_on(self):
        # lab_home.py's lunchNote fires when the last notebook before lunch is open (the bar matches open paths only),
        # and names the Course Docs step after it in its text.
        steps = self.data["steps"]
        lunch = next(i for i, s in enumerate(steps) if s["kind"] == "lunch")
        counted = [s for s in steps[:lunch] if s.get("kind") in course_ui.STEP_KINDS]
        self.assertEqual([s["id"] for s in counted if s.get("path")][-1], "part1")
        self.assertEqual(counted[-1]["id"], "agent")

    def test_one_assessment_with_its_marker_name(self):
        self.assertEqual([s["id"] for s in self.data["steps"] if s.get("kind") == "assessment"], ["part1"])
        self.assertEqual(self.by["part1"]["path"], "assessment.ipynb")
        self.assertIn("Submit cell shows PASSED", " ".join(self.by["part1"]["done_when"]))

    def test_minutes_follow_the_day(self):
        # VSS Coding Agent Skills grew from 25 to 40 minutes on 2026-11-04, and lunch went from 60 to 45 minutes, so
        # every Part 2 time stays and the day is still 480 minutes.
        minutes = {s["id"]: s["minutes"] for s in self.data["steps"]}
        self.assertEqual(minutes, {"intro": 15, "onboarding": 15, "nb1": 15, "nb2": 40, "break": 10, "nb3": 35, "nb4": 30,
                                   "part1": 25, "agent": 40, "lunch": 45, "part2": 210})
        self.assertEqual(sum(minutes.values()), 480)

    def test_paths_and_titles(self):
        expected = {
            "onboarding": ("00-onboarding.ipynb", "0. Course Onboarding"),
            "nb1": ("01-inspect-vss-deployment.ipynb", "1. Inspect VSS Deployment"),
            "nb2": ("02-rt-cv-detection-and-tracking.ipynb", "2. RT-CV: Detection & Tracking"),
            "nb3": ("03-rt-vlm-captioning-and-prompting.ipynb", "3. RT-VLM: Captioning & Prompting"),
            "nb4": ("04-rt-embed-video-search.ipynb", "4. RT-Embed: Video Search"),
            "part1": ("assessment.ipynb", "Part 1 Assessment"),
        }
        self.assertEqual({k: (self.by[k]["path"], self.by[k]["title"]) for k in expected}, expected)
        # The last step is the Course Docs page docs/course/vss-coding-agent-skills.md, not a notebook.
        agent = self.by["agent"]
        self.assertEqual((agent["title"], agent["course_docs"], agent.get("path"), agent.get("milestone_cell_id")),
                         ("VSS Coding Agent Skills", "course/vss-coding-agent-skills.md", None, None))

    def test_the_text_follows_the_house_rules(self):
        text = (PART1 / "course_path.json").read_text()
        # The retired model names are assembled so that the literals never appear in this file.
        for banned in (chr(0x2014), "Cosmos " + "Reason", "Cosmos 3 " + "Reasoner", "warehouse", "A1", "hand-off",
                       "review row", "verdict rule", "abstain", "the whole task", "certificate"):
            self.assertNotIn(banned, text)

    def test_every_step_says_what_to_look_for(self):
        # The card at the end of each notebook names the cell that moves the course bar and what it prints. A Course
        # Docs page has no card and no cell: its done_when names the result, and its evidence file moves the bar.
        for s in self.data["steps"]:
            if s.get("kind") in ("notebook", "terminal"):
                self.assertTrue(s.get("done_when"), s["id"])
                if s.get("course_docs"):
                    self.assertTrue(s.get("evidence"), s["id"])
                    self.assertNotIn("cell", " ".join(s["done_when"]), s["id"])
                else:
                    self.assertIn("prints", " ".join(s["done_when"]), s["id"])
        self.assertNotIn("needs_before", self.by["onboarding"])     # onboarding waits for the lab itself (0.3)




class HeadersAgree(unittest.TestCase):
    """Every counted step's notebook has a generated header, and no header prints a second step number.

    The headers are written by task1/scripts/course_path.py --apply; this fails until it has run over the
    current notebooks.
    """

    def test_every_step_has_a_generated_header_without_a_step_number(self):
        task = PART1
        nowhere = task / "nowhere"
        o = course_ui.course_overview(prep_file=nowhere, results_dir=nowhere, task_root=nowhere)
        self.assertEqual(o["n"], 7)
        self.assertEqual(o["current"]["id"], "onboarding")
        self.assertEqual(list(o["assessments"]), ["part1"])
        shipped = {s["id"]: s for s in json.loads((PART1 / "course_path.json").read_text())["steps"]}
        for s in o["steps"]:
            if not s["k"]:
                continue
            if s["path"] is None:                            # a Course Docs page has no notebook and no header
                self.assertTrue(shipped[s["id"]].get("course_docs"), s["id"])
                continue
            path = task / s["path"]
            self.assertTrue(path.is_file(), f"{s['path']} is missing")
            nb = json.loads(path.read_text())
            headers = [c for c in nb["cells"] if c.get("metadata", {}).get("course_path") == "header"]
            self.assertTrue(headers, f"{s['path']} has no generated header: run python3 -I task1/scripts/course_path.py --apply")
            header = "".join(headers[0]["source"])
            self.assertNotIn(f"Step {s['k']} of", header, s["id"])
            if s.get("block"):
                self.assertIn(f"**{s['block']}**", header, s["id"])


if __name__ == "__main__":
    unittest.main()
