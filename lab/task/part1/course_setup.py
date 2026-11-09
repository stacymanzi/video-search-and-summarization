"""course_setup: the shared VSS helper for course 1 (CONTRACTS.md section 2).

Imported by the Part 1 notebooks and the Part 1 assessment, and by
``/dli/prep/prepare_course.py`` (the launch-time ``course-prep`` service). Notebook 2 (RT-CV) writes the three-call
ingest out by hand: that is the lesson. Notebooks 3 and 4 import these
functions so the API plumbing stops being repeated and the cells can
concentrate on the question being asked.

Rules this file keeps (BUILD.md D7, D11; CONTRACTS.md section 2):

* Pure functions over ``requests``. No state, no caching, no configuration
  lookups: ``BASE`` is the one constant and it is ``http://nginx``.
* No fallbacks. Every HTTP call carries a timeout and every non-2xx answer
  raises ``RuntimeError`` naming the method, the URL, the status and the first
  300 characters of the body. A missing field in a response raises too.
* Progress loops print the elapsed seconds on every iteration and raise
  ``TimeoutError`` naming the index, the sensor and the last count.
* No secrets. Nothing here reads a key and nothing here needs one.

Every docstring names the notebook that teaches the underlying call, so a
learner who wants to see the request itself knows where it is written out.
"""

from __future__ import annotations

import json
import math
import numbers
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

BASE = "http://nginx"

AGENT_URL = BASE + "/agent/api/v1"
VIOS_URL = BASE + "/vios/vst/api/v1"
ES_URL = BASE + "/es"
RT_VLM_URL = BASE + "/rtvi-vlm/v1"
RT_EMBED_URL = BASE + "/rtvi-embed/v1"

# The VSS agent service hands back VIOS's public upload port; from inside the
# lab the upload has to go to VIOS's internal ingress. Holds while
# VSS_EXTERNAL_HOST is empty, which is the shipped configuration (CONTRACTS.md
# section 1).
UPLOAD_PUBLIC_PORT = ":80/"
UPLOAD_INTERNAL_PORT = ":30888/"

EMBED_MODEL = "cosmos-embed1-448p-anomaly-detection"   # Cosmos-Embed1, 768-d
EMBED_INDEX = "mdx-embed-filtered-2025-01-01"
EMBEDDING_ANCHOR = "2025-01-01T00:00:00.000Z"          # file analysis is indexed here regardless of upload metadata

SEED_STATUS_FILE = "/opt/vss/_course-runtime/seed-status"
SEED_LOG_FILE = "/opt/vss/_course-runtime/seed.log"

BODY_PREVIEW_CHARS = 300

__all__ = [
    "BASE", "EMBED_MODEL", "EMBED_INDEX", "EMBEDDING_ANCHOR",
    "upload_url", "ingest_file", "sensors", "find_sensor", "timelines",
    "source_filter", "wait_for_detections",
    "clip_bytes", "iso_offset", "offset_seconds", "es", "vlm_model_id",
    "caption", "register_vios_window", "text_embedding", "knn_search",
    "status_tail",
]


# ---------------------------------------------------------------------------
# HTTP plumbing. Everything above this line in the call stack is one of the
# contract functions; everything below is these three helpers.
# ---------------------------------------------------------------------------

def _body_preview(response) -> str:
    try:
        text = response.text
    except Exception:  # a body that cannot be decoded still gets reported
        text = repr(getattr(response, "content", b""))
    return (text or "")[:BODY_PREVIEW_CHARS]


def _request(method: str, url: str, timeout: float, allow_status=(), **kwargs):
    """One HTTP call. Always has a timeout; raises RuntimeError on any failure.

    ``allow_status`` lists non-2xx codes the caller handles itself (used once,
    for the 404 an Elasticsearch index returns before Logstash has created it).
    """
    if not timeout or timeout <= 0:
        raise ValueError(f"{method} {url}: every call needs a positive timeout, got {timeout!r}")
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"{method} {url} -> no response ({type(exc).__name__}: {exc}). Is the stack up? "
            "Run the readiness check in notebook 1 (section 1.2), and this in a new cell: "
            "import course_setup; print(course_setup.status_tail())"
        ) from exc
    status = response.status_code
    if 200 <= status < 300 or status in allow_status:
        return response
    raise RuntimeError(f"{method} {url} -> HTTP {status}: {_body_preview(response)}")


def _json(method: str, url: str, timeout: float, **kwargs):
    """``_request`` plus JSON decoding; a non-JSON body is an error, not None."""
    response = _request(method, url, timeout, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{method} {url} -> HTTP {response.status_code} but the body is not JSON: {_body_preview(response)}"
        ) from exc


