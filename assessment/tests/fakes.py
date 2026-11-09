"""Fake HTTP transport for the grader tests: VIOS, RT-Embed and Elasticsearch, served from fixtures/world.json.

``FakeHttp`` answers what the Part 1 grader asks, through ``http://nginx``:

* VIOS: ``GET /vios/vst/api/v1/sensor/list`` and ``GET .../storage/<sensorId>/timelines``.
* RT-Embed: ``POST /rtvi-embed/v1/generate_text_embeddings`` for the queries in
  ``world["rtvi_embed"]["text_embeddings"]``; another model id answers 400, as RT-Embed does.
* Elasticsearch: ``POST /es/<index or pattern>/_count`` and ``/_search``, evaluated over the
  documents in ``world["es"]``. It understands the query clauses the grader uses (``match_all``,
  ``term``, ``terms``, ``prefix``, ``bool``, ``nested`` and a nested ``knn`` ranked by cosine
  similarity, with ``inner_hits``), ``sort`` on a date field, ``_source`` includes and excludes, and
  nested ``terms``/``cardinality`` aggregations. A named index that does not exist answers 404, like
  Elasticsearch; a pattern that matches nothing answers with no hits.

Anything else is a test failure, not a silent 404: the request is recorded in ``errors`` and an
AssertionError is raised (the grader turns it into a "grader error" detail, which the harness
rejects). With ``down=True`` every call raises ``ServiceError`` the way the real transport does
for a refused connection.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import math
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from grader import common

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_world(path: Path = FIXTURES / "world.json") -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class FakeError(AssertionError):
    """The grader sent something the fake (and so the tests' picture of the service) does not cover."""


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def values_at(doc, field: str) -> list:
    """Leaf values at a dotted path; ``.keyword`` reads the field itself; lists along the way are flattened."""
    if field.endswith(".keyword"):
        field = field[: -len(".keyword")]
    current = [doc]
    for part in field.split("."):
        following = []
        for item in current:
            if isinstance(item, dict) and part in item:
                value = item[part]
                following.extend(value if isinstance(value, list) else [value])
        current = following
    return [v for v in current if not isinstance(v, (dict, list))]


def vector_at(item, field: str) -> Optional[List[float]]:
    current = item
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if isinstance(current, list) and current and all(isinstance(x, (int, float)) for x in current):
        return [float(x) for x in current]
    return None


def nested_items(doc: dict, path: str) -> list:
    value = doc
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return [item for item in as_list(value) if isinstance(item, dict)]


def relative(field: str, path: str) -> Optional[str]:
    return field[len(path) + 1:] if field.startswith(path + ".") else None


def cosine(a: List[float], b: List[float]) -> Optional[float]:
    if len(a) != len(b):
        raise FakeError(f"kNN vectors of different length: {len(a)} and {len(b)}")
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def matches(root: dict, query: dict, scope: Optional[Tuple[str, dict]] = None) -> bool:
    """Whether ``root`` matches ``query``; inside a nested clause ``scope`` is (path, nested object)."""
    if not isinstance(query, dict) or len(query) != 1:
        raise FakeError(f"a query clause must be an object with one key: {query!r}")
    kind, spec = next(iter(query.items()))
    if kind == "match_all":
        return True
    if kind in ("term", "terms", "prefix"):
        if not isinstance(spec, dict) or len(spec) != 1:
            raise FakeError(f"{kind} needs exactly one field: {spec!r}")
        field, wanted = next(iter(spec.items()))
        values = field_values(root, field, scope)
        if kind == "term":
            target = wanted.get("value") if isinstance(wanted, dict) else wanted
            return target in values
        if kind == "terms":
            return any(v in as_list(wanted) for v in values)
        prefix = wanted.get("value") if isinstance(wanted, dict) else wanted
        return any(isinstance(v, str) and v.startswith(prefix) for v in values)
    if kind == "bool":
        unknown = set(spec) - {"filter", "must", "must_not", "should", "minimum_should_match"}
        if unknown:
            raise FakeError(f"bool clauses the fake does not know: {sorted(unknown)}")
        if not all(matches(root, clause, scope) for clause in as_list(spec.get("filter")) + as_list(spec.get("must"))):
            return False
        if any(matches(root, clause, scope) for clause in as_list(spec.get("must_not"))):
            return False
        should = as_list(spec.get("should"))
        if should:
            return sum(1 for clause in should if matches(root, clause, scope)) >= int(spec.get("minimum_should_match", 1))
        return True
    if kind == "nested":
        unknown = set(spec) - {"path", "query", "score_mode", "inner_hits"}
        if unknown or "path" not in spec or "query" not in spec:
            raise FakeError(f"nested needs path and query (and may set score_mode and inner_hits): {spec!r}")
        return any(matches(root, spec["query"], (spec["path"], item)) for item in nested_items(root, spec["path"]))
    if kind == "knn":
        unknown = set(spec) - {"field", "query_vector", "k", "num_candidates", "filter"}
        if unknown or not {"field", "query_vector", "k", "num_candidates"} <= set(spec):
            raise FakeError(f"knn needs field, query_vector, k and num_candidates: {sorted(spec)}")
        if "filter" in spec and not matches(root, spec["filter"]):
            return False   # the filter reads the parent document, as notebook 4's search relies on
        if scope is None:
            raise FakeError("a knn clause outside a nested clause: the grader only searches object embeddings")
        path, item = scope
        field = relative(spec["field"], path)
        return field is not None and vector_at(item, field) is not None
    raise FakeError(f"the fake does not understand the {kind!r} query")


def field_values(root: dict, field: str, scope: Optional[Tuple[str, dict]]) -> list:
    if scope is not None:
        path, item = scope
        inner = relative(field, path)
        if inner is not None:
            return values_at(item, inner)
    return values_at(root, field)


def find_nested_knn(query: dict) -> Optional[Tuple[str, dict, Optional[dict]]]:
    """(nested path, knn spec, inner_hits spec or None) when the query ranks by a nested kNN, else None."""
    if not isinstance(query, dict) or len(query) != 1:
        return None
    kind, spec = next(iter(query.items()))
    if kind == "nested" and isinstance(spec.get("query"), dict) and "knn" in spec["query"]:
        return spec["path"], spec["query"]["knn"], spec.get("inner_hits")
    if kind == "bool":
        for key in ("must", "filter", "should"):
            for clause in as_list(spec.get(key)):
                found = find_nested_knn(clause)
                if found:
                    return found
    return None


def nested_scores(doc: dict, path: str, knn: dict) -> List[Tuple[float, int]]:
    """(Elasticsearch's cosine score (1 + cos) / 2, offset) of every nested object with a vector, best first."""
    field = relative(knn["field"], path)
    scored = []
    for offset, item in enumerate(nested_items(doc, path)):
        vector = vector_at(item, field) if field else None
        if vector is None:
            continue
        similarity = cosine([float(x) for x in knn["query_vector"]], vector)
        if similarity is not None:
            scored.append(((1.0 + similarity) / 2.0, offset))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return scored


def knn_score(doc: dict, path: str, knn: dict) -> Optional[float]:
    """The score of the best nested object (score_mode max)."""
    scored = nested_scores(doc, path, knn)
    return scored[0][0] if scored else None


def inner_hits(name: str, doc_id: str, doc: dict, path: str, knn: dict, spec: dict) -> dict:
    """The ``inner_hits`` of one hit: its best nested objects, with ``_nested.offset`` into the source array."""
    unknown = set(spec) - {"size", "_source"}
    if unknown:
        raise FakeError(f"inner_hits options the fake does not know: {sorted(unknown)}")
    items = nested_items(doc, path)
    scored = nested_scores(doc, path, knn)
    hits = []
    for score, offset in scored[: int(spec.get("size", 3))]:
        hit = {"_index": name, "_id": doc_id, "_nested": {"field": path, "offset": offset}, "_score": score}
        if spec.get("_source", True) is not False:
            hit["_source"] = copy.deepcopy(items[offset])
        hits.append(hit)
    return {path: {"hits": {"total": {"value": len(scored), "relation": "eq"},
                            "max_score": hits[0]["_score"] if hits else None, "hits": hits}}}


def pick(source: dict, includes: List[str]) -> dict:
    """``_source`` filtering by include paths ("sensor.id", "objects")."""
    out: dict = {}
    for path in includes:
        copy_path(source, out, path.split("."))
    return out


def drop_path(value, parts: List[str]) -> None:
    """Remove one dotted path from a copied ``_source``; lists along the way are walked item by item."""
    if isinstance(value, list):
        for item in value:
            drop_path(item, parts)
    elif isinstance(value, dict) and parts[0] in value:
        if len(parts) == 1:
            del value[parts[0]]
        else:
            drop_path(value[parts[0]], parts[1:])


def copy_path(src, dst: dict, parts: List[str]) -> None:
    key = parts[0]
    if not isinstance(src, dict) or key not in src:
        return
    value = src[key]
    if len(parts) == 1:
        dst[key] = copy.deepcopy(value)
    elif isinstance(value, dict):
        copy_path(value, dst.setdefault(key, {}), parts[1:])
    elif isinstance(value, list):
        existing = dst.setdefault(key, [{} for _ in value])
        for item, target in zip(value, existing):
            copy_path(item, target, parts[1:])


def sort_key(doc: dict, field: str):
    values = values_at(doc, field)
    if not values:
        raise FakeError(f"sort field {field!r} is missing from a document")
    try:
        return common.parse_iso(values[0])
    except common.ArtefactError:
        return values[0]


def aggregate(items: list, aggs: dict, path: Optional[str] = None) -> dict:
    """Bucket aggregations over documents, or over nested objects once ``path`` is set."""
    out = {}
    for name, spec in aggs.items():
        kinds = set(spec) - {"aggs"}
        if len(kinds) != 1:
            raise FakeError(f"aggregation {name!r} must have one type: {sorted(spec)}")
        kind = kinds.pop()
        body = spec[kind]

        def read(item, field):
            inner = relative(field, path) if path else field
            if inner is None:
                raise FakeError(f"aggregation field {field!r} is outside the nested path {path!r}")
            return values_at(item, inner)

        if kind == "nested":
            nested = [obj for item in items for obj in nested_items(item, body["path"])]
            result = {"doc_count": len(nested)}
            result.update(aggregate(nested, spec.get("aggs", {}), body["path"]))
        elif kind == "terms":
            counts: dict = {}
            for item in items:
                for value in read(item, body["field"]):
                    counts[value] = counts.get(value, 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
            size = int(body.get("size", 10))
            result = {"doc_count_error_upper_bound": 0,
                      "sum_other_doc_count": sum(n for _, n in ranked[size:]),
                      "buckets": [{"key": key, "doc_count": n} for key, n in ranked[:size]]}
        elif kind == "cardinality":
            result = {"value": len({v for item in items for v in read(item, body["field"])})}
        else:
            raise FakeError(f"the fake does not know the {kind!r} aggregation")
        out[name] = result
    return out


class FakeHttp:
    def __init__(self, world: dict, down: bool = False) -> None:
        self.world = world
        self.down = down
        self.calls: List[Tuple[str, str, object]] = []
        self.errors: List[str] = []

    def __call__(self, method: str, url: str, body: Optional[bytes], timeout: float):
        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append((method, url, payload))
        if self.down:
            raise common.ServiceError(f"{method} {url} -> no response (ConnectionRefusedError: [Errno 111] Connection refused)")
        try:
            parts = urlsplit(url)
            if parts.scheme != "http" or parts.netloc != "nginx":
                raise FakeError(f"{url}: every service is reached through http://nginx")
            path = unquote(parts.path)
            if path.startswith("/vios/"):
                status, data = self._vios(method, path)
            elif path.startswith("/rtvi-embed/"):
                status, data = self._rtvi_embed(method, path, payload)
            elif path.startswith("/es/"):
                status, data = self._es(method, path, payload)
            else:
                raise FakeError(f"no fake service behind {method} {path}")
        except FakeError as exc:
            self.errors.append(str(exc))
            raise
        return status, json.dumps(data).encode("utf-8")

    # -- VIOS ------------------------------------------------------------------

    def _vios(self, method: str, path: str):
        vios = self.world["vios"]
        prefix = "/vios/vst/api/v1"
        if method == "GET" and path == prefix + "/sensor/list":
            return 200, vios["sensors"]
        if method == "GET" and path.startswith(prefix + "/storage/") and path.endswith("/timelines"):
            sensor_id = path[len(prefix + "/storage/"):-len("/timelines")]
            if sensor_id not in vios["timelines"]:
                return 404, {"error_code": "VMSInvalidSensorId", "error_message": f"sensor {sensor_id} not found"}
            return 200, vios["timelines"][sensor_id]
        raise FakeError(f"no fake VIOS route for {method} {path}")

    # -- RT-Embed ----------------------------------------------------------------

    def _rtvi_embed(self, method: str, path: str, payload):
        embed = self.world["rtvi_embed"]
        if method != "POST" or path != "/rtvi-embed/v1/generate_text_embeddings":
            raise FakeError(f"no fake RT-Embed route for {method} {path}")
        if not isinstance(payload, dict) or set(payload) != {"text_input", "model"}:
            raise FakeError(f"generate_text_embeddings takes text_input and model, as course_setup sends them: {payload!r}")
        if payload["model"] != embed["model"]:
            return 400, {"detail": f"model {payload['model']!r} is not served here"}
        texts = payload["text_input"]
        if not isinstance(texts, list) or len(texts) != 1 or not isinstance(texts[0], str):
            raise FakeError(f"text_input must be a list with one query: {texts!r}")
        if texts[0] not in embed["text_embeddings"]:
            raise FakeError(f"no fake embedding for the query {texts[0]!r}")
        return 200, {"model": embed["model"], "data": [{"embeddings": embed["text_embeddings"][texts[0]]}]}

    # -- Elasticsearch -----------------------------------------------------------

    def _indices(self, expression: str) -> Optional[List[str]]:
        names = sorted(self.world["es"])
        if "*" in expression:
            return [name for name in names if fnmatch.fnmatchcase(name, expression)]
        return [expression] if expression in self.world["es"] else None

    def _es(self, method: str, path: str, payload):
        expression, _, operation = path[len("/es/"):].partition("/")
        if method != "POST" or operation not in ("_count", "_search"):
            raise FakeError(f"no fake Elasticsearch route for {method} {path}")
        names = self._indices(expression)
        if names is None:
            return 404, {"error": {"type": "index_not_found_exception", "reason": f"no such index [{expression}]"}, "status": 404}
        payload = payload or {}
        docs = [(name, f"{name}#{n}", doc) for name in names for n, doc in enumerate(self.world["es"][name])]
        query = payload.get("query", {"match_all": {}})
        if operation == "_count":
            if set(payload) - {"query"}:
                raise FakeError(f"_count takes only a query: {sorted(payload)}")
            count = sum(1 for _, _, doc in docs if matches(doc, query))
            return 200, {"count": count, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0}}
        return 200, self._search(docs, payload, query)

    def _search(self, docs, payload: dict, query: dict) -> dict:
        unknown = set(payload) - {"size", "query", "sort", "_source", "aggs"}
        if unknown:
            raise FakeError(f"_search keys the fake does not know: {sorted(unknown)}")
        selected = [(name, doc_id, doc, None) for name, doc_id, doc in docs if matches(doc, query)]
        knn = find_nested_knn(query)
        if knn is not None:
            path, spec, _ = knn
            scored = []
            for name, doc_id, doc, _ in selected:
                score = knn_score(doc, path, spec)
                if score is not None:
                    scored.append((name, doc_id, doc, score))
            scored.sort(key=lambda row: -row[3])
            selected = scored[: int(spec["k"])]
        for spec in reversed(as_list(payload.get("sort"))):
            if not isinstance(spec, dict) or len(spec) != 1:
                raise FakeError(f"sort entries are one-field objects: {spec!r}")
            field, order = next(iter(spec.items()))
            order = order.get("order", "asc") if isinstance(order, dict) else order
            selected.sort(key=lambda row: sort_key(row[2], field), reverse=(order == "desc"))
        size = int(payload.get("size", 10))
        source = payload.get("_source", True)
        hits = []
        for name, doc_id, doc, score in selected[:size]:
            if source is True:
                shown = copy.deepcopy(doc)
            elif isinstance(source, list):
                shown = pick(doc, source)
            elif isinstance(source, dict) and set(source) == {"excludes"}:
                shown = copy.deepcopy(doc)
                for excluded in source["excludes"]:
                    drop_path(shown, excluded.split("."))
            else:
                raise FakeError(f"_source must be a list of fields or {{'excludes': [...]}} here: {source!r}")
            hit = {"_index": name, "_id": doc_id, "_score": score, "_source": shown}
            if knn is not None and knn[2] is not None:
                hit["inner_hits"] = inner_hits(name, doc_id, doc, knn[0], knn[1], knn[2])
            hits.append(hit)
        response = {"took": 1, "timed_out": False,
                    "hits": {"total": {"value": len(selected), "relation": "eq"},
                             "max_score": max((h["_score"] for h in hits if h["_score"] is not None), default=None),
                             "hits": hits}}
        if "aggs" in payload:
            response["aggregations"] = aggregate([doc for _, _, doc, _ in selected], payload["aggs"])
        return response
