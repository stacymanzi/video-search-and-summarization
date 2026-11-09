"""Live RT-VLM smoke test; run in the lab container, not during unit tests.

Uploads six-second excerpts, generates captions, and verifies Kafka/Logstash
delivery to Elasticsearch. Creates test assets and caption indices; it does
not overwrite notebooks or re-ingest videos through the detection pipeline.
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://nginx")
    parser.add_argument("--all-clips", action="store_true")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    response = requests.get(base + "/rtvi-vlm/v1/models", timeout=30)
    response.raise_for_status()
    model = response.json()["data"][0]["id"]
    print("Model:", model, flush=True)
    inputs = [Path("/dli/task/data/traffic_short.mp4")]
    if args.all_clips:
        inputs.extend(Path("/dli/task/data/search-videos") / (name + "_eval.mp4")
                      for name in ("normal", "rain", "fog", "night"))
    for source in inputs:
        with tempfile.TemporaryDirectory(prefix="vlm-smoke-") as directory:
            excerpt = Path(directory) / source.name
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(source),
                            "-t", "6", "-c", "copy", str(excerpt)], check=True)
            with excerpt.open("rb") as stream:
                response = requests.post(base + "/rtvi-vlm/v1/files", timeout=120,
                                         files={"file": (source.name, stream, "video/mp4")},
                                         data={"purpose": "vision", "media_type": "video",
                                               "creation_time": "2025-01-01T00:00:00.000Z"})
            response.raise_for_status()
            file_id = response.json()["id"]
        started = time.monotonic()
        response = requests.post(base + "/rtvi-vlm/v1/generate_captions", timeout=900,
                                 json={"id": file_id, "model": model,
                                       "prompt": "Describe the vehicles and road conditions visible in this video.",
                                       "creation_time": "2025-01-01T00:00:00Z",
                                       "chunk_duration": 0})
        response.raise_for_status()
        chunks = response.json().get("chunk_responses", [])
        assert chunks and all(chunk.get("content", "").strip() for chunk in chunks), response.text
        index = "default_" + file_id.replace("-", "_")
        for attempt in range(30):
            indexed = requests.get(base + "/es/" + index + "/_search", timeout=30)
            if indexed.status_code == 200 and indexed.json().get("hits", {}).get("hits"):
                break
            if indexed.status_code not in (200, 404):
                indexed.raise_for_status()
            time.sleep(2)
        else:
            raise AssertionError("Captions did not reach Elasticsearch: " + index)
        print(json.dumps({"clip": source.name, "file_id": file_id,
                          "seconds": round(time.monotonic() - started, 1),
                          "chunks": len(chunks), "indexed": True,
                          "caption": chunks[0]["content"]}), flush=True)


if __name__ == "__main__":
    main()
