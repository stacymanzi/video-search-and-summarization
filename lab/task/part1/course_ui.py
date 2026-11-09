"""course_ui.py: the course's visual layer inside JupyterLab.

Plain HTML strings for what a learner looks at during the day: the path
through the course with where they are, the grader's result after a Submit
cell, a timeline bar and short notes. Nothing here needs a widget, a
JupyterLab extension or a kernel round trip after the cell has run, so every
card survives Run All and a browser reload; lab_home.save_htmls carries
``text/html`` outputs into the take-home copy.

Two layers:

- ``*_html(...)`` functions return a string. They import nothing from
  IPython and are what the tests exercise.
- The functions without the suffix wrap the same string in
  ``IPython.display.HTML`` so a notebook can end a cell with them or pass them
  to ``display()``.

Colors come from JupyterLab's own CSS variables where they exist, so the
cards follow the light and dark themes; the one fixed color is the accent.
Layout-critical rules (the timeline bar) are also inline, so a notebook that
JupyterLab does not trust (its sanitizer drops ``<style>``) still shows the
bar; the rest degrades to plain tables and text.

Progress comes from ``course_path.json`` beside this file, the same file
task1/scripts/course_path.py projects into the notebooks. ``course_status``
and ``course_overview`` read it; the course bar (lab_home.py) polls
``course_overview`` and the Home card renders ``mission_control``. The
assessments are the steps whose kind is ``assessment``; each one's grader
marker is ``/dli/assessment_results/<step id>.json``.
"""
from __future__ import annotations

import html as _html
import json
from pathlib import Path

ACCENT = "#76b900"
PATH_FILE = Path(__file__).with_name("course_path.json")
TASK_ROOT = Path("/dli/task")
PREP_STATUS_FILE = TASK_ROOT / "data" / "course_prep_status.json"
RESULTS_DIR = Path("/dli/assessment_results")
STEP_KINDS = ("notebook", "terminal", "assessment")

_KIND_COLORS = {
    "pass": "rgba(118, 185, 0, 0.85)",
    "fail": "rgba(226, 75, 74, 0.8)",
    "warn": "rgba(239, 159, 39, 0.85)",
    "pick": "rgba(55, 138, 221, 0.9)",
    "candidate": "rgba(128, 128, 128, 0.45)",
    "event": "rgba(55, 138, 221, 0.35)",
}

_CSS_TEMPLATE = """
<style>
.cui{font-family:var(--jp-ui-font-family,system-ui,sans-serif);font-size:13px;line-height:1.5;color:var(--jp-ui-font-color1,#222);max-width:960px}
.cui .card{border:1px solid var(--jp-border-color2,#d8d8d8);border-radius:10px;padding:14px 16px;margin:6px 0 10px;background:var(--jp-layout-color1,#fff)}
.cui .head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:8px}
.cui .title{font-size:15px;font-weight:600}
.cui .muted{color:var(--jp-ui-font-color2,#666)}
.cui .chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600;line-height:1.6;white-space:nowrap}
.cui .chip.pass,.cui .chip.ok{background:rgba(118,185,0,.30)}
.cui .chip.fail{background:rgba(226,75,74,.30)}
.cui .chip.warn,.cui .chip.running{background:rgba(239,159,39,.34)}
.cui .chip.info{background:rgba(55,138,221,.30)}
.cui .chip.neutral{background:rgba(128,128,128,.18)}
.cui table{border-collapse:collapse;width:100%;font-size:13px}
.cui th{text-align:left;font-weight:600;color:var(--jp-ui-font-color2,#666);padding:4px 8px;border-bottom:1px solid var(--jp-border-color2,#d8d8d8)}
.cui td{padding:5px 8px;border-bottom:1px solid var(--jp-border-color3,#eee);vertical-align:top}
.cui .ticks{display:flex;justify-content:space-between;font-size:11px;color:var(--jp-ui-font-color2,#666);margin-top:2px}
.cui .banner{border-left:4px solid __ACCENT__;padding:8px 12px;border-radius:4px;background:rgba(118,185,0,.08);margin:6px 0 10px}
.cui .banner.fail{border-left-color:#e24b4a;background:rgba(226,75,74,.08)}
.cui .steps{list-style:none;margin:0;padding:0}
.cui .steps li{display:grid;grid-template-columns:28px 1fr max-content;gap:10px;align-items:start;padding:7px 0;border-bottom:1px solid var(--jp-border-color3,#eee)}
.cui .steps li.now{background:rgba(118,185,0,.08);border-radius:6px;padding-left:6px;margin-left:-6px}
.cui .dot{width:18px;height:18px;border-radius:50%;border:2px solid var(--jp-border-color1,#bbb);display:inline-block;margin-top:2px;text-align:center;font-size:11px;line-height:14px}
.cui .dot.done{background:__ACCENT__;border-color:__ACCENT__;color:#fff}
.cui .dot.now{border-color:__ACCENT__}
.cui .dot.pause{border-style:dashed}
.cui a{color:inherit;text-decoration:underline;text-decoration-color:__ACCENT__;text-underline-offset:2px}
</style>
"""
_CSS = _CSS_TEMPLATE.replace("__ACCENT__", ACCENT)