def _field(data, key: str, where: str):
    """Read a required key from a response; a missing key is reported, never defaulted."""
    if not isinstance(data, dict) or key not in data:
        raise RuntimeError(f"{where}: response has no {key!r} field; got {json.dumps(data)[:BODY_PREVIEW_CHARS]}")
    return data[key]


# ---------------------------------------------------------------------------
# Ingest (notebook 2, section 2.2 "Add a video to VIOS")
# ---------------------------------------------------------------------------

def upload_url(agent_url: str) -> str:
    """Turn the upload URL from the VSS agent service into the one the lab can reach.

    Notebook 2 (section 2.2, the first of the three upload calls) shows the
    VSS agent service returning a URL on the deployment's public port 80; the
    lab uploads through VIOS's internal ingress on 30888 instead. This is a
    plain string rewrite and it is only valid while ``VSS_EXTERNAL_HOST`` is
    empty. A URL without ``:80/`` means the deployment is configured
    differently, so the function raises rather than guessing.
    """
    if UPLOAD_PUBLIC_PORT not in agent_url:
        raise RuntimeError(
            f"upload URL {agent_url!r} does not carry the public port {UPLOAD_PUBLIC_PORT!r}; "
            "the VSS_EXTERNAL_HOST assumption in CONTRACTS.md section 1 no longer holds"
        )
    return agent_url.replace(UPLOAD_PUBLIC_PORT, UPLOAD_INTERNAL_PORT, 1)


