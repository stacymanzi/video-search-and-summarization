# Search-clip ground truth (grader copy)

`search_videos.json` labels the four search clips that course-prep ingests at launch
(`/dli/task/data/search-videos/{normal,rain,fog,night}_eval.mp4`), in clip seconds:

```json
[{"file": "rain_eval.mp4", "duration_s": 180.25,
  "events": [{"type": "collision", "start_s": 19.21, "end_s": 43.75}, ...]}]
```

Each clip has two `collision` and two `stalled` events. The Part 1 grader reads the collisions:
`part1.search_hit_overlaps_collision`, a should-check, passes when the learner's RT-Embed window
overlaps one. It is reported to the learner and does not decide the result: the must-check for T5
repeats the learner's search instead (`part1.search_matches_reference`).

## Source

This file is byte-identical to `s3://dli-lms/data/c-fx-17-v2/ground_truth/search_videos.json`
(1,872 bytes, sha256 `61af5f2feaf5827df9c7e8c29557747096751ac019404b699f72c69e6b788a56`, uploaded
2026-11-02), which the S3 loader puts at `/dli/task/data/ground_truth/search_videos.json` for the
notebooks. It was derived from the Singapore AI Day course's
`x-fx-138-v1/part2/traffic_anomaly_dataset/eval_videos/timelines.json`: `normal_eval` is
`eval_video_01`, `night_eval` is `eval_video_02`, `fog_eval` is `eval_video_03` and `rain_eval` is
`eval_video_04`.

## Two copies, one file

The grader keeps its own copy because the lab can write to `/dli/task/data`. It ships only in the
assessment image (`.launchignore` keeps `assessment/` out of the launch tarball). When the labels
change, upload the new file to S3, copy it here unchanged, update the hash in
`tests/test_contracts.py` (`GROUND_TRUTH_SHA256`), regenerate the fixtures and rerun the tests.
