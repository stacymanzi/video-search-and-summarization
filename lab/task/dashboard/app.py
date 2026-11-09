#!/usr/bin/env python3
"""Collision clips: a starter page for the coding agent to build out.

Run it:      python3 /dli/task/dashboard/app.py
Then open:   http://<your-lab-host>/dashboard/

It is the starting point of the optional dashboard task of VSS Coding Agent
Skills, a page in Course Docs. It reads /dli/task/work/agent/collisions.json,
a list of windows like this one:

    [{"file": "rain_eval.mp4", "start_s": 20.0, "end_s": 30.0, "description": "..."}]

and lists one row per window. Each "watch" link asks VIOS for that window of
the video and sends your browser to the clip through the course proxy.
Nothing in the course writes collisions.json for you: until it exists, the
page lists no windows. In the optional task, the coding agent rewrites this
file into a real dashboard from the files it keeps in /dli/task/work/agent/
(agent_sources.json, search_selection.json, live_source.json and
live_alert.json).

Standard library only, so there is nothing to install.
"""

import html
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

PORT = 6006                                                # nginx serves this port at /dashboard/
BASE = "http://nginx"                                      # the VSS services, from inside the lab
COLLISIONS = Path("/dli/task/work/agent/collisions.json")  # a list of windows; nothing writes it for you
REGISTRY = Path("/dli/task/data/search_sources.json")      # file name -> VIOS sensor_id and creation_time
CLIP_PREFIX = "/vios"                                      # the browser reaches VIOS through the same proxy as this page


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_windows():
    """The windows in collisions.json, or None while the file does not exist."""
    if not COLLISIONS.is_file():
        return None
    windows = json.loads(COLLISIONS.read_text())
    if not isinstance(windows, list):
        raise ValueError(f"{COLLISIONS} must hold a JSON list of windows")
    return windows


def vios_time(anchor, seconds):
    """The VIOS time `seconds` after `anchor`, an ISO time such as 2025-01-02T00:00:00.000Z."""
    start = datetime.fromisoformat(anchor.replace("Z", "+00:00")).astimezone(timezone.utc)
    moment = start + timedelta(seconds=float(seconds))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def clip_path(file, start_s, end_s):
    """Ask VIOS for one window of `file` and return the path the browser can play it from.

    `start_s` and `end_s` are seconds from the start of the video. The registry
    gives the video's VIOS sensor id and the time its VIOS timeline starts.
    """
    registry = json.loads(REGISTRY.read_text())
    if file not in registry:
        raise LookupError(f"{file} is not in {REGISTRY}, so this page cannot find its VIOS source")
    source = registry[file]
    query = urllib.parse.urlencode({
        "startTime": vios_time(source["creation_time"], start_s),
        "endTime": vios_time(source["creation_time"], end_s),
        "container": "mp4",
    })
    url = f"{BASE}/vios/vst/api/v1/storage/file/{source['sensor_id']}/url?{query}"
    with urllib.request.urlopen(url, timeout=60) as response:
        video_url = json.load(response)["videoUrl"]
    # VIOS answers with an address inside the deployment. Keep its path and let the proxy serve it.
    parts = urllib.parse.urlsplit(video_url)
    return CLIP_PREFIX + parts.path + ("?" + parts.query if parts.query else "")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Collision clips</title>
  <style>
    body   {{ font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }}
    h1     {{ font-weight: 600; }}
    table  {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #333; vertical-align: top; }}
    th     {{ color: #999; font-weight: 500; font-size: .85rem; text-transform: uppercase; }}
    a      {{ color: #6cf; }}
    .muted {{ color: #888; }}
  </style>
</head>
<body>
  <h1>Collision clips</h1>
  {body}
</body>
</html>
"""


def esc(value):
    return html.escape("" if value is None else str(value))


def render(windows):
    if windows is None:
        return PAGE.format(body='<p class="muted">No windows yet. In the optional dashboard task of VSS Coding Agent '
                                'Skills, the coding agent builds this page out from its files in /dli/task/work/agent/.</p>')
    if not windows:
        return PAGE.format(body=f'<p class="muted">{esc(COLLISIONS)} lists no windows yet.</p>')
    rows = []
    for number, window in enumerate(windows, start=1):
        window = window if isinstance(window, dict) else {}
        link = "clip?" + urllib.parse.urlencode({key: window.get(key, "") for key in ("file", "start_s", "end_s")})
        rows.append(
            f"<tr><td>{number}</td><td>{esc(window.get('file'))}</td>"
            f"<td>{esc(window.get('start_s'))} to {esc(window.get('end_s'))} s</td>"
            f"<td>{esc(window.get('description'))}</td>"
            f'<td><a href="{esc(link)}">watch</a></td></tr>'
        )
    table = ("<table><tr><th>#</th><th>File</th><th>Window</th><th>Description</th><th>Clip</th></tr>"
             + "".join(rows) + "</table>")
    return PAGE.format(body=f"<p>{len(windows)} windows from {esc(COLLISIONS)}</p>{table}")


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, headers=()):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                return self.send(200, render(load_windows()))
            if url.path == "/clip":
                location = clip_path(query["file"][0], float(query["start_s"][0]), float(query["end_s"][0]))
                return self.send(302, f'<a href="{esc(location)}">clip</a>', [("Location", location)])
            return self.send(404, PAGE.format(body=f'<p class="muted">No page at {esc(url.path)}.</p>'))
        except (OSError, ValueError, LookupError) as exc:
            # Show the reason on the page instead of a blank error.
            return self.send(500, PAGE.format(body=f'<p class="muted">{esc(type(exc).__name__)}: {esc(exc)}</p>'))

    def log_message(self, *args):
        return  # keep the console for your own prints


if __name__ == "__main__":
    print(f"dashboard on :{PORT}  ->  open /dashboard/ on your lab address", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