def ingest_file(path: str, name: str, timestamp_iso: str) -> str:
    """Ingest one file through the VSS agent service and return its VIOS ``sensorId``.

    The three-call flow notebook 2 (section 2.2) writes out by hand:

    1. ``POST /agent/api/v1/videos`` with the filename -> upload URL.
    2. ``POST`` the file to that URL (rewritten by ``upload_url``) as a
       single chunk with the ``nvstreamer-*`` headers and a ``metadata`` part
       carrying ``timestamp_iso``. VIOS answers with the ``sensorId``.
    3. ``POST /agent/api/v1/videos/{sensorId}/complete`` so the VSS agent
       service fans the file out to RT-CV (TrafficCamNet RT-DETR) and RT-Embed
       (Cosmos-Embed1).

    ``timestamp_iso`` sets the VIOS timeline anchor (``traffic_short``:
    ``2025-01-01T00:00:00``; the four search clips: ``2025-01-02``). File
    analysis is indexed at 2025-01-01 regardless, see ``iso_offset``.
    Large clips take minutes: the upload has a 900 s timeout and so does the
    completion call.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"ingest_file: {source} is not a file")
    if not name or not timestamp_iso:
        raise ValueError("ingest_file: name and timestamp_iso are required")

    issued = _json("POST", AGENT_URL + "/videos", timeout=60, json={"filename": name})
    target = upload_url(_field(issued, "url", "POST " + AGENT_URL + "/videos"))

    headers = {
        "nvstreamer-chunk-number": "1",
        "nvstreamer-total-chunks": "1",
        "nvstreamer-is-last-chunk": "true",
        "nvstreamer-identifier": name,
        "nvstreamer-file-name": name,
    }
    with source.open("rb") as handle:
        uploaded = _json(
            "POST", target, timeout=900, headers=headers,
            files={
                "metadata": (None, json.dumps({"timestamp": timestamp_iso})),
                "file": (name, handle, "video/mp4"),
            },
        )
    sensor_id = _field(uploaded, "sensorId", "POST " + target)
    if not isinstance(sensor_id, str) or not sensor_id:
        raise RuntimeError(f"POST {target}: sensorId is empty or not a string: {sensor_id!r}")

    _json("POST", AGENT_URL + "/videos/" + sensor_id + "/complete", timeout=900, json={"filename": name})
    return sensor_id


# ---------------------------------------------------------------------------
# VIOS (notebook 2, sections 2.1 to 2.4: the starting state, the timeline, the clip)
# ---------------------------------------------------------------------------

def sensors() -> list[dict]:
    """Every source VIOS holds, as raw records: ``GET /vios/vst/api/v1/sensor/list``.

    Notebook 2 (section 2.1, the starting state) makes this call by hand. Each
    record carries ``sensorId`` (the VIOS UUID) and ``name`` (the VIOS name:
    the file stem plus a suffix, ``traffic_short_1`` for an ingest through the
    VSS agent service; display and lookup only, see ``find_sensor``). The list
    may be empty on a fresh deployment; anything that is not a list is an
    error.
    """
    listed = _json("GET", VIOS_URL + "/sensor/list", timeout=30)
    if not isinstance(listed, list):
        raise RuntimeError(f"GET {VIOS_URL}/sensor/list: expected a list, got {json.dumps(listed)[:BODY_PREVIEW_CHARS]}")
    return listed


def find_sensor(name_prefix: str) -> dict | None:
    """Return the VIOS sensor record for a file-ingested source, or ``None``.

    ``name_prefix`` is the FILE STEM (``Path(filename).stem``, ``traffic_short``).
    Notebook 2 lists ``GET /vios/vst/api/v1/sensor/list``; the match is a
    prefix because VIOS does not list the stem itself: measured on the devbox
    (2026-10-21), a source ingested through the VSS agent service is listed
    as ``<stem>_1`` (``normal_eval_1``, ``traffic_short_1``) and a re-ingest
    counts on (``_2``). So the match is ``name == prefix`` or
    ``name.startswith(prefix + "_")``; ``traffic_shortcut`` does not match.

    Three identifiers name one source (CONTRACTS.md section 1). The record
    carries two of them: ``sensorId`` (the VIOS UUID: clips, and RT-Embed's
    ``sensor.id.keyword``) and ``name`` (the VIOS name: display and this
    lookup only). The third is the stem you passed in: RT-CV writes it,
    without the suffix, to ``sensorId.keyword`` in ``mdx-raw-*`` and behavior
    analytics to ``sensor.id.keyword`` (a ``_count`` for ``normal_eval``
    returned 4321 frames; ``traffic_short_1`` returned 0). Never filter
    those indices by ``name``.

    When several records match, the first one in VIOS's list order is
    returned; pass the exact name to pin one.
    """
    if not name_prefix:
        raise ValueError("find_sensor: name_prefix is required")
    # VERIFY ON DEVBOX: list order when the same clip was ingested twice (which record comes first).
    for sensor in sensors():
        name = sensor.get("name") if isinstance(sensor, dict) else None
        if isinstance(name, str) and (name == name_prefix or name.startswith(name_prefix + "_")):
            return sensor
    return None


def timelines(sensor_id: str) -> list[dict]:
    """The spans of footage VIOS can cut for one source: ``GET .../storage/{sensor}/timelines``.

    Notebook 2 (section 2.3, the timeline) shows the call. Each span
    has ``startTime`` and ``endTime`` on the source's VIOS timeline (the upload
    anchor); a file upload has one span. An empty list raises, because a source
    with no timeline has nothing to cut, and a span without both fields
    raises because every window check in the course compares against them.
    """
    if not sensor_id:
        raise ValueError("timelines: sensor_id is required")
    url = VIOS_URL + "/storage/" + sensor_id + "/timelines"
    spans = _json("GET", url, timeout=30)
    # VERIFY ON DEVBOX: timelines response shape on 3.2.1 (a list of {"startTime", "endTime"}; Berlin's dashboard read it so).
    if not isinstance(spans, list) or not spans:
        raise RuntimeError(f"GET {url}: no timeline spans for sensor {sensor_id}; VIOS has no footage for it")
    for span in spans:
        if not isinstance(span, dict) or "startTime" not in span or "endTime" not in span:
            raise RuntimeError(f"GET {url}: span without startTime/endTime: {json.dumps(span)[:BODY_PREVIEW_CHARS]}")
    return spans


def clip_bytes(sensor_id: str, start_iso: str, end_iso: str) -> bytes:
    """Cut an MP4 for one window of one VIOS source and return its bytes.

    Notebook 2 (section 2.4, watch the clip) calls this helper, which makes two requests: ask
    ``GET /vios/vst/api/v1/storage/file/{sensor}/url`` for the window, then
    fetch the returned ``videoUrl`` through the course proxy as
    ``http://nginx/vios/<path>``. Times are VIOS timeline times (the upload
    anchor), not embedding-index times: convert with ``offset_seconds`` and
    ``iso_offset`` first, as notebook 4 does. An empty body means the window
    lies outside the source's timeline and is reported as an error.
    """
    if not sensor_id:
        raise ValueError("clip_bytes: sensor_id is required")
    url_endpoint = VIOS_URL + "/storage/file/" + sensor_id + "/url"
    issued = _json("GET", url_endpoint, timeout=60,
                   params={"startTime": start_iso, "endTime": end_iso, "container": "mp4"})
    video_url = _field(issued, "videoUrl", "GET " + url_endpoint)
    parts = urlsplit(video_url)
    path = parts.path.lstrip("/") + ("?" + parts.query if parts.query else "")
    if not path:
        raise RuntimeError(f"GET {url_endpoint}: videoUrl {video_url!r} has no path to fetch")
    content = _request("GET", BASE + "/vios/" + path, timeout=120).content
    if not content:
        raise RuntimeError(
            f"VIOS returned an empty clip for sensor {sensor_id} between {start_iso} and {end_iso}; "
            "check the window against GET /vios/vst/api/v1/storage/{sensor}/timelines and the anchor date"
        )
    return content


# ---------------------------------------------------------------------------
# Time arithmetic (notebook 2's seconds-to-ISO helper, notebook 4's anchor conversion)
# ---------------------------------------------------------------------------

def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware UTC datetime. Naive means UTC."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"expected an ISO-8601 timestamp string, got {value!r}")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cannot parse ISO-8601 timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_offset(anchor_iso: str, seconds: float) -> str:
    """``anchor_iso`` plus ``seconds``, as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    Notebook 2 writes this arithmetic out for the 2025-01-01 anchor;
    notebook 4 uses it to move an embedding hit (indexed at 2025-01-01
    whatever the upload said) onto the source's VIOS timeline (2025-01-02 for
    the four search clips) before cutting the clip.
    Milliseconds are kept because VIOS accepts them and the embedding index
    stores them.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
        raise ValueError(f"iso_offset: seconds must be a finite number, got {seconds!r}")
    moment = _parse_iso(anchor_iso) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def offset_seconds(anchor_iso: str, time_iso: str) -> float:
    """Seconds from ``anchor_iso`` to ``time_iso`` (negative when earlier).

    The other half of notebook 4's conversion: an embedding hit's
    ``timestamp`` minus ``EMBEDDING_ANCHOR`` is the clip-relative offset, and
    ``iso_offset(source_creation_time, that)`` is the VIOS time to cut. Not in
    CONTRACTS.md section 2 as signed; proposed as an addition there.
    """
    return (_parse_iso(time_iso) - _parse_iso(anchor_iso)).total_seconds()


