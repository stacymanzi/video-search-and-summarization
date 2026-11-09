"""serve.py: the grader's HTTP front end inside the ``assessment`` container.

``POST /grade/part1``  runs the Part 1 grader, writes
``/dli/assessment_results/part1.json`` and returns the marker as JSON.
``GET /health``  reports whether the grader can grade (rubric loaded, answer
key and ground truth present, results directory writable) and whether
course-prep's registry of the search clips exists yet.

Listens on 8081 (``GRADER_PORT``). The lab reaches it as
``http://assessment:8081`` over the compose network; the LMS never calls this
port, it calls the Go runner on 8080, which executes ``tasktest.py``.

One grading run at a time: a second POST while one is in progress gets 409.
A grader that crashes still produces a marker, with every check failed and the
exception in the detail (fail closed), and the traceback goes to stderr.

``python3 -I serve.py --grade part1`` grades once from the production paths and
prints the summary, for the instructor's terminal.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    # The image's PYTHONPATH names /dli/task/part1, which the lab can write, and a json.py planted there would
    # run inside the grader. Keep only the interpreter's own directories; the grader package is added below.
    # Paths are compared after resolving symbolic links, as the interpreter's prefixes often are links.
    _OWN = {os.path.realpath(d) for d in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)}
    sys.path[:] = [p for p in sys.path if p and any(os.path.realpath(p) == d or os.path.realpath(p).startswith(d + os.sep)
                                                    for d in _OWN)]

import argparse  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import traceback  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable, Dict, Optional, Tuple  # noqa: E402

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grader import common as c  # noqa: E402
from grader import grade_part1  # noqa: E402

DEFAULT_PORT = 8081
GRADERS: Dict[str, Callable[[c.Context], dict]] = {"part1": grade_part1.grade}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} grader: {message}", file=sys.stderr, flush=True)


def crash_marker(ctx: c.Context, assessment: str, exc: BaseException) -> dict:
    detail = f"The grader crashed ({type(exc).__name__}: {exc}). Tell the instructor."
    results = [c.check(spec["id"], False, detail) for spec in ctx.rubric[assessment]["checks"]]
    return c.build_marker(ctx, assessment, results)


def grade_and_record(ctx: c.Context, assessment: str) -> dict:
    """Run one grader, always leave a marker behind, return it."""
    try:
        marker = GRADERS[assessment](ctx)
    except Exception as exc:  # the marker must say why, and the next request must still work
        traceback.print_exc(file=sys.stderr)
        marker = crash_marker(ctx, assessment, exc)
    c.write_marker(ctx, marker)
    log(f"{assessment} graded: {'PASS' if marker['passed'] else 'FAIL'} "
        f"({sum(1 for k in marker['checks'] if k['passed'])}/{len(marker['checks'])} checks)")
    return marker


def health(ctx: c.Context) -> Tuple[int, dict]:
    assessments = c.assessment_ids(ctx.rubric)
    report = {
        "status": "ok",
        "rubric_version": ctx.rubric.get("version"),
        "graders": sorted(GRADERS),
        "answers_file": str(ctx.answers_file),
        "answers_present": ctx.answers_file.is_file(),
        "results_dir": str(ctx.results_dir),
        "ground_truth_present": ctx.ground_truth_file.is_file(),
        "answer_keys_present": {a: (ctx.answer_keys_dir / f"{a}.json").is_file() for a in assessments},
        # course-prep writes the registry after launch, so a missing one is reported but does not degrade health.
        "registry_file": str(ctx.registry_file),
        "registry_present": ctx.registry_file.is_file(),
        "markers": {a: c.marker_path(ctx, a).is_file() for a in assessments},
    }
    problems = []  # a broken image (a key or label file it ships is missing) or no place to write markers
    if not report["ground_truth_present"]:
        problems.append(f"ground truth missing: {ctx.ground_truth_file}")
    problems += [f"answer key missing: {ctx.answer_keys_dir / (a + '.json')}"
                 for a, present in report["answer_keys_present"].items() if not present]
    try:
        ctx.results_dir.mkdir(parents=True, exist_ok=True)
        probe = ctx.results_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["results_dir_writable"] = True
    except OSError as exc:
        report["results_dir_writable"] = False
        problems.append(f"results dir not writable: {exc}")
    if problems:
        report["status"] = "degraded"
        report["error"] = "; ".join(problems)
    return (200 if report["status"] == "ok" else 503), report


class GraderHandler(BaseHTTPRequestHandler):
    ctx: c.Context
    lock: threading.Lock
    server_version = "course1-grader/2.0"

    def log_message(self, fmt, *args):  # route the default access log through ours
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _routes(self) -> str:
        return " or ".join(f"POST /grade/{a}" for a in sorted(GRADERS))

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path.rstrip("/") == "/health":
            status, report = health(self.ctx)
            self._send(status, report)
            return
        self._send(404, {"error": f"unknown path {self.path}; use GET /health or {self._routes()}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if not path.startswith("/grade/"):
            self._send(404, {"error": f"unknown path {self.path}; use {self._routes()}"})
            return
        assessment = path[len("/grade/"):]
        if assessment not in GRADERS:
            self._send(404, {"error": f"unknown assessment {assessment!r}; choose one of {sorted(GRADERS)}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # the body is ignored; the answers file on disk is the submission
        if not self.lock.acquire(blocking=False):
            self._send(409, {"error": "a grading run is already in progress; wait for it to finish and submit again"})
            return
        try:
            marker = grade_and_record(self.ctx, assessment)
        finally:
            self.lock.release()
        self._send(200, marker)


def make_server(ctx: c.Context, port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type("BoundGraderHandler", (GraderHandler,), {"ctx": ctx, "lock": threading.Lock()})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="course grader front end")
    parser.add_argument("--port", type=int, default=None, help=f"listen port (default GRADER_PORT or {DEFAULT_PORT})")
    parser.add_argument("--grade", choices=sorted(GRADERS), help="grade one assessment now, print the summary, exit")
    args = parser.parse_args(argv)
    ctx = c.default_context()
    if args.grade:
        marker = grade_and_record(ctx, args.grade)
        print(c.summarize(marker))
        return 0 if marker["passed"] else 1
    port = args.port or int(os.environ.get("GRADER_PORT", DEFAULT_PORT))
    server = make_server(ctx, port)
    log(f"listening on :{port}; answers from {ctx.answers_file}, markers to {ctx.results_dir}, "
        f"rubric {ctx.rubric.get('version')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