# --------------------------------------------------------------------------- helpers

def _esc(value) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def _seconds(value) -> str:
    """46.0 -> '46.0 s'; 125.0 -> '2:05 (125.0 s)'."""
    s = float(value)
    if s < 60:
        return f"{s:.1f} s"
    return f"{int(s // 60)}:{int(s % 60):02d} ({s:.1f} s)"


def _pct(value, total) -> float:
    if not total or total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * float(value) / float(total)))


def _join(items) -> str:
    """'a', 'a and b', 'a, b and c'."""
    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _wrap(body: str) -> str:
    return _CSS + '<div class="cui">' + body + "</div>"


def _display(markup: str):
    from IPython.display import HTML
    return HTML(markup)


# --------------------------------------------------------------------------- timeline

def timeline_html(duration_s, windows, *, small=False, ticks=True) -> str:
    """A bar for one clip with windows drawn on it.

    ``windows`` is a list of dicts with ``start_s``, ``end_s`` and an optional
    ``kind`` (pass, fail, warn, pick, candidate, event) and ``label`` (shown
    as the hover title). Positions are inline styles, so the bar survives
    JupyterLab's sanitizer in an untrusted notebook.
    """
    parts = []
    for w in windows or []:
        left = _pct(w.get("start_s", 0), duration_s)
        right = _pct(w.get("end_s", 0), duration_s)
        width = max(0.6, right - left)
        color = _KIND_COLORS.get(w.get("kind", "candidate"), _KIND_COLORS["candidate"])
        label = w.get("label") or f"{float(w.get('start_s', 0)):.1f} to {float(w.get('end_s', 0)):.1f} s"
        parts.append(f'<span title="{_esc(label)}" style="position:absolute;top:0;height:100%;'
                     f'left:{left:.2f}%;width:{width:.2f}%;background:{color}"></span>')
    height = 8 if small else 14
    extra = "min-width:120px;" if small else ""
    box = (f'<div style="position:relative;height:{height}px;border-radius:{height // 2}px;'
           f'background:rgba(128,128,128,.18);overflow:hidden;{extra}">' + "".join(parts) + "</div>")
    if ticks and not small:
        box += f'<div class="ticks"><span>0 s</span><span>{_esc(_seconds(duration_s))}</span></div>'
    return box


def timeline(duration_s, windows, **kw):
    return _display(_wrap(timeline_html(duration_s, windows, **kw)))


# --------------------------------------------------------------------------- the path

def load_path(path_file: Path = PATH_FILE) -> dict:
    return json.loads(Path(path_file).read_text())


def _summary(step: dict) -> str:
    """The one line about a step ('question' is the field's name in older copies of the JSON)."""
    return step.get("summary") or step.get("question") or ""


def assessment_steps(data: dict) -> list:
    """The steps whose kind is assessment, in the order of the path. Each one's marker is <id>.json."""
    return [s for s in data["steps"] if s.get("kind") == "assessment"]