# ---------------------------------------------------------------------------
# Elasticsearch (notebook 2)
# ---------------------------------------------------------------------------

def es(method: str, path: str, **kwargs) -> dict:
    """One Elasticsearch request through the proxy, decoded as JSON.

    ``path`` is relative to the Elasticsearch root, for example
    ``"/mdx-raw-2025-01-01/_count"`` or ``"/mdx-behavior-*/_search"``;
    notebook 2 shows these requests written out. Pass the body as
    ``json=...`` and, when you need more than the default 60 s, ``timeout=``.
    Any non-2xx status raises; a missing index therefore raises too, which is
    what you want everywhere except the detection wait, which handles that one
    case itself.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"es: path must start with '/', got {path!r}")
    if path.startswith("/es/"):
        raise ValueError(f"es: pass the path relative to Elasticsearch, without the '/es' prefix: {path!r}")
    timeout = kwargs.pop("timeout", 60)
    return _json(method.upper(), ES_URL + path, timeout=timeout, **kwargs)


def source_filter(name: str) -> dict:
    """The Elasticsearch query that selects one RT-CV source's records by file stem.

    ``name`` is the FILE STEM (``traffic_short``), the value RT-CV writes to
    ``sensorId.keyword``. Measured on the devbox (2026-10-21): the records carry
    the stem without VIOS's suffix (a ``_count`` for ``normal_eval`` returned
    4321 frames while VIOS listed the source as ``normal_eval_1``; a count for
    the VIOS name ``traffic_short_1`` returned 0 for ten minutes). So pass the
    stem, never ``find_sensor(...)["name"]`` and never the UUID.

    Notebook 2 (the source-filter exercise in section 2.7) has the learner
    write this query: ``sensorId.keyword`` equals ``name`` exactly **or**
    starts with ``name + "_"``. The prefix clause
    is kept as written (it is the mirror of ``find_sensor`` and would also catch
    a source whose RT-CV id carried a suffix, which the devbox did not show);
    ``traffic_shortcut`` does not match ``traffic_short``. Use it as the
    ``query`` of a ``_count`` or ``_search`` on ``mdx-raw-2025-01-01``, or as
    one entry of a ``bool.filter`` list.
    """
    if not name:
        raise ValueError("source_filter: name is required")
    # Measured 2026-10-21 (c-fx-17-v2-dev): RT-CV sensorId.keyword == the file stem; the VIOS name (stem_1) matches nothing.
    # VERIFY ON DEVBOX: whether a true re-ingest (a second upload of the same file through the VSS agent service) is ever keyed stem_<n> in mdx-raw.
    return {"bool": {
        "should": [
            {"term": {"sensorId.keyword": name}},
            {"prefix": {"sensorId.keyword": name + "_"}},
        ],
        "minimum_should_match": 1,
    }}


def _count_frames(index: str, sensor_name: str) -> int | None:
    """Root records in ``index`` for one RT-CV source (file stem, or stem_<n>); ``None`` while the index does not exist."""
    url = ES_URL + "/" + index + "/_count"
    response = _request("POST", url, timeout=20, allow_status=(404,),
                        json={"query": source_filter(sensor_name)})
    if response.status_code == 404:
        return None
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"POST {url} -> body is not JSON: {_body_preview(response)}") from exc
    count = _field(data, "count", "POST " + url)
    if not isinstance(count, int):
        raise RuntimeError(f"POST {url}: count is not an integer: {count!r}")
    return count


def wait_for_detections(index: str, sensor_name: str, timeout_s=1200, every_s=10) -> int:
    """Block until RT-CV frames for ``sensor_name`` appear in ``index``; return the count.

    Notebook 2 (section 2.5, wait for RT-CV's detections) calls this helper.
    Sections 2.7 and 2.8 write out the same ``_count`` query on
    ``sensorId.keyword``. ``sensor_name`` is the FILE STEM
    of the source (``Path(filename).stem``, ``traffic_short``): that is what
    RT-CV writes as its sensor id. It is not the VIOS name
    (``find_sensor(...)["name"]`` is ``traffic_short_1`` and counted 0 for ten
    minutes on the devbox) and not the VIOS UUID. The count uses
    ``source_filter``, so the stem also matches a ``stem_<n>`` id should one
    appear; ``index`` is normally ``mdx-raw-2025-01-01``. A 404 from
    Elasticsearch means Logstash has not created the index yet and counts as
    zero; any other error raises. Prints the elapsed seconds on every poll and
    raises ``TimeoutError`` naming the index, the sensor and the last count. A
    nonzero count means records have started to land, not that the whole clip
    is processed.
    """
    if timeout_s < 0 or every_s <= 0:
        raise ValueError(f"wait_for_detections: timeout_s={timeout_s!r} every_s={every_s!r} are out of range")
    started = time.monotonic()
    last = 0
    while True:
        counted = _count_frames(index, sensor_name)
        last = 0 if counted is None else counted
        elapsed = time.monotonic() - started
        if last > 0:
            print(f"{index}: {last} frames of detections for {sensor_name!r} after {elapsed:.0f} s", flush=True)
            return last
        if elapsed >= timeout_s:
            raise TimeoutError(
                f"no detections in {index} for sensor {sensor_name!r} after {elapsed:.0f} s "
                f"(last count {last}). Check RT-CV, Kafka and Logstash with the readiness check in notebook 1 "
                "(section 1.2), and run this in a new cell: import course_setup; print(course_setup.status_tail())"
            )
        state = "index not created yet" if counted is None else f"{last} so far"
        print(f"waiting for detections in {index} for {sensor_name!r}: {state}, {elapsed:.0f} s elapsed "
              f"(limit {timeout_s} s)", flush=True)
        time.sleep(every_s)


# ---------------------------------------------------------------------------
# RT-VLM, Cosmos 3 Super Reasoner (notebook 3)
# ---------------------------------------------------------------------------

def vlm_model_id() -> str:
    """The deployed RT-VLM model id, read from ``GET /rtvi-vlm/v1/models``.

    Notebook 3's setup cell does this read; the id (Cosmos 3 Super Reasoner,
    FP8) goes into every ``generate_captions`` and ``chat/completions``
    request. An empty model list is an error: it means the service is up but
    has not loaded its model.
    """
    data = _json("GET", RT_VLM_URL + "/models", timeout=30)
    models = _field(data, "data", "GET " + RT_VLM_URL + "/models")
    if not isinstance(models, list) or not models:
        raise RuntimeError(f"GET {RT_VLM_URL}/models: no models listed; RT-VLM has not loaded its model")
    model_id = _field(models[0], "id", "GET " + RT_VLM_URL + "/models (data[0])")
    if not isinstance(model_id, str) or not model_id:
        raise RuntimeError(f"GET {RT_VLM_URL}/models: first model has an empty id: {models[0]!r}")
    return model_id


def register_vios_window(sensor_id, start_iso, end_iso, creation_time_iso) -> str:
    """Register one VIOS window with RT-VLM and return the RT-VLM file id.

    Notebook 3 (section 3.1, register the clip with RT-VLM) shows both
    steps: ask VIOS for the window's URL exactly as ``clip_bytes`` does, then
    ``POST /rtvi-vlm/v1/files`` as a multipart form with ``purpose=vision``,
    ``media_type=video``, that ``url`` and ``creation_time``. RT-VLM fetches
    the URL itself, inside the stack. ``creation_time_iso`` is the timestamp
    the caption windows will be reported against; use the window's VIOS
    start time (or the upload anchor when captioning a whole clip).
    """
    if not sensor_id or not creation_time_iso:
        raise ValueError("register_vios_window: sensor_id and creation_time_iso are required")
    url_endpoint = VIOS_URL + "/storage/file/" + sensor_id + "/url"
    issued = _json("GET", url_endpoint, timeout=60,
                   params={"startTime": start_iso, "endTime": end_iso, "container": "mp4"})
    video_url = _field(issued, "videoUrl", "GET " + url_endpoint)
    registered = _json("POST", RT_VLM_URL + "/files", timeout=120, files={
        "purpose": (None, "vision"),
        "media_type": (None, "video"),
        "url": (None, video_url),
        "creation_time": (None, creation_time_iso),
    })
    file_id = _field(registered, "id", "POST " + RT_VLM_URL + "/files")
    if not isinstance(file_id, str) or not file_id:
        raise RuntimeError(f"POST {RT_VLM_URL}/files: file id is empty: {registered!r}")
    return file_id


def caption(file_id: str, prompt: str, chunk_duration=10, *, creation_time_iso: str | None = None,
            frames: float | None = None, use_fps: bool = False, system_prompt: str | None = None,
            max_tokens: int | None = None) -> list[dict]:
    """Ask Cosmos 3 Super Reasoner to caption a registered file; return the chunks.

    Sends ``POST /rtvi-vlm/v1/generate_captions``; notebook 3 (sections 3.2 to
    3.4) uses it. ``chunk_duration`` splits the file into windows of that many
    seconds; ``0`` captions it as one chunk. The model id is read live with
    ``vlm_model_id``. Returns the ``chunk_responses`` list unchanged: each
    chunk has ``start_time``, ``end_time``, ``content`` and ``vlm_latency_ms``,
    and the RT-VLM 3.2 API also reports ``frame_count`` (the frames the model
    saw) and ``input_tokens``. An empty list raises, because a caption request
    that produced nothing is a failure, not a result.

    The keyword arguments are optional and each is sent only when given, so a
    call without them sends exactly what the notebooks always sent. Field
    names follow the RT-VLM 3.2 request model (``VlmQuery``).

    * ``creation_time_iso`` is sent as ``creation_time``. No notebook passes it:
      when it is omitted, RT-VLM reports chunk times against the
      ``creation_time`` the file was registered with.
    * ``frames`` sets how many frames RT-VLM samples from each chunk: it is
      sent as ``num_frames_per_second_or_fixed_frames_chunk`` together with
      ``use_fps_for_chunking=use_fps``. With ``use_fps=False`` it is a fixed
      number of frames per chunk; with ``use_fps=True`` it is frames per
      second. Leave it out to get the deployment's default. ``use_fps=True``
      without ``frames`` raises, because it would change nothing.
    * ``system_prompt`` and ``max_tokens`` are sent under those names.

    Wrong types, a ``frames`` that is not a positive number, and a
    ``max_tokens`` below 1 raise ``ValueError`` before any request is made.
    Upper limits are RT-VLM's own: a value above them comes back as an HTTP
    error. The RT-VLM 3.2 request model allows ``frames`` up to 256.
    """
    if not file_id or not prompt:
        raise ValueError("caption: file_id and prompt are required")
    options = {}
    if creation_time_iso is not None:
        options["creation_time"] = creation_time_iso
    if frames is not None:
        if isinstance(frames, bool) or not isinstance(frames, numbers.Real) or not math.isfinite(frames) or frames <= 0:
            raise ValueError(f"caption: frames must be a positive number, got {frames!r}; "
                             "leave it out to use the deployment's default")
        if not isinstance(use_fps, bool):
            raise ValueError(f"caption: use_fps must be True or False, got {use_fps!r}")
        options["num_frames_per_second_or_fixed_frames_chunk"] = (
            int(frames) if isinstance(frames, numbers.Integral) else float(frames))
        options["use_fps_for_chunking"] = use_fps
    elif use_fps:
        raise ValueError("caption: use_fps applies only together with frames (frames per second); pass frames too")
    if system_prompt is not None:
        if not isinstance(system_prompt, str):
            raise ValueError(f"caption: system_prompt must be a string, got {system_prompt!r}")
        options["system_prompt"] = system_prompt
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, numbers.Integral) or max_tokens < 1:
            raise ValueError(f"caption: max_tokens must be a positive integer, got {max_tokens!r}")
        options["max_tokens"] = int(max_tokens)
    payload = {"id": file_id, "model": vlm_model_id(), "prompt": prompt, "chunk_duration": chunk_duration, **options}
    # VERIFY ON DEVBOX: generate_captions without creation_time uses the file's registered creation_time
    # for chunk start_time/end_time (no notebook sends it; 3.2.1 behaviour when omitted is unprobed).
    data = _json("POST", RT_VLM_URL + "/generate_captions", timeout=900, json=payload)
    chunks = _field(data, "chunk_responses", "POST " + RT_VLM_URL + "/generate_captions")
    if not isinstance(chunks, list) or not chunks:
        raise RuntimeError(
            f"POST {RT_VLM_URL}/generate_captions: no chunk_responses for file {file_id}; "
            f"response was {json.dumps(data)[:BODY_PREVIEW_CHARS]}"
        )
    return chunks


# ---------------------------------------------------------------------------
# RT-Embed, Cosmos-Embed1 (notebook 4)
# ---------------------------------------------------------------------------

def text_embedding(text: str) -> list[float]:
    """Embed one query string with Cosmos-Embed1 and return the 768 floats.

    Notebook 4 (section 4.3, encode a query) writes the ``POST
    /rtvi-embed/v1/generate_text_embeddings`` out by hand with model
    ``cosmos-embed1-448p-anomaly-detection``. The vector lives in the same
    space as the indexed video-chunk vectors, which is what makes
    ``knn_search`` work. An empty or non-numeric vector raises.
    """
    if not text or not text.strip():
        raise ValueError("text_embedding: text is required")
    url = RT_EMBED_URL + "/generate_text_embeddings"
    data = _json("POST", url, timeout=60, json={"text_input": [text], "model": EMBED_MODEL})
    items = _field(data, "data", "POST " + url)
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"POST {url}: empty data list for {text!r}")
    vector = _field(items[0], "embeddings", "POST " + url + " (data[0])")
    if not isinstance(vector, list) or not vector or not all(isinstance(v, (int, float)) for v in vector):
        raise RuntimeError(f"POST {url}: embeddings is not a list of numbers (length {len(vector) if isinstance(vector, list) else 'n/a'})")
    return [float(v) for v in vector]


def knn_search(vector, sensor_ids: list[str], k: int) -> list[dict]:
    """Nearest video chunks to ``vector`` among ``sensor_ids``; returns raw ES hits.

    Notebook 4 (section 4.4, search across files) writes
    this query out: a ``bool`` filter on ``sensor.id.keyword`` (the RT-Embed
    source field, which holds the VIOS UUID, not the file stem) around a
    ``nested`` ``knn`` on ``llm.visionEmbeddings.vector`` that carries the
    same filter, so the neighbors are restricted inside the vector search
    and not only after it. Each hit's ``_source`` has ``sensor.id``,
    ``timestamp`` and ``end`` on the 2025-01-01 embedding anchor, and
    ``_score`` is a similarity, not a probability. Nothing is deduplicated
    here: that is the caller's job and the lesson (dedup on sensor id plus
    window, never on time alone). An empty ``sensor_ids`` raises instead of
    searching every source.
    """
    if not isinstance(vector, (list, tuple)) or not vector:
        raise ValueError("knn_search: vector must be a non-empty list of floats")
    if not sensor_ids:
        raise ValueError("knn_search: sensor_ids is empty; an empty filter would search every source")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError(f"knn_search: k must be a positive integer, got {k!r}")
    ids = list(sensor_ids)
    body = {
        "size": k,
        "_source": ["sensor.id", "timestamp", "end"],
        "query": {"bool": {
            "filter": [{"terms": {"sensor.id.keyword": ids}}],
            "must": [{"nested": {
                "path": "llm.visionEmbeddings", "score_mode": "max",
                "query": {"knn": {"field": "llm.visionEmbeddings.vector",
                                  "query_vector": list(vector), "k": k,
                                  "filter": {"terms": {"sensor.id.keyword": ids}},
                                  "num_candidates": max(8 * k, 40)}},
            }}],
        }},
    }
    data = es("POST", "/" + EMBED_INDEX + "/_search", timeout=60, json=body)
    hits = _field(_field(data, "hits", "knn_search"), "hits", "knn_search (hits)")
    if not isinstance(hits, list):
        raise RuntimeError(f"knn_search: hits.hits is not a list: {hits!r}")
    return hits


# ---------------------------------------------------------------------------
# Launch status (course-prep's status file and registry; the deployer's status file)
# ---------------------------------------------------------------------------

PREP_STATUS_FILE = "/dli/task/data/course_prep_status.json"   # written by course-prep (CONTRACTS.md section 6)
REGISTRY_FILE = "/dli/task/data/search_sources.json"           # written by course-prep when every clip is searchable


def require_course_prep(status_path=PREP_STATUS_FILE) -> dict:
    """The course-prep status when it reads OK; a RuntimeError saying why otherwise.

    Call this before reading anything course-prep writes (the registry of the
    four search clips). course-prep writes the status file as soon as it
    starts, then waits for the S3 course data and the VSS agent service before
    it ingests anything, so RUNNING is normal for many minutes after
    JupyterLab opens, and the message says to wait. A missing file means
    course-prep has not started yet. FAILED is not normal, and the message
    says to call the instructor. The advice matches the status cell of the
    onboarding notebook (part1/00-onboarding.ipynb, section 0.4), which prints
    the progress clip by clip.
    """
    path = Path(status_path)
    if not path.is_file():
        raise RuntimeError(f"{path} does not exist yet: the course-prep service has not started. "
                           "Run this cell again in a minute. If it is still missing ten minutes after the "
                           "readiness check in 00-onboarding (section 0.3) said READY TO CONTINUE, "
                           "call the instructor.")
    prep = json.loads(path.read_text())
    state = prep.get("state")
    if state == "RUNNING":
        detail = prep.get("detail") or "preparing the four search clips"
        raise RuntimeError(f"course-prep is still RUNNING: {detail}. Run this cell again in a few minutes. "
                           "The status cell in 00-onboarding, section 0.4, shows the progress clip by clip.")
    if state != "OK":
        raise RuntimeError(f"course-prep reports {state!r}. Waiting will not help, so call the instructor "
                           "and show them the output of the status cell in 00-onboarding, section 0.4.")
    return prep


def registry_entry(filename: str, registry_path=REGISTRY_FILE, status_path=PREP_STATUS_FILE) -> dict:
    """The registry entry for ``filename`` once course-prep has completed its ingest.

    Raises RuntimeError with the reason: course-prep not finished (see
    require_course_prep), the registry missing although the status reads OK,
    the clip absent from the registry, or its upload not completed. Returns
    the entry dict (sensor_id, creation_time, duration_seconds, completed).
    """
    registry_file = Path(registry_path)
    if not registry_file.is_file():
        require_course_prep(status_path)
        raise RuntimeError(f"course-prep reports OK but {registry_file} is missing: the registry was removed after "
                           "the preparation. Call the instructor, and do not upload the clip yourself.")
    registry = json.loads(registry_file.read_text())
    if filename not in registry:
        raise RuntimeError(f"{filename} is not in the registry: course-prep did not ingest it. "
                           "Run the status cell in 00-onboarding, section 0.4, and call the instructor.")
    entry = registry[filename]
    if not entry.get("completed"):
        raise RuntimeError(f"{filename}: the registry says its upload was not completed: {entry}")
    return entry


def status_tail(n=5) -> str:
    """The deployer's last status line and the last ``n`` lines of its log.

    The readiness check in 00-onboarding (section 0.3) prints this while it
    waits, and so does course-prep while it waits for the stack. The error
    messages of this module point to it: call ``course_setup.status_tail()``
    from any cell when a request gets no answer. The deployer
    (``task1/deploy/deploy-vss.sh``) writes
    ``/opt/vss/_course-runtime/seed-status`` one line per launch step, ``<UTC
    time> RUNNING|OK|FAILED: <text>``, and its readiness waiter appends one
    line per change, so the file is multi-line and only the **last** line is
    the current state: that is the line returned here. Everything the deployer
    prints goes to ``seed.log`` with keys redacted; both files are mounted
    read-only in the lab. Returns the literal ``"status file not found:
    <path>"`` when the status file is absent, which on a fresh VM means the
    deployer has not started yet.
    """
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"status_tail: n must be a positive integer, got {n!r}")
    status_path = Path(SEED_STATUS_FILE)
    log_path = Path(SEED_LOG_FILE)
    if not status_path.is_file():
        return f"status file not found: {status_path}"
    status_lines = status_path.read_text(errors="replace").splitlines()
    lines = ["seed-status: " + (status_lines[-1] if status_lines else "(empty)")]
    if log_path.is_file():
        tail = log_path.read_text(errors="replace").splitlines()[-n:]
        lines.append(f"seed.log (last {len(tail)} of {n} requested lines):")
        lines.extend("  " + line for line in tail)
    else:
        lines.append(f"seed.log not found: {log_path}")
    return "\n".join(lines)
