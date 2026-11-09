"""
lab_home.py: the course bar, its status server, and Save HTMLs for a DLI course.

Put this file next to 00_Home.ipynb. From a notebook:

    from lab_home import show_course_bar, save_htmls
    show_course_bar()                          # the bar across the top of JupyterLab
    show_course_bar(demo_session_minutes=12)   # same, with a simulated session clock
    save_htmls()                               # all notebooks as HTML in one zip, with a download link

The course bar shows, on every JupyterLab page: where the learner is on the
path (k of n steps done, the current step's title, one segment per step), the assessments on
the path (passed, not passed, to do), whether the lab is ready (services up and
the search videos indexed), and the time left in the session. Click the step
for the whole day with links, click the lab pill for the services, containers
and GPUs behind the lab, click the bell for sound alerts. A toast appears when
a step is done, when an assessment passes or fails, when the lab becomes
ready, at 10, 5 and 1 minute before the session ends, and once when the
learner opens the last notebook before lunch (click ASSESS TASK, then save your
work: Part 2 runs on its own launch). The steps, the assessments (steps whose
kind is assessment) and the lunch text all come from part1/course_path.json.

How it is always there: this same file runs as a small background web server
(`python lab_home.py --serve`, started by lab/entrypoint.sh with the
container), reached through jupyter-server-proxy at <base URL>proxy/8050/. It
serves the bar's script at .../bar.js and the state at .../status, and nginx
(task1/nginx.conf, location /lab) adds one <script> tag to every JupyterLab
page, so the bar is drawn on first load without running a cell. The Home
notebook's first cell calls show_course_bar() as well: that starts the server
if it is missing (a lab opened without nginx) and refreshes a bar that is
already on the page. Progress comes from course_ui.course_overview(), the same
function behind the Home page card, so the two never disagree.

To adapt it to another course, edit LAB_CHECKS below. Leave both lists empty for
a lab with nothing to wait on; the pill then depends on course-prep alone (Lab
ready once it reports OK) and the details page says "Lab running". Outside the lab
container (tests, previews) point LAB_HOME_TASK_ROOT at a copy of lab/task and
LAB_HOME_RESULTS_DIR at a folder of grader markers.

Session time is read on every request from the first source that exists:
  1. a JSON file at the path in $DLI_SESSION_FILE (for the platform to provide):
       {"session_started_at": "2026-10-20T20:30:00Z",
        "session_expires_at": "2026-10-20T22:30:00Z"}
  2. the demo file written by show_course_bar(demo_session_minutes=...)
  3. $DLI_SESSION_EXPIRES_AT, $DLI_SESSION_STARTED_AT
Times are ISO 8601 UTC or epoch seconds. An absolute expiry stays correct
whenever it is read; a file the platform rewrites also supports extensions.
"""
from __future__ import annotations   # str | None annotations on Python 3.9 (tests) as well as 3.12 (the lab)

# ================================================================ course settings
# What "ready" means for this lab. An HTTP check passes when the status code is
# in ok_status (default [200]) and, if json_field is set, that field's value is
# in ok_values. Use "path" (joined to base_url) or a full "url". A container
# passes when it is running and its health check, if any, is healthy.
#
# Keep in step with the readiness check o0-gate (part1/00-onboarding.ipynb) and
# task1/deploy/wait-for-vss.sh: the pill waits for exactly what they require
# (tests/test_lab_home.py compares this list with o0-gate). Kibana, NVStreamer
# and the VIOS UI are left out on purpose: only optional sections use them, and
# a restart of one would keep the pill amber on a lab that works.

LAB_CHECKS = {
    "base_url": "http://nginx",
    "http_checks": [
        {"name": "RT-CV",         "path": "/rtvi-cv/api/v1/ready"},
        {"name": "RT-VLM",        "path": "/rtvi-vlm/v1/health/ready"},
        {"name": "RT-Embed",      "path": "/rtvi-embed/v1/ready"},
        {"name": "VIOS",          "path": "/vios/vst/api/v1/sensor/list"},
        {"name": "Elasticsearch", "path": "/es/_cluster/health", "json_field": "status", "ok_values": ["green", "yellow"]},
        {"name": "VSS agent",     "path": "/agent/health"},
    ],
    "containers": [
        "vss-agent", "vss-rtvi-cv", "vss-rtvi-vlm", "vss-rtvi-embed",
        "vss-vios-ingress", "vss-vios-sensor", "vss-vios-streamprocessing",
        "elasticsearch", "kafka", "logstash", "vss-behavior-analytics",
    ],
}

PORT = 8050
HOME_NOTEBOOK = "00_Home.ipynb"     # left out of the Save HTMLs bundle
COURSE_TITLE = "Build and Evaluate a Visual AI Agent"   # the name at the left of the course bar

# ================================================================ implementation

import argparse
import base64
import html as _html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
DEMO_SESSION_FILE = Path(tempfile.gettempdir()) / "lab_home_session_demo.json"
HTTP_TIMEOUT = 6
APP = "lab_home"


def _build_id() -> str:
    """Changes whenever this file is edited, so a stale background server gets replaced."""
    st = HERE.stat()
    return f"{int(st.st_mtime)}-{st.st_size}"


# ---------------------------------------------------------------- readiness checks

def check_http(check, base):
    """One HTTP check. Fields: name, path or url, ok_status, json_field + ok_values."""
    name = check.get("name", check.get("path", "?"))
    url = check.get("url") or (base.rstrip("/") + check.get("path", "/"))
    ok_status = check.get("ok_status", [200])
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, allow_redirects=False)
        ok = r.status_code in ok_status
        detail = f"HTTP {r.status_code}"
        if ok and check.get("json_field"):
            value = r.json().get(check["json_field"])
            ok = value in check.get("ok_values", [])
            detail += f", {check['json_field']} {value}"
    except (requests.RequestException, ValueError) as exc:
        ok, detail = False, f"not reachable ({type(exc).__name__})"
    return {"name": name, "ok": ok, "detail": detail}


def docker_api_base():
    host = os.environ.get("DOCKER_HOST", "")
    return "http://" + host[len("tcp://"):] if host.startswith("tcp://") else None


def inspect_containers(names):
    """Return ({name: (state, health)}, error_or_None)."""
    if not names:
        return {}, None
    if shutil.which("docker"):
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
                 *names],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {}, f"Docker unavailable ({type(exc).__name__})"
        states = {}
        for line in result.stdout.splitlines():
            parts = line.split("|")
            if len(parts) == 3:
                states[parts[0].lstrip("/")] = (parts[1], parts[2])
        return states, ("Docker inspection failed" if result.returncode and not states else None)

    api = docker_api_base()
    if not api:
        return {}, "Docker unavailable (no CLI and no DOCKER_HOST)"
    states = {}
    for n in names:
        try:
            r = requests.get(f"{api}/containers/{n}/json", timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                continue
            s = r.json().get("State", {})
            states[n] = (s.get("Status", "unknown"), (s.get("Health") or {}).get("Status", "no-healthcheck"))
        except (requests.RequestException, ValueError) as exc:
            return states, f"Docker API error ({type(exc).__name__})"
    return states, None


def count_containers():
    """Number of running containers, or None if Docker is not reachable."""
    if shutil.which("docker"):
        try:
            out = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=30)
            return len(out.stdout.split()) if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    api = docker_api_base()
    if not api:
        return None
    try:
        return len(requests.get(f"{api}/containers/json", timeout=HTTP_TIMEOUT).json())
    except (requests.RequestException, ValueError):
        return None


# ---------------------------------------------------------------- session time