def _read_json(path: Path):
    """The parsed file, or None when it does not exist. A file that exists but is not JSON raises."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    return json.loads(text)


_RAN = {}   # (notebook path, cell id) -> ((mtime_ns, size), ran): a notebook is parsed again only after it was saved


def milestone_ran(notebook: Path, cell_id: str) -> bool:
    """True when the saved notebook shows that cell ran and ended without an error.

    JupyterLab saves the execution count and the outputs with the notebook (on its own every
    couple of minutes, or on Ctrl+S), so this reads the learner's progress from the file. A
    notebook that is missing, half written or not JSON gives no signal yet (False) and is read
    again on the next call.
    """
    notebook = Path(notebook)
    try:
        st = notebook.stat()
    except OSError:
        return False
    key, stamp = (str(notebook), cell_id), (st.st_mtime_ns, st.st_size)
    hit = _RAN.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        nb = json.loads(notebook.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    ran = False
    for cell in nb.get("cells", []):
        if cell.get("id") == cell_id:
            ran = (cell.get("cell_type") == "code" and cell.get("execution_count") is not None
                   and not any(o.get("output_type") == "error" for o in cell.get("outputs", [])))
            break
    _RAN[key] = (stamp, ran)
    return ran


def course_status(*, prep_file: Path = PREP_STATUS_FILE, results_dir: Path = RESULTS_DIR,
                  task_root: Path = TASK_ROOT, path_file: Path = PATH_FILE) -> dict:
    """What the lab knows about the learner's progress.

    ``prep_state`` is course-prep's state. ``assessments`` maps the id of
    every assessment step to True or False (its marker's ``passed``) or None
    (never submitted). A notebook or terminal step is reached once its
    milestone cell has run without an error in the saved notebook, or once a
    file matching one of its ``evidence`` globs (relative to /dli/task)
    exists; an assessment is reached once it was submitted. ``done`` is every
    counted step up to the furthest one reached, except an assessment that
    has not passed; ``current`` is that unpassed assessment, otherwise the
    first counted step after ``done``.
    """
    prep = _read_json(prep_file) or {}
    data = load_path(path_file)
    steps = data["steps"]
    markers = {}
    for s in assessment_steps(data):
        m = _read_json(Path(results_dir) / f"{s['id']}.json")
        markers[s["id"]] = None if m is None else bool(m.get("passed"))
    kind_of = {s["id"]: s.get("kind", "notebook") for s in steps}
    touched = set()                      # steps reached: milestone run, last file on disk, or an assessment submitted
    for s in steps:
        if any(any(Path(task_root).glob(pattern)) for pattern in s.get("evidence", [])):
            touched.add(s["id"])
        elif (kind_of[s["id"]] in ("notebook", "terminal") and s.get("path") and s.get("milestone_cell_id")
              and milestone_ran(Path(task_root) / s["path"], s["milestone_cell_id"])):
            touched.add(s["id"])
    touched.update(k for k, v in markers.items() if v is not None)
    counted = [s["id"] for s in steps if kind_of[s["id"]] in STEP_KINDS]
    reached = max((counted.index(i) for i in touched if i in counted), default=-1)
    done = set(counted[:reached + 1])     # everything up to the furthest touched step
    for sid in list(done):               # an assessment counts as done only when its marker says passed
        if kind_of[sid] == "assessment" and not markers.get(sid):
            done.discard(sid)
    if reached >= 0 and kind_of[counted[reached]] == "assessment" and not markers.get(counted[reached]):
        current = counted[reached]       # still working on the assessment that was touched
    else:
        current = counted[reached + 1] if reached + 1 < len(counted) else None
    return {"prep_state": prep.get("state"), "prep_detail": prep.get("detail"),
            "assessments": markers, "done": done, "current": current}


def course_overview(*, status: dict | None = None, path_file: Path = PATH_FILE, **status_kw) -> dict:
    """The path as plain data: every step with its number, its state and its link.

    This is what the course bar (lab_home.py) polls, so the bar and the Home
    card can never disagree: both come from ``course_status``. JSON-serialisable.
    ``k`` numbers the counted steps 1..n in the order of the file and is None
    for slides, breaks and lunch. The generated path headers (course_path.py)
    print no step number. ``passed`` is the assessment marker (True, False
    or None for not submitted) and None for every other kind. ``assessments``
    maps each assessment step's id to the same value.
    """
    status = status or course_status(path_file=path_file, **status_kw)
    data = load_path(path_file)
    markers = status.get("assessments", {})
    done_ids = set(status.get("done") or ())
    steps, k = [], 0
    for s in data["steps"]:
        sid, kind = s["id"], s.get("kind", "notebook")
        counted = kind in STEP_KINDS
        if counted:
            k += 1
        steps.append({
            "id": sid, "k": k if counted else None, "kind": kind, "block": s.get("block"),
            "minutes": s.get("minutes"), "title": s["title"], "path": s.get("path"),
            "summary": _summary(s),
            "done": sid in done_ids or (kind == "assessment" and bool(markers.get(sid))),
            "current": sid == status.get("current"),
            "passed": markers.get(sid) if kind == "assessment" else None,
        })
    current = next((s for s in steps if s["current"]), None)
    return {"version": data.get("version"), "n": k,
            "done_count": sum(1 for s in steps if s["k"] and s["done"]),
            "current": current, "steps": steps,
            "assessments": {s["id"]: markers.get(s["id"]) for s in assessment_steps(data)},
            "prep_state": status.get("prep_state"), "prep_detail": status.get("prep_detail")}


def _rel(target: str, here: str) -> str:
    """A link to ``target`` (relative to the lab root) from a notebook in folder ``here`` (also relative)."""
    if not target:
        return ""
    import posixpath
    return posixpath.relpath(target, here if here not in ("", None) else ".")


def _meta(step: dict) -> str:
    """'Part 1 &middot; 35 min', leaving out what the step does not have."""
    bits = [_esc(step.get("block") or ""), f"{_esc(step['minutes'])} min" if step.get("minutes") else ""]
    return " &middot; ".join(b for b in bits if b)


def path_html(*, here: str = ".", current: str | None = None, status: dict | None = None,
              path_file: Path = PATH_FILE, compact: bool = False) -> str:
    """The day as a list of steps with where the learner is and what is already done."""
    data = load_path(path_file)
    status = status or {}
    markers = status.get("assessments", {})
    done_ids = set(status.get("done") or ())
    current = current if current is not None else status.get("current")
    items = []
    for step in data["steps"]:
        sid = step["id"]
        kind = step.get("kind", "notebook")
        is_now = sid == current
        done = sid in done_ids or (kind == "assessment" and bool(markers.get(sid)))
        dot_cls = "dot done" if done else ("dot now" if is_now else ("dot" if kind in STEP_KINDS else "dot pause"))
        dot = "&#10003;" if done else ""
        title = _esc(step["title"])
        if step.get("path"):
            title = f'<a href="{_esc(_rel(step["path"], here))}">{title}</a>'
        meta = f'<span class="muted">{_meta(step)}</span>'
        line2 = "" if compact else f'<div class="muted">{_esc(_summary(step))}</div>'
        items.append(f'<li class="{"now" if is_now else ""}"><span class="{dot_cls}">{dot}</span>'
                     f"<div><div>{title}</div>{line2}</div>{meta}</li>")
    return '<ul class="steps">' + "".join(items) + "</ul>"


def path(**kw):
    return _display(_wrap(path_html(**kw)))


def _after_last_step(data: dict) -> dict | None:
    """The first slide, break or lunch after the last counted step that says what to do in it."""
    steps = data["steps"]
    last = max((i for i, s in enumerate(steps) if s.get("kind", "notebook") in STEP_KINDS), default=-1)
    return next((s for s in steps[last + 1:] if _summary(s)), None)


def mission_control_html(*, here: str = ".", status: dict | None = None, path_file: Path = PATH_FILE) -> str:
    """The Home page card: readiness, the assessments, where you are, then the whole day."""
    status = status or course_status(path_file=path_file)
    data = load_path(path_file)
    markers = status.get("assessments", {})
    prep = status.get("prep_state")
    prep_chip = {"OK": ("ok", "clips ready"), "RUNNING": ("running", "preparing clips"), "FAILED": ("fail", "preparation failed")}.get(
        prep, ("neutral", "preparation not started"))
    tests = assessment_steps(data)
    a_chips = []
    for s in tests:
        v = markers.get(s["id"])
        cls, txt = ("pass", "passed") if v else (("fail", "not passed") if v is False else ("neutral", "not submitted"))
        a_chips.append(f'<span class="chip {cls}">{_esc(s["title"])}: {txt}</span>')
    detail = f'<div class="muted">{_esc(status.get("prep_detail"))}</div>' if prep == "RUNNING" and status.get("prep_detail") else ""
    by_id = {s["id"]: s for s in data["steps"]}
    cur = by_id.get(status.get("current") or "")
    open_titles = [s["title"] for s in tests if markers.get(s["id"]) is not True]
    if cur:
        bits = [b for b in (cur.get("block"), f"about {cur['minutes']} min" if cur.get("minutes") else "") if b]
        paren = f' <span class="muted">({_esc(", ".join(bits))})</span>' if bits else ""
        where = f'<div style="margin-top:6px"><b>Up next:</b> {_esc(cur["title"])}{paren}</div>'
    elif open_titles:
        # "not passed yet" covers never submitted (no marker) as well as submitted and not passed.
        where = (f'<div style="margin-top:6px"><b>{_esc(_join(open_titles))} '
                 f'{"is" if len(open_titles) == 1 else "are"} not passed yet.</b> '
                 f'Open {"it" if len(open_titles) == 1 else "each one"} and run its Submit cell.</div>')
    else:
        nxt = _after_last_step(data)
        # Clicking ASSESS TASK comes before what the next pause says to do (lunch: press the Part 2 launch button).
        remind = " If you have not done so yet, click ASSESS TASK on the course page." if tests else ""
        then = f" {_esc(nxt['title'])} is next. {_esc(_summary(nxt))}" if nxt else ""
        where = f'<div style="margin-top:6px"><b>Every step is done.</b>{remind}{then}</div>'
    graded = ""
    if tests:
        names = _join(s["title"] for s in tests)
        one = len(tests) == 1
        graded = (f'<div class="muted" style="margin-top:6px">{_esc(names)} {"is" if one else "are"} graded when you '
                  f'run {"its" if one else "their"} Submit cell. You can submit as often as you like.</div>')
    strip = (f'<div class="card"><div class="head"><span class="title">Where you are</span>'
             f'<span><span class="chip {prep_chip[0]}">{prep_chip[1]}</span> {" ".join(a_chips)}</span></div>{detail}{where}'
             f"{graded}</div>")
    return strip + '<div class="card">' + path_html(here=here, status=status, path_file=path_file) + "</div>"


def mission_control(**kw):
    return _display(_wrap(mission_control_html(**kw)))


# --------------------------------------------------------------------------- grader result

def _assessment_title(assessment_id, path_file: Path) -> str:
    """The step title for a marker's ``assessment`` id, or the id itself when the path does not name it."""
    if assessment_id and Path(path_file).is_file():
        for s in load_path(path_file)["steps"]:
            if s["id"] == assessment_id:
                return s["title"]
    return str(assessment_id or "Assessment")


def results_html(result: dict, label: str | None = None, *, path_file: Path = PATH_FILE) -> str:
    """The grader's answer: one row per check with its detail, the result, and what to do next.

    ``result`` is the grader's JSON, the marker it also saves; a response
    without ``checks`` is malformed and raises. ``label`` names the
    assessment in the banner; left out, it is the title of the step whose id
    is the marker's ``assessment`` field.
    """
    checks = result["checks"]
    if label is None:
        label = _assessment_title(result.get("assessment"), path_file)
    passed = bool(result.get("passed"))
    n_ok = sum(1 for c in checks if c.get("passed"))
    rows = []
    for c in checks:
        ok = bool(c.get("passed"))
        must = c.get("must", True)
        rows.append(
            f'<tr><td><span class="chip {"pass" if ok else "fail"}">{"PASS" if ok else "FAIL"}</span></td>'
            f"<td><code>{_esc(c.get('id'))}</code><div class=\"muted\">{'must' if must else 'should'}</div></td>"
            f"<td>{_esc(c.get('detail'))}</td></tr>")
    banner_cls = "banner" if passed else "banner fail"
    verdict = f"{_esc(label)}: {'PASSED' if passed else 'NOT PASSED'}"
    when = result.get("graded_at")
    # The banner comes before the table. Only must checks decide the result (the marker's passed is
    # all must checks passed), so the advice points at those and says the should rows do not block.
    advice = ("Your result is saved. Click ASSESS TASK on the course page so the course records it."
              if passed else
              "Fix the FAIL rows marked <b>must</b> in the table below. The right column says what the grader found. "
              "Rows marked <b>should</b> do not stop you passing. Then run this cell again. "
              "You can submit as often as you like.")
    return (f'<div class="{banner_cls}"><b>{verdict}</b> <span class="muted">{n_ok} of {len(checks)} checks passed'
            f"{', graded at ' + _esc(when) if when else ''}</span><div>{advice}</div></div>"
            '<div class="card"><table><tr><th></th><th>Check</th><th>What the grader saw</th></tr>'
            + "".join(rows) + "</table></div>")


def results(result: dict, label: str | None = None, **kw):
    return _display(_wrap(results_html(result, label, **kw)))


# --------------------------------------------------------------------------- small pieces

def note_html(text, *, kind="info", title=None) -> str:
    cls = "banner" if kind != "fail" else "banner fail"
    head = f"<b>{_esc(title)}</b> " if title else ""
    return f'<div class="{cls}">{head}{_esc(text)}</div>'


def note(text, **kw):
    return _display(_wrap(note_html(text, **kw)))
