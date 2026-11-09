"""
lab_home_v2.py: lab status badge, session clock, and Save HTMLs for a DLI course.

Put this file next to 00_Home_v2.ipynb. From a notebook:

    from lab_home_v2 import show_lab_badge, save_htmls
    show_lab_badge(session_length="9:00")     # badge in the bottom-right of JupyterLab, 9-hour session
    save_htmls()                              # all notebooks as HTML in one zip, with a download link

The badge shows whether the lab is ready and how much session time is left:
"2 h 45 min left" above an hour, "58 min left" below, and a "9:30 left"
(minutes:seconds) countdown in the last 10 minutes.
Click it for details (services, containers, GPUs, session); click the bell to
opt into sound alerts. Visual warnings appear at 10, 5, and 1 minute left.

Behind the badge, this same file runs as a small background web server
(`python lab_home_v2.py --serve`), started automatically and reached through
jupyter-server-proxy at <base URL>proxy/8051/.

To adapt it to another course, edit LAB_CHECKS below. Leave both lists empty for
a lab with nothing to wait on; the badge then shows "Lab running".

Session time is read on every request from the first source that exists:
  1. a JSON file at the path in $DLI_SESSION_FILE (for the platform to provide):
       {"session_started_at": "2026-10-20T20:30:00Z",
        "session_expires_at": "2026-10-21T05:30:00Z"}
  2. the clock started by show_lab_badge(session_length=...), marked "demo" on screen
  3. $DLI_SESSION_EXPIRES_AT and $DLI_SESSION_STARTED_AT
Times are ISO 8601 UTC or epoch seconds. An absolute expiry stays correct
whenever it is read; a file the platform rewrites also supports extensions.
"""

# ================================================================ course settings
# What "ready" means for this lab. An HTTP check passes when the status code is
# in ok_status (default [200]) and, if json_field is set, that field's value is
# in ok_values. Use "path" (joined to base_url) or a full "url". A container
# passes when it is running and its health check, if any, is healthy.

LAB_CHECKS = {
    "base_url": "http://nginx",
    "http_checks": [
        {"name": "RT-CV",         "path": "/rtvi-cv/api/v1/ready"},
        {"name": "RT-VLM",        "path": "/rtvi-vlm/v1/health/ready"},
        {"name": "RT-Embed",      "path": "/rtvi-embed/v1/ready"},
        {"name": "VIOS",          "path": "/vios/vst/api/v1/sensor/list"},
        {"name": "Elasticsearch", "path": "/es/_cluster/health", "json_field": "status", "ok_values": ["green", "yellow"]},
        {"name": "Kibana",        "path": "/kibana/", "ok_status": [200, 301, 302]},
        {"name": "VSS agent",     "path": "/agent/health"},
    ],
    "containers": [
        "vss-agent", "vss-rtvi-cv", "vss-rtvi-vlm", "vss-rtvi-embed",
        "vss-vios-ingress", "vss-vios-sensor", "vss-vios-streamprocessing",
        "elasticsearch", "kafka", "logstash", "vss-behavior-analytics",
    ],
}

PORT = 8051
HOME_NOTEBOOKS = {"00_Home.ipynb", "00_Home_v2.ipynb"}     # left out of the Save HTMLs bundle

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
DEMO_SESSION_FILE = Path(tempfile.gettempdir()) / "lab_home_v2_session_demo.json"
HTTP_TIMEOUT = 6
APP = "lab_home_v2"


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
        with self.lock:
            snap = dict(self.snapshot or {"checked_at": None})
            snap.update(app=APP, build=self.build, pid=os.getpid(), started=self.started,
                        ready_since=self.ready_since, interval=self.interval,
                        now=time.time(), session=read_session())
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
    print(f"lab_home_v2 monitor on port {port}. Ctrl+C to stop.", flush=True)
    try:
        monitor.loop()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- badge (notebook side)

def _server_info(port: int):
    """Return the running server's /status as a dict, {} if it is not lab_home_v2, None if nothing is listening."""
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
    if info:                                   # our server, but this file has changed since
        try:
            os.kill(info["pid"], 15)
        except (OSError, KeyError):
            pass
        for _ in range(20):
            if _server_info(port) is None:
                break
            time.sleep(0.25)
    elif info == {}:
        print(f"Port {port} is in use by another program (perhaps an older monitor script).\n"
              f"Stop it in a terminal, for example with `pkill -f vss_monitor.py`, then rerun this cell.")
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


