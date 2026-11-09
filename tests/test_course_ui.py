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
PART1 = TASK1 / "lab" / "task" / "part1"
sys.path.insert(0, str(PART1))

import course_ui  # noqa: E402

_spec = importlib.util.spec_from_file_location("course_path", TASK1 / "scripts" / "course_path.py")
course_path = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(course_path)

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
     "done_when": ["the Submit cell shows PASSED and you have clicked ASSESS TASK on the course page"],
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
        self.assertIn("ASSESS TASK", html)
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
        self.assertIn("If you have not done so yet, click ASSESS TASK on the course page.", html)
        self.assertLess(html.index("ASSESS TASK"), html.index("Lunch is next"))

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
        self.assertNotIn("ASSESS TASK", html)                              # nothing to record on the platform


class ShippedPath(unittest.TestCase):
    """part1/course_path.json against the names other files depend on (PLAN 8b) and the day's minutes."""

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
        self.assertEqual(self.by["part1"]["path"], "part1/assessment.ipynb")
        self.assertIn("ASSESS TASK", " ".join(self.by["part1"]["done_when"]))

    def test_minutes_follow_the_day(self):
        # VSS Coding Agent Skills grew from 25 to 40 minutes on 2026-11-04, and lunch went from 60 to 45 minutes, so
        # every Part 2 time stays and the day is still 480 minutes.
        minutes = {s["id"]: s["minutes"] for s in self.data["steps"]}
        self.assertEqual(minutes, {"intro": 15, "onboarding": 15, "nb1": 15, "nb2": 40, "break": 10, "nb3": 35, "nb4": 30,
                                   "part1": 25, "agent": 40, "lunch": 45, "part2": 210})
        self.assertEqual(sum(minutes.values()), 480)

    def test_paths_and_titles(self):
        expected = {
            "onboarding": ("part1/00-onboarding.ipynb", "0. Course Onboarding"),
            "nb1": ("part1/01-inspect-vss-deployment.ipynb", "1. Inspect VSS Deployment"),
            "nb2": ("part1/02-rt-cv-detection-and-tracking.ipynb", "2. RT-CV: Detection & Tracking"),
            "nb3": ("part1/03-rt-vlm-captioning-and-prompting.ipynb", "3. RT-VLM: Captioning & Prompting"),
            "nb4": ("part1/04-rt-embed-video-search.ipynb", "4. RT-Embed: Video Search"),
            "part1": ("part1/assessment.ipynb", "Part 1 Assessment"),
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
        self.assertEqual(course_path.path_problems(course_path.load_path()), [])

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


class Generator(unittest.TestCase):
    """The cells task1/scripts/course_path.py writes into the notebooks, from a small path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path_file = pathlib.Path(self.tmp.name) / "course_path.json"
        path_file.write_text(json.dumps({"steps": STEPS}))
        self.data = course_path.load_path(path_file)
        self.by = {s["id"]: s for s in self.data["steps"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_header_places_the_notebook_without_repeating_its_title_cell(self):
        text = course_path.header_text(self.by["nb2"], self.data)
        self.assertIn("**Part 1** &middot; about 40 min", text)
        self.assertNotIn("Step 2 of 4", text)          # the title cell carries the notebook's own number
        self.assertIn("Previous: [0. Onboarding](00-onboarding.ipynb)", text)
        self.assertIn("Next: [Part 1 Assessment](assessment.ipynb)", text)
        self.assertNotIn("Detect things.", text)

    def test_the_card_before_an_assessment_says_what_it_grades(self):
        text = course_path.handoff_text(self.by["onboarding"], self.data)
        self.assertIn("**Next: [2. Detection](02.ipynb)**", text)
        self.assertIn("After that: **[Part 1 Assessment](assessment.ipynb)** (about 25 min). It checks the building blocks.", text)
        text = course_path.handoff_text(self.by["nb2"], self.data)
        self.assertIn("**Then:** Break, 10 min.", text)

    def test_the_last_card_lists_the_pauses_and_names_the_assessments(self):
        text = course_path.handoff_text(self.by["agent"], self.data)
        self.assertIn("> - Lunch, 60 min. Save your work, then press the Part 2 launch button.", text)
        self.assertIn("> - Part 2, 210 min. On its own launch.", text)
        self.assertIn("**This is the last step on this launch.** Before you leave, click ASSESS TASK on the course page "
                      "if you have not done so yet. The Submit cell in [Part 1 Assessment](assessment.ipynb) must show "
                      "PASSED first.", text)
        # Read from the top, the card says to click ASSESS TASK before it says to press the Part 2 launch button.
        self.assertLess(text.index("ASSESS TASK"), text.index("Lunch, 60 min"))
        for old in ("A1", "A2", "A3", "certificate"):
            self.assertNotIn(old, text)

    def test_the_last_card_names_every_assessment(self):
        steps = [dict(s) for s in STEPS]
        steps.insert(3, {"id": "quiz", "kind": "assessment", "minutes": 5, "title": "Quiz", "path": "part1/quiz.ipynb",
                         "milestone_cell_id": "q"})
        path_file = pathlib.Path(self.tmp.name) / "two_assessments.json"
        path_file.write_text(json.dumps({"steps": steps}))
        data = course_path.load_path(path_file)
        text = course_path.handoff_text(next(s for s in data["steps"] if s["id"] == "agent"), data)
        self.assertIn("The Submit cells in [Quiz](quiz.ipynb) and [Part 1 Assessment](assessment.ipynb) must show "
                      "PASSED first.", text)

    def test_a_course_docs_step_says_where_to_open_it(self):
        path_file = pathlib.Path(self.tmp.name) / "docs_step.json"
        path_file.write_text(json.dumps({"steps": with_docs_agent()}))
        data = course_path.load_path(path_file)
        by = {s["id"]: s for s in data["steps"]}
        named = "Coding agent (open Course Docs from the JupyterLab Launcher)"
        self.assertIn(f"Previous: [2. Detection](02.ipynb) &middot; Next: {named}\n", course_path.header_text(by["part1"], data))
        card = course_path.handoff_text(by["part1"], data)
        self.assertIn(f"> **Next: {named}** &middot; Part 1 &middot; about 40 min  \n> Ask the coding agent.", card)
        self.assertNotIn("agent.md", card)                   # never a link to the page source or into JupyterLab
        self.assertIn(f"| {named} | Part 1 | 40 | Ask the coding agent. |", course_path.home_map_text(data))
        self.assertEqual(course_path.path_problems(data), [])  # a page needs no notebook and no milestone cell

    def test_a_course_docs_page_must_exist_with_the_step_title(self):
        docs = pathlib.Path(self.tmp.name) / "docs"
        data = {"steps": with_docs_agent()}
        self.assertEqual(course_path.course_docs_problems(data, docs),
                         ["docs/course/agent.md: the Course Docs page of step 'agent' is missing"])
        (docs / "course").mkdir(parents=True)
        page = docs / "course" / "agent.md"
        page.write_text("(agent)=\n\n```text\n# a prompt line, not a heading\n```\n\n# Something Else\n")
        self.assertEqual(course_path.course_docs_problems(data, docs),
                         ["docs/course/agent.md: its H1 is 'Something Else', but step 'agent' calls it 'Coding agent'"])
        page.write_text("# Coding agent\n\nText.\n")
        self.assertEqual(course_path.course_docs_problems(data, docs), [])

    def test_the_home_map_adds_up_the_parts_of_the_day(self):
        text = course_path.home_map_text(self.data)
        self.assertIn("| Step | Part | Min | What you do |", text)
        self.assertIn("| [Part 1 Assessment](part1/assessment.ipynb) | Part 1 | 25 | On your own. |", text)
        self.assertIn("The day: Introduction 30 min, Part 1 100 min, Lunch 60 min, Part 2 210 min (400 min in all).", text)

    def test_stale_generated_cells_and_milestone_tags_are_removed(self):
        cells = [
            {"cell_type": "markdown", "id": "title", "metadata": {}, "source": ["# 2. Detection\n"]},
            {"cell_type": "markdown", "id": "path-header-b3-03", "metadata": {"course_path": "header"}, "source": ["old\n"]},
            {"cell_type": "markdown", "id": "path-header-nb2", "metadata": {"course_path": "header"}, "source": ["mine\n"]},
            {"cell_type": "code", "id": "03-review-code", "metadata": {"tags": ["milestone"], "course_path_milestone": "b3-03"}},
            {"cell_type": "code", "id": "keep-tags", "metadata": {"tags": ["milestone", "instructor-only"]}},
            {"cell_type": "code", "id": "ex-check", "metadata": {"tags": ["milestone"], "course_path_milestone": "nb2"}},
            {"cell_type": "markdown", "id": "path-handoff-b3-03", "metadata": {"course_path": "handoff"}, "source": ["old\n"]},
        ]
        course_path.drop_stale(cells, self.by["nb2"])
        self.assertEqual([c["id"] for c in cells], ["title", "path-header-nb2", "03-review-code", "keep-tags", "ex-check"])
        self.assertEqual(cells[2]["metadata"], {})
        self.assertEqual(cells[3]["metadata"], {"tags": ["instructor-only"]})
        self.assertEqual(cells[4]["metadata"], {"tags": ["milestone"], "course_path_milestone": "nb2"})

    def test_the_json_is_checked(self):
        steps = [dict(s) for s in STEPS] + [{"id": "nb2", "kind": "notebook", "title": "again"},
                                           {"id": "x", "kind": "lecture", "title": "x"}]
        problems = course_path.path_problems({"steps": steps})
        self.assertTrue(any("used twice" in p for p in problems), problems)
        self.assertTrue(any("unknown kind" in p for p in problems), problems)
        self.assertTrue(any("no path or no milestone_cell_id" in p for p in problems), problems)
        both = dict(DOCS_AGENT, path="part1/agent.ipynb")              # a page and a notebook at once
        problems = course_path.path_problems({"steps": [both]})
        self.assertTrue(any("names a Course Docs page and also a notebook path" in p for p in problems), problems)


class HeadersAgree(unittest.TestCase):
    """Every counted step's notebook has a generated header, and no header prints a second step number.

    The headers are written by task1/scripts/course_path.py --apply; this fails until it has run over the
    current notebooks.
    """

    def test_every_step_has_a_generated_header_without_a_step_number(self):
        task = PART1.parent
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
