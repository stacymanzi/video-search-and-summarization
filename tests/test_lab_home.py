#!/usr/bin/env python3
"""lab_home.py: the course bar's status server, and the three places that wire the bar in.

    python3 -I task1/tests/test_lab_home.py
"""
import ast
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # a CI runner's http_proxy must not catch 127.0.0.1

TASK1 = pathlib.Path(__file__).resolve().parents[1]
TASK = TASK1 / "lab" / "task"
sys.path.insert(0, str(TASK))

import lab_home  # noqa: E402


class CourseFolder(unittest.TestCase):
    """A throwaway lab: the shipped course_path.json, no evidence, no markers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.task_root = root / "task"
        (self.task_root / "part1").mkdir(parents=True)
        (self.task_root / "data").mkdir()
        shutil.copy(TASK / "part1" / "course_path.json", self.task_root / "part1" / "course_path.json")
        self.results = root / "results"
        self.results.mkdir()
        self.saved = {k: os.environ.get(k) for k in ("LAB_HOME_TASK_ROOT", "LAB_HOME_RESULTS_DIR")}
        os.environ["LAB_HOME_TASK_ROOT"] = str(self.task_root)
        os.environ["LAB_HOME_RESULTS_DIR"] = str(self.results)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


class Payload(CourseFolder):
    def test_a_fresh_lab_is_step_one_of_seven(self):
        c = lab_home.course_payload()
        self.assertNotIn("error", c)
        self.assertEqual(c["n"], 7)
        self.assertEqual(c["done_count"], 0)
        self.assertEqual((c["current"]["id"], c["current"]["k"]), ("onboarding", 1))
        self.assertEqual(c["assessments"], {"part1": None})             # the steps whose kind is assessment
        self.assertIsNone(c["prep_state"])
        json.dumps(c)                                   # what the server sends

    def test_markers_move_the_bar(self):
        (self.results / "part1.json").write_text(json.dumps({"assessment": "part1", "passed": True}))
        (self.task_root / "data" / "course_prep_status.json").write_text(json.dumps({"state": "OK"}))
        c = lab_home.course_payload()
        self.assertEqual(c["prep_state"], "OK")
        self.assertIs(c["assessments"]["part1"], True)
        self.assertEqual(c["current"]["id"], "agent")   # the step after the assessment
        self.assertEqual(c["done_count"], 6)
        part1 = next(s for s in c["steps"] if s["id"] == "part1")
        self.assertEqual((part1["k"], part1["done"], part1["passed"]), (6, True, True))

    def test_an_unpassed_assessment_stays_current(self):
        (self.results / "part1.json").write_text(json.dumps({"assessment": "part1", "passed": False}))
        c = lab_home.course_payload()
        self.assertEqual(c["current"]["id"], "part1")
        self.assertIs(c["assessments"]["part1"], False)
        self.assertEqual(c["done_count"], 5)

    def test_a_check_that_ran_in_the_saved_notebook_moves_the_bar(self):
        nb = {"cells": [{"cell_type": "code", "id": "03-ex3-check", "execution_count": 7, "metadata": {},
                         "outputs": [{"output_type": "stream", "name": "stdout", "text": ["track 12\n"]}], "source": []}],
              "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        (self.task_root / "part1" / "02-rt-cv-detection-and-tracking.ipynb").write_text(json.dumps(nb))
        c = lab_home.course_payload()
        self.assertEqual((c["current"]["id"], c["current"]["k"]), ("nb3", 4))
        self.assertEqual(c["done_count"], 3)            # onboarding, nb1, nb2

    def test_the_coding_agent_file_reaches_the_end_of_the_launch(self):
        # VSS Coding Agent Skills is a Course Docs page: no notebook, no milestone cell. The file the coding agent
        # writes when the live stream is set up is its evidence; the retired notebook's collisions.json is not.
        (self.task_root / "work" / "agent").mkdir(parents=True)
        (self.task_root / "work" / "agent" / "collisions.json").write_text("[]")
        self.assertEqual(lab_home.course_payload()["current"]["id"], "onboarding")
        agent = next(s for s in lab_home.course_payload()["steps"] if s["id"] == "agent")
        self.assertEqual((agent["k"], agent["path"]), (7, None))       # counted, but nothing to link to
        (self.task_root / "work" / "agent" / "live_source.json").write_text("{}")
        c = lab_home.course_payload()
        self.assertIsNone(c["current"])
        self.assertEqual(c["done_count"], 6)            # every step but the assessment, which waits for its marker
        (self.results / "part1.json").write_text(json.dumps({"assessment": "part1", "passed": True}))
        c = lab_home.course_payload()
        self.assertEqual(c["done_count"], 7)
        lunch = next(s for s in c["steps"] if s["kind"] == "lunch")
        self.assertIn("STOP TASK for Part 1 and START for Part 2", lunch["summary"])   # the bar's text once every step is done

    def test_a_broken_path_file_is_reported_not_fatal(self):
        (self.task_root / "part1" / "course_path.json").write_text("{not json")
        c = lab_home.course_payload()
        self.assertIn("JSONDecodeError", c["error"])


class Server(CourseFolder):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), lab_home.make_handler(lab_home.Monitor(10)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path):
        with OPENER.open(self.url + path, timeout=5) as r:
            return r.status, r.headers, r.read()

    def test_status_carries_the_course(self):
        status, headers, body = self.get("/status")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        d = json.loads(body)
        self.assertEqual(d["app"], "lab_home")
        self.assertIsNone(d["checked_at"])             # no check has run yet
        self.assertEqual(d["course"]["n"], 7)
        self.assertEqual(d["course"]["current"]["id"], "onboarding")

    def test_bar_js_is_served_as_javascript(self):
        for path in ("/bar.js", "/lab/proxy/8050/bar.js"):   # bare, and as the proxy may hand it over
            status, headers, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "application/javascript; charset=utf-8")
            self.assertEqual(headers["Cache-Control"], "no-store")
        js = body.decode()
        self.assertNotIn("__PORT__", js)
        self.assertNotIn("__COURSE__", js)
        self.assertIn(f"const COURSE = {json.dumps(lab_home.COURSE_TITLE)};", js)
        self.assertEqual(lab_home.COURSE_TITLE, "Build and Evaluate a Visual AI Agent")
        self.assertIn('"proxy/8050/"', js)
        self.assertIn("window.__courseBar", js)
        self.assertIn('id = "course-bar"', js)
        self.assertIn('toastEl.id = "course-bar-toast"', js)            # the toast is not inside the hideable bar
        self.assertIn('course.prep_state === "OK"', js)                   # the ready chime needs the clips too
        self.assertIn('s.kind === "assessment"', js)                      # the chips come from the path, not a fixed list
        self.assertIn('s.kind === "lunch"', js)                           # the reminder to save before lunch
        # Before the Part 2 launch button (the all-done text, the lunch toast): click ASSESS TASK first.
        self.assertIn("click ASSESS TASK on the course page if you have not done so yet", js)
        self.assertIn("If you have not done so yet, click ASSESS TASK on the course page.", js)
        self.assertIn("and run its Submit cell.", js)                     # also right when nothing was submitted yet
        self.assertIn("(Ctrl+S, or Cmd+S on a Mac)", js)
        for old in ('"a1"', "A1", "certificate", "Every step has its evidence", "and submit again", "check named at the end"):
            self.assertNotIn(old, js)
        self.assertNotIn(chr(0x2014), js)                 # no em dashes anywhere in the course

    def test_everything_else_is_the_details_page(self):
        status, headers, body = self.get("/")
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"<title>Lab status</title>", body)


class Wiring(unittest.TestCase):
    def test_nginx_injects_the_script_and_mirrors_the_server_headers(self):
        conf = (TASK1 / "nginx.conf").read_text()
        lab = re.search(r"\n\t\tlocation /lab \{(.*?)\n\t\t\}", conf, re.S).group(1)
        self.assertIn("sub_filter '</head>' '<script defer src=\"/lab/proxy/8050/bar.js\"></script></head>';", lab)
        self.assertIn("sub_filter_once on;", lab)
        self.assertIn('proxy_set_header Accept-Encoding "";', lab)        # sub_filter needs a plain body
        # A location with its own proxy_set_header inherits none from the server block, so /lab must
        # carry every server-level header itself (the kernel websockets need Upgrade and Connection).
        server_level = conf[: conf.index("\t\tlocation = /")]
        mirrored = set(re.findall(r"^\t\tproxy_set_header (\S+) (.*);$", server_level, re.M))
        self.assertEqual({h for h, _ in mirrored}, {"Upgrade", "Connection", "Host", "X-Forwarded-For"})
        for header, value in mirrored:
            self.assertIn(f"proxy_set_header {header} {value};", lab)
        self.assertNotIn("sub_filter", conf.replace(lab, ""))               # only /lab is filtered

    def test_entrypoint_starts_the_server_before_jupyter(self):
        sh = (TASK1 / "lab" / "entrypoint.sh").read_text()
        self.assertLess(sh.index("lab_home.py --serve --port 8050"), sh.index("exec jupyter lab"))
        self.assertIn("/var/log/lab_home.log", sh)
        loop = sh[sh.index("( set +e"): sh.index("done )")]                # the loop inherits set -e; it must switch it off
        self.assertIn("while true; do", loop)
        self.assertIn("lab_home.py --serve --port 8050", loop)
        self.assertIn("WARNING: course bar status server did not answer", sh)   # the ready line is conditional
        self.assertIn('if [ "${lab_home_up}" = 1 ]; then', sh)
        self.assertNotIn('"$JUPYTER_TOKEN"', sh)                          # unset on a devbox: must not abort under set -u
        self.assertIn('if [ -n "${JUPYTER_TOKEN:-}" ]; then', sh)

    def test_the_home_cell_shows_the_bar_and_the_card(self):
        nb = json.loads((TASK / "00_Home.ipynb").read_text())
        first = "".join(next(c for c in nb["cells"] if c["cell_type"] == "code")["source"])
        self.assertIn("course_ui.mission_control()", first)
        calls = [l for l in first.splitlines() if l.startswith("show_course_bar(")]
        self.assertEqual(calls, ["show_course_bar()"])                   # no demo clock in the shipped cell: its warnings would fire for real learners

    def test_the_bar_and_the_home_card_share_one_source(self):
        self.assertIn("course_ui.course_overview(", (TASK / "lab_home.py").read_text())

    def test_the_lab_pill_waits_for_what_the_readiness_check_requires(self):
        # An extra check here (Kibana, NVStreamer, the VIOS UI) would keep the pill amber on a lab that the
        # readiness check in onboarding (o0-gate) calls ready, and the "Lab ready" toast would never come.
        nb = json.loads((TASK / "part1" / "00-onboarding.ipynb").read_text())
        gate = "".join(next(c for c in nb["cells"] if c["id"] == "o0-gate")["source"])
        values = {}
        for node in ast.parse(gate).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in ("SERVICES", "OPTIONAL", "CONTAINERS"):
                    values[node.targets[0].id] = ast.literal_eval(node.value)
        required = {name: path for name, path in values["SERVICES"].items() if name not in values["OPTIONAL"]}
        self.assertEqual({c["name"]: c["path"] for c in lab_home.LAB_CHECKS["http_checks"]}, required)
        self.assertEqual(sorted(lab_home.LAB_CHECKS["containers"]), sorted(values["CONTAINERS"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
