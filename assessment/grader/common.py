"""common.py: shared plumbing for the course grader (task1/assessment/README.md).

Everything a grader needs lives here: the paths it reads, the HTTP seam the
unit tests replace, a small JSON-schema validator so the grader has no
third-party dependency, the VIOS, Elasticsearch, RT-Embed, registry,
ground-truth and answer-key helpers, and the marker writer.

Rules this file keeps:

* Fail closed. A service that does not answer, a file that does not parse, a
  response of the wrong shape: each becomes a failed check with a detail
  line, never a pass and never a crash that leaves an earlier marker in
  place. ``ServiceError`` and ``ArtefactError`` are the two signals;
  ``run_checks`` converts them.
* No fallbacks. A missing answer key or ground-truth entry is a failed
  check, never a default.
* No secrets. Nothing here reads a key, a token or an environment file.
* Standard library only. The assessment container is the lab image, but the
  grader must also run under ``python3 -I`` on a machine with nothing
  installed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Locations and constants
# ---------------------------------------------------------------------------

GRADER_DIR = Path(__file__).resolve().parent
RUBRIC_PATH = GRADER_DIR / "rubric.json"
SCHEMAS_DIR = GRADER_DIR / "schemas"
ANSWER_KEYS_DIR = GRADER_DIR / "answer_keys"
GROUND_TRUTH_PATH = ANSWER_KEYS_DIR / "ground_truth" / "search_videos.json"

BASE = "http://nginx"
VIOS_URL = BASE + "/vios/vst/api/v1"
ES_URL = BASE + "/es"
RT_EMBED_URL = BASE + "/rtvi-embed/v1"

# Elasticsearch and RT-Embed names, as task1/lab/task/part1/course_setup.py and the notebooks use them.
RAW_INDEX = "mdx-raw-2025-01-01"                 # RT-CV frame records of uploaded files; sensorId.keyword is the file stem
EMBED_INDEX = "mdx-embed-filtered-2025-01-01"    # RT-Embed chunk embeddings of uploaded files; sensor.id.keyword is the VIOS id
EMBED_MODEL = "cosmos-embed1-448p-anomaly-detection"   # the Cosmos-Embed1 model id RT-Embed serves
EMBEDDING_ANCHOR = "2025-01-01T00:00:00.000Z"    # file analysis is indexed at this instant, whatever the upload said

DEFAULT_ANSWERS_FILE = "/dli/task/work/assessment/answers.json"
DEFAULT_RESULTS_DIR = "/dli/assessment_results"
DEFAULT_REGISTRY_FILE = "/dli/task/data/search_sources.json"   # course-prep's list of the four search clips

BODY_PREVIEW_CHARS = 300
LETTER_RE = re.compile(r"^[A-H]$")


class ServiceError(RuntimeError):
    """A service did not answer as required. Fails closed."""


class ArtefactError(RuntimeError):
    """A learner file, an answer key or the ground truth is missing, unreadable or of the wrong shape."""


# ---------------------------------------------------------------------------
# Context: every external seam in one object so tests can replace them
# ---------------------------------------------------------------------------

HttpFn = Callable[[str, str, Optional[bytes], float], Tuple[int, bytes]]


def urllib_http(method: str, url: str, body: Optional[bytes], timeout: float) -> Tuple[int, bytes]:
    """The real transport: one request, (status, body). A transport failure raises ServiceError."""
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ServiceError(f"{method} {url} -> no response ({type(exc).__name__}: {exc})") from exc


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Context:
    answers_file: Path       # what the assessment notebook's task cells write
    results_dir: Path        # where markers go (read-only in the lab)
    answer_keys_dir: Path    # <assessment>.json multiple-choice keys; never in the lab image
    ground_truth_file: Path  # labelled events of the four search clips
    registry_file: Path      # course-prep's search_sources.json: file name -> VIOS sensor id of each search clip
    rubric: dict
    http: HttpFn
    now: Callable[[], datetime]

    def threshold(self, assessment: str, name: str):
        """A number from rubric.json. part1/quiz.py repeats the numbers its own checks use: change both together."""
        section = self.rubric.get(assessment, {})
        thresholds = section.get("thresholds", {})
        if name in thresholds:
            return thresholds[name]
        shared = self.rubric.get("thresholds", {})
        if name in shared:
            return shared[name]
        raise ArtefactError(f"rubric.json has no threshold {name!r} for {assessment}")


def assessment_ids(rubric: dict, where: str = "rubric.json") -> List[str]:
    """The assessments the rubric lists, in order. tasktest.py reads the same list."""
    ids = rubric.get("assessments") if isinstance(rubric, dict) else None
    if not isinstance(ids, list) or not ids or not all(isinstance(a, str) and a for a in ids):
        raise ArtefactError(f"{where}: 'assessments' must be a non-empty list of assessment ids")
    return list(ids)


def load_rubric(path: Path = RUBRIC_PATH) -> dict:
    rubric = load_json(path)
    for assessment in assessment_ids(rubric, str(path)):
        section = rubric.get(assessment)
        if not isinstance(section, dict) or not isinstance(section.get("checks"), list) or not section["checks"]:
            raise ArtefactError(f"{path}: rubric has no checks for {assessment}")
        for spec in section["checks"]:
            if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not isinstance(spec.get("must"), bool):
                raise ArtefactError(f"{path}: every {assessment} check needs a string id and a boolean must: {spec!r}")
    return rubric


def default_context() -> Context:
    """Production paths, overridable through GRADER_* environment variables for development."""
    env = os.environ.get
    return Context(
        answers_file=Path(env("GRADER_ANSWERS_FILE", DEFAULT_ANSWERS_FILE)),
        results_dir=Path(env("GRADER_RESULTS_DIR", DEFAULT_RESULTS_DIR)),
        answer_keys_dir=Path(env("GRADER_ANSWER_KEYS_DIR", str(ANSWER_KEYS_DIR))),
        ground_truth_file=Path(env("GRADER_GROUND_TRUTH_FILE", str(GROUND_TRUTH_PATH))),
        registry_file=Path(env("GRADER_REGISTRY_FILE", DEFAULT_REGISTRY_FILE)),
        rubric=load_rubric(),
        http=urllib_http,
        now=utc_now,
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def load_json(path: Path):
    path = Path(path)
    if not path.is_file():
        raise ArtefactError(f"{path} is missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ArtefactError(f"{path} cannot be read: {exc}") from exc
    except ValueError as exc:
        raise ArtefactError(f"{path} is not valid JSON: {exc}") from exc


def load_schema(name: str) -> dict:
    return load_json(SCHEMAS_DIR / f"{name}.schema.json")


# ---------------------------------------------------------------------------
# A small JSON-schema validator (draft-07 subset: type, enum, const, required,
# properties, additionalProperties(bool), items, minItems, maxItems,
# minLength, maxLength, minimum, maximum, pattern). Enough for every schema
# under grader/schemas/ and dependency-free.
# ---------------------------------------------------------------------------

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(instance, schema: dict, where: str = "$") -> List[str]:
    """Return a list of human-readable violations; empty means valid."""
    errors: List[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS[t](instance) for t in types):
            errors.append(f"{where}: expected {' or '.join(types)}, got {type(instance).__name__}")
            return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} is not one of {schema['enum']}")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{where}: expected {schema['const']!r}, got {instance!r}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{where}: missing required field {key!r}")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in instance:
                errors.extend(validate(instance[key], sub, f"{where}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{where}: unexpected field {key!r}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{where}: needs at least {schema['minItems']} items, has {len(instance)}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{where}: allows at most {schema['maxItems']} items, has {len(instance)}")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{where}[{index}]"))
    if isinstance(instance, str):
        if "minLength" in schema and len(instance.strip()) < schema["minLength"]:
            errors.append(f"{where}: needs at least {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{where}: allows at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{where}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{where}: {instance} is below the minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{where}: {instance} is above the maximum {schema['maximum']}")
    return errors


def require_valid(instance, schema_name: str, label: str) -> None:
    errors = validate(instance, load_schema(schema_name))
    if errors:
        shown = "; ".join(errors[:6])
        more = f" (+{len(errors) - 6} more)" if len(errors) > 6 else ""
        raise ArtefactError(f"{label} does not match the {schema_name} schema: {shown}{more}")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

_FRACTION_RE = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d+)")


def parse_iso(value) -> datetime:
    """ISO-8601 to an aware UTC datetime; naive means UTC; anything else raises ArtefactError.

    Any number of fractional digits is accepted: Protobuf's JSON form of a
    timestamp prints up to nine, and datetime keeps six.
    """
    if not isinstance(value, str) or not value.strip():
        raise ArtefactError(f"expected an ISO-8601 timestamp, got {value!r}")
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(lambda m: f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}", text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ArtefactError(f"cannot parse timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_iso(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def offset_seconds(anchor_iso: str, time_iso: str) -> float:
    """Seconds from ``anchor_iso`` to ``time_iso``, as course_setup.offset_seconds computes them."""
    return (parse_iso(time_iso) - parse_iso(anchor_iso)).total_seconds()


def overlap_seconds(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


# ---------------------------------------------------------------------------
# HTTP against http://nginx (fail closed)
# ---------------------------------------------------------------------------

def request_json(ctx: Context, method: str, url: str, body=None, timeout: float = 30.0,
                 allow: Sequence[int] = ()) -> Tuple[int, object]:
    """One JSON request: (status, decoded body).

    A status listed in ``allow`` is returned with whatever JSON its body holds
    (None when it holds none). Any other non-2xx status, a transport failure
    or a 2xx body that is not JSON raises ServiceError.
    """
    payload = None if body is None else json.dumps(body).encode("utf-8")
    status, raw = ctx.http(method, url, payload, timeout)
    preview = raw[:BODY_PREVIEW_CHARS].decode("utf-8", errors="replace")
    if status in allow:
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            return status, None
    if not 200 <= status < 300:
        raise ServiceError(f"{method} {url} -> HTTP {status}: {preview}")
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServiceError(f"{method} {url} -> HTTP {status} but the body is not JSON: {preview}") from exc


def http_json(ctx: Context, method: str, url: str, body=None, timeout: float = 30.0):
    """One JSON request. Transport failure, non-2xx and non-JSON bodies raise ServiceError."""
    return request_json(ctx, method, url, body, timeout)[1]


# ---------------------------------------------------------------------------
# VIOS
# ---------------------------------------------------------------------------

Timeline = List[Tuple[datetime, datetime]]


def vios_sensors(ctx: Context) -> Dict[str, dict]:
    """``sensorId -> record`` from ``GET /vios/vst/api/v1/sensor/list``."""
    data = http_json(ctx, "GET", VIOS_URL + "/sensor/list")
    if not isinstance(data, list):
        raise ServiceError(f"GET {VIOS_URL}/sensor/list: expected a list, got {type(data).__name__}")
    sensors: Dict[str, dict] = {}
    for record in data:
        if isinstance(record, dict) and isinstance(record.get("sensorId"), str):
            sensors[record["sensorId"]] = record
    return sensors


def vios_timeline(ctx: Context, sensor_id: str) -> Timeline:
    """Recorded segments for one sensor as (start, end) pairs, earliest first.

    ``GET /vios/vst/api/v1/storage/{sensor}/timelines`` returns a JSON list of
    ``{"startTime", "endTime"}`` (the shape the course's notebooks read). A
    dict or a segment without both fields is a service error, never
    "no footage".
    """
    data = http_json(ctx, "GET", f"{VIOS_URL}/storage/{sensor_id}/timelines")
    if not isinstance(data, list):
        raise ServiceError(f"timelines for {sensor_id}: expected a list, got {type(data).__name__}")
    segments: Timeline = []
    for item in data:
        if not isinstance(item, dict) or "startTime" not in item or "endTime" not in item:
            raise ServiceError(f"timelines for {sensor_id}: segment without startTime/endTime: {item!r}")
        try:
            segments.append((parse_iso(item["startTime"]), parse_iso(item["endTime"])))
        except ArtefactError as exc:
            raise ServiceError(f"timelines for {sensor_id}: {exc}") from exc
    segments.sort()
    return segments


def sensor_name_matches(name, stem: str) -> bool:
    """VIOS names an uploaded file after its stem and appends ``_<n>`` (``rain_check_1``)."""
    return isinstance(name, str) and (name == stem or name.startswith(stem + "_"))


# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------

def es_count(ctx: Context, index: str, query: dict) -> Optional[int]:
    """Root documents in ``index`` (a name or a pattern) that match ``query``.

    ``POST /es/<index>/_count``. Returns None when a named index does not
    exist (HTTP 404). A pattern that matches no index counts 0, which is how
    Elasticsearch answers it.
    """
    url = f"{ES_URL}/{index}/_count"
    status, data = request_json(ctx, "POST", url, {"query": query}, allow=(404,))
    if status == 404:
        return None
    count = data.get("count") if isinstance(data, dict) else None
    if isinstance(count, bool) or not isinstance(count, int):
        raise ServiceError(f"POST {url}: count is not an integer: {json.dumps(data)[:BODY_PREVIEW_CHARS]}")
    return count


def es_search(ctx: Context, index: str, body: dict) -> Optional[dict]:
    """``POST /es/<index>/_search``: the decoded response, or None when a named index does not exist."""
    url = f"{ES_URL}/{index}/_search"
    status, data = request_json(ctx, "POST", url, body, timeout=60.0, allow=(404,))
    if status == 404:
        return None
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        raise ServiceError(f"POST {url}: the response has no hits list: {json.dumps(data)[:BODY_PREVIEW_CHARS]}")
    return data


def es_hits(response: Optional[dict]) -> List[dict]:
    """The hits of an ``es_search`` response (none for a missing index); each must carry a ``_source`` object."""
    if response is None:
        return []
    hits = response["hits"]["hits"]
    for hit in hits:
        if not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict):
            raise ServiceError(f"an Elasticsearch hit has no _source object: {json.dumps(hit)[:BODY_PREVIEW_CHARS]}")
    return hits


# ---------------------------------------------------------------------------
# RT-Embed
# ---------------------------------------------------------------------------

def text_embedding(ctx: Context, text: str) -> List[float]:
    """Cosmos-Embed1's vector for one query, as ``course_setup.text_embedding`` asks RT-Embed for it.

    ``POST /rtvi-embed/v1/generate_text_embeddings`` with ``{"text_input": [text], "model": EMBED_MODEL}``;
    the vector is ``data[0].embeddings``. The text is sent exactly as given, so the grader embeds the
    learner's query, not a cleaned copy of it.
    """
    url = RT_EMBED_URL + "/generate_text_embeddings"
    data = http_json(ctx, "POST", url, {"text_input": [text], "model": EMBED_MODEL}, timeout=60.0)
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ServiceError(f"POST {url}: the response has no data list: {json.dumps(data)[:BODY_PREVIEW_CHARS]}")
    vector = items[0].get("embeddings")
    if not isinstance(vector, list) or not vector or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in vector):
        raise ServiceError(f"POST {url}: data[0].embeddings is not a list of numbers")
    return [float(v) for v in vector]


# ---------------------------------------------------------------------------
# The search-clip registry, ground truth and answer keys
# ---------------------------------------------------------------------------

def registry(ctx: Context) -> dict:
    """course-prep's ``search_sources.json``: ``{filename: {"sensor_id", "creation_time", ...}}``.

    course-prep writes it at launch; the assessment notebook reads the same
    file to search the four clips, so the grader searches the same sensors.
    """
    path = ctx.registry_file
    if not Path(path).is_file():
        raise ArtefactError(f"course-prep's list of the four search videos, {path}, is missing, so the grader "
                            "cannot repeat your search. Tell the instructor.")
    data = load_json(path)
    if not isinstance(data, dict):
        raise ArtefactError(f"{path} must map file names to their VIOS sensor ids. Tell the instructor.")
    return data


def registry_sensor_id(ctx: Context, filename: str) -> str:
    """The VIOS sensor id course-prep recorded for one search clip; a missing entry raises ArtefactError."""
    entry = registry(ctx).get(filename)
    sensor_id = entry.get("sensor_id") if isinstance(entry, dict) else None
    if not isinstance(sensor_id, str) or not sensor_id:
        raise ArtefactError(f"{ctx.registry_file} has no VIOS sensor id for {filename}. Tell the instructor.")
    return sensor_id


def ground_truth(ctx: Context) -> List[dict]:
    """The labelled search clips: ``[{"file", "duration_s", "events": [{"type", "start_s", "end_s"}]}]``, clip seconds."""
    path = ctx.ground_truth_file
    try:
        data = load_json(path)
    except ArtefactError as exc:
        raise ArtefactError(f"{exc}. Tell the instructor.") from None
    if not isinstance(data, list) or not data:
        raise ArtefactError(f"{path}: expected a non-empty list of clips. Tell the instructor.")
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not isinstance(entry.get("events"), list):
            raise ArtefactError(f"{path}: every clip needs a file name and an events list: {entry!r}")
        for event in entry["events"]:
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ArtefactError(f"{path}: malformed event in {entry['file']}: {event!r}")
            start, end = event.get("start_s"), event.get("end_s")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)) or end <= start:
                raise ArtefactError(f"{path}: event of {entry['file']} needs start_s < end_s: {event!r}")
    return data


def ground_truth_entry(ctx: Context, filename) -> Optional[dict]:
    """The ground-truth entry of one search clip, matched on the file stem (``rain_eval`` or ``rain_eval.mp4``).

    None when the clip is not one of the labelled search clips; a missing or
    malformed ground-truth file raises ArtefactError.
    """
    wanted = Path(str(filename)).stem
    for entry in ground_truth(ctx):
        if Path(entry["file"]).stem == wanted:
            return entry
    return None


def answer_key_problems(key) -> List[str]:
    """What is wrong with a multiple-choice key: {"version": int, "questions": {id: letter}, "min_correct": int}."""
    if not isinstance(key, dict):
        return ["the key must be a JSON object"]
    problems = []
    version = key.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        problems.append("version must be an integer")
    questions = key.get("questions")
    if not isinstance(questions, dict) or not questions:
        problems.append("questions must map question ids to letters")
        return problems
    bad = sorted(q for q, letter in questions.items() if not (isinstance(letter, str) and LETTER_RE.match(letter)))
    if bad:
        problems.append(f"each answer must be one capital letter, A to H: {bad}")
    least = key.get("min_correct")
    if isinstance(least, bool) or not isinstance(least, int) or not 1 <= least <= len(questions):
        problems.append(f"min_correct must be an integer from 1 to {len(questions)}")
    return problems


def answer_key(ctx: Context, assessment: str) -> dict:
    """``<answer_keys_dir>/<assessment>.json``, checked for shape. Never shipped to the lab."""
    path = ctx.answer_keys_dir / f"{assessment}.json"
    try:
        key = load_json(path)
    except ArtefactError as exc:
        raise ArtefactError(f"{exc}. Tell the instructor.") from None
    problems = answer_key_problems(key)
    if problems:
        raise ArtefactError(f"{path}: {'; '.join(problems)}. Tell the instructor.")
    return key


# ---------------------------------------------------------------------------
# Checks and markers
# ---------------------------------------------------------------------------

def check(check_id: str, passed: bool, detail: str) -> dict:
    return {"id": check_id, "passed": bool(passed), "detail": detail}


CheckFn = Callable[[], Tuple[bool, str]]


def run_checks(steps: Sequence[Tuple[str, CheckFn]]) -> List[dict]:
    """Run each check; convert every failure signal into a failed check with a detail line.

    Nothing is swallowed: the detail carries the message, and anything that is
    not one of the two expected signals is also written to stderr with its
    traceback so the instructor can see a grader bug for what it is.
    """
    results: List[dict] = []
    for check_id, fn in steps:
        try:
            passed, detail = fn()
        except ServiceError as exc:
            passed, detail = False, (f"A service did not answer, so this check cannot pass yet ({exc}). Wait a minute "
                                     "and submit again. If it happens again, tell the instructor.")
        except ArtefactError as exc:
            passed, detail = False, str(exc)
        except Exception as exc:  # a grader bug must fail the check visibly, not pass it or hide it
            traceback.print_exc(file=sys.stderr)
            passed, detail = False, f"The grader hit a bug ({type(exc).__name__}: {exc}). Tell the instructor."
        results.append(check(check_id, passed, detail))
    return results


def build_marker(ctx: Context, assessment: str, results: Sequence[dict]) -> dict:
    """Order the results by rubric; a rubric check that was not evaluated counts as failed."""
    rubric_checks = ctx.rubric[assessment]["checks"]
    by_id = {r["id"]: r for r in results}
    checks: List[dict] = []
    for spec in rubric_checks:
        result = by_id.get(spec["id"]) or check(spec["id"], False, "not evaluated")
        checks.append({"id": spec["id"], "must": bool(spec.get("must", True)), "passed": result["passed"], "detail": result["detail"]})
    unknown = [r["id"] for r in results if r["id"] not in {s["id"] for s in rubric_checks}]
    if unknown:
        raise ArtefactError(f"grader produced checks that rubric.json does not list for {assessment}: {unknown}")
    return {
        "assessment": assessment,
        "passed": all(c["passed"] for c in checks if c["must"]),
        "checks": checks,
        "graded_at": fmt_iso(ctx.now()),
        "rubric_version": ctx.rubric.get("version"),
    }


def marker_path(ctx: Context, assessment: str) -> Path:
    return ctx.results_dir / f"{assessment}.json"


def write_marker(ctx: Context, marker: dict) -> Path:
    """Atomic write of ``/dli/assessment_results/<assessment>.json``."""
    ctx.results_dir.mkdir(parents=True, exist_ok=True)
    target = marker_path(ctx, marker["assessment"])
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def summarize(marker: dict) -> str:
    """One line per check, for logs and the instructor's terminal."""
    lines = [f"{marker['assessment']}: {'PASS' if marker['passed'] else 'FAIL'}"]
    for item in marker["checks"]:
        flag = "ok  " if item["passed"] else "FAIL"
        must = "" if item.get("must", True) else " (should)"
        lines.append(f"  {flag} {item['id']}{must}: {item['detail']}")
    return "\n".join(lines)