def _ts(value):
    """ISO 8601 (with Z or offset) or epoch seconds -> epoch seconds, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _platform_data():
    """Return (source, dict) from the first session source that exists, else (None, {})."""
    for source, path in (("platform", os.environ.get("DLI_SESSION_FILE")), ("demo", str(DEMO_SESSION_FILE))):
        if not path or not Path(path).is_file():
            continue
        try:
            return source, json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
    env = {k: os.environ.get(v) for k, v in (
        ("session_expires_at", "DLI_SESSION_EXPIRES_AT"), ("session_started_at", "DLI_SESSION_STARTED_AT"))
        if os.environ.get(v)}
    return ("platform", env) if env else (None, {})


def read_session():
    """Return {source, started_at, expires_at} in epoch seconds, or None if unknown."""
    source, data = _platform_data()
    expires = _ts(data.get("session_expires_at"))
    if not expires:
        return None
    return {"source": data.get("source", source), "started_at": _ts(data.get("session_started_at")),
            "expires_at": expires}


# ---------------------------------------------------------------- hardware

_STATIC_HW = None


def _static_hardware():
    """Facts that do not change while the lab runs (read once)."""
    import re
    info = {"cpu_model": None, "cpu_cores": os.cpu_count(), "ram_gb": None,
            "driver": None, "cuda": None, "links": [],
            "visible": os.environ.get("CUDA_VISIBLE_DEVICES")}
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["cpu_model"] = " ".join(line.split(":", 1)[1].split())
                    break
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    info["ram_gb"] = round(int(line.split()[1]) / 1024 / 1024)
                    break
    except OSError:
        pass
    try:
        head = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=15).stdout
        if m := re.search(r"Driver Version:\s*([\d.]+)", head):
            info["driver"] = m.group(1)
        if m := re.search(r"CUDA Version:\s*([\d.]+)", head):
            info["cuda"] = m.group(1)
        topo = subprocess.run(["nvidia-smi", "topo", "-m"], capture_output=True, text=True, timeout=15).stdout
        rows = [l.split() for l in topo.splitlines() if re.match(r"\s*GPU\d+\s", l)]
        n = len(rows)
        for i, row in enumerate(rows):
            for j in range(i + 1, n):
                if 1 + j < len(row):
                    info["links"].append({"a": i, "b": j, "type": row[1 + j]})
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


def hardware():
    global _STATIC_HW
    if _STATIC_HW is None:
        _STATIC_HW = _static_hardware()
    gpus = []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) == 6:
                num = lambda v: int(v) if v.isdigit() else None
                gpus.append({"index": num(p[0]), "name": p[1], "mem_used": num(p[2]),
                             "mem_total": num(p[3]), "util": num(p[4]), "temp": num(p[5])})
    except (OSError, subprocess.TimeoutExpired):
        pass
    return dict(_STATIC_HW, gpus=gpus)


# ---------------------------------------------------------------- course progress

def _course_paths() -> dict:
    """Keyword arguments for course_ui.course_overview: course_ui's own defaults inside the lab
    (/dli/task, /dli/assessment_results), the LAB_HOME_* folders elsewhere."""
    kw = {}
    root = os.environ.get("LAB_HOME_TASK_ROOT")
    if root:
        root = Path(root)
        kw.update(task_root=root, prep_file=root / "data" / "course_prep_status.json",
                  path_file=root / "part1" / "course_path.json")
    results = os.environ.get("LAB_HOME_RESULTS_DIR")
    if results:
        kw["results_dir"] = Path(results)
    return kw


def course_payload() -> dict:
    """The learner's progress as the bar reads it: course_ui.course_overview().

    A failure (course_path.json unreadable, part1 missing) comes back as
    {"error": ...}, which the bar prints in place of the step, so one bad file
    does not take the lab and session checks down with it.
    """
    try:
        part1 = str(HERE.parent / "part1")
        if part1 not in sys.path:
            sys.path.insert(0, part1)
        import course_ui
        return course_ui.course_overview(**_course_paths())
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- monitor server

def run_checks():
    cfg = LAB_CHECKS
    base = cfg.get("base_url", "http://nginx")
    http_checks, containers = cfg.get("http_checks", []), cfg.get("containers", [])
    with ThreadPoolExecutor(max_workers=len(http_checks) + 3) as pool:
        http_futures = [pool.submit(check_http, c, base) for c in http_checks]
        inspect_future = pool.submit(inspect_containers, containers)
        count_future = pool.submit(count_containers)
        hw_future = pool.submit(hardware)
        http_rows = [f.result() for f in http_futures]
        states, inspect_err = inspect_future.result()
        container_count = count_future.result()
        hw = hw_future.result()

    container_rows = []
    for name in containers:
        state, health = states.get(name, ("missing", "unknown"))
        ok = state == "running" and health in ("healthy", "no-healthcheck")
        container_rows.append({"name": name, "ok": ok, "detail": f"{state}, {health}"})

    checks = http_rows + container_rows
    pending = [r["name"] for r in checks if not r["ok"]]
    if inspect_err and not pending:
        pending.append(inspect_err)
    groups = []
    if http_rows:
        groups.append({"label": "Services", "mono": False, "rows": http_rows})
    if container_rows:
        groups.append({"label": "Containers", "mono": True, "rows": container_rows})

    return {
        "checked_at": time.time(),
        "has_checks": bool(checks),
        "ready": not pending,
        "passing": sum(r["ok"] for r in checks),
        "total": len(checks),
        "pending": pending,
        "groups": groups,
        "errors": [inspect_err] if inspect_err else [],
        "container_count": container_count,
        "hardware": hw,
    }


class Monitor:
    def __init__(self, interval):
        self.interval = interval
        self.started = time.time()
        self.build = _build_id()
        self.ready_since = None
        self.snapshot = None
        self.lock = threading.Lock()

    def status_json(self):
        course = course_payload()                 # outside the lock: it reads files
        with self.lock:
            snap = dict(self.snapshot or {"checked_at": None})
            snap.update(app=APP, build=self.build, pid=os.getpid(), started=self.started,
                        ready_since=self.ready_since, interval=self.interval,
                        now=time.time(), session=read_session(), course=course)
        return json.dumps(snap)

    def loop(self):
        while True:
            snap = run_checks()
            with self.lock:
                if snap["ready"] and self.ready_since is None:
                    self.ready_since = snap["checked_at"]
                elif not snap["ready"]:
                    self.ready_since = None
                self.snapshot = snap
            time.sleep(self.interval)


def make_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path.endswith("/status") or path == "status":
                body, ctype = monitor.status_json().encode(), "application/json"
            elif path.endswith("/bar.js"):
                body, ctype = bar_js().encode(), "application/javascript; charset=utf-8"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def serve(port: int, interval: float) -> None:
    monitor = Monitor(interval)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(monitor))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"lab_home status server on 127.0.0.1:{port} (course bar, /status, /bar.js). Ctrl+C to stop.", flush=True)
    try:
        monitor.loop()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- the bar (notebook side)

def _server_info(port: int):
    """Return the running server's /status as a dict, {} if it is not lab_home, None if nothing is listening."""
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return None
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as r:
            data = json.loads(r.read())
        return data if data.get("app") == APP else {}
    except Exception:
        return {}


def _ensure_server(port: int) -> bool:
    """Start the background server, replacing an older copy of this file's server."""
    info = _server_info(port)
    if info and info.get("build") == _build_id():
        return True
    if info:                                   # our server, but this file has changed since: replace it
        try:
            os.kill(info["pid"], 15)
        except (OSError, KeyError):
            pass
        for _ in range(20):
            if _server_info(port) is None:
                break
            time.sleep(0.25)
        for _ in range(16):                    # entrypoint.sh's loop restarts it from the new file within 2 s; spawn only if nothing comes back
            time.sleep(0.5)
            if _server_info(port):
                return True
    elif info == {}:
        print(f"Port {port} is in use by another program. In a terminal, `ss -ltnp 'sport = :{port}'` shows its pid;\n"
              f"`kill <pid>` it, then run this cell again. The lab's own server logs to /var/log/lab_home.log.")
        return False
    subprocess.Popen([sys.executable, str(HERE), "--serve", "--port", str(port)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if _server_info(port):
            return True
        time.sleep(0.5)
    print(f"The status server did not start. Run `python {HERE.name} --serve` in a terminal to see the error.")
    return False


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_demo_session(minutes_left, length_minutes) -> None:
    """Write (or remove) the simulated session the server reads."""
    if minutes_left is None:
        DEMO_SESSION_FILE.unlink(missing_ok=True)
        return
    now = time.time()
    length = max(length_minutes, minutes_left)
    DEMO_SESSION_FILE.write_text(json.dumps({
        "source": "demo",
        "session_started_at": _iso(now - (length - minutes_left) * 60),
        "session_expires_at": _iso(now + minutes_left * 60),
    }))


def bar_js(port: int = PORT, proxy_path: str | None = None) -> str:
    """The course bar's script, as the status server serves it at /bar.js and the Home cell displays it."""
    return (_JS.replace("__PORT__", str(port)).replace("__PROXY_PATH__", json.dumps(proxy_path))
            .replace("__COURSE__", json.dumps(COURSE_TITLE)))


def show_course_bar(demo_session_minutes: float | None = None,
                    demo_session_length_minutes: float = 120,
                    port: int = PORT, proxy_path: str | None = None) -> None:
    """Show the course bar, starting the background server if it is not running.

    On a lab behind nginx the bar is already on the page (nginx injects the
    script); this then only refreshes it. demo_session_minutes: simulate a
    session with this many minutes left (marked "demo" on screen). Leave as
    None to use the real value once the platform provides it; any earlier demo
    value is cleared.
    """
    from IPython.display import Javascript, display
    _write_demo_session(demo_session_minutes, demo_session_length_minutes)
    if not _ensure_server(port):
        return
    display(Javascript(bar_js(port, proxy_path)))


show_lab_badge = show_course_bar      # the name the first version of this file used


# ---------------------------------------------------------------- Save HTMLs

def _md_to_html(text: str) -> str:
    try:
        import markdown
        return markdown.markdown(text, extensions=["extra", "nl2br"])
    except ImportError:
        return f"<pre>{_html.escape(text)}</pre>"


def _embed_local_images(html: str, base_dir: Path) -> str:
    """Replace <img src="localpath"> with base64 data URLs when the file exists."""
    def repl(m):
        prefix, quote, src = m.group(1), m.group(2), m.group(3).strip()
        if src.startswith(("data:", "http://", "https://")):
            return m.group(0)
        path = (base_dir / src).resolve()
        if not path.is_file():
            return m.group(0)
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",   # the course figures are GIF and PNG
                ".svg": "image/svg+xml", ".webp": "image/webp"}.get(path.suffix.lower(), "image/png")
        return f'<img {prefix}src={quote}data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}{quote}'
    return re.sub(r'<img\s+([^>]*?)src\s*=\s*(["\'])([^"\']+)\2', repl, html, flags=re.I)


def notebook_to_html(notebook_path: Path, output_path: Path) -> Path:
    """Write one notebook as a self-contained HTML file (markdown, code, outputs, images)."""
    def text(v):
        return "".join(v) if isinstance(v, list) else (v or "")
    nb = json.loads(Path(notebook_path).read_text(encoding="utf-8"))
    base_dir = Path(notebook_path).resolve().parent
    parts = []
    for cell in nb.get("cells", []):
        source = text(cell.get("source"))
        if cell.get("cell_type") == "markdown":
            parts.append(f'<div class="cell cell-markdown">\n{_embed_local_images(_md_to_html(source), base_dir)}\n</div>')
        elif cell.get("cell_type") == "code":
            if source.strip():
                parts.append(f'<div class="cell cell-code"><pre class="code">{_html.escape(source)}</pre></div>')
            for out in cell.get("outputs", []):
                kind = out.get("output_type")
                if kind == "stream" and text(out.get("text")).strip():
                    parts.append(f'<div class="cell cell-output"><pre class="stream">{_html.escape(text(out.get("text")))}</pre></div>')
                elif kind in ("execute_result", "display_data"):
                    data = out.get("data", {})
                    imgs = [f'<img src="data:{k};base64,{text(data[k])}" alt="output" class="nb-output-img"/>'
                            for k in ("image/png", "image/jpeg") if k in data]
                    if imgs:
                        parts.append('<div class="cell cell-output">\n' + "\n".join(imgs) + "\n</div>")
                    elif "text/html" in data:
                        # course_ui cards and other HTML outputs, minus inline video (tens of MB of base64 per clip).
                        # The data URL sits on <video> itself or on a <source> inside it (IPython's Video(embed=True)).
                        markup = re.sub(r'<video\b.*?</video>',
                                        lambda m: ('<p class="nb-output-text">[video not included in the export]</p>'
                                                   if re.search(r'data:video/', m.group(0), re.I) else m.group(0)),
                                        text(data["text/html"]), flags=re.S | re.I)
                        parts.append(f'<div class="cell cell-output">{markup}</div>')
                    elif "text/markdown" in data:
                        parts.append(f'<div class="cell cell-output">{_md_to_html(text(data["text/markdown"]))}</div>')
                    elif "text/plain" in data:
                        parts.append(f'<div class="cell cell-output"><pre class="nb-output-text">{_html.escape(text(data["text/plain"]))}</pre></div>')
                elif kind == "error":
                    tb = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(out.get("traceback", [])))
                    parts.append(f'<div class="cell cell-output error"><pre>{_html.escape(tb)}</pre></div>')
    body = "\n".join(parts)
    output_path.write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_html.escape(Path(notebook_path).name)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 900px; line-height: 1.5; }}