def _hours(value, name="value"):
    """A duration in hours from "9:00" (h:mm), "9h", "8h 30m", "45m", 9, or 8.5. None stays None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if m := re.fullmatch(r"(\d+):([0-5]\d)", s):
        return int(m.group(1)) + int(m.group(2)) / 60
    if m := re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?", s):
        if m.group(1) or m.group(2):
            return float(m.group(1) or 0) + int(m.group(2) or 0) / 60
    try:
        return float(s)
    except ValueError:
        raise ValueError(f'{name}={value!r}: use hours and minutes like "9:00" or "8h 30m", or a number of hours like 8.5') from None


def _container_started():
    """When this container's first process started, in epoch seconds, or None if unknown."""
    try:
        btime = next(int(line.split()[1]) for line in open("/proc/stat") if line.startswith("btime"))
        ticks = int(Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19])
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def _write_demo_session(length_hours) -> None:
    """Start (or keep) the session clock the server reads; remove it when length_hours is None.

    The clock starts the first time the badge cell runs in a lab and keeps running when
    the cell is rerun. It starts over if the saved clock is older than this container
    (a restarted lab) or has already run out."""
    if length_hours is None:
        DEMO_SESSION_FILE.unlink(missing_ok=True)
        return
    now, length = time.time(), length_hours * 3600
    try:
        started = _ts(json.loads(DEMO_SESSION_FILE.read_text()).get("session_started_at"))
    except (OSError, ValueError, AttributeError):
        started = None
    boot = _container_started()
    if not started or started + length <= now or (boot and started < boot):
        started = now
    DEMO_SESSION_FILE.write_text(json.dumps({"source": "demo", "session_started_at": _iso(started),
                                             "session_expires_at": _iso(started + length)}))


def show_lab_badge(session_length: str | float | None = None, *,
                   port: int = PORT, proxy_path: str | None = None) -> None:
    """Show the lab status badge, starting the background server if needed.

    session_length: how long a session lasts, as "9:00" (h:mm), "9h", "8h 30m", or a
        number of hours such as 8.5. The clock starts the first time this runs in the
        lab and keeps running when the cell is rerun. It is marked "demo" on screen
        until the platform provides the real session time. Leave it out to use only
        the platform's value.
    """
    from IPython.display import Javascript, display
    _write_demo_session(_hours(session_length, "session_length"))
    if not _ensure_server(port):
        return
    display(Javascript(_JS.replace("__PORT__", str(port)).replace("__PROXY_PATH__", json.dumps(proxy_path))))


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
        mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
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
                       if ".ipynb_checkpoints" not in p.parts and p.name not in HOME_NOTEBOOKS)
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


# ---------------------------------------------------------------- badge script and panel page

