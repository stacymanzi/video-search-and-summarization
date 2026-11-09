"""grade_part1.py: the Part 1 assessment (task1/lab/task/part1/assessment.ipynb).

The notebook's task cells write their results to
``/dli/task/work/assessment/answers.json`` (shape: ``schemas/answers.schema.json``).
This grader reads that file and checks each answer again, against the running
services and the grader's own keys. Every check id, its public text and the
numbers the checks use are in ``rubric.json``. The clip is the rubric's
``clip_stem``, never the stem the answers file names.

The notebook runs its own check after every task (``part1/quiz.py``). The
grader repeats them as a second line, with the same rules, so a task the
notebook passed fails here only when the services changed. The exception is
T6 when answers.json does not name the query frame: the grader then searches
from the earliest frame that holds the object, and it also accepts the query
object's own track.

Checks, in rubric order (task ids as the notebook numbers them):

===================================  ======  ======  ==========================================================
check                                task    level   passes when
===================================  ======  ======  ==========================================================
part1.video_in_vios                  p1-t1   must    VIOS lists t1's sensor under t1's name (stem or stem_<n>) with a timeline
part1.detections_indexed             p1-t2   must    t2 equals the live mdx-raw frame count of the stem and names its top object type
part1.kafka_decoded                  p1-t3   should  t3 names the stem and a frame Elasticsearch holds with that id and timestamp
part1.captions_indexed               p1-t4   must    the chunk and frame counts are right and default_<file id> holds captions
part1.search_matches_reference       p1-t5   must    searching the four search clips for t5.query returns t5's window first
part1.search_hit_overlaps_collision  p1-t5   should  t5's window lies inside its search clip and overlaps a labelled collision
part1.appearance_match               p1-t6   should  t6's answer is the object the notebook's nested kNN ranks first
part1.throughput_estimate            p1-t7   should  t7 has 8,640 requests per camera per day and the RT-VLM seconds they take
part1.questions                      p1-q*   must    at least min_correct answers match answer_keys/part1.json
===================================  ======  ======  ==========================================================

Run by ``serve.py`` on ``POST /grade/part1``; ``python3 -I grade_part1.py``
grades once from the production paths and prints the summary.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grader import common as c  # noqa: E402

ASSESSMENT = "part1"

# The appearance search, exactly as the notebook's T6 cell and part1/quiz.py send it.
KNN_K = 10
KNN_CANDIDATES = 100

# An RT-VLM file id becomes part of an index name: letters, digits, "-" and "_" only, so no wildcard slips in.
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# A T5 window shifted this far from the top hit was measured from the wrong anchor: the embedding index puts every
# file at 2025-01-01, and VIOS keeps the search clips on timelines that start a day later.
ANCHOR_MISTAKE_S = 3600.0

# Check id -> method name, in rubric order (tests/test_contracts.py keeps the two in step).
CHECKS = (
    ("part1.video_in_vios", "video_in_vios"),
    ("part1.detections_indexed", "detections_indexed"),
    ("part1.kafka_decoded", "kafka_decoded"),
    ("part1.captions_indexed", "captions_indexed"),
    ("part1.search_matches_reference", "search_matches_reference"),
    ("part1.search_hit_overlaps_collision", "search_hit_overlaps_collision"),
    ("part1.appearance_match", "appearance_match"),
    ("part1.throughput_estimate", "throughput_estimate"),
    ("part1.questions", "questions"),
)


def embedding_search_body(vector: List[float], sensor_ids: List[str], k: int) -> dict:
    """The kNN request of ``course_setup.knn_search``, unchanged, so the grader repeats the notebook's T5 search.

    A ``terms`` filter on ``sensor.id.keyword`` (the VIOS sensor ids of the four search clips) around a nested
    kNN on ``llm.visionEmbeddings.vector`` that carries the same filter.
    """
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


def stem_or_suffix_filter(stem: str) -> dict:
    """The stem or stem_<n>: notebook 2's source filter (course_setup.source_filter), which the notebook's checks use."""
    return {"bool": {"should": [{"term": {"sensorId.keyword": stem}},
                                {"prefix": {"sensorId.keyword": stem + "_"}}],
                     "minimum_should_match": 1}}


def appearance_search_body(stem: str, vector: List[float], frame_time: str) -> dict:
    """The T6 search, as the notebook's T6 cell and part1/quiz.py send it: the frame of the clip, other than the
    query's own, whose objects look most like ``vector``. ``inner_hits`` names the object that matched."""
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
                "k": KNN_K,
                "num_candidates": KNN_CANDIDATES,
                "filter": {"bool": {"filter": [stem_or_suffix_filter(stem)],
                                    "must_not": [{"term": {"timestamp": frame_time}}]}},
            }},
        }},
    }


