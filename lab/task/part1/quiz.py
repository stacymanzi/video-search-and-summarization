"""Multiple-choice questions and live task checks for the Part 1 assessment.

A question is shown in a markdown cell. You answer it in the code cell under it:

    quiz.answer("p1-q3", "B")

The answer key is not stored in plain text. Each question keeps a hash of its
correct option, and the explanation is encrypted with that option, so it shows
only once you pick it. This keeps the key out of sight while you read the file;
it is not meant to stop someone determined. The key loads from
assessment_key.json next to this file, which task1/scripts/make_quiz_key.py
writes from the grader's plaintext key.

A task saves its result in answers.json and then runs a live check:

    quiz.save("t1", {"vios_sensor_id": sensor_id, "vios_name": name})
    quiz.task("p1-t1", "Add the clip to VIOS", quiz.check_t1)

A check returns (passed: bool, message: str). The check_* functions below read
your saved answer back from answers.json and test it against the live services
through http://nginx, as the grader does when you submit, with the same
numbers. They use the standard library only.

Every answer and every check is also recorded in results.json, so the last
cells of the assessment can print a summary with quiz.summary().
answers.json is the file the grader reads.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WORK = Path(os.environ.get("ASSESSMENT_DIR", "/dli/task/work/assessment"))
RESULTS = Path(os.environ.get("ASSESSMENT_RESULTS", str(WORK / "results.json")))
ANSWERS = Path(os.environ.get("ASSESSMENT_ANSWERS", str(WORK / "answers.json")))
KEY_FILE = Path(os.environ.get("ASSESSMENT_KEY", str(Path(__file__).with_name("assessment_key.json"))))
GROUND_TRUTH = Path(os.environ.get("ASSESSMENT_GROUND_TRUTH", "/dli/task/data/ground_truth/search_videos.json"))
REGISTRY = Path(os.environ.get("ASSESSMENT_REGISTRY", "/dli/task/data/search_sources.json"))
BASE = os.environ.get("ASSESSMENT_BASE_URL", "http://nginx")

# The grader uses the same numbers (rubric.json, part1.thresholds). Change both together.
RAW_INDEX = "mdx-raw-2025-01-01"   # RT-CV frame records of file uploads, whatever the upload timestamp
CHUNK_S = 10                       # the chunk length T4 and T7 use
DAY_S = 24 * 60 * 60               # T7: one day of live video, in seconds
CLIP_CHUNKS_10 = (3, 4)            # a cut that runs a fraction of a second past 30 s adds a short fourth chunk
FRAMES_PER_CHUNK = 8               # the sampled frames T4 asks for
TOLERANCE = 0.15                   # T7: a number within 15 % counts
KAFKA_SLACK_S = 0.5                # T3: the Kafka time and the indexed time of one frame agree to this

# T5 repeats notebook 4's search: course_setup.text_embedding, then course_setup.knn_search(vector, ids, k=5).
EMBED_INDEX = "mdx-embed-filtered-2025-01-01"
EMBED_MODEL = "cosmos-embed1-448p-anomaly-detection"
EMBEDDING_ANCHOR = "2025-01-01T00:00:00.000Z"   # the embedding index starts every video here
SEARCH_FILES = ("normal_eval.mp4", "rain_eval.mp4", "fog_eval.mp4", "night_eval.mp4")
SEARCH_K = 5
SEARCH_SLACK_S = 0.1               # T5: your window and the search's top window agree to this, at each end

# qid -> (sha256 of "qid:option", encrypted explanation as hex, number of options)
KEY: dict[str, tuple[str, str, int]] = {}
_KEY_ERROR: str | None = None


# ---------------------------------------------------------------------------
# The answer key
# ---------------------------------------------------------------------------

def _digest(qid: str, option: str) -> str:
    return hashlib.sha256(f"{qid}:{option.strip().upper()}".encode()).hexdigest()


def _stream(qid: str, option: str, n: int) -> bytes:
    out = b""
    counter = 0
    seed = f"explain:{qid}:{option.strip().upper()}".encode()
    while len(out) < n:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:n]


def make_key(qid: str, correct: str, explanation: str, options: int = 4) -> tuple[str, str, int]:
    """Author helper: the KEY entry for one question. Not used by learners."""
    data = explanation.encode()
    cipher = bytes(a ^ b for a, b in zip(data, _stream(qid, correct, len(data))))
    return (_digest(qid, correct), cipher.hex(), options)


def load_key(entries: dict[str, list]) -> None:
    for qid, (digest, cipher, options) in entries.items():
        KEY[qid] = (digest, cipher, int(options))


def load_key_file(path: Path | str | None = None) -> int:
    """Load the hashed key file ({"questions": {qid: [digest, cipher, options]}}); return the number of questions."""
    path = Path(path) if path is not None else KEY_FILE
    data = json.loads(path.read_text())
    entries = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError(f"{path} has no 'questions' entries")
    load_key(entries)
    return len(entries)


def _ensure_key() -> None:
    if not KEY:
        reason = _KEY_ERROR or f"{KEY_FILE} is missing"
        raise RuntimeError(f"The answer key could not be loaded: {reason}. Call the instructor.")


if KEY_FILE.is_file():
    try:
        load_key_file(KEY_FILE)
    except (OSError, ValueError, TypeError) as exc:   # reported by answer(), where the learner sees it
        _KEY_ERROR = f"{KEY_FILE}: {exc}"


# ---------------------------------------------------------------------------
# results.json (your progress) and answers.json (what the grader reads)
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _record(item: str, payload: dict) -> None:
    data = _read_json(RESULTS)
    data[item] = payload
    _write_json(RESULTS, data)


def answers() -> dict:
    """Everything saved in answers.json so far."""
    return _read_json(ANSWERS)


def save(key: str, value):
    """Save one answer in answers.json. Other keys stay as they are; "questions" entries are merged."""
    if not key or not isinstance(key, str):
        raise ValueError("save: key must be a non-empty string")
    data = _read_json(ANSWERS)
    if key == "questions" and isinstance(value, dict):
        merged = dict(data.get("questions") or {})
        merged.update(value)
        value = merged
    data[key] = value
    _write_json(ANSWERS, data)
    return value


# ---------------------------------------------------------------------------
# Questions, tasks and the summary
# ---------------------------------------------------------------------------

def answer(qid: str, option: str) -> bool:
    """Check one multiple-choice answer, print the result, and record it."""
    _ensure_key()
    if qid not in KEY:
        raise KeyError(f"unknown question id {qid!r}")
    digest, cipher, options = KEY[qid]
    choice = (option or "").strip().upper()
    letters = "ABCDEFGH"[:options]
    if len(choice) != 1 or choice not in letters:
        print(f"{qid}: answer with one letter, {', '.join(letters)}.")
        return False
    correct = _digest(qid, choice) == digest
    if correct:
        data = bytes.fromhex(cipher)
        text = bytes(a ^ b for a, b in zip(data, _stream(qid, choice, len(data)))).decode()
        print(f"{qid}: {choice} is correct. {text}")
    else:
        print(f"{qid}: {choice} is not the best answer. Reread the question and try again.")
    _record(qid, {"kind": "question", "answer": choice, "correct": correct})
    save("questions", {qid: choice})
    return correct


def task(tid: str, title: str, check) -> bool:
    """Run one live check for a hands-on task, print the result, and record it."""
    try:
        passed, message = check()
    except Exception as exc:  # a failed request is a result, not a crash
        passed, message = False, f"{type(exc).__name__}: {exc}"
    message = str(message)
    message = message[:1].upper() + message[1:]    # the message follows "PASS." or "NOT YET." as a sentence
    print(f"{tid} {title}: {'PASS' if passed else 'NOT YET'}. {message}")
    _record(tid, {"kind": "task", "title": title, "passed": bool(passed), "message": message})
    return bool(passed)


def summary() -> dict:
    """Print every recorded task and answer, and return the counts."""
    data = _read_json(RESULTS)
    tasks = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("kind") == "task"}
    questions = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("kind") == "question"}
    for k in sorted(tasks):
        print(f"{k:8} {'PASS   ' if tasks[k]['passed'] else 'NOT YET'} {tasks[k]['title']}")
    for k in sorted(questions):
        print(f"{k:8} {'correct' if questions[k]['correct'] else 'not yet'}")
    counts = {
        "tasks_passed": sum(1 for v in tasks.values() if v["passed"]),
        "tasks": len(tasks),
        "questions_correct": sum(1 for v in questions.values() if v["correct"]),
        "questions": len(questions),
    }
    print(f"\nTasks passed: {counts['tasks_passed']} of {counts['tasks']}. "
          f"Questions correct: {counts['questions_correct']} of {counts['questions']}.")
    print(f"Your answers for the grader are in {ANSWERS}.")
    return counts


# ---------------------------------------------------------------------------
# HTTP through the course proxy (the seam the tests replace)
# ---------------------------------------------------------------------------

def _http(method: str, path: str, body=None, timeout: float = 60):
    """One request to BASE + path. Returns (status, decoded body). Raises only when nothing answers."""
    url = BASE + path
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"{method} {url}: no response ({exc})") from exc
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, raw.decode(errors="replace")[:300]


def _json(method: str, path: str, body=None, timeout: float = 60):
    status, data = _http(method, path, body, timeout)
    if not 200 <= status < 300:
        raise RuntimeError(f"{method} {BASE + path} -> HTTP {status}: {str(data)[:300]}")
    return data


def _count(index: str, query: dict) -> int:
    """Root documents matching `query`; 0 while the index does not exist yet."""
    status, data = _http("POST", f"/es/{index}/_count", {"query": query}, timeout=30)
    if status == 404:
        return 0
    if status != 200 or not isinstance(data, dict) or "count" not in data:
        raise RuntimeError(f"POST {BASE}/es/{index}/_count -> HTTP {status}: {str(data)[:300]}")
    return int(data["count"])


def _stem_term(stem: str) -> dict:
    return {"term": {"sensorId.keyword": stem}}


def _stem_or_suffix(stem: str) -> dict:
    return {"bool": {"should": [{"term": {"sensorId.keyword": stem}},
                                {"prefix": {"sensorId.keyword": stem + "_"}}],
                     "minimum_should_match": 1}}


def _parse_time(value) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected an ISO 8601 time, got {value!r}")
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    head, dot, rest = text.partition(".")
    if dot:   # keep at most microseconds: Protobuf times can carry nanoseconds
        n = 0
        while n < len(rest) and rest[n].isdigit():
            n += 1
        text = head + "." + (rest[:n] + "000000")[:6] + rest[n:]
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _answer(key: str) -> dict:
    value = answers().get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"answers.json has no {key!r} entry yet: run the cell above this check first")
    return value


def _clip_stem() -> str:
    stem = answers().get("clip_stem")
    if not isinstance(stem, str) or not stem:
        raise RuntimeError("answers.json has no clip_stem: run the setup cell at the top of the notebook")
    return stem


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def wait_until_steady(stem: str, *, index: str = RAW_INDEX, every_s: float = 10, steady_s: float = 20,
                      timeout_s: float = 600) -> int:
    """Wait until RT-CV's frame count for one clip stops growing, and return it.

    Polls the count every `every_s` seconds and returns once it is above zero and
    has not changed for `steady_s` seconds. Prints every poll. Raises TimeoutError
    after `timeout_s`.
    """
    started = time.monotonic()
    last, since = None, started
    while True:
        count = _count(index, _stem_term(stem))
        now = time.monotonic()
        if count != last:
            last, since = count, now
        elapsed = now - started
        if count > 0 and now - since >= steady_s:
            print(f"RT-CV has finished {stem}: {count} frame records, steady for {now - since:.0f} s.")
            return count
        hint = ""
        if count == 0 and elapsed >= 120:
            hint = " Still nothing: check that the third call of T1 (complete) ran."
        print(f"{elapsed:4.0f} s: {count} frame records for {stem}.{hint}", flush=True)
        if elapsed >= timeout_s:
            raise TimeoutError(f"the frame count for {stem} did not settle within {timeout_s:.0f} s (last count {count})")
        time.sleep(every_s)


# ---------------------------------------------------------------------------
# Part 1 task checks. Each reads your saved answer and returns (passed, message).
# ---------------------------------------------------------------------------

def check_t1():
    """T1: VIOS lists the clip as <stem> or <stem>_<n>, with a timeline."""
    stem, t1 = _clip_stem(), _answer("t1")
    sensor_id = t1.get("vios_sensor_id")
    listed = _json("GET", "/vios/vst/api/v1/sensor/list", timeout=30)
    record = next((s for s in listed or [] if isinstance(s, dict) and s.get("sensorId") == sensor_id), None)
    if record is None:
        return False, f"VIOS lists no source with the sensor id {sensor_id!r}. Run the upload cell, then this check."
    name = record.get("name") or ""
    if not (name == stem or name.startswith(stem + "_")):
        return False, f"that sensor id belongs to {name!r}, not to {stem}. Upload {stem}.mp4 and save its sensorId."
    if t1.get("vios_name") != name:
        return False, f"you saved the VIOS name {t1.get('vios_name')!r}, but VIOS names this source {name!r}."
    spans = _json("GET", f"/vios/vst/api/v1/storage/{sensor_id}/timelines", timeout=30)
    if not isinstance(spans, list) or not spans:
        return False, f"VIOS has no timeline for {name}: no video was stored. Run the upload cell again."
    frames = _count(RAW_INDEX, _stem_term(stem))
    if frames:
        note = f"RT-CV has written {frames} frame records so far."
    else:
        note = "RT-CV has written no records yet. If that stays so for a minute, the third call (complete) did not run."
    return True, f"VIOS lists {name} with a timeline that starts at {spans[0].get('startTime')}. {note}"


def check_t2():
    """T2: the frame count and the most frequent object type match Elasticsearch now."""
    stem, t2 = _clip_stem(), _answer("t2")
    frames, top_type = t2.get("frames"), t2.get("top_type")
    if not _is_int(frames):
        return False, f"frames must be a whole number, got {frames!r}."
    exact = _count(RAW_INDEX, _stem_term(stem))
    wide = _count(RAW_INDEX, _stem_or_suffix(stem))
    if exact == 0 and wide == 0:
        return False, f"Elasticsearch holds no RT-CV records for {stem} yet. Run the wait cell first."
    if frames not in (exact, wide):
        if frames < exact:
            return False, ("your count is lower than the number of records in Elasticsearch now. RT-CV was still "
                           "writing: run the wait cell, then your cell and this check again.")
        return False, f"your count does not match the records for {stem}. Filter on the field RT-CV writes the file stem to."
    result = _json("POST", f"/es/{RAW_INDEX}/_search", {
        "size": 0, "query": _stem_term(stem),
        "aggs": {"objects": {"nested": {"path": "objects"},
                             "aggs": {"types": {"terms": {"field": "objects.type.keyword", "size": 10}}}}}})
    buckets = result["aggregations"]["objects"]["types"]["buckets"]
    if not buckets:
        return False, f"the records for {stem} hold no objects yet."
    top = buckets[0]
    if top_type != top["key"]:
        return False, f"{top_type!r} is not the most frequent object type. Count the types inside the nested objects."
    return True, f"{frames} frame records; the most frequent object type is {top['key']} ({top['doc_count']} detections)."


def check_t3():
    """T3: the decoded Kafka message comes from the clip, and Elasticsearch holds the same frame."""
    stem, t3 = _clip_stem(), _answer("t3")
    if t3.get("sensor_id") != stem:
        return False, f"the message you saved comes from {t3.get('sensor_id')!r}, not {stem}. Keep a message whose sensorId is {stem}."
    frame_id, stamp = str(t3.get("frame_id") or ""), t3.get("timestamp")
    if not frame_id:
        return False, "the saved frame_id is empty: save frame.id from the decoded message."
    when = _parse_time(stamp)
    found = _json("POST", f"/es/{RAW_INDEX}/_search", {
        "size": 10, "_source": ["id", "timestamp", "sensorId"],
        "query": {"bool": {"filter": [
            _stem_term(stem),
            {"bool": {"should": [{"term": {"id.keyword": frame_id}}, {"term": {"id": frame_id}}],
                      "minimum_should_match": 1}}]}}})
    hits = found["hits"]["hits"]
    if not hits:
        return False, f"Elasticsearch has no record of frame {frame_id} for {stem}. Save frame.id, not a track id."
    times = [hit["_source"].get("timestamp") for hit in hits]
    if not any(abs((_parse_time(t) - when).total_seconds()) <= KAFKA_SLACK_S for t in times if t):
        return False, f"frame {frame_id} is indexed at {times[0]}, not at {stamp}. Save frame.timestamp.ToJsonString()."
    return True, f"frame {frame_id} of {stem} at {stamp}: Logstash indexed the same frame in Elasticsearch."


def _frame_counts_ok(counts) -> bool:
    """3 or 4 chunks. The first three report FRAMES_PER_CHUNK frames; a fourth is a sub-second tail and may hold fewer."""
    if not isinstance(counts, list) or len(counts) not in CLIP_CHUNKS_10:
        return False
    full, tail = counts[:3], counts[3:]
    return (all(_is_int(c) and c == FRAMES_PER_CHUNK for c in full)
            and all(_is_int(c) and 1 <= c <= FRAMES_PER_CHUNK for c in tail))


def check_t4(*, wait_s: float = 60, every_s: float = 3):
    """T4: 3 chunks at 10 s (4 when the cut runs a little past 30 s), 1 chunk at 0, 8 frames in each full chunk
    of the 8-frame run, and the captions indexed."""
    t4 = _answer("t4")
    problems = []
    chunks_10, chunks_0 = t4.get("chunks_10"), t4.get("chunks_0")
    if not _is_int(chunks_10) or chunks_10 not in CLIP_CHUNKS_10:
        problems.append(f"with chunk_duration=10 you got {chunks_10} chunks; a 30-second clip gives 3, or 4 when the "
                        "cut runs a little past 30 s")
    if not _is_int(chunks_0) or chunks_0 != 1:
        problems.append(f"with chunk_duration=0 you got {chunks_0} chunks; 0 means one chunk for the whole clip")
    counts = t4.get("frame_counts_8")
    if isinstance(counts, list) and counts and all(c is None for c in counts):
        problems.append("RT-VLM did not report frame_count for its chunks. Tell the instructor")
    elif not _frame_counts_ok(counts):
        if isinstance(counts, list) and len(counts) not in CLIP_CHUNKS_10:
            problems.append(f"the 8-frame request returned {len(counts)} chunk(s), not 3 (or 4 with a short last "
                            "one). Name the field that sets the chunk length, with the value 10")
        else:
            problems.append(f"the 8-frame request reported frame_count {counts}; each full 10-second chunk should "
                            f"report {FRAMES_PER_CHUNK}. Keep the two frames fields as the cell gives them")
    if problems:
        return False, "; ".join(problems) + "."
    file_id = t4.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return False, "the saved file_id is empty: save the RT-VLM file id the registration returned."
    index = "default_" + file_id.replace("-", "_")
    deadline = time.monotonic() + wait_s
    while True:
        stored = _count(index, {"match_all": {}})
        if stored > 0:
            break
        if time.monotonic() >= deadline:
            return False, f"RT-VLM answered, but {index} is still empty after {wait_s:.0f} s. Run this check again in a minute."
        time.sleep(every_s)
    return True, (f"{chunks_10} chunks at 10 s, 1 chunk at 0, {FRAMES_PER_CHUNK} frames in each full chunk of the "
                  f"8-frame run; {stored} captions stored in {index}.")


def _search_sensors() -> dict:
    """VIOS sensor id -> file name for the four search videos, read from course-prep's registry."""
    try:
        registry = json.loads(REGISTRY.read_text())
    except FileNotFoundError:
        raise RuntimeError(f"{REGISTRY} is missing, so the search cannot run. Tell the instructor.") from None
    by_sensor = {}
    for name in SEARCH_FILES:
        entry = registry.get(name) if isinstance(registry, dict) else None
        sensor_id = entry.get("sensor_id") if isinstance(entry, dict) else None
        if not isinstance(sensor_id, str) or not sensor_id:
            raise RuntimeError(f"{REGISTRY} has no sensor id for {name}. Tell the instructor.")
        by_sensor[sensor_id] = name
    return by_sensor