_JS = r"""
(function () {
  // Remove any earlier badge, including ones left by older versions of this script.
  const prev = window.__labBadgeV2;
  if (prev) {
    if (typeof prev.stop === "function") prev.stop();
    else { [prev.timer, prev.fitTimer].forEach((t) => t && clearInterval(t)); if (prev.root) prev.root.remove(); }
  }
  document.querySelectorAll("#lab-badge-v2").forEach((n) => n.remove());
  window.__labBadgeV2 = null;

  let base = "/";
  try { base = JSON.parse(document.getElementById("jupyter-config-data").textContent).baseUrl || "/"; } catch (e) {}
  const panelUrl = __PROXY_PATH__ || (base.replace(/\/?$/, "/") + "proxy/__PORT__/");
  const statusUrl = panelUrl + "status";

  const WARNINGS = [
    { t: 600, beeps: 1, level: "warn", msg: "10 minutes left in this session. Save a copy of your notebooks now with Save HTMLs on the Home page." },
    { t: 300, beeps: 2, level: "crit", msg: "5 minutes left in this session. Run Save HTMLs on the Home page now if you have not already." },
    { t: 60,  beeps: 3, level: "crit", msg: "1 minute left. This session is about to end." },
  ];

  const css = `
    #lab-badge-v2 { position: fixed; right: 14px; bottom: 34px; z-index: 99999; display: flex; flex-direction: column;
      align-items: flex-end; gap: 8px; font: 500 12.5px "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
      --b-bg: #FFFFFF; --b-ink: #17232F; --b-line: #CBD5DD; --b-muted: #5B6B79;
      --b-pass: #2E7D57; --b-wait: #B87A12; --b-crit: #B23A3A; --b-link: #3D6C8E; }
    @media (prefers-color-scheme: dark) { #lab-badge-v2 { --b-bg: #1C2731; --b-ink: #E2E9EF; --b-line: #2E3B46; --b-muted: #93A3B1;
      --b-pass: #3FA673; --b-wait: #E0A43A; --b-crit: #E06666; --b-link: #7FAFD1; } }
    #lab-badge-v2 [hidden] { display: none !important; }
    #lab-badge-v2 .row { display: flex; box-shadow: 0 1px 3px rgba(23,35,47,.2); border-radius: 99px; }
    #lab-badge-v2 .row button { font: inherit; color: var(--b-ink); background: var(--b-bg); border: 1px solid var(--b-line); cursor: pointer; }
    #lab-badge-v2 .row button:focus-visible { outline: 2px solid var(--b-wait); outline-offset: 2px; }
    #lab-badge-v2 .pill { display: flex; align-items: center; gap: 7px; padding: 4px 6px 4px 10px; border-radius: 99px 0 0 99px; }
    #lab-badge-v2 .bell { display: flex; align-items: center; padding: 4px 8px; border-left: 0 !important; border-radius: 0; }
    #lab-badge-v2 .close { display: flex; align-items: center; padding: 4px 10px 4px 7px; border-left: 0 !important; border-radius: 0 99px 99px 0; }
    #lab-badge-v2 .close svg { opacity: .7; } #lab-badge-v2 .close:hover svg { opacity: 1; }
    #lab-badge-v2 .bell .slash { display: inline; } #lab-badge-v2 .bell[aria-pressed="true"] .slash { display: none; }
    #lab-badge-v2 .bell[aria-pressed="false"] { color: var(--b-muted); }
    #lab-badge-v2 .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--b-wait); flex: none; }
    #lab-badge-v2.ready .pill, #lab-badge-v2.ready .bell, #lab-badge-v2.ready .close { background: var(--b-pass); border-color: var(--b-pass); color: #FFFFFF; }
    #lab-badge-v2.ready .bell[aria-pressed="false"] { color: rgba(255,255,255,.75); }
    #lab-badge-v2.ready .dot { background: #FFFFFF; }
    #lab-badge-v2.offline .dot { background: #8A98A5; }
    #lab-badge-v2 .time { padding: 1px 8px; border-radius: 99px; font-variant-numeric: tabular-nums; border: 1px solid transparent;
      border-left-color: currentColor; border-radius: 0; opacity: .95; }
    #lab-badge-v2 .time small { font-size: 10.5px; opacity: .75; margin-left: 4px; }
    #lab-badge-v2.t-warn .time, #lab-badge-v2.t-crit .time, #lab-badge-v2.t-end .time { border-radius: 99px; color: #FFFFFF; border-color: transparent; }
    #lab-badge-v2.t-warn .time { background: var(--b-wait); }
    #lab-badge-v2.t-crit .time, #lab-badge-v2.t-end .time { background: var(--b-crit); }
    #lab-badge-v2.t-last .time { animation: labblink 1s ease-in-out infinite; }
    @keyframes labblink { 50% { opacity: .55; } }
    #lab-badge-v2.pulse .row { animation: labpulse 1.2s ease-out 2; }
    @keyframes labpulse { 0% { box-shadow: 0 0 0 0 rgba(46,125,87,.6); } 100% { box-shadow: 0 0 0 14px rgba(46,125,87,0); } }
    #lab-badge-v2 .toast { max-width: 340px; display: flex; gap: 10px; align-items: flex-start; padding: 9px 11px; border-radius: 6px;
      background: var(--b-bg); color: var(--b-ink); border: 1px solid var(--b-wait); border-left-width: 4px; box-shadow: 0 4px 16px rgba(23,35,47,.22); }
    #lab-badge-v2 .toast.crit { border-color: var(--b-crit); }
    #lab-badge-v2 .toast p { margin: 0; font-weight: 400; line-height: 1.4; }
    #lab-badge-v2 .toast button { flex: none; font: inherit; font-size: 12px; color: var(--b-link); background: none; border: 0; padding: 0; cursor: pointer; text-decoration: underline; }
    #lab-badge-v2 .panel { width: min(280px, calc(100vw - 28px)); background: var(--b-bg); border: 1px solid var(--b-line); border-radius: 8px;
      overflow: hidden; box-shadow: 0 8px 28px rgba(23,35,47,.22); }
    #lab-badge-v2 iframe { display: block; width: 100%; height: 360px; border: 0; }
    @media (prefers-reduced-motion: reduce) { #lab-badge-v2 .row, #lab-badge-v2 .time { animation: none !important; } }
  `;
  const bellSvg = `<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6z"/><path d="M10 19a2 2 0 0 0 4 0"/><line class="slash" x1="4" y1="4" x2="20" y2="20"/></svg>`;

  const closeSvg = `<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
    <path d="M6 6l12 12M18 6L6 18"/></svg>`;

  const root = document.createElement("div");
  root.id = "lab-badge-v2";
  root.innerHTML = `<style>${css}</style>
    <div class="toast" role="status" aria-live="polite" hidden><p></p><button type="button">Dismiss</button></div>
    <div class="panel" hidden></div>
    <div class="row">
      <button class="pill" type="button" aria-expanded="false"><span class="dot"></span><span class="txt">Lab status</span><span class="time" hidden></span></button>
      <button class="bell" type="button" aria-pressed="false">${bellSvg}</button>
      <button class="close" type="button" aria-label="Close the badge" title="Close the badge. Run the dashboard cell on the Home page to bring it back.">${closeSvg}</button>
    </div>`;
  document.body.appendChild(root);

  const q = (s) => root.querySelector(s);
  const panel = q(".panel"), pill = q(".pill"), txt = q(".txt"), timeEl = q(".time"), bell = q(".bell"), toast = q(".toast");

  // ---- storage (per browser; failures are harmless)
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };

  // ---- sound (opt-in)
  let audio = null;
  let soundOn = store.get("labBadgeV2.sound") === "on";
  function unlockAudio() {
    try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); audio.resume(); } catch (e) {}
  }
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
    bell.title = soundOn ? "Sound alerts on. Click to turn off." : "Sound alerts off. Click to hear a chime when the lab is ready and at 10, 5, and 1 minute before the session ends.";
    bell.setAttribute("aria-label", soundOn ? "Sound alerts on" : "Sound alerts off");
  }
  bell.addEventListener("click", () => {
    soundOn = !soundOn; store.set("labBadgeV2.sound", soundOn ? "on" : "off");
    if (soundOn) { unlockAudio(); setTimeout(() => play("alert", 1), 50); }
    renderBell();
  });
  // Browsers need a click before sound can play; after a reload, any click re-enables it.
  const onFirstGesture = () => { if (soundOn) unlockAudio(); document.removeEventListener("pointerdown", onFirstGesture, true); };
  document.addEventListener("pointerdown", onFirstGesture, true);
  renderBell();

  // ---- pop-over panel: a narrow column above the badge, as tall as its content allows.
  // The page inside it has its own open-in-new-tab and close buttons; it reports its
  // height and asks to close through postMessage.
  function fitFrame(h) {
    const f = panel.querySelector("iframe");
    if (!f) return;
    if (!h) { try { h = f.contentDocument.body.scrollHeight; } catch (e) {} }
    if (h > 0) f.style.height = Math.min(h, Math.max(200, window.innerHeight - 120)) + "px";
  }
  function setOpen(open) {
    if (open && !panel.querySelector("iframe")) {
      const f = document.createElement("iframe"); f.src = panelUrl; f.title = "Lab status";
      f.addEventListener("load", () => fitFrame()); panel.appendChild(f);
    }
    panel.hidden = !open; pill.setAttribute("aria-expanded", String(open));
  }
  pill.addEventListener("click", () => setOpen(panel.hidden));
  const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
  const onDown = (e) => { if (!root.contains(e.target)) setOpen(false); };
  const onMsg = (e) => {
    const f = panel.querySelector("iframe");
    if (!f || e.source !== f.contentWindow || !e.data || typeof e.data !== "object") return;
    if (e.data.labPanel === "close") { setOpen(false); pill.focus(); }
    else if (e.data.labPanel === "size") fitFrame(Number(e.data.height));
  };
  document.addEventListener("keydown", onKey);
  document.addEventListener("mousedown", onDown);
  window.addEventListener("message", onMsg);

  // ---- toast
  function showToast(msg, level) {
    toast.className = "toast " + level; toast.querySelector("p").textContent = msg; toast.hidden = false;
  }
  toast.querySelector("button").addEventListener("click", () => { toast.hidden = true; });

  // ---- session clock
  let session = null, offset = 0, fired = null, firedKey = null;
  const clock = (s) => { s = Math.max(0, Math.ceil(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  // "2 h 45 min left" above an hour, "58 min left" below, "9:30 left" (m:ss) in the last 10 minutes.
  function fmtLeft(s) {
    if (s <= 0) return "Session ended";
    if (s <= 600) return `${clock(s)} left`;
    const t = Math.ceil(s / 60), h = Math.floor(t / 60), m = t % 60;
    return h ? (m ? `${h} h ${m} min left` : `${h} h left`) : `${t} min left`;
  }

  function loadFired(expires, left) {
    firedKey = "labBadgeV2.fired." + Math.round(expires);
    try { fired = new Set(JSON.parse(store.get(firedKey) || "[]")); } catch (e) { fired = new Set(); }
    // Warnings that passed more than a minute ago (for example, before a page reload) stay silent.
    WARNINGS.forEach((w) => { if (left < w.t - 60) fired.add(w.t); });
    store.set(firedKey, JSON.stringify([...fired]));
  }

  function tick() {
    root.classList.remove("t-ok", "t-warn", "t-crit", "t-last", "t-end");
    if (!session) { timeEl.hidden = true; return; }
    const left = session.expires_at - (Date.now() / 1000 + offset);
    if (firedKey !== "labBadgeV2.fired." + Math.round(session.expires_at)) loadFired(session.expires_at, left);
    timeEl.hidden = false;
    timeEl.textContent = fmtLeft(left);
    if (session.source === "demo") { const s = document.createElement("small"); s.textContent = "demo"; timeEl.append(s); }
    root.classList.add(left <= 0 ? "t-end" : left <= 300 ? "t-crit" : left <= 600 ? "t-warn" : "t-ok");
    if (left > 0 && left <= 60) root.classList.add("t-last");

    // Fire the most urgent warning crossed since the last tick (background tabs tick slowly).
    const crossed = WARNINGS.filter((w) => left <= w.t && !fired.has(w.t));
    if (crossed.length && left > 0) {
      crossed.forEach((w) => fired.add(w.t)); store.set(firedKey, JSON.stringify([...fired]));
      const w = crossed[crossed.length - 1];
      showToast(w.msg, w.level); play("alert", w.beeps);
    }
  }

  // ---- status polling
  let lastReady = null;
  async function poll() {
    try {
      const d = await (await fetch(statusUrl, { cache: "no-store", credentials: "same-origin" })).json();
      offset = d.now - Date.now() / 1000;
      session = d.session || null;
      root.classList.remove("offline");
      if (!d.checked_at) { txt.textContent = "Checking"; tick(); return; }
      root.classList.toggle("ready", d.ready);
      txt.textContent = d.ready ? "Lab ready" : `Lab starting, ${d.passing}/${d.total}`;
      const when = session ? ` Session ends at ${new Date(session.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}${session.source === "demo" ? " (demo value)" : ""}.` : "";
      pill.title = (d.ready ? "All checks passed." : "Waiting on: " + d.pending.join(", ") + ".") + when + " Click for details.";
      if (lastReady === false && d.ready) {
        root.classList.add("pulse"); setTimeout(() => root.classList.remove("pulse"), 2600); play("ready");
      }
      lastReady = d.ready;
      tick();
    } catch (e) {
      root.classList.remove("ready"); root.classList.add("offline");
      txt.textContent = "Lab status offline"; pill.title = "The monitor is not responding. Rerun the dashboard cell on the Home page.";
    }
  }

  poll();
  const timers = [setInterval(poll, 5000), setInterval(tick, 1000), setInterval(() => { if (!panel.hidden) fitFrame(); }, 1000)];
  q(".close").addEventListener("click", () => window.__labBadgeV2 && window.__labBadgeV2.stop());
  window.__labBadgeV2 = {
    stop() {
      timers.forEach(clearInterval);
      document.removeEventListener("keydown", onKey); document.removeEventListener("mousedown", onDown);
      window.removeEventListener("message", onMsg);
      document.removeEventListener("pointerdown", onFirstGesture, true);
      root.remove(); window.__labBadgeV2 = null;
    },
  };
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
  --page: #F4F6F8; --bg: #FFFFFF; --ink: #17232F; --muted: #5B6B79; --line: #D3DBE2;
  --pass: #2E7D57; --pass-ink: #FFFFFF; --wait: #B87A12; --wait-bg: #FBF3E4; --crit: #B23A3A;
  --gpu: #3D6C8E;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark;
    --page: #161F27; --bg: #1C2731; --ink: #E2E9EF; --muted: #93A3B1; --line: #2E3B46;
    --pass: #3FA673; --pass-ink: #07130D; --wait: #E0A43A; --wait-bg: #2A2416; --crit: #E06666;
    --gpu: #7FAFD1; }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { margin: 0; }
body { background: var(--page); color: var(--ink); font: 13px/1.4 var(--sans); }
body.embedded { background: var(--bg); }
.panel { background: var(--bg); }
body:not(.embedded) .panel { max-width: 360px; margin: 24px auto; border: 1px solid var(--line); border-radius: 8px; overflow: clip; }

/* header: status, counts, and (in the pop-over) open-in-tab and close */
.head { position: sticky; top: 0; z-index: 1; padding: 9px 8px 9px 12px; background: var(--bg);
  border-bottom: 1px solid var(--line); transition: background .5s, color .5s; }
.head .t { display: flex; align-items: center; gap: 7px; min-height: 22px; }
.head h1 { flex: 1; margin: 0; font-size: 14px; font-weight: 600; }
.head .big { width: 9px; height: 9px; border-radius: 50%; background: var(--wait); flex: none; }
.head .s { margin-top: 1px; padding-left: 16px; font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
.ready .head { background: var(--pass); color: var(--pass-ink); border-color: var(--pass); }
.ready .head .big { background: var(--pass-ink); }
.ready .head .s { color: inherit; opacity: .85; }
.ic { width: 22px; height: 22px; display: grid; place-items: center; flex: none; padding: 0; color: inherit;
  background: none; border: 0; border-radius: 4px; cursor: pointer; opacity: .75; }
.ic:hover { opacity: 1; background: rgba(127,127,127,.18); }
.txtbtn { flex: none; font: 500 11.5px var(--sans); color: inherit; background: transparent; border: 1px solid currentColor;
  border-radius: 4px; padding: 1px 7px; cursor: pointer; opacity: .8; }
.txtbtn:hover { opacity: 1; }
.txtbtn[disabled] { cursor: default; }
.ic:focus-visible, .txtbtn:focus-visible, .grp-h:focus-visible { outline: 2px solid var(--wait); outline-offset: 1px; }

/* sections */
.sec { padding: 10px 12px 11px; }
.sec + .sec { border-top: 1px solid var(--line); }
.lbl { display: flex; align-items: center; gap: 6px; margin-bottom: 5px; font: 10.5px var(--mono);
  letter-spacing: .07em; text-transform: uppercase; color: var(--muted); }
.tag { margin-left: auto; padding: 0 5px; font: 10.5px/1.5 var(--sans); letter-spacing: 0; text-transform: none;
  border: 1px solid currentColor; border-radius: 3px; }
.tag.demo { color: var(--wait); }
.tag.nb { color: var(--gpu); }
.sub { font-size: 11.5px; color: var(--muted); }

/* session */
.num { font-size: 20px; font-weight: 600; line-height: 1.15; font-variant-numeric: tabular-nums; }
.drain, .meter { height: 4px; background: var(--line); border-radius: 2px; overflow: hidden; }
.drain { margin: 7px 0 5px; }
.drain i, .meter i { display: block; height: 100%; background: var(--gpu); transition: width .6s, background .4s; }
.s-warn .num { color: var(--wait); } .s-warn .drain i { background: var(--wait); }
.s-crit .num, .s-end .num { color: var(--crit); } .s-crit .drain i { background: var(--crit); }
.note { margin-top: 7px; padding: 6px 8px; font-size: 11.5px; background: var(--wait-bg);
  border-left: 3px solid var(--wait); border-radius: 0 3px 3px 0; }
.s-crit .note { border-left-color: var(--crit); }

/* hardware */
.gpu .top { display: flex; align-items: baseline; gap: 6px; font-size: 12.5px; }
.gpu .top b { font-weight: 600; }
.meter { margin: 5px 0 3px; }
.gpu .sub { display: flex; justify-content: space-between; gap: 8px; font-variant-numeric: tabular-nums; }
.link { display: flex; align-items: center; gap: 8px; height: 22px; margin: 4px 0 4px 3px; font-size: 10.5px; color: var(--gpu); }
.link::before { content: ""; align-self: stretch; border-left: 3px double var(--gpu); }
.link.pcie { color: var(--muted); }
.link.pcie::before { border-left: 1px dashed var(--muted); margin: 0 1px; }
.sys { margin-top: 9px; font-size: 11.5px; color: var(--muted); line-height: 1.5; }

/* checks: one line per group, failing items always shown */
.grp-h { display: flex; align-items: center; gap: 7px; width: 100%; padding: 4px 0; font: 500 12.5px var(--sans);
  color: inherit; background: none; border: 0; border-radius: 3px; cursor: pointer; text-align: left; }
.grp-h .n { flex: 1; }
.grp-h .c { font-size: 11.5px; color: var(--pass); font-variant-numeric: tabular-nums; }
.grp-h .c.bad { color: var(--wait); }
.grp-h .chev { flex: none; opacity: .55; transition: transform .15s; }
.grp.open .chev { transform: rotate(90deg); }
.items { list-style: none; margin: 1px 0 5px; padding: 0 0 0 2px; }
.items li { display: flex; flex-wrap: wrap; align-items: center; gap: 0 8px; padding: 2px 0; font-size: 12px; }
.items.mono li { font: 11.5px var(--mono); }
.items .d { width: 6px; height: 6px; border-radius: 50%; background: var(--pass); flex: none; }
.items li.w .d { background: var(--wait); }
.items .why { flex-basis: 100%; padding-left: 14px; font: 11px var(--sans); color: var(--wait); }
.more { padding: 1px 0 4px 16px; font-size: 11.5px; color: var(--muted); }
.error { margin-top: 4px; font-size: 11.5px; color: var(--wait); }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div class="panel" id="panel">
  <div class="head">
    <div class="t">
      <span class="big"></span><h1 id="headline">Checking the lab</h1>
      <button class="txtbtn" id="alerts" type="button" hidden>Turn on alerts</button>
      <a class="ic" id="newtab" target="_blank" rel="noopener" title="Open in new tab" aria-label="Open in new tab" hidden><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a>
      <button class="ic" id="close" type="button" title="Close" aria-label="Close" hidden><svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </div>
    <div class="s" id="meta">Waiting for the first result</div>
  </div>
  <section class="sec" id="session-sec" hidden></section>
  <section class="sec" id="hw-sec" hidden></section>
  <section class="sec" id="checks-sec" hidden><div class="lbl">Checks</div><div id="groups"></div></section>
</div>

<script>
const statusUrl = location.pathname.replace(/\/?$/, "/") + "status";
const embedded = window.self !== window.top;   // inside the badge pop-over, the badge owns alerts and closing
let lastReady = null, audio = null, last = null, offset = 0;
const openGroups = {};
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function dur(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`; }
function clock(s) { s = Math.max(0, Math.ceil(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function hm(s) { s = Math.max(0, Math.round(s / 60)); const h = Math.floor(s / 60), m = s % 60;
  return h && m ? `${h} h ${m} min` : h ? `${h} h` : `${m} min`; }
// Same as the badge: "2 h 45 min left", "58 min left", then "9:30 left" (m:ss) in the last 10 minutes.
function fmtLeft(s) {
  if (s <= 0) return "Session ended";
  if (s <= 600) return `${clock(s)} left`;
  const t = Math.ceil(s / 60), h = Math.floor(t / 60), m = t % 60;
  return h ? (m ? `${h} h ${m} min left` : `${h} h left`) : `${t} min left`;
}
const CHEV = '<svg class="chev" viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>';

// ---- talking to the badge
function tell(data) { if (embedded) { try { window.parent.postMessage(data, "*"); } catch (e) {} } }
function sendSize() { tell({ labPanel: "size", height: document.body.scrollHeight }); }

if (embedded) {
  document.body.classList.add("embedded");
  $("newtab").href = location.href; $("newtab").hidden = false;
  $("close").hidden = false;
  $("close").addEventListener("click", () => tell({ labPanel: "close" }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") tell({ labPanel: "close" }); });
} else {
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

// ---- building blocks
function label(text, tag) { const l = el("div", "lbl", text); if (tag) l.append(tag); return l; }
function demoTag(source, what) {
  if (source !== "demo") return null;
  const t = el("span", "tag demo", "Demo"); t.title = `Simulated until the platform provides the real ${what}`; return t;
}
function drain(frac) {
  const d = el("div", "drain"), i = el("i");
  i.style.width = (Math.max(0, Math.min(1, frac)) * 100).toFixed(2) + "%"; d.append(i); return d;
}

// ---- checks
function renderGroups(d) {
  const sec = $("checks-sec"), root = $("groups");
  const errors = d.errors || [];
  sec.hidden = !d.groups.length && !errors.length;
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.g : null;
  root.replaceChildren();
  d.groups.forEach((g) => {
    const failing = g.rows.filter((r) => !r.ok), okCount = g.rows.length - failing.length;
    const open = !!openGroups[g.label];
    const box = el("div", "grp" + (open ? " open" : ""));
    const h = el("button", "grp-h"); h.type = "button"; h.dataset.g = g.label;
    h.setAttribute("aria-expanded", String(open));
    h.title = open ? `Hide the full list of ${g.label.toLowerCase()}` : `Show all ${g.label.toLowerCase()}`;
    h.innerHTML = CHEV;
    h.append(el("span", "n", g.label),
      el("span", "c" + (failing.length ? " bad" : ""), `${okCount}/${g.rows.length}` + (failing.length ? "" : " ✓")));
    h.addEventListener("click", () => { openGroups[g.label] = !open; renderGroups(last); sendSize(); });
    box.append(h);
    const shown = open ? [...failing, ...g.rows.filter((r) => r.ok)] : failing;
    if (shown.length) {
      const ul = el("ul", "items" + (g.mono ? " mono" : ""));
      shown.forEach((r) => {
        const li = el("li", r.ok ? null : "w"); li.title = r.detail;
        li.append(el("span", "d"), el("span", null, r.name));
        if (!r.ok) li.append(el("span", "why", r.detail));
        ul.append(li);
      });
      box.append(ul);
    }
    if (!open && failing.length && okCount) box.append(el("div", "more", `${okCount} other${okCount === 1 ? "" : "s"} ready`));
    root.append(box);
  });
  if (errors.length) root.append(el("div", "error", errors.join(". ")));
  if (focused) { const b = [...root.querySelectorAll(".grp-h")].find((x) => x.dataset.g === focused); if (b) b.focus(); }
}

// ---- session
function renderSession() {
  const s = last && last.session, sec = $("session-sec");
  sec.hidden = !s;
  if (!s) return;
  const now = Date.now() / 1000 + offset, left = s.expires_at - now;
  sec.className = "sec " + (left <= 0 ? "s-end" : left <= 300 ? "s-crit" : left <= 600 ? "s-warn" : "");
  const ends = new Date(s.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const parts = [
    label("This session", demoTag(s.source, "session time")),
    el("div", "num", fmtLeft(left)),
    drain(s.started_at ? left / (s.expires_at - s.started_at) : 1),
    el("div", "sub", `${left <= 0 ? "Ended" : "Ends"} at ${ends}` + (s.started_at ? ` · ${hm(s.expires_at - s.started_at)} session` : "")),
  ];
  if (left > 0 && left <= 600) parts.push(el("div", "note", "Save a copy of your notebooks now with Save HTMLs on the Home page."));
  sec.replaceChildren(...parts);
}

// ---- hardware
function linkLabel(t) {
  if (!t) return null;
  const m = /^NV(\d+)$/.exec(t);
  return m ? { text: `NVLink ×${m[1]}`, nv: true } : { text: "PCIe", nv: false };
}
function renderHardware(hw) {
  const sec = $("hw-sec");
  sec.hidden = !hw;
  if (!hw) return;
  const visible = hw.visible == null || hw.visible === "" ? null : hw.visible.split(",").map((x) => x.trim());
  const gb = (v) => (v / 1024).toFixed(1);
  const parts = [label("Hardware")];
  hw.gpus.forEach((g, i) => {
    if (i > 0) {
      const l = hw.links.find((k) => k.a === i - 1 && k.b === i), lab = linkLabel(l && l.type);
      const e = el("div", "link" + (lab && lab.nv ? "" : " pcie"), lab ? lab.text : "");
      if (l) e.title = `GPU ${i - 1} to GPU ${i}: ${l.type}`;
      parts.push(e);
    }
    const card = el("div", "gpu"), top = el("div", "top");
    top.append(el("b", null, `GPU ${g.index}`), el("span", null, g.name.replace(/^NVIDIA /, "")));
    if (visible && visible.includes(String(g.index))) {
      const t = el("span", "tag nb", "Notebook"); t.title = "CUDA_VISIBLE_DEVICES: code you run in the notebook uses this GPU"; top.append(t);
    }
    const pct = g.mem_total ? Math.round((100 * g.mem_used) / g.mem_total) : 0;
    const meter = el("div", "meter"), bar = el("i"); bar.style.width = pct + "%"; meter.append(bar);
    meter.title = `${pct}% of GPU memory allocated`;
    const sub = el("div", "sub");
    sub.append(el("span", null, `${gb(g.mem_used)} / ${gb(g.mem_total)} GB`),
      el("span", null, `${g.util ?? "?"}% busy · ${g.temp ?? "?"}°C`));
    card.append(top, meter, sub);
    parts.push(card);
  });
  if (!hw.gpus.length) parts.push(el("div", "sub", "No GPUs reported by nvidia-smi."));
  const host = [], soft = [];
  if (hw.cpu_cores) host.push(`${hw.cpu_cores} CPU cores`);
  if (hw.ram_gb) host.push(`${hw.ram_gb} GB RAM`);
  if (hw.driver) soft.push(`Driver ${hw.driver}`);
  if (hw.cuda) soft.push(`CUDA ${hw.cuda}`);
  if (host.length || soft.length) {
    const sys = el("div", "sys"); if (hw.cpu_model) sys.title = hw.cpu_model;
    [host, soft].forEach((x) => { if (x.length) sys.append(el("div", null, x.join(" · "))); });
    parts.push(sys);
  }
  sec.replaceChildren(...parts);
}

// ---- everything
function render(d) {
  last = d; offset = d.now - Date.now() / 1000;
  $("panel").classList.toggle("ready", d.ready);
  $("headline").textContent = d.ready ? (d.has_checks ? "Ready to continue" : "Lab running") : `Waiting on ${d.total - d.passing}`;
  const bits = [];
  if (d.has_checks) bits.push(`${d.passing}/${d.total} checks`);
  if (d.container_count != null) bits.push(`${d.container_count} containers`);
  bits.push(`${dur(d.now - d.checked_at)} ago`);
  $("meta").textContent = bits.join(" · ");
  const since = !d.has_checks ? `Running for ${dur(d.now - d.started)}` : d.ready ? `Ready after ${dur(d.ready_since - d.started)}` : `Monitoring for ${dur(d.now - d.started)}`;
  $("meta").title = `${since}. Last checked ${dur(d.now - d.checked_at)} ago.`;
  renderSession(); renderHardware(d.hardware); renderGroups(d);
  if (!embedded) document.title = d.ready ? "✅ Lab ready" : `⏳ Lab: ${d.passing}/${d.total}`;
  if (lastReady === false && d.ready) chime();
  lastReady = d.ready;
  sendSize();
}

async function poll() {
  try { const d = await (await fetch(statusUrl, { cache: "no-store" })).json(); if (d.checked_at) render(d); }
  catch (e) { $("meta").textContent = "Lost contact with the monitor. Rerun the dashboard cell on the Home page."; sendSize(); }
}
poll(); setInterval(poll, 3000); setInterval(() => { renderSession(); sendSize(); }, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the lab_home_v2 status server.")
    ap.add_argument("--serve", action="store_true", help="run the background status server")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--interval", type=float, default=10)
    args = ap.parse_args()
    if args.serve:
        serve(args.port, args.interval)
    else:
        ap.print_help()