def object_vector(obj) -> Optional[List[float]]:
    """An RT-CV object's appearance embedding (objects[].embedding.vector), or None when it carries none."""
    if not isinstance(obj, dict) or not isinstance(obj.get("embedding"), dict):
        return None
    vector = obj["embedding"].get("vector")
    if not isinstance(vector, list) or not vector:
        return None
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vector):
        return None
    return [float(x) for x in vector]


def number(value) -> str:
    """8640 and 8640.0 both print as 8,640; other numbers keep one decimal."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"


class Part1:
    def __init__(self, ctx: c.Context) -> None:
        self.ctx = ctx
        self.section = ctx.rubric[ASSESSMENT]
        self.stem = self.section.get("clip_stem")
        self.notebook = self.section.get("notebook", "the assessment notebook")
        self.answers: Optional[dict] = None
        self.load_error = "answers.json was not read."
        self.sensors: Optional[Dict[str, dict]] = None

    # -- loading -------------------------------------------------------------

    def load(self) -> None:
        """Read answers.json once. A problem is recorded, not raised, so every check can report it."""
        path = self.ctx.answers_file
        try:
            data = c.load_json(path)
        except c.ArtefactError as exc:
            self.load_error = f"{exc}. Run the task cells in {self.notebook}, then submit again."
            return
        if not isinstance(data, dict):
            self.load_error = f"{path} must hold one JSON object. Run the task cells in {self.notebook} again."
            return
        self.answers = data

    def _answers(self) -> dict:
        if self.answers is None:
            raise c.ArtefactError(self.load_error)
        return self.answers

    def _task(self, key: str) -> dict:
        """One task's answers, checked against its part of answers.schema.json, for this assessment's clip."""
        answers = self._answers()
        if not isinstance(self.stem, str) or not self.stem:
            raise c.ArtefactError("rubric.json part1 has no clip_stem. Tell the instructor.")
        if "clip_stem" not in answers:
            raise c.ArtefactError(f"answers.json has no clip_stem. Run {self.notebook} from the top.")
        if answers["clip_stem"] != self.stem:
            raise c.ArtefactError(f"answers.json is for clip {answers['clip_stem']!r}, but this assessment grades "
                                  f"{self.stem}. Run {self.notebook} from the top.")
        if key not in answers:
            raise c.ArtefactError(f"answers.json has no {key}. Run the {key.upper()} cells, then submit again.")
        value = answers[key]
        if key == "t1" and isinstance(value, dict) and value.get("vios_sensor_id") in (None, ""):
            raise c.ArtefactError("answers.json has no VIOS sensor id for T1. Run the T1 upload cell and the T1 check, "
                                  "then submit again.")
        schema = c.load_schema("answers")["properties"][key]
        errors = c.validate(value, schema, f"answers.{key}")
        if errors:
            raise c.ArtefactError(f"answers.json {key} has the wrong shape: {'; '.join(errors[:4])}. "
                                  f"Run the {key.upper()} cells again.")
        return value

    def _threshold(self, name: str):
        return self.ctx.threshold(ASSESSMENT, name)

    def _sensors(self) -> Dict[str, dict]:
        if self.sensors is None:
            self.sensors = c.vios_sensors(self.ctx)
        return self.sensors

    def _stem_filter(self) -> dict:
        """RT-CV writes the file stem to sensorId (course_setup.source_filter); the VIOS name never appears there."""
        return {"term": {"sensorId.keyword": self.stem}}

    def _search_files(self) -> List[str]:
        files = self.section.get("search_files")
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
            raise c.ArtefactError("rubric.json part1 has no search_files list. Tell the instructor.")
        return files

    def _search_file(self, given) -> Optional[str]:
        """The search clip ``given`` names, with or without .mp4; None for any other file."""
        wanted = Path(str(given)).stem
        return next((name for name in self._search_files() if Path(name).stem == wanted), None)

    def _not_a_search_clip(self, given) -> str:
        return (f"{given!r} is not one of the four traffic videos from notebook 4 "
                f"({', '.join(self._search_files())}). Search those four videos.")

    # -- T1 ------------------------------------------------------------------

    def video_in_vios(self) -> Tuple[bool, str]:
        t1 = self._task("t1")
        sensors = self._sensors()
        sensor_id, given_name = t1["vios_sensor_id"], t1["vios_name"]
        record = sensors.get(sensor_id)
        if record is None:
            live = sorted(f"{r.get('name')} ({sid})" for sid, r in sensors.items() if c.sensor_name_matches(r.get("name"), self.stem))
            if live:
                return False, f"VIOS does not list sensor {sensor_id}. For {self.stem} it lists {', '.join(live)}."
            return False, (f"VIOS does not list sensor {sensor_id}, and no VIOS sensor is named {self.stem} or "
                           f"{self.stem}_<n> yet. Run the T1 upload cell.")
        name = record.get("name")
        if not c.sensor_name_matches(name, self.stem):
            return False, (f"VIOS calls sensor {sensor_id} {name!r}, not {self.stem} or {self.stem}_<n>. "
                           f"Upload {self.stem}.mp4 and use the sensor id the upload returns.")
        if given_name != name:
            return False, (f"answers.json gives the VIOS name {given_name!r}, but VIOS calls sensor {sensor_id} "
                           f"{name!r}. Read the name from the VIOS sensor list.")
        timeline = c.vios_timeline(self.ctx, sensor_id)
        if not timeline:
            return False, (f"VIOS lists {name} ({sensor_id}) but holds no video for it. Run the T1 upload cell again, "
                           "then the T1 check cell, then submit again.")
        seconds = sum((end - start).total_seconds() for start, end in timeline)
        return True, f"VIOS lists {name} ({sensor_id}) with {seconds:.0f} s of recorded video."

    # -- T2 ------------------------------------------------------------------

    def _type_buckets(self) -> List[dict]:
        response = c.es_search(self.ctx, c.RAW_INDEX, {
            "size": 0,
            "query": self._stem_filter(),
            "aggs": {"objs": {"nested": {"path": "objects"},
                              "aggs": {"by_type": {"terms": {"field": "objects.type.keyword", "size": 20}}}}},
        })
        if response is None:
            return []
        try:
            buckets = response["aggregations"]["objs"]["by_type"]["buckets"]
        except (KeyError, TypeError) as exc:
            raise c.ServiceError(f"{c.RAW_INDEX}: the objects.type aggregation came back without buckets ({exc!r})") from exc
        if not isinstance(buckets, list) or not all(
                isinstance(b, dict) and isinstance(b.get("key"), str) and isinstance(b.get("doc_count"), int) for b in buckets):
            raise c.ServiceError(f"{c.RAW_INDEX}: malformed objects.type buckets: {buckets!r}")
        return buckets

    def detections_indexed(self) -> Tuple[bool, str]:
        t2 = self._task("t2")
        live = c.es_count(self.ctx, c.RAW_INDEX, self._stem_filter())
        if not live:
            return False, (f"Elasticsearch has no RT-CV frame records for sensorId {self.stem} yet. Finish T1 "
                           "(the complete call starts RT-CV), wait a minute, then run T2 again.")
        if t2["frames"] != live and t2["frames"] != c.es_count(self.ctx, c.RAW_INDEX, stem_or_suffix_filter(self.stem)):
            return False, (f"answers.json counts {t2['frames']} frame records. Elasticsearch counts {live} for sensorId "
                           f"{self.stem} now. Run T2 again after the count stops growing, then submit again.")
        buckets = self._type_buckets()
        if not buckets:
            return False, f"The frame records of {self.stem} hold no detected objects, so there is no most frequent type."
        top = max(b["doc_count"] for b in buckets)
        leaders = sorted(b["key"] for b in buckets if b["doc_count"] == top)
        if t2["top_type"].strip().lower() not in {key.lower() for key in leaders}:
            return False, (f"answers.json names {t2['top_type']!r} as the most frequent object type, but that is not the "
                           "top class in Elasticsearch. Run the T2 aggregation on objects.type again.")
        return True, f"{t2['frames']} frame records for {self.stem}. Most frequent type: {leaders[0]} ({top} detections)."

    # -- T3 ------------------------------------------------------------------

    def kafka_decoded(self) -> Tuple[bool, str]:
        t3 = self._task("t3")
        if t3["sensor_id"] != self.stem:
            return False, (f"The decoded message is from sensor {t3['sensor_id']!r}, not {self.stem}. The mdx-raw topic "
                           f"holds every clip's frames. Keep reading until a message has sensorId {self.stem}.")
        moment = c.parse_iso(t3["timestamp"])
        frame_id = t3["frame_id"]
        same_frame = {"bool": {"should": [{"term": {"id.keyword": frame_id}}, {"term": {"id": frame_id}}],
                               "minimum_should_match": 1}}
        response = c.es_search(self.ctx, c.RAW_INDEX, {
            "size": 20,
            "_source": ["id", "sensorId", "timestamp"],
            "query": {"bool": {"filter": [self._stem_filter(), same_frame]}},
        })
        hits = c.es_hits(response)
        if not hits:
            return False, f"Elasticsearch has no {self.stem} frame with id {frame_id!r}. Copy Frame.id from the decoded message."
        slack = float(self._threshold("kafka_timestamp_slack_s"))
        indexed: List[str] = []
        for hit in hits:
            stamp = hit["_source"].get("timestamp")
            try:
                when = c.parse_iso(stamp)
            except c.ArtefactError:
                raise c.ServiceError(f"{c.RAW_INDEX}: frame {frame_id} of {self.stem} has an unreadable timestamp {stamp!r}") from None
            if abs((when - moment).total_seconds()) <= slack:
                return True, f"Frame {frame_id} of {self.stem} at {c.fmt_iso(when)} matches the indexed record."
            indexed.append(c.fmt_iso(when))
        return False, (f"Frame {frame_id} of {self.stem} is indexed at {', '.join(indexed)}, but answers.json says "
                       f"{t3['timestamp']}. Copy Frame.timestamp from the decoded message.")

    # -- T4 ------------------------------------------------------------------

    def captions_indexed(self) -> Tuple[bool, str]:
        t4 = self._task("t4")
        clip_s = self._threshold("clip_seconds")
        want_10 = self._threshold("chunks_at_10_s")
        allowed_10 = [int(n) for n in (want_10 if isinstance(want_10, list) else [want_10])]
        want_0 = int(self._threshold("chunks_at_0_s"))
        frames = int(self._threshold("frames_per_chunk"))
        problems = []
        if t4["chunks_10"] not in allowed_10:
            gives = " or ".join(str(n) for n in allowed_10)
            if len(allowed_10) > 1:
                gives += f" ({allowed_10[-1]} when the cut ends with a short extra chunk)"
            problems.append(f"At chunk_duration 10 you recorded {t4['chunks_10']} chunks. The {clip_s}-second clip "
                            f"gives {gives}")
        if t4["chunks_0"] != want_0:
            problems.append(f"At chunk_duration 0 you recorded {t4['chunks_0']} chunks. A chunk_duration of 0 makes "
                            f"{want_0} chunk of the whole clip")
        counts = t4["frame_counts_8"]
        full, tail = counts[:3], counts[3:]   # a fourth chunk is the short tail of a cut that runs past 30 s
        if counts and all(count is None for count in counts):
            problems.append("RT-VLM did not report frame_count for its chunks, so the frames setting cannot be checked. "
                            "Tell the instructor")
        elif (len(counts) not in allowed_10 or any(count != frames for count in full)
              or any(count is None or not 1 <= count <= frames for count in tail)):
            problems.append(f"With {frames} sampled frames, each full 10-second chunk should report frame_count {frames} "
                            f"(a short last chunk may report fewer), but answers.json has {counts}. Send "
                            f"num_frames_per_second_or_fixed_frames_chunk={frames} with use_fps_for_chunking=False")
        if problems:
            return False, ". ".join(problems) + "."
        file_id = t4["file_id"].strip()
        if not FILE_ID_RE.match(file_id):
            return False, (f"t4.file_id {t4['file_id']!r} is not an RT-VLM file id. Use the id that POST /rtvi-vlm/v1/files "
                           "returned.")
        index = ("default_" + file_id.replace("-", "_")).lower()   # the caption index RT-VLM's captions land in
        stored = c.es_count(self.ctx, index, {"match_all": {}})
        if stored is None:
            return False, (f"Elasticsearch has no caption index {index}. Caption the registered file, wait a minute "
                           "for indexing, then submit again.")
        if stored == 0:
            return False, f"The caption index {index} is still empty. Wait a minute for indexing, then submit again."
        return True, (f"{index} holds {stored} caption(s). {t4['chunks_10']} chunks at chunk_duration 10, {want_0} at 0, "
                      f"and frame_count {frames} in each full chunk with {frames} sampled frames.")

    # -- T5 ------------------------------------------------------------------

    def search_matches_reference(self) -> Tuple[bool, str]:
        """Repeat the learner's search: the same query, the same four clips, the same kNN request as the notebook."""
        t5 = self._task("t5")
        filename = self._search_file(t5["file"])
        if filename is None:
            return False, self._not_a_search_clip(t5["file"])
        k = int(self._threshold("search_k"))
        slack = float(self._threshold("search_slack_s"))
        by_sensor = {c.registry_sensor_id(self.ctx, name): name for name in self._search_files()}
        query = t5["query"]
        vector = c.text_embedding(self.ctx, query)
        hits = c.es_hits(c.es_search(self.ctx, c.EMBED_INDEX, embedding_search_body(vector, list(by_sensor), k)))
        if not hits:
            return False, (f"Searching the four videos for {query!r} returned no window: {c.EMBED_INDEX} holds no "
                           "embeddings for them. Tell the instructor.")
        top = hits[0]["_source"]
        sensor = top.get("sensor").get("id") if isinstance(top.get("sensor"), dict) else None
        if sensor not in by_sensor:
            raise c.ServiceError(f"{c.EMBED_INDEX}: the top hit's sensor.id {sensor!r} is not one of the four search clips")
        try:
            start = c.offset_seconds(c.EMBEDDING_ANCHOR, top.get("timestamp"))
            end = c.offset_seconds(c.EMBEDDING_ANCHOR, top.get("end"))
        except c.ArtefactError as exc:
            raise c.ServiceError(f"{c.EMBED_INDEX}: the top hit has an unreadable time ({exc})") from None
        top_file = by_sensor[sensor]
        given_start, given_end = float(t5["start_s"]), float(t5["end_s"])
        found = f"Searching the four videos for {query!r} returns {top_file} {start:.1f} to {end:.1f} s first"
        if top_file == filename and abs(given_start - start) <= slack and abs(given_end - end) <= slack:
            return True, found + ", the window you reported."
        detail = (found + f", not your window ({filename} {given_start:g} to {given_end:g} s). "
                  "Report the top hit of your own search.")
        shift = given_start - start
        same_shift = abs((given_end - end) - shift) <= slack
        if top_file == filename and same_shift and (given_start < 0 or abs(shift) >= ANCHOR_MISTAKE_S):
            detail += (f" Your times are {shift:+,.0f} s off. Measure the hit's start and end in seconds from "
                       f"{c.EMBEDDING_ANCHOR}, the time the embedding index gives every file.")
        return False, detail

    def search_hit_overlaps_collision(self) -> Tuple[bool, str]:
        t5 = self._task("t5")
        filename = self._search_file(t5["file"])
        if filename is None:
            return False, self._not_a_search_clip(t5["file"])
        entry = c.ground_truth_entry(self.ctx, filename)
        if entry is None:
            raise c.ArtefactError(f"{self.ctx.ground_truth_file} has no labels for {filename}. Tell the instructor.")
        start, end = float(t5["start_s"]), float(t5["end_s"])
        slack = float(self._threshold("window_slack_s"))
        duration = entry.get("duration_s")
        if start < 0 or end <= start or (isinstance(duration, (int, float)) and end > duration + slack):
            return False, (f"{start:g} to {end:g} s does not fit inside {filename}. Measure the hit's start and end in "
                           f"seconds from {c.EMBEDDING_ANCHOR}, the time the embedding index gives every file.")
        collisions = [e for e in entry["events"] if e["type"] == "collision"]
        overlap = max((c.overlap_seconds(start, end, float(e["start_s"]), float(e["end_s"])) for e in collisions), default=0.0)
        if overlap <= 0:
            return False, (f"{filename} {start:.1f} to {end:.1f} s overlaps no labelled collision. Watch the window, then "
                           "change the wording of your query and search again.")
        return True, f"{filename} {start:.1f} to {end:.1f} s overlaps a labelled collision by {overlap:.1f} s."

    # -- T6 ------------------------------------------------------------------

    def _query_frame(self, object_id: str, frame_id: Optional[str], frame_time: Optional[str]) -> Optional[dict]:
        """The frame the query object is taken from: the frame the answers name, else the clip's earliest frame that holds it."""
        filters = [self._stem_filter(),
                   {"nested": {"path": "objects", "query": {"term": {"objects.id.keyword": object_id}}}}]
        if frame_id:
            filters.append({"bool": {"should": [{"term": {"id.keyword": frame_id}}, {"term": {"id": frame_id}}],
                                     "minimum_should_match": 1}})
        if frame_time:
            filters.append({"term": {"timestamp": frame_time}})
        hits = c.es_hits(c.es_search(self.ctx, c.RAW_INDEX, {
            "size": 1,
            "sort": [{"timestamp": {"order": "asc"}}],
            "_source": ["id", "timestamp", "objects"],
            "query": {"bool": {"filter": filters}},
        }))
        return hits[0] if hits else None

    def _nearest_other_object(self, vector: List[float], query_time: str) -> Optional[Tuple[str, str, Optional[float]]]:
        """(object id, frame id, score) of the top match in another frame of the clip, read as the notebook reads it."""
        hits = c.es_hits(c.es_search(self.ctx, c.RAW_INDEX, appearance_search_body(self.stem, vector, query_time)))
        if not hits:
            return None
        hit = hits[0]
        try:
            inner = hit["inner_hits"]["objects"]["hits"]["hits"]
            offset = inner[0]["_nested"]["offset"]
        except (KeyError, IndexError, TypeError) as exc:
            raise c.ServiceError(f"{c.RAW_INDEX}: the appearance search returned a frame without inner_hits ({exc!r})") from None
        objects = hit["_source"].get("objects")
        if not isinstance(offset, int) or isinstance(offset, bool) or not isinstance(objects, list) or not 0 <= offset < len(objects):
            raise c.ServiceError(f"{c.RAW_INDEX}: the appearance search's inner hit points at no object (offset {offset!r})")
        score = inner[0].get("_score")
        return str(objects[offset].get("id")), str(hit["_source"].get("id")), score if isinstance(score, (int, float)) else None

    def appearance_match(self) -> Tuple[bool, str]:
        t6 = self._task("t6")
        query_id, answer = t6["query_object_id"], t6["answer_object_id"]
        frame_id, frame_time = t6.get("query_frame_id"), t6.get("query_frame_time")
        named = bool(frame_id or frame_time)
        query_hit = self._query_frame(query_id, frame_id, frame_time)
        if query_hit is None:
            where = " in the frame answers.json names" if named else ""
            return False, f"No {self.stem} frame in Elasticsearch holds object {query_id}{where}. Use the object id the T6 cell printed."
        source_frame = str(query_hit["_source"].get("id"))
        query_time = query_hit["_source"].get("timestamp")
        objects = [o for o in query_hit["_source"].get("objects") or [] if str(o.get("id")) == query_id]
        vector = object_vector(objects[0]) if objects else None
        if vector is None:
            return False, f"Object {query_id} has no embedding in frame {source_frame}, so it cannot be searched by appearance."
        if not isinstance(query_time, str) or not query_time:
            raise c.ServiceError(f"{c.RAW_INDEX}: frame {source_frame} of {self.stem} has no timestamp")
        nearest = self._nearest_other_object(vector, query_time)
        if nearest is None:
            return False, f"The nested kNN from object {query_id} (frame {source_frame}) returned no other frame of {self.stem}."
        expected, found_in, score = nearest
        scored = f" (score {score:.3f})" if score is not None else ""
        if answer == expected:
            return True, f"Object {answer} in frame {found_in} is the closest match to object {query_id}{scored}."
        if not named and answer == query_id:
            return True, (f"Object {answer} is the query object's own track. Searching from frame {source_frame}, the "
                          f"grader ranks object {expected} first, and answers.json does not say which frame you searched from.")
        return False, (f"The nested kNN from object {query_id} (frame {source_frame}) ranks object {expected} in frame "
                       f"{found_in} first, not {answer}. Search objects.embedding.vector inside the objects field, skip "
                       "the query's own frame, and report the top object's id.")

    # -- T7 ------------------------------------------------------------------

    def throughput_estimate(self) -> Tuple[bool, str]:
        t7 = self._task("t7")
        latency = float(t7["latency_ms"])
        if not math.isfinite(latency) or latency <= 0:
            return False, ("t7.latency_ms must be a positive number of milliseconds: the mean vlm_latency_ms of your "
                           "chunk_duration 10 captions from T4.")
        day_s = int(self._threshold("seconds_per_day"))
        chunk_s = int(self._threshold("chunk_duration_s"))
        tolerance = float(self._threshold("throughput_tolerance"))
        requests = day_s // chunk_s
        busy = requests * latency / 1000.0
        given_requests, given_busy = t7["requests_per_day"], float(t7["busy_seconds_per_day"])
        if not (math.isfinite(given_requests) and math.isfinite(given_busy)):
            return False, "t7.requests_per_day and t7.busy_seconds_per_day must be numbers. Run the T7 cells again."
        problems = []
        if given_requests != requests:
            problems.append(f"t7.requests_per_day is {number(given_requests)}. A camera sends one {chunk_s}-second chunk "
                            f"every {chunk_s} seconds, and a day has {number(day_s)} seconds, so it makes "
                            f"{number(requests)} requests")
        if abs(given_busy - busy) > tolerance * busy:
            problems.append(f"t7.busy_seconds_per_day is {number(given_busy)}. {number(requests)} requests x "
                            f"{latency:,.0f} ms / 1000 gives {number(round(busy))} seconds")
        if problems:
            return False, ". ".join(problems) + "."
        return True, (f"{number(requests)} requests and about {number(round(busy))} s of RT-VLM time per camera per day, "
                      "if requests ran one at a time. RT-VLM batches requests that arrive together, so measure under "
                      "load before you size a deployment.")

    # -- multiple choice -----------------------------------------------------

    def questions(self) -> Tuple[bool, str]:
        answers = self._answers()
        key = c.answer_key(self.ctx, ASSESSMENT)
        given = answers.get("questions")
        if not isinstance(given, dict) or not given:
            raise c.ArtefactError("answers.json has no questions. Answer the multiple-choice cells, then submit again.")
        correct, wrong, missing = [], [], []
        for question, letter in key["questions"].items():
            if question not in given:
                missing.append(question)
            elif isinstance(given[question], str) and given[question].strip().upper() == letter:
                correct.append(question)
            else:
                wrong.append(question)
        need = key["min_correct"]
        detail = f"{len(correct)} of {len(key['questions'])} answers are correct (you need {need})"
        if wrong:
            detail += f". Not correct yet: {', '.join(wrong)}"
        if missing:
            detail += f". Not answered: {', '.join(missing)}"
        return len(correct) >= need, detail + "."


def grade(ctx: c.Context) -> dict:
    grader = Part1(ctx)
    grader.load()
    results = c.run_checks([(check_id, getattr(grader, method)) for check_id, method in CHECKS])
    return c.build_marker(ctx, ASSESSMENT, results)


def main() -> int:
    ctx = c.default_context()
    marker = grade(ctx)
    c.write_marker(ctx, marker)
    print(c.summarize(marker))
    return 0 if marker["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