.cell {{ margin-bottom: 1.5rem; }}
.cell-code .code {{ background: #f5f5f5; padding: 0.75rem; border-radius: 4px; overflow-x: auto; font-size: 0.9em; }}
.cell-output .nb-output-text {{ background: #f9f9f9; padding: 0.5rem; font-size: 0.9em; }}
.cell-output.error pre {{ color: #c00; }}
.nb-output-img {{ max-width: 100%; height: auto; display: block; margin: 0.5rem 0; }}
pre {{ margin: 0; white-space: pre-wrap; }}
</style>
</head>
<body>
{body}
</body>
</html>""", encoding="utf-8")
    return output_path


def save_htmls(base_dir: str | Path = ".", bundle_name: str = "MyCourseCopy") -> Path | None:
    """Convert every notebook under base_dir to HTML (keeping the folder structure),
    zip them as <bundle_name>_YYYY-MM-DD_HH-MM.zip, and show a download link."""
    base = Path(base_dir).resolve()
    notebooks = sorted(p for p in base.rglob("*.ipynb")
                       if ".ipynb_checkpoints" not in p.parts and p.name != HOME_NOTEBOOK)
    if not notebooks:
        print("No notebooks found to save.")
        return None
    zip_path = base / f"{bundle_name}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.zip"
    skipped = []
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for nb in notebooks:
            rel = nb.relative_to(base).with_suffix(".html")
            out = Path(tmp) / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                notebook_to_html(nb, out)
                zf.write(out, f"{bundle_name}/{rel.as_posix()}")
            except Exception as exc:
                skipped.append(f"{nb.name} ({exc})")
    print(f"Saved {len(notebooks) - len(skipped)} notebooks as HTML.")
    if skipped:
        print("Skipped: " + "; ".join(skipped))
    try:
        from IPython.display import FileLink, display
        link = os.path.relpath(zip_path, Path.cwd())
        display(FileLink(link, result_html_prefix="Download (click to save to your computer): "))
    except ImportError:
        print(f"Created {zip_path}")
    return zip_path


# ---------------------------------------------------------------- the bar script and the details page

_JS = r"""
(function () {
  "use strict";
  // The course bar: one strip across the top of JupyterLab with the learner's place on the path,
  // the assessments on the path, the lab's state and the session clock. nginx injects this script into
  // every JupyterLab page (task1/nginx.conf, location /lab) and the Home notebook's first cell
  // displays it too; a second copy only refreshes the bar that is already there.
  if (window.self !== window.top) return;                       // never inside an iframe (HTML outputs, Course Docs, the details page)
  if (!document.getElementById("jupyter-config-data")) return;  // not a JupyterLab page (an error page, a served HTML file)
  if (window.__courseBar && typeof window.__courseBar.refresh === "function") { window.__courseBar.refresh(); return; }
  const older = window.__labBadge;                              // the badge this bar replaces
  if (older && typeof older.stop === "function") { try { older.stop(); } catch (e) {} }
  window.__labBadge = null;
  document.querySelectorAll("#lab-badge, #course-bar, #course-bar-tab, #course-bar-toast").forEach((n) => n.remove());

  let config = {};
  try { config = JSON.parse(document.getElementById("jupyter-config-data").textContent) || {}; } catch (e) {}
  const base = (config.baseUrl || "/").replace(/\/?$/, "/");
  const panelUrl = __PROXY_PATH__ || (base + "proxy/__PORT__/");
  const statusUrl = panelUrl + "status";
  // JupyterLab opens a file at <base><treeUrl>/<path>; treeUrl is "/lab/tree/" relative to the base.
  const treeUrl = base + (config.treeUrl || "lab/tree/").replace(/^\//, "").replace(/\/?$/, "/");
  const appName = config.appName || "JupyterLab";
  const HEIGHT = 36;
  const COURSE = __COURSE__;                                     // lab_home.COURSE_TITLE

  const WARNINGS = [
    { t: 600, beeps: 1, level: "warn", msg: "10 minutes left in this session. Save a copy of your notebooks now with Save HTMLs on the Home page." },
    { t: 300, beeps: 2, level: "crit", msg: "5 minutes left in this session. Run Save HTMLs on the Home page now if you have not already." },
    { t: 60,  beeps: 3, level: "crit", msg: "1 minute left. This session is about to end." },
  ];

  const css = `
    #course-bar { position: fixed; top: 0; left: 0; right: 0; height: ${HEIGHT}px; z-index: 1000; display: flex; align-items: center;
      gap: 10px; padding: 3px 12px 0 12px; box-sizing: border-box; background: var(--jp-layout-color1, #fff);
      color: var(--jp-ui-font-color1, #222); border-bottom: 1px solid var(--jp-border-color2, #d8d8d8);
      font-family: var(--jp-ui-font-family, system-ui, -apple-system, "Segoe UI", sans-serif); font-size: 12.5px; line-height: 1.3;
      --cb-accent: #76b900; --cb-warn: #ef9f27; --cb-fail: #e24b4a; --cb-info: #378add;
      --cb-muted: var(--jp-ui-font-color2, #666); --cb-line: var(--jp-border-color2, #d8d8d8); --cb-soft: var(--jp-layout-color2, #f3f3f3); }
    #course-bar::before { content: ""; position: absolute; left: 0; right: 0; top: 0; height: 3px; background: var(--cb-accent); }
    #course-bar[hidden], #course-bar-tab[hidden], #course-bar [hidden] { display: none !important; }
    body.course-bar-on > .jp-LabShell, body.course-bar-on > #main { top: ${HEIGHT}px !important; }
    #course-bar button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; }
    #course-bar a { color: inherit; }
    #course-bar button:focus-visible, #course-bar a:focus-visible { outline: 2px solid var(--cb-info); outline-offset: 2px; border-radius: 4px; }
    #course-bar .cb-home { display: flex; align-items: center; gap: 8px; text-decoration: none; font-weight: 600; white-space: nowrap; flex: none; }
    #course-bar .cb-home .cb-mark { width: 14px; height: 14px; background: var(--cb-accent); border-radius: 2px; flex: none; }
    #course-bar .cb-track { display: flex; gap: 3px; flex: 0 1 280px; min-width: 140px; height: 12px; align-items: center; }
    #course-bar .cb-seg { display: block; flex: 1; height: 6px; border-radius: 3px; background: var(--cb-soft); border: 1px solid var(--cb-line); box-sizing: border-box; }
    #course-bar .cb-seg.assessment { height: 11px; border-radius: 3px; }
    #course-bar .cb-seg.done { background: var(--cb-accent); border-color: var(--cb-accent); }
    #course-bar .cb-seg.now { border-color: var(--cb-accent); box-shadow: 0 0 0 2px rgba(118, 185, 0, .3); }
    #course-bar .cb-seg.fail { background: var(--cb-fail); border-color: var(--cb-fail); }
    #course-bar .cb-where { display: flex; align-items: baseline; gap: 8px; min-width: 0; flex: 1 1 auto; text-align: left; }
    #course-bar .cb-step { font-weight: 600; white-space: nowrap; }
    #course-bar .cb-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--cb-muted); }
    #course-bar .cb-here { font-size: 11px; padding: 1px 7px; border-radius: 99px; border: 1px solid var(--cb-line); color: var(--cb-muted); white-space: nowrap; flex: none; }
    #course-bar .cb-right { margin-left: auto; display: flex; align-items: center; gap: 8px; flex: none; }
    #course-bar .cb-a { display: flex; gap: 5px; }
    #course-bar .cb-chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 99px; border: 1px solid var(--cb-line);
      white-space: nowrap; font-size: 11.5px; text-decoration: none; }
    #course-bar .cb-chip .d { width: 7px; height: 7px; border-radius: 50%; background: var(--cb-muted); flex: none; }
    #course-bar .cb-chip.pass { border-color: var(--cb-accent); } #course-bar .cb-chip.pass .d { background: var(--cb-accent); }
    #course-bar .cb-chip.fail { border-color: var(--cb-fail); } #course-bar .cb-chip.fail .d { background: var(--cb-fail); }
    #course-bar .cb-lab { display: inline-flex; align-items: center; gap: 7px; padding: 3px 10px; border-radius: 99px; border: 1px solid var(--cb-warn); white-space: nowrap; }
    #course-bar .cb-lab .d { width: 8px; height: 8px; border-radius: 50%; background: var(--cb-warn); flex: none; }
    #course-bar .cb-lab.ready { background: var(--cb-accent); border-color: var(--cb-accent); color: #fff; } #course-bar .cb-lab.ready .d { background: #fff; }
    #course-bar .cb-lab.fail { background: var(--cb-fail); border-color: var(--cb-fail); color: #fff; } #course-bar .cb-lab.fail .d { background: #fff; }
    #course-bar .cb-lab.offline { border-color: var(--cb-muted); } #course-bar .cb-lab.offline .d { background: var(--cb-muted); }
    #course-bar.pulse .cb-lab { animation: cbpulse 1.2s ease-out 2; }
    @keyframes cbpulse { 0% { box-shadow: 0 0 0 0 rgba(118, 185, 0, .6); } 100% { box-shadow: 0 0 0 14px rgba(118, 185, 0, 0); } }
    #course-bar .cb-time { font-variant-numeric: tabular-nums; white-space: nowrap; padding: 2px 8px; border-radius: 99px; }
    #course-bar .cb-time small { font-size: 10px; opacity: .75; margin-left: 4px; }
    #course-bar.t-warn .cb-time { background: var(--cb-warn); color: #fff; }
    #course-bar.t-crit .cb-time, #course-bar.t-end .cb-time { background: var(--cb-fail); color: #fff; }
    #course-bar.t-last .cb-time { animation: cbblink 1s ease-in-out infinite; }
    @keyframes cbblink { 50% { opacity: .5; } }
    #course-bar .cb-icon { display: inline-flex; padding: 3px; color: var(--cb-muted); border-radius: 4px; }
    #course-bar .cb-icon:hover { color: inherit; background: var(--cb-soft); }
    #course-bar .cb-bell[aria-pressed="true"] { color: inherit; }
    #course-bar .cb-bell .slash { display: inline; } #course-bar .cb-bell[aria-pressed="true"] .slash { display: none; }
    #course-bar .cb-drop { position: absolute; top: ${HEIGHT}px; background: var(--jp-layout-color1, #fff); color: var(--jp-ui-font-color1, #222);
      border: 1px solid var(--cb-line); border-top: 0; border-radius: 0 0 8px 8px; box-shadow: 0 10px 28px rgba(0, 0, 0, .18);
      max-height: calc(100vh - 60px); overflow: auto; }
    #course-bar .cb-steps { left: 12px; width: min(640px, 92vw); padding: 8px 0 4px; }
    #course-bar .cb-steps ol { list-style: none; margin: 0; padding: 0; }
    #course-bar .cb-steps li { display: grid; grid-template-columns: 22px 1fr auto; gap: 8px; align-items: baseline; padding: 5px 14px; }
    #course-bar .cb-steps li.now { background: rgba(118, 185, 0, .10); }
    #course-bar .cb-steps .dot { width: 12px; height: 12px; border-radius: 50%; border: 1px solid var(--cb-line); box-sizing: border-box; display: inline-flex;
      align-items: center; justify-content: center; font-size: 9px; color: #fff; position: relative; top: 2px; }
    #course-bar .cb-steps .dot.done { background: var(--cb-accent); border-color: var(--cb-accent); }
    #course-bar .cb-steps .dot.now { border: 2px solid var(--cb-accent); }
    #course-bar .cb-steps .dot.pause { border-style: dashed; }
    #course-bar .cb-steps a { text-decoration: none; } #course-bar .cb-steps a:hover { text-decoration: underline; }
    #course-bar .cb-steps .q, #course-bar .cb-steps .m { color: var(--cb-muted); font-size: 11.5px; }
    #course-bar .cb-steps .m { white-space: nowrap; }
    #course-bar .cb-steps .foot { padding: 8px 14px 4px; color: var(--cb-muted); font-size: 11.5px; border-top: 1px solid var(--cb-line); margin-top: 6px; }
    #course-bar .cb-steps .foot a { text-decoration: underline; }
    #course-bar .cb-panel { right: 12px; width: min(780px, 92vw); }
    #course-bar .cb-panel .bar { display: flex; justify-content: flex-end; gap: 12px; padding: 5px 10px; font-size: 12px; border-bottom: 1px solid var(--cb-line); }
    #course-bar .cb-panel .bar a, #course-bar .cb-panel .bar button { color: var(--cb-info); text-decoration: underline; }
    #course-bar .cb-panel iframe { display: block; width: 100%; height: 260px; border: 0; }
    #course-bar-toast { position: fixed; top: ${HEIGHT + 6}px; right: 12px; z-index: 1001; max-width: 400px; display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px;
      border-radius: 8px; background: var(--jp-layout-color1, #fff); color: var(--jp-ui-font-color1, #222); border: 1px solid #76b900;
      border-left-width: 5px; box-shadow: 0 8px 24px rgba(0, 0, 0, .2); font-family: var(--jp-ui-font-family, system-ui, sans-serif); font-size: 12.5px; }
    #course-bar-toast.warn { border-color: #ef9f27; } #course-bar-toast.crit { border-color: #e24b4a; }
    #course-bar-toast[hidden] { display: none !important; }
    #course-bar-toast p { margin: 0; line-height: 1.4; }
    #course-bar-toast button { font: inherit; font-size: 12px; color: #378add; background: none; border: 0; padding: 0; cursor: pointer; text-decoration: underline; flex: none; }
    #course-bar-tab { position: fixed; top: 0; right: 12px; z-index: 1000; font: 600 11.5px var(--jp-ui-font-family, system-ui, sans-serif); color: #fff;
      background: #76b900; border: 0; border-radius: 0 0 8px 8px; padding: 3px 10px; cursor: pointer; }
    @media (max-width: 1180px) { #course-bar .cb-home b { display: none; } }
    @media (max-width: 980px) { #course-bar .cb-track { display: none; } #course-bar .cb-chip .t { display: none; } }
    @media (prefers-reduced-motion: reduce) { #course-bar, #course-bar * { animation: none !important; } }
  `;
  const bellSvg = `<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6z"/><path d="M10 19a2 2 0 0 0 4 0"/><line class="slash" x1="4" y1="4" x2="20" y2="20"/></svg>`;
  const closeSvg = `<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
    <path d="M6 6l12 12M18 6L6 18"/></svg>`;
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
  const homeUrl = treeUrl + "00_Home.ipynb";

  // ---- storage (per browser; failures are harmless)
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };

  // The style and the body class go in NOW, before the shell exists: the offset rule then applies the
  // moment JupyterLab attaches its shell, so nothing jumps when the bar appears a little later.
  const styleEl = document.createElement("style"); styleEl.id = "course-bar-style"; styleEl.textContent = css;
  document.querySelectorAll("#course-bar-style").forEach((n) => n.remove());
  document.head.appendChild(styleEl);
  let hidden = store.get("courseBar.hidden") === "1";
  document.body.classList.toggle("course-bar-on", !hidden);

  const root = document.createElement("div");
  root.id = "course-bar"; root.setAttribute("role", "region"); root.setAttribute("aria-label", "Course bar");
  root.innerHTML = `<a class="cb-home" href="${homeUrl}" title="Open the Home page"><span class="cb-mark"></span><b>${esc(COURSE)}</b></a>
    <div class="cb-track" role="list" aria-label="The steps of the day"></div>
    <button class="cb-where" type="button" aria-expanded="false" title="The whole day, with links"><span class="cb-step">Course status</span><span class="cb-title"></span><span class="cb-here" hidden></span></button>
    <div class="cb-right">
      <span class="cb-a"></span>
      <button class="cb-lab" type="button" aria-expanded="false"><span class="d"></span><span class="txt">Checking the lab</span></button>
      <span class="cb-time" hidden></span>
      <button class="cb-icon cb-bell" type="button" aria-pressed="false">${bellSvg}</button>
      <button class="cb-icon cb-hide" type="button" title="Hide the course bar. A small green tab at the top right brings it back." aria-label="Hide the course bar">${closeSvg}</button>
    </div>
    <div class="cb-drop cb-steps" hidden></div>
    <div class="cb-drop cb-panel" hidden><div class="bar"><a target="_blank" rel="noopener">Open in new tab</a><button type="button">Close</button></div></div>`;
  // The toast is its own element next to the bar, not inside it, so a session warning still shows when the bar is hidden.
  const toastEl = document.createElement("div");
  toastEl.id = "course-bar-toast"; toastEl.setAttribute("role", "status"); toastEl.setAttribute("aria-live", "polite"); toastEl.hidden = true;
  toastEl.innerHTML = `<p></p><button type="button">Dismiss</button>`;
  const tab = document.createElement("button");
  tab.id = "course-bar-tab"; tab.type = "button"; tab.hidden = true; tab.title = "Show the course bar"; tab.textContent = "Course bar";

  const q = (s) => root.querySelector(s);
  const stepsDrop = q(".cb-steps"), panelDrop = q(".cb-panel"), whereBtn = q(".cb-where"), labBtn = q(".cb-lab"), timeEl = q(".cb-time"), bell = q(".cb-bell");
  panelDrop.querySelector("a").href = panelUrl;

  // ---- sound (opt-in, same keys as the badge it replaces)
  let audio = null;
  let soundOn = store.get("labBadge.sound") === "on";
  function unlockAudio() { try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); audio.resume(); } catch (e) {} }
  function tone(f, at, len, vol) {
    const o = audio.createOscillator(), g = audio.createGain(), s = audio.currentTime + at;
    o.frequency.value = f; g.gain.setValueAtTime(0, s); g.gain.linearRampToValueAtTime(vol, s + 0.02);
    g.gain.exponentialRampToValueAtTime(0.001, s + len); o.connect(g).connect(audio.destination); o.start(s); o.stop(s + len + 0.05);
  }
  function play(kind, n) {
    if (!soundOn || !audio) return;
    try {
      if (kind === "ready") [[660, 0], [880, 0.18], [1320, 0.36]].forEach(([f, t]) => tone(f, t, 0.5, 0.3));
      else for (let i = 0; i < n; i++) { tone(988, i * 0.32, 0.16, 0.35); tone(740, i * 0.32 + 0.12, 0.16, 0.3); }
    } catch (e) {}
  }
  function renderBell() {
    bell.setAttribute("aria-pressed", String(soundOn));
    bell.title = soundOn ? "Sound alerts on. Click to turn off." : "Sound alerts off. Click to hear a chime when the lab is ready, when an assessment passes, and at 10, 5 and 1 minute before the session ends.";
    bell.setAttribute("aria-label", soundOn ? "Sound alerts on" : "Sound alerts off");
  }
  bell.addEventListener("click", () => {
    soundOn = !soundOn; store.set("labBadge.sound", soundOn ? "on" : "off");
    if (soundOn) { unlockAudio(); setTimeout(() => play("alert", 1), 50); }
    renderBell();
  });
  const onFirstGesture = () => { if (soundOn) unlockAudio(); document.removeEventListener("pointerdown", onFirstGesture, true); };
  document.addEventListener("pointerdown", onFirstGesture, true);
  renderBell();

  // ---- toast: one at a time; a session warning (warn, crit) stays until dismissed and is never replaced by good news
  let toastTimer = null;
  function toast(msg, level, ms) {
    const sticky = !toastEl.hidden && /\b(warn|crit)\b/.test(toastEl.className);
    if (sticky && level === "good") return;
    toastEl.className = level || ""; toastEl.querySelector("p").textContent = msg; toastEl.hidden = false;
    clearTimeout(toastTimer);
    if (ms) toastTimer = setTimeout(() => { toastEl.hidden = true; }, ms);
  }
  toastEl.querySelector("button").addEventListener("click", () => { toastEl.hidden = true; });

  // ---- drop-downs
  function fitFrame() {
    const f = panelDrop.querySelector("iframe");
    try { const h = f.contentDocument.body.scrollHeight; if (h) f.style.height = Math.min(h + 2, window.innerHeight * 0.7) + "px"; } catch (e) {}
  }
  function setDrop(which, open) {
    const other = which === stepsDrop ? panelDrop : stepsDrop, btn = which === stepsDrop ? whereBtn : labBtn, otherBtn = which === stepsDrop ? labBtn : whereBtn;
    other.hidden = true; otherBtn.setAttribute("aria-expanded", "false");
    if (!open || which !== panelDrop) panelDrop.querySelectorAll("iframe").forEach((f) => f.remove());   // a closed details page stops polling
    if (open && which === panelDrop && !panelDrop.querySelector("iframe")) {
      const f = document.createElement("iframe"); f.src = panelUrl; f.title = "Lab status"; f.addEventListener("load", fitFrame); panelDrop.appendChild(f);
    }
    if (open && which === stepsDrop && course && !course.error) renderSteps(course);
    which.hidden = !open; btn.setAttribute("aria-expanded", String(open));
  }
  whereBtn.addEventListener("click", () => setDrop(stepsDrop, stepsDrop.hidden));
  labBtn.addEventListener("click", () => setDrop(panelDrop, panelDrop.hidden));
  panelDrop.querySelector(".bar button").addEventListener("click", () => setDrop(panelDrop, false));
  const onKey = (e) => { if (e.key === "Escape") { setDrop(stepsDrop, false); setDrop(panelDrop, false); } };
  const onDown = (e) => { if (!root.contains(e.target)) { setDrop(stepsDrop, false); setDrop(panelDrop, false); } };
  document.addEventListener("keydown", onKey);
  document.addEventListener("mousedown", onDown);

  // ---- the path
  let status = null, course = null, session = null, offset = 0, lastReady = null, lastGreen = null, lastSig = null;
  const stepUrl = (s) => (s.path ? treeUrl + s.path.split("/").map(encodeURIComponent).join("/") : null);

  function renderTrack(c) {
    const track = q(".cb-track"); track.replaceChildren();
    c.steps.filter((s) => s.k).forEach((s) => {
      const seg = document.createElement(s.path ? "a" : "span");
      seg.className = "cb-seg" + (s.done ? " done" : "") + (s.current ? " now" : "") + (s.kind === "assessment" ? " assessment" : "") + (s.passed === false ? " fail" : "");
      seg.setAttribute("role", "listitem");
      seg.title = s.title + (s.done ? ", done" : s.current ? ", up next" : "") + (s.passed === false ? " (submitted, not passed yet)" : "");
      if (s.path) seg.href = stepUrl(s);
      track.append(seg);
    });
  }
  // The assessments are the steps whose kind is "assessment"; c.assessments maps each id to its marker's passed (or null).
  const tests = (c) => c.steps.filter((s) => s.kind === "assessment");
  const openTests = (c) => tests(c).filter((s) => c.assessments[s.id] !== true).map((s) => s.title);
  const names = (a) => (a.length < 2 ? a.join("") : a.slice(0, -1).join(", ") + " and " + a[a.length - 1]);
  function afterLast(c) {
    // The first slide, break or lunch after the last counted step that says what to do in it (on this launch: lunch).
    let last = -1;
    c.steps.forEach((s, i) => { if (s.k) last = i; });
    return c.steps.slice(last + 1).find((s) => s.summary) || null;
  }
  function whenAllDone(c) {
    // Every step done (so every assessment passed): ASSESS TASK first, then what the next pause says to do
    // (lunch: press the Part 2 launch button). Empty when there is neither.
    const nxt = afterLast(c);
    return [tests(c).length ? "If you have not done so yet, click ASSESS TASK on the course page." : "",
            nxt ? `${nxt.title} is next. ${nxt.summary}` : ""].filter(Boolean).join(" ");
  }
  function renderWhere(c) {
    const cur = c.current, open = openTests(c);
    if (cur) { q(".cb-step").textContent = `${c.done_count} of ${c.n} steps done`; q(".cb-title").textContent = cur.title; tab.textContent = `Course bar: ${c.done_count} of ${c.n} steps done`; }
    else if (open.length) {
      // "Not passed yet" covers never submitted as well as submitted and not passed.
      q(".cb-step").textContent = `${c.done_count} of ${c.n} steps done`;
      q(".cb-title").textContent = `Not passed yet: ${names(open)}. Open ${open.length === 1 ? "it" : "each one"} and run its Submit cell.`;
      tab.textContent = `Course bar: ${c.done_count} of ${c.n} steps done`;
    } else {
      q(".cb-step").textContent = `All ${c.n} steps done`;
      q(".cb-title").textContent = whenAllDone(c) || "Nothing is left on this launch.";
      tab.textContent = "Course bar: all steps done";
    }
    renderHere();
  }
  function renderAssessments(c) {
    const box = q(".cb-a"); box.replaceChildren();
    tests(c).forEach((s) => {
      const v = c.assessments[s.id];
      const chip = document.createElement(s.path ? "a" : "span");
      chip.className = "cb-chip " + (v ? "pass" : v === false ? "fail" : "");
      chip.innerHTML = `<span class="d"></span>${esc(s.title)}<span class="t">&nbsp;${v ? "passed" : v === false ? "not passed" : "not submitted"}</span>`;
      chip.title = s.title + ". " + (v ? "Passed." : v === false ? "Submitted, not passed yet: the Submit cell lists what to fix." : "Not submitted yet.");
      if (s.path) chip.href = stepUrl(s);
      box.append(chip);
    });
  }
  function renderSteps(c) {
    const items = c.steps.map((s) => {
      const cls = s.done ? "dot done" : s.current ? "dot now" : s.k ? "dot" : "dot pause";
      const title = s.path ? `<a href="${stepUrl(s)}">${esc(s.title)}</a>` : esc(s.title);
      const meta = [s.block || "", s.minutes ? `${s.minutes} min` : ""].filter(Boolean).join(" &middot; ");
      return `<li class="${s.current ? "now" : ""}"><span class="${cls}">${s.done ? "&#10003;" : ""}</span>` +
        `<div><div>${title}</div>${s.summary ? `<div class="q">${esc(s.summary)}</div>` : ""}</div><span class="m">${meta}</span></li>`;
    });
    stepsDrop.innerHTML = `<ol>${items.join("")}</ol><div class="foot">${c.done_count} of ${c.n} steps done. A step is done when the cell named at the end ` +
      `of its notebook has run without an error and the notebook is saved (Ctrl+S, or Cmd+S on a Mac). ` +
      `A step on a Course Docs page has no notebook: it is done once the file that the Home page names exists. ` +
      `An assessment is done when its Submit cell says PASSED. <a href="${homeUrl}">Home</a> has the same map with more detail.</div>`;
  }
  function openDocs() {
    // Where the active document can be read from, best source first: the URL (JupyterLab keeps
    // <base>lab[/workspaces/<name>]/tree/<path> current; after a file open it may hold only the folder), then the
    // current tab of the main area, whose tooltip carries "Path: <path>". The title is no use (long names are shortened).
    const found = [];
    let rel = decodeURIComponent(location.pathname);
    if (rel.startsWith(base)) rel = rel.slice(base.length);
    const m = /^(lab|doc)(\/workspaces\/[A-Za-z0-9_-]+)?\/tree\/(.+)$/.exec(rel);
    if (m && m[3]) found.push(m[3]);
    const tab = document.querySelector("#jp-main-dock-panel .lm-TabBar-tab.lm-mod-current");
    const line = tab && (tab.getAttribute("title") || "").split("\n").find((l) => l.startsWith("Path: "));
    if (line) found.push(line.slice(6).trim());
    return found;
  }
  function stepAt(path) {
    return course.steps.find((s) => s.path && (s.path === path || s.path.split("/").pop() === path.split("/").pop()));
  }
  function lunchNote(c) {
    // The last notebook before lunch, and what to do before lunch: click ASSESS TASK for the assessments before
    // it (the lunch summary sends the learner to the next launch button), then the lunch step's summary. The note
    // fires when that notebook is open (stepAt matches paths only), so a counted step after it without a notebook,
    // a Course Docs page such as VSS Coding Agent Skills, is named in the text instead.
    const i = c.steps.findIndex((s) => s.kind === "lunch");
    if (i < 0 || !c.steps[i].summary) return null;
    const counted = c.steps.slice(0, i).filter((s) => s.k);
    const before = counted.filter((s) => s.path).pop();
    if (!before) return null;
    const later = counted.slice(counted.indexOf(before) + 1).map((s) => s.title);
    const when = later.length ? `after ${names(later)}, which ${later.length === 1 ? "follows" : "follow"} this step` : "after this step";
    const graded = c.steps.slice(0, i).filter((s) => s.kind === "assessment").map((s) => s.title);
    const assess = graded.length ? ` Before you leave this launch, click ASSESS TASK on the course page if you have not done so yet. ` +
      `The Submit ${graded.length === 1 ? "cell" : "cells"} in ${names(graded)} must show PASSED first.` : "";
    return { id: before.id, text: `${c.steps[i].title} comes ${when}.${assess} ${c.steps[i].summary}` };
  }
  let lunchNoted = false;                                       // once per page even where localStorage is blocked
  function remindBeforeLunch(here) {
    // Once per browser, when the learner opens the last notebook before lunch: Part 2 runs on its own launch, so click
    // ASSESS TASK and save first.
    const note = here ? lunchNote(course) : null;
    if (lunchNoted || !note || here.id !== note.id) return;
    lunchNoted = true;
    const key = "courseBar.lunchNote." + (course.version || "");
    if (store.get(key)) return;
    store.set(key, "1");
    toast(note.text, "warn");
  }
  function renderHere() {
    const el = q(".cb-here");
    if (!course || course.error) { el.hidden = true; return; }
    const here = openDocs().map(stepAt).find(Boolean);
    remindBeforeLunch(here);
    if (!here || !here.k || here.current) { el.hidden = true; return; }
    const cur = course.current;
    el.hidden = false;
    if (here.done) el.textContent = "This notebook is done";
    else if (cur && here.k > cur.k) el.textContent = `${cur.title} is not done yet`;
    else el.textContent = `This notebook: ${here.title}`;
  }
  function nextText(c) {
    if (c.current) {
      const when = [c.current.block, c.current.minutes ? `about ${c.current.minutes} min` : ""].filter(Boolean).join(", ");
      return ` Up next: ${c.current.title}` + (when ? ` (${when}).` : ".");
    }
    const open = openTests(c);
    if (open.length) return ` Still open: ${names(open)}, not passed yet.`;
    return " " + (whenAllDone(c) || "Every step is done.");
  }
  function announce(prev, c) {
    // One toast per poll: a graded assessment outranks a finished step (both can land in the same poll).
    if (!prev || prev.error || c.error) return;
    const was = Object.fromEntries(prev.steps.map((s) => [s.id, s]));
    let said = false;
    tests(c).forEach((s) => {
      const a = (prev.assessments || {})[s.id], b = c.assessments[s.id];
      if (a === b || b == null) return;
      said = true;
      if (b) { toast(`${s.title} passed.` + nextText(c), "good", 15000); play("ready"); }
      else toast(`${s.title} submitted, not passed yet. Its Submit cell lists what to fix.`, "warn", 15000);
    });
    const fresh = c.steps.filter((s) => s.k && s.done && s.kind !== "assessment" && !(was[s.id] && was[s.id].done));
    if (fresh.length && !said) { const s = fresh[fresh.length - 1]; toast(`Done: ${s.title}.` + nextText(c), "good", 15000); }
  }

  // ---- the lab
  function renderLab(d) {
    const txt = labBtn.querySelector(".txt");
    labBtn.classList.remove("ready", "fail", "offline");
    if (!d) { labBtn.classList.add("offline"); txt.textContent = "Lab status offline"; labBtn.title = "The status server is not answering. Reload the page; if it stays offline, tell the instructor."; return; }
    if (!d.checked_at) { txt.textContent = "Checking the lab"; labBtn.title = "First check in progress."; return; }
    const prep = course && !course.error ? course.prep_state : null;
    const ends = session ? ` Session ends at ${new Date(session.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}${session.source === "demo" ? " (demo value)" : ""}.` : "";
    if (!d.ready) { txt.textContent = `Lab starting, ${d.passing}/${d.total}`; labBtn.title = "Waiting on: " + d.pending.join(", ") + "." + ends + " Click for details."; }
    else if (course && course.error) { txt.textContent = "Lab up"; labBtn.title = "Every service answers. Course status: " + course.error + ends; }
    else if (prep === "FAILED") { labBtn.classList.add("fail"); txt.textContent = "Preparation failed"; labBtn.title = "course-prep failed: " + (course.prep_detail || "see data/course_prep_status.json") + ". Tell the instructor." + ends; }
    else if (prep === "OK") { labBtn.classList.add("ready"); txt.textContent = "Lab ready"; labBtn.title = "Every service answers and the search videos are indexed." + ends + " Click for details."; }
    else if (prep === "RUNNING") { txt.textContent = "Lab up, indexing clips"; labBtn.title = (course.prep_detail || "course-prep is still indexing the clips") + "." + ends + " Click for details."; }
    else { txt.textContent = "Lab up, clips not prepared"; labBtn.title = "Every service answers but course-prep has not reported yet." + ends + " Click for details."; }
  }

  // ---- session clock
  let fired = null, firedKey = null;
  const clock = (s) => { s = Math.max(0, Math.ceil(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  function loadFired(expires, left) {
    firedKey = "labBadge.fired." + Math.round(expires);
    try { fired = new Set(JSON.parse(store.get(firedKey) || "[]")); } catch (e) { fired = new Set(); }
    WARNINGS.forEach((w) => { if (left < w.t - 60) fired.add(w.t); });   // warnings long past (a reload) stay silent
    store.set(firedKey, JSON.stringify([...fired]));
  }
  function tick() {
    root.classList.remove("t-ok", "t-warn", "t-crit", "t-last", "t-end");
    if (!session) { timeEl.hidden = true; return; }
    const left = session.expires_at - (Date.now() / 1000 + offset);
    if (firedKey !== "labBadge.fired." + Math.round(session.expires_at)) loadFired(session.expires_at, left);
    timeEl.hidden = false;
    timeEl.textContent = left <= 0 ? "Session ended" : left > 600 ? `${Math.ceil(left / 60)} min left` : `${clock(left)} left`;
    timeEl.title = "Time left in this session." + (session.source === "demo" ? " Demo value until the platform provides the real one." : "");
    if (session.source === "demo") { const s = document.createElement("small"); s.textContent = "demo"; timeEl.append(s); }
    root.classList.add(left <= 0 ? "t-end" : left <= 300 ? "t-crit" : left <= 600 ? "t-warn" : "t-ok");
    if (left > 0 && left <= 60) root.classList.add("t-last");
    const crossed = WARNINGS.filter((w) => left <= w.t && !fired.has(w.t));
    if (crossed.length && left > 0) {
      crossed.forEach((w) => fired.add(w.t)); store.set(firedKey, JSON.stringify([...fired]));
      const w = crossed[crossed.length - 1];
      toast(w.msg, w.level); play("alert", w.beeps);
    }
  }

  // ---- polling (every 5 s; after a failure the pause doubles, up to 40 s, so a server that is down is not hammered)
  let pollTimer = null, failures = 0, stopped = false;
  function schedule() {
    clearTimeout(pollTimer);
    if (!stopped) pollTimer = setTimeout(poll, failures ? Math.min(40000, 5000 * 2 ** failures) : 5000);
  }
  async function poll() {
    if (stopped) return;
    let d;
    try {
      const r = await fetch(statusUrl, { cache: "no-store", credentials: "same-origin" });
      if (!r.ok || !(r.headers.get("Content-Type") || "").startsWith("application/json")) throw new Error("status " + r.status);
      d = await r.json();
    } catch (e) { if (stopped) return; failures += 1; status = null; renderLab(null); schedule(); return; }
    if (stopped) return;
    failures = 0; schedule();
    status = d; offset = d.now - Date.now() / 1000; session = d.session || null;
    const before = course;
    course = d.course || { error: "the status server sent no course data" };
    const sig = JSON.stringify(course);
    if (sig !== lastSig) {                                   // redraw only when something changed (every 5 s otherwise churns the DOM)
      lastSig = sig;
      if (course.error) { q(".cb-step").textContent = "Course status unavailable"; q(".cb-title").textContent = course.error; q(".cb-here").hidden = true; tab.textContent = "Course bar"; }
      else { renderTrack(course); renderWhere(course); renderAssessments(course); if (!stepsDrop.hidden) renderSteps(course); }
    }
    announce(before, course);
    renderLab(d);
    if (d.checked_at) {
      const green = d.ready && !course.error && course.prep_state === "OK";   // the pill's own green: services up AND clips indexed
      if (lastGreen === false && green) {
        root.classList.add("pulse"); setTimeout(() => root.classList.remove("pulse"), 2600); play("ready");
        toast("Lab ready: every service answers and the search videos are indexed." + nextText(course), "good", 15000);
      } else if (lastReady === false && d.ready && !green) {
        toast("Every service answers. The search videos are still being indexed; the lab pill turns green when that finishes.", "good", 12000);
      }
      lastReady = d.ready; lastGreen = green;
    }
    tick();
  }

  // ---- show, hide, attach
  const timers = [];
  function setHidden(h) {
    hidden = h; root.hidden = h; tab.hidden = !h;
    document.body.classList.toggle("course-bar-on", !h);
    store.set("courseBar.hidden", h ? "1" : "0");
    window.dispatchEvent(new Event("resize"));              // JupyterLab re-fits its shell to the new top offset
  }
  q(".cb-hide").addEventListener("click", () => setHidden(true));
  tab.addEventListener("click", () => setHidden(false));

  let attached = false;
  function attach() {
    if (attached) return;
    attached = true;
    document.body.append(root, tab, toastEl);
    setHidden(hidden);
    poll();
    timers.push(setInterval(tick, 1000), setInterval(renderHere, 1000), setInterval(() => { if (!panelDrop.hidden) fitFrame(); }, 1000));
  }
  let shellObserver = null;
  function whenShell(cb) {
    // The page's deferred bundle builds the shell after this script runs; wait for it (up to three minutes). A page that
    // never gets a shell (an error page) gets its offset and style taken back.
    if (document.querySelector(".jp-LabShell")) return cb();
    const started = Date.now();
    shellObserver = new MutationObserver(() => {
      if (document.querySelector(".jp-LabShell")) { shellObserver.disconnect(); shellObserver = null; cb(); }
      else if (Date.now() - started > 180000) { shellObserver.disconnect(); shellObserver = null; document.body.classList.remove("course-bar-on"); styleEl.remove(); window.__courseBar = null; }
    });
    shellObserver.observe(document.body, { childList: true, subtree: true });
  }

  window.__courseBar = {
    refresh() { if (attached) poll(); },
    show() { if (attached) setHidden(false); },
    hide() { if (attached) setHidden(true); },
    stop() {
      stopped = true;
      timers.forEach(clearInterval); clearTimeout(pollTimer); clearTimeout(toastTimer);
      if (shellObserver) { shellObserver.disconnect(); shellObserver = null; }
      document.removeEventListener("keydown", onKey); document.removeEventListener("mousedown", onDown);
      document.removeEventListener("pointerdown", onFirstGesture, true);
      document.body.classList.remove("course-bar-on"); root.remove(); tab.remove(); toastEl.remove(); styleEl.remove(); window.__courseBar = null;
      window.dispatchEvent(new Event("resize"));
    },
  };
  whenShell(attach);
})();
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lab status</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  color-scheme: light;
  --bg: #F4F6F8; --ink: #17232F; --muted: #5B6B79; --line: #D3DBE2; --chip: #FFFFFF;
  --pass: #2E7D57; --pass-ink: #FFFFFF; --wait: #B87A12; --wait-bg: #FBF3E4; --crit: #B23A3A;
  --gpu: #3D6C8E;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark;
    --bg: #161F27; --ink: #E2E9EF; --muted: #93A3B1; --line: #2E3B46; --chip: #1C2731;
    --pass: #3FA673; --pass-ink: #07130D; --wait: #E0A43A; --wait-bg: #2A2416; --crit: #E06666;
    --gpu: #7FAFD1; }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--ink); font: 13px/1.4 var(--sans); }
.panel { border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }

.head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; padding: 10px 14px;
  border-bottom: 1px solid var(--line); transition: background .5s, color .5s; }
.head h1 { margin: 0; font-size: 17px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.head .big { width: 11px; height: 11px; border-radius: 50%; background: var(--wait); flex: none; }
.count { font-weight: 500; color: var(--muted); }
.meta { margin-left: auto; color: var(--muted); }
.ready .head { background: var(--pass); color: var(--pass-ink); border-color: var(--pass); }
.ready .head .big { background: var(--pass-ink); }
.ready .count, .ready .meta { color: inherit; opacity: .85; }
button { font: 500 12px var(--sans); color: inherit; background: transparent; border: 1px solid currentColor;
  border-radius: 4px; padding: 3px 9px; cursor: pointer; opacity: .8; }
button:hover { opacity: 1; }
button:focus-visible { outline: 2px solid var(--wait); outline-offset: 2px; }
button[disabled] { cursor: default; }

.group { display: grid; grid-template-columns: 96px 1fr; gap: 8px; padding: 8px 14px 0; align-items: start; }
.groups { padding-bottom: 8px; }
.label { color: var(--muted); padding-top: 3px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px 2px 7px; background: var(--chip);
  border: 1px solid var(--line); border-radius: 99px; white-space: nowrap; }
.chip.mono { font: 12px var(--mono); }
.chip .d { width: 7px; height: 7px; border-radius: 50%; background: var(--pass); }
.chip.wait { background: var(--wait-bg); border-color: var(--wait); }
.chip.wait .d { background: var(--wait); }
.chip .why { color: var(--muted); font: 11px var(--sans); }
.error { color: var(--wait); font-size: 12px; padding-top: 3px; }

.session { display: flex; flex-direction: column; gap: 4px; padding-top: 2px; }
.session .line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.session .left { font-weight: 600; font-variant-numeric: tabular-nums; }
.session .sub { color: var(--muted); font-size: 12px; }
.session .tag { font-size: 11px; color: var(--wait); border: 1px solid var(--wait); border-radius: 3px; padding: 0 5px; }
.drain { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; max-width: 520px; }
.drain i { display: block; height: 100%; background: var(--gpu); transition: width 1s linear, background .4s; }
.s-warn .left { color: var(--wait); } .s-warn .drain i { background: var(--wait); }
.s-crit .left, .s-end .left { color: var(--crit); } .s-crit .drain i { background: var(--crit); }

.hw { display: flex; flex-direction: column; gap: 6px; }
.gpus { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 0; }
.gpu { flex: 1 1 220px; max-width: 320px; padding: 6px 9px; background: var(--chip); border: 1px solid var(--line); border-radius: 5px; }
.gpu .top { display: flex; align-items: baseline; gap: 6px; }
.gpu .top b { font-weight: 600; }
.gpu .tag { margin-left: auto; font-size: 11px; color: var(--gpu); border: 1px solid var(--gpu); border-radius: 3px; padding: 0 5px; }
.meter { height: 6px; margin: 5px 0 3px; background: var(--line); border-radius: 3px; overflow: hidden; }
.meter i { display: block; height: 100%; background: var(--gpu); transition: width .6s; }
.gpu .sub { color: var(--muted); font-size: 12px; }
.link { flex: 0 0 78px; display: flex; flex-direction: column; align-items: center; color: var(--gpu); font-size: 11px; }
.link::before { content: ""; width: 100%; border-top: 3px double var(--gpu); margin-bottom: 2px; }
.link.pcie { color: var(--muted); } .link.pcie::before { border-top: 1px dashed var(--muted); }
.sys { color: var(--muted); font-size: 12px; }
@media (max-width: 520px) { .group { grid-template-columns: 1fr; gap: 4px; } .meta { margin-left: 0; } }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div class="panel" id="panel">
  <div class="head">
    <h1><span class="big"></span><span id="headline">Checking the lab</span></h1>
    <span class="count" id="count"></span>
    <span class="meta" id="meta">Waiting for the first result</span>
    <button id="alerts" type="button" hidden>Turn on alerts</button>
  </div>
  <div class="groups">
    <div class="group" id="session-row" hidden><span class="label">This session</span><div class="session" id="session"></div></div>
    <div class="group"><span class="label">Hardware</span><div class="hw"><div class="gpus" id="gpus"></div><div class="sys" id="sys"></div></div></div>
    <div id="groups"></div>
  </div>
</div>

<script>
const statusUrl = location.pathname.replace(/\/?$/, "/") + "status";
const embedded = window.self !== window.top;   // inside the bar's drop-down, the bar owns alerts
let lastReady = null, audio = null, last = null, offset = 0;
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function dur(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`; }
function clock(s) { s = Math.max(0, Math.ceil(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }

if (!embedded) {
  $("alerts").hidden = false;
  $("alerts").addEventListener("click", async () => {
    try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); await audio.resume(); } catch (e) {}
    $("alerts").textContent = "Sound on"; $("alerts").disabled = true; chime(0.12);
  });
}
function chime(vol = 0.3) {
  if (!audio) return;
  [[660, 0], [880, 0.18], [1320, 0.36]].forEach(([f, t]) => {
    const o = audio.createOscillator(), g = audio.createGain(), s = audio.currentTime + t;
    o.frequency.value = f; g.gain.setValueAtTime(0, s); g.gain.linearRampToValueAtTime(vol, s + 0.02);
    g.gain.exponentialRampToValueAtTime(0.001, s + 0.5); o.connect(g).connect(audio.destination); o.start(s); o.stop(s + 0.55);
  });
}

function renderGroups(d) {
  const root = $("groups"); root.replaceChildren();
  d.groups.forEach((g) => {
    const row = el("div", "group"); row.append(el("span", "label", g.label));
    const chips = el("div", "chips");
    g.rows.forEach((r) => {
      const c = el("span", "chip" + (g.mono ? " mono" : "") + (r.ok ? "" : " wait"));
      c.title = r.detail; c.append(el("span", "d"), el("span", null, r.name));
      if (!r.ok) c.append(el("span", "why", r.detail.replace(/^not reachable /, "")));
      chips.append(c);
    });
    row.append(chips); root.append(row);
  });
  if (d.errors && d.errors.length) {
    const row = el("div", "group"); row.append(el("span", "label", "Notes"), el("div", "error", d.errors.join(". "))); root.append(row);
  }
}

function renderSession() {
  const s = last && last.session;
  $("session-row").hidden = !s;
  if (!s) return;
  const now = Date.now() / 1000 + offset, left = s.expires_at - now;
  const box = $("session");
  box.className = "session " + (left <= 0 ? "s-end" : left <= 300 ? "s-crit" : left <= 600 ? "s-warn" : "");
  const line = el("div", "line");
  line.append(el("span", "left", left <= 0 ? "Session ended" : left > 600 ? `${Math.ceil(left / 60)} min left` : `${clock(left)} left`));
  const ends = new Date(s.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const total = s.started_at ? ` of ${dur(s.expires_at - s.started_at)}` : "";
  line.append(el("span", "sub", `Session length${total}, ends at ${ends}`));
  if (s.source === "demo") { const t = el("span", "tag", "Demo value"); t.title = "Simulated until the platform provides the real session time"; line.append(t); }
  const drain = el("div", "drain"), bar = el("i");
  const frac = s.started_at ? Math.max(0, Math.min(1, left / (s.expires_at - s.started_at))) : 1;
  bar.style.width = (frac * 100).toFixed(2) + "%"; drain.append(bar);
  box.replaceChildren(line, drain);
}


function linkLabel(t) {
  if (!t) return null;
  const m = /^NV(\d+)$/.exec(t);
  return m ? { text: `NVLink \u00d7${m[1]}`, nv: true } : { text: "PCIe", nv: false };
}
function renderHardware(hw) {
  if (!hw) return;
  const visible = hw.visible == null || hw.visible === "" ? null : hw.visible.split(",").map((x) => x.trim());
  const parts = [];
  hw.gpus.forEach((g, i) => {
    if (i > 0) {
      const l = hw.links.find((k) => k.a === i - 1 && k.b === i), lab = linkLabel(l && l.type);
      const e = el("div", "link" + (lab && lab.nv ? "" : " pcie"), lab ? lab.text : ""); if (l) e.title = `GPU ${i - 1} to GPU ${i}: ${l.type}`; parts.push(e);
    }
    const card = el("div", "gpu"), top = el("div", "top");
    top.append(el("b", null, `GPU ${g.index}`), el("span", null, g.name.replace(/^NVIDIA /, "")));
    if (visible && visible.includes(String(g.index))) { const t = el("span", "tag", "Notebook"); t.title = "CUDA_VISIBLE_DEVICES: code you run in the notebook uses this GPU"; top.append(t); }
    const pct = g.mem_total ? Math.round((100 * g.mem_used) / g.mem_total) : 0;
    const meter = el("div", "meter"), bar = el("i"); bar.style.width = pct + "%"; meter.append(bar); meter.title = `${pct}% of GPU memory allocated`;
    const gb = (v) => (v / 1024).toFixed(1);
    card.append(top, meter, el("div", "sub", `${gb(g.mem_used)} of ${gb(g.mem_total)} GB used, ${g.util ?? "?"}% busy, ${g.temp ?? "?"}\u00b0C`));
    parts.push(card);
  });
  if (!hw.gpus.length) parts.push(el("div", "sys", "No GPUs reported by nvidia-smi."));
  $("gpus").replaceChildren(...parts);
  const bits = [];
  if (hw.cpu_cores) bits.push(`${hw.cpu_cores} CPU cores` + (hw.cpu_model ? ` (${hw.cpu_model})` : ""));
  if (hw.ram_gb) bits.push(`${hw.ram_gb} GB RAM`);
  if (hw.driver) bits.push(`driver ${hw.driver}`);
  if (hw.cuda) bits.push(`CUDA ${hw.cuda}`);
  $("sys").textContent = bits.join(", ");
}

function render(d) {
  last = d; offset = d.now - Date.now() / 1000;
  $("panel").classList.toggle("ready", d.ready);
  $("headline").textContent = d.ready ? (d.has_checks ? "Ready to continue" : "Lab running") : `Waiting on ${d.total - d.passing}`;
  $("count").textContent = d.has_checks ? `${d.passing}/${d.total} checks` : "";
  const since = !d.has_checks ? `running ${dur(d.now - d.started)}` : d.ready ? `ready after ${dur(d.ready_since - d.started)}` : `monitoring ${dur(d.now - d.started)}`;
  const cc = d.container_count == null ? "" : ` ${d.container_count} containers running.`;
  $("meta").textContent = `${since}.${cc} Checked ${dur(d.now - d.checked_at)} ago.`;
  renderGroups(d); renderSession(); renderHardware(d.hardware);
  if (!embedded) document.title = d.ready ? "\u2705 Lab ready" : `\u23F3 Lab: ${d.passing}/${d.total}`;
  if (lastReady === false && d.ready) chime();
  lastReady = d.ready;
}

async function poll() {
  try { const d = await (await fetch(statusUrl, { cache: "no-store" })).json(); if (d.checked_at) render(d); }
  catch (e) { $("meta").textContent = "Lost contact with the status server. Reload the page; if it stays offline, tell the instructor."; }
}
poll(); setInterval(poll, 3000); setInterval(renderSession, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the lab_home status server (the course bar's backend).")
    ap.add_argument("--serve", action="store_true", help="run the background status server")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--interval", type=float, default=10)
    args = ap.parse_args()
    if args.serve:
        serve(args.port, args.interval)
    else:
        ap.print_help()