def _search_body(vector: list, sensor_ids: list, k: int = SEARCH_K) -> dict:
    """The request course_setup.knn_search sends: a nested kNN on the chunk embeddings of these VIOS sources only."""
    ids = list(sensor_ids)
    return {
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


def _reference_top_window(query: str) -> tuple:
    """(file name, start s, end s) of the top hit for `query` over the four search videos.

    The two requests of the T5 cell: the Cosmos-Embed1 text embedding, then the kNN search with k=5. The
    times are seconds from EMBEDDING_ANCHOR.
    """
    data = _json("POST", "/rtvi-embed/v1/generate_text_embeddings", {"text_input": [query], "model": EMBED_MODEL})
    items = data.get("data") if isinstance(data, dict) else None
    vector = items[0].get("embeddings") if isinstance(items, list) and items and isinstance(items[0], dict) else None
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("RT-Embed returned no text embedding for your query")
    by_sensor = _search_sensors()
    found = _json("POST", f"/es/{EMBED_INDEX}/_search", _search_body(vector, list(by_sensor)))
    hits = ((found or {}).get("hits") or {}).get("hits") or []
    if not hits:
        raise RuntimeError(f"the search returned no window from {EMBED_INDEX}. Tell the instructor.")
    top = hits[0].get("_source") or {}
    anchor = _parse_time(EMBEDDING_ANCHOR)
    start = (_parse_time(top.get("timestamp")) - anchor).total_seconds()
    end = (_parse_time(top.get("end")) - anchor).total_seconds()
    return by_sensor.get((top.get("sensor") or {}).get("id")), start, end


def _collision_note(name: str, start: float, end: float) -> str:
    """One sentence on whether a window overlaps a labelled collision. It never changes a result."""
    try:
        truth = json.loads(GROUND_TRUTH.read_text())
    except FileNotFoundError:
        return "The labels file is missing: tell the instructor."
    except ValueError:
        return "The labels file cannot be read: tell the instructor."
    entries = truth if isinstance(truth, list) else []
    entry = next((item for item in entries if isinstance(item, dict) and item.get("file") == name), None) or {}
    for event in entry.get("events") or []:
        if event.get("type") == "collision" and start < float(event["end_s"]) and end > float(event["start_s"]):
            return (f"It overlaps the labelled collision at {float(event['start_s']):.1f} to "
                    f"{float(event['end_s']):.1f} s.")
    return "It overlaps no labelled collision. Watch it and try another wording."


def check_t5():
    """T5: the T5 search, run again with your saved query, gives your saved window first.

    The window is compared in seconds from EMBEDDING_ANCHOR, to SEARCH_SLACK_S at each end. Whether it
    overlaps a labelled collision is reported, but it does not decide the result.
    """
    t5 = _answer("t5")
    query, name, start, end = t5.get("query"), t5.get("file"), t5.get("start_s"), t5.get("end_s")
    if not isinstance(query, str) or not query.strip():
        return False, "save the query you searched with: answers.json has no query for T5."
    if not (_is_number(start) and _is_number(end)):
        return False, f"the saved start_s and end_s must be numbers, got {start!r} and {end!r}."
    top_name, top_start, top_end = _reference_top_window(query)
    found = f"the same search gives {top_name} {top_start:.1f} to {top_end:.1f} s"
    if name != top_name:
        return False, f"{found}, not a window of {name}. Run your search cell again, then this check."
    if abs(start - top_start) > SEARCH_SLACK_S or abs(end - top_end) > SEARCH_SLACK_S:
        return False, (f"{found}. Run your T5 cell again with the same query, then this check: the cell measures the "
                       "hit's times from the anchor the embedding index uses.")
    return True, (f"your window is the top hit for {query!r}: {name} {start:.1f} to {end:.1f} s. "
                  + _collision_note(name, start, end))


def _object_knn(stem: str, vector: list, frame_time: str) -> dict:
    """The T6 search: the frame of this clip, other than `frame_time`, whose objects look most like `vector`."""
    return {
        "size": 1,
        "_source": {"excludes": ["objects.embedding"]},
        "query": {"nested": {
            "path": "objects",
            "score_mode": "max",
            "inner_hits": {"size": 1, "_source": False},
            "query": {"knn": {
                "field": "objects.embedding.vector",
                "query_vector": list(vector),
                "k": 10,
                "num_candidates": 100,
                "filter": {"bool": {"filter": [_stem_or_suffix(stem)],
                                    "must_not": [{"term": {"timestamp": frame_time}}]}},
            }},
        }},
    }


def check_t6(query_frame_time: str | None = None):
    """T6: answer_object_id is the top hit of the nested kNN search for the given object.

    The query frame is the saved t6.query_frame_time unless `query_frame_time` is passed.
    """
    stem, t6 = _clip_stem(), _answer("t6")
    query_id, answer_id = str(t6.get("query_object_id") or ""), str(t6.get("answer_object_id") or "")
    if not query_id or not answer_id:
        return False, "save both query_object_id and answer_object_id."
    query_frame_time = query_frame_time or t6.get("query_frame_time")
    if not isinstance(query_frame_time, str) or not query_frame_time:
        return False, "save query_frame_time, the time of the frame the car came from."
    frame = _json("POST", f"/es/{RAW_INDEX}/_search", {
        "size": 5, "query": {"bool": {"filter": [_stem_or_suffix(stem), {"term": {"timestamp": query_frame_time}}]}}})
    vector = None
    for hit in frame["hits"]["hits"]:
        for obj in hit["_source"].get("objects") or []:
            if str(obj.get("id")) == query_id and (obj.get("embedding") or {}).get("vector"):
                vector = obj["embedding"]["vector"]
                break
        if vector:
            break
    if vector is None:
        return False, f"object {query_id} is not in the frame at {query_frame_time}. Run the cell that picks the car again."
    hits = _json("POST", f"/es/{RAW_INDEX}/_search", _object_knn(stem, vector, query_frame_time))["hits"]["hits"]
    if not hits:
        return False, "the nested kNN search returned no frame."
    inner = hits[0].get("inner_hits", {}).get("objects", {}).get("hits", {}).get("hits") or []
    objects = hits[0].get("_source", {}).get("objects") or []
    offset = inner[0].get("_nested", {}).get("offset") if inner else None
    if not isinstance(offset, int) or not 0 <= offset < len(objects):
        return False, "the nested kNN search returned a frame without a matching object."
    best = str(objects[offset].get("id"))
    if answer_id != best:
        return False, (f"object {answer_id} is not the closest match. Search objects.embedding.vector with the "
                       "car's embedding, in every frame of the clip except its own.")
    same = " It is the same vehicle: the tracker kept its id across frames." if best == query_id else ""
    return True, f"object {best} in the frame at {hits[0]['_source'].get('timestamp')} is the closest match.{same}"


def check_t7():
    """T7: what captioning one camera for a day costs RT-VLM, one request at a time.

    latency_ms is within 15 % of the mean vlm_latency_ms T4 measured at chunk_duration=10,
    requests_per_day is the number of 10-second chunks in a day (8,640), and busy_seconds_per_day
    (GPU_SECONDS_PER_DAY in the notebook) is within 15 % of those requests times latency_ms / 1000.
    """
    t7 = _answer("t7")
    saved_path = WORK / "t4_captions.json"
    if not saved_path.is_file():
        return False, f"{saved_path} is missing: run the T4 cells first."
    saved = json.loads(saved_path.read_text())
    latencies = [chunk.get("vlm_latency_ms") for chunk in saved.get("chunk_duration_10") or []]
    if not latencies or not all(_is_number(v) and v > 0 for v in latencies):
        return False, "T4 did not record vlm_latency_ms for every chunk_duration=10 chunk. Run T4 again."
    mean = sum(latencies) / len(latencies)
    latency, requests, busy = t7.get("latency_ms"), t7.get("requests_per_day"), t7.get("busy_seconds_per_day")
    if not _is_number(latency) or latency <= 0 or abs(latency - mean) > TOLERANCE * mean:
        return False, "your LATENCY_MS is not the mean vlm_latency_ms of your chunk_duration=10 captions from T4."
    expected = DAY_S // CHUNK_S
    if not _is_number(requests) or requests != expected:
        return False, (f"your REQUESTS_PER_DAY is {requests!r}. Count the {CHUNK_S}-second chunks one camera "
                       "produces in 24 hours.")
    model_s = expected * latency / 1000
    if not _is_number(busy) or abs(busy - model_s) > TOLERANCE * model_s:
        return False, ("your GPU_SECONDS_PER_DAY does not match REQUESTS_PER_DAY times LATENCY_MS, converted from "
                       "milliseconds to seconds.")
    return True, (f"{expected:,} requests a day at {latency:.0f} ms each: {busy / 3600:.1f} GPU-hours a day for one "
                  "camera, one request at a time.")
