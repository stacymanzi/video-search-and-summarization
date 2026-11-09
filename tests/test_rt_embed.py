"""Live RT-Embed regression: full course videos, text embeddings, and search.

Run inside the lab container. Creates test file registrations and embedding
records dated 2026-10-07, separate from the course's January 2025 collection.
"""
import json
import math
from pathlib import Path
import subprocess
import time

import requests

BASE = "http://nginx"
MODEL = "cosmos-embed1-448p-anomaly-detection"
ANCHOR = "2026-10-07T00:00:00.000Z"


def api(method, path, **kwargs):
    response = requests.request(method, BASE + path, timeout=900, **kwargs)
    response.raise_for_status()
    return response.json()


def valid_vector(vector, dimensions=None):
    assert vector and all(math.isfinite(value) for value in vector)
    assert any(value != 0 for value in vector)
    if dimensions is not None:
        assert len(vector) == dimensions, (len(vector), dimensions)


def main():
    vector = api("POST", "/rtvi-embed/v1/generate_text_embeddings", json={
        "text_input": ["two vehicles collide at an intersection"], "model": MODEL,
    })["data"][0]["embeddings"]
    valid_vector(vector)
    sources = json.loads(Path("/dli/task/data/search_sources.json").read_text())
    existing = requests.head(BASE + "/es/mdx-embed-filtered-2025-01-01", timeout=30)
    if existing.status_code == 404:
        print("Existing course embedding index is absent; testing fresh embeddings instead.", flush=True)
        sources = {}
    else:
        existing.raise_for_status()
    for name, source in sources.items():
        source_filter = {"term": {"sensor.id.keyword": source["sensor_id"]}}
        hits = api("POST", "/es/mdx-embed-filtered-2025-01-01/_search", json={
            "size": 3, "_source": ["sensor.id"],
            "query": {"nested": {"path": "llm.visionEmbeddings", "score_mode": "max",
                "query": {"knn": {"field": "llm.visionEmbeddings.vector",
                    "query_vector": vector, "k": 3, "num_candidates": 40,
                    "filter": source_filter}}}},
        })["hits"]["hits"]
        assert hits and all(hit["_source"]["sensor"]["id"] == source["sensor_id"] for hit in hits)
        print(json.dumps({"search": name, "hits": len(hits), "dimensions": len(vector)}), flush=True)

    inputs = [Path("/dli/task/data/traffic_short.mp4")]
    inputs += [Path("/dli/task/data/search-videos") / (name + "_eval.mp4")
               for name in ("normal", "rain", "fog", "night")]
    for source in inputs:
        duration = float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source)], text=True))
        with source.open("rb") as stream:
            file_id = api("POST", "/rtvi-embed/v1/files",
                          files={"file": (source.name, stream, "video/mp4")},
                          data={"purpose": "vision", "media_type": "video",
                                "creation_time": ANCHOR})["id"]
        started = time.monotonic()
        result = api("POST", "/rtvi-embed/v1/generate_video_embeddings", json={
            "id": file_id, "model": MODEL, "chunk_duration": 5,
            "creation_time": ANCHOR,
        })
        chunks = result.get("chunk_responses", [])
        assert chunks, result
        for chunk in chunks:
            valid_vector(chunk["embeddings"], len(vector))
        from datetime import datetime
        last = chunks[-1]["end_time"]
        end = ((datetime.fromisoformat(last.replace("Z", "+00:00")) -
                datetime.fromisoformat(ANCHOR.replace("Z", "+00:00"))).total_seconds()
               if "T" in last else float(last))
        assert end >= duration - 1, (end, duration)
        index = "/es/mdx-embed-filtered-2026-10-07"
        source_filter = {"term": {"sensor.id.keyword": file_id}}
        for attempt in range(30):
            response = requests.post(BASE + index + "/_count",
                                     json={"query": source_filter}, timeout=30)
            if response.status_code == 200 and response.json()["count"] >= len(chunks):
                break
            # A newly auto-created index may briefly have no active primary.
            # Bound the wait; a persistent 503 still fails the regression.
            if response.status_code not in (200, 404, 503):
                response.raise_for_status()
            time.sleep(2)
        else:
            raise AssertionError("New embeddings did not reach Elasticsearch: " + file_id)
        hits = api("POST", index + "/_search", json={
            "size": 3, "_source": ["sensor.id"],
            "query": {"nested": {"path": "llm.visionEmbeddings", "score_mode": "max",
                "query": {"knn": {"field": "llm.visionEmbeddings.vector",
                    "query_vector": vector, "k": 3, "num_candidates": 40,
                    "filter": source_filter}}}},
        })["hits"]["hits"]
        assert len(hits) == min(3, len(chunks)), hits
        assert all(hit["_source"]["sensor"]["id"] == file_id for hit in hits)
        print(json.dumps({"video": source.name, "file_id": file_id,
                          "chunks": len(chunks), "duration": duration, "end": end,
                          "indexed": True, "search_hits": len(hits),
                          "seconds": round(time.monotonic() - started, 1)}), flush=True)


if __name__ == "__main__":
    main()
