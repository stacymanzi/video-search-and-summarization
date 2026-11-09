"""Part 1 grader: every fixture case, plus the behaviour the fixture cases cannot show."""

from __future__ import annotations

import copy
import json

import pytest

from grader import common, grade_part1
from tests.conftest import FIXTURES, assert_case, assert_clean, build_context, failed_ids, fixture_cases
from tests.fakes import load_world

CASES = fixture_cases()


def detail(marker: dict, check_id: str) -> str:
    return next(item["detail"] for item in marker["checks"] if item["id"] == check_id)


def grade(ctx) -> dict:
    marker = grade_part1.grade(ctx)
    assert not common.validate(marker, common.load_schema("marker")), common.validate(marker, common.load_schema("marker"))
    assert [item["id"] for item in marker["checks"]] == [spec["id"] for spec in ctx.rubric["part1"]["checks"]]
    return marker


def test_pass_set_passes_every_check(tmp_path):
    ctx = build_context(tmp_path)
    marker = grade(ctx)
    assert marker["passed"] and not failed_ids(marker), common.summarize(marker)
    assert_clean(ctx, marker)
    assert marker["assessment"] == "part1" and marker["rubric_version"] == ctx.rubric["version"]


@pytest.mark.parametrize("case_name,case_dir", CASES, ids=[name for name, _ in CASES])
def test_fixture_case(tmp_path, case_name, case_dir):
    expect = json.loads((case_dir / "expect.json").read_text(encoding="utf-8"))
    ctx = build_context(tmp_path, case_dir, services=expect["services"])
    marker = grade(ctx)
    assert_case(marker, case_dir, case_name)
    assert_clean(ctx, marker)


def test_service_down_says_fail_closed(tmp_path):
    ctx = build_context(tmp_path, services="down")
    marker = grade(ctx)
    for check_id in ("part1.video_in_vios", "part1.detections_indexed", "part1.captions_indexed",
                     "part1.search_matches_reference"):
        text = detail(marker, check_id)
        assert text.startswith("A service did not answer, so this check cannot pass yet"), common.summarize(marker)
        assert text.endswith("If it happens again, tell the instructor.")


def test_missing_answers_file_names_the_notebook(tmp_path):
    ctx = build_context(tmp_path, FIXTURES / "cases" / "no_answers_file")
    marker = grade(ctx)
    assert all("part1/assessment.ipynb" in item["detail"] for item in marker["checks"]), common.summarize(marker)


def test_the_rubric_stem_is_graded_not_the_answers_stem(tmp_path):
    """normal_eval has frames of its own; answers that point there still fail, because the grader uses clip_stem."""
    ctx = build_context(tmp_path, FIXTURES / "cases" / "other_clip")
    marker = grade(ctx)
    assert "this assessment grades rain_check" in detail(marker, "part1.detections_indexed")


def test_detections_not_indexed_yet(tmp_path):
    world = load_world()
    world["es"]["mdx-raw-2025-01-01"] = [d for d in world["es"]["mdx-raw-2025-01-01"] if d["sensorId"] != "rain_check"]
    ctx = build_context(tmp_path, world=world)
    marker = grade(ctx)
    assert "no RT-CV frame records for sensorId rain_check" in detail(marker, "part1.detections_indexed")
    assert not marker["passed"]


def test_count_mismatch_shows_the_live_count(tmp_path):
    world = load_world()
    frames = world["es"]["mdx-raw-2025-01-01"]
    frames.remove(next(d for d in frames if d["sensorId"] == "rain_check" and d["id"] == "11"))
    ctx = build_context(tmp_path, world=world)
    marker = grade(ctx)
    assert "counts 11 for sensorId rain_check" in detail(marker, "part1.detections_indexed")


def test_tied_top_types_accept_either(tmp_path):
    world = load_world()
    frames = [d for d in world["es"]["mdx-raw-2025-01-01"] if d["sensorId"] == "rain_check"]
    cars = sum(1 for d in frames for o in d["objects"] if o["type"] == "car")
    persons = sum(1 for d in frames for o in d["objects"] if o["type"] == "person")
    for n in range(cars - persons):   # add people until the two classes tie
        frames[n % len(frames)]["objects"].append({"id": f"p{n}", "type": "person", "confidence": 0.8})
    for top in ("car", "person"):
        answers = json.loads((FIXTURES / "pass" / "answers.json").read_text())
        answers["t2"]["top_type"] = top
        case = tmp_path / top
        case.mkdir()
        (case / "answers.json").write_text(json.dumps(answers))
        ctx = build_context(tmp_path / f"run-{top}", case, world=copy.deepcopy(world))
        marker = grade(ctx)
        assert "part1.detections_indexed" not in failed_ids(marker), common.summarize(marker)


def test_no_recorded_video_yet(tmp_path):
    world = load_world()
    rain_check = next(s["sensorId"] for s in world["vios"]["sensors"] if s["name"] == "rain_check_1")
    world["vios"]["timelines"][rain_check] = []
    ctx = build_context(tmp_path, world=world)
    marker = grade(ctx)
    text = detail(marker, "part1.video_in_vios")
    assert "holds no video for it. Run the T1 upload cell again" in text and "complete" not in text


def test_upload_that_never_ran_is_named(tmp_path):
    marker = grade(build_context(tmp_path, FIXTURES / "cases" / "upload_never_ran"))
    assert detail(marker, "part1.video_in_vios") == (
        "answers.json has no VIOS sensor id for T1. Run the T1 upload cell and the T1 check, then submit again.")


def test_caption_index_still_empty(tmp_path):
    world = load_world()
    index = next(name for name in world["es"] if name.startswith("default_"))
    world["es"][index] = []
    ctx = build_context(tmp_path, world=world)
    marker = grade(ctx)
    assert f"The caption index {index} is still empty" in detail(marker, "part1.captions_indexed")
    world["es"][index] = load_world()["es"][index][:1]   # one stored caption is enough, as in the notebook's check
    assert "part1.captions_indexed" not in failed_ids(grade(build_context(tmp_path / "one", world=world)))


def test_upper_case_file_id_finds_the_lower_case_index(tmp_path):
    answers = json.loads((FIXTURES / "pass" / "answers.json").read_text())
    answers["t4"]["file_id"] = answers["t4"]["file_id"].upper()
    case = tmp_path / "case"
    case.mkdir()
    (case / "answers.json").write_text(json.dumps(answers))
    marker = grade(build_context(tmp_path, case))
    assert "part1.captions_indexed" not in failed_ids(marker), common.summarize(marker)


def test_count_with_the_suffix_filter_is_accepted(tmp_path):
    """Notebook 2's source filter also counts stem_<n> records; the notebook's own T2 check accepts that count."""
    world = load_world()
    extra = copy.deepcopy(next(d for d in world["es"]["mdx-raw-2025-01-01"] if d["sensorId"] == "rain_check"))
    extra["sensorId"] = "rain_check_2"
    world["es"]["mdx-raw-2025-01-01"].append(extra)
    answers = json.loads((FIXTURES / "pass" / "answers.json").read_text())
    for frames, ok in ((12, True), (13, True), (14, False)):
        answers["t2"]["frames"] = frames
        case = tmp_path / f"case{frames}"
        case.mkdir()
        (case / "answers.json").write_text(json.dumps(answers))
        marker = grade(build_context(tmp_path / f"run{frames}", case, world=copy.deepcopy(world)))
        assert ("part1.detections_indexed" not in failed_ids(marker)) is ok, common.summarize(marker)


def test_missing_ground_truth_fails_only_the_reported_overlap(tmp_path):
    """The labels feed a should-check only: without them the learner still passes, and is told to call the instructor."""
    ctx = build_context(tmp_path)
    ctx.ground_truth_file = tmp_path / "nowhere" / "search_videos.json"
    marker = grade(ctx)
    assert failed_ids(marker) == {"part1.search_hit_overlaps_collision"} and marker["passed"]
    assert detail(marker, "part1.search_hit_overlaps_collision").endswith("is missing. Tell the instructor.")


def test_missing_registry_fails_the_search_only(tmp_path):
    marker = grade(build_context(tmp_path, registry=False))
    assert failed_ids(marker) == {"part1.search_matches_reference"}
    text = detail(marker, "part1.search_matches_reference")
    assert "search_sources.json, is missing" in text and text.endswith("Tell the instructor.")


def test_missing_embedding_index_fails_the_search(tmp_path):
    world = load_world()
    del world["es"][common.EMBED_INDEX]
    marker = grade(build_context(tmp_path, world=world))
    assert failed_ids(marker) == {"part1.search_matches_reference"}
    assert "returned no window" in detail(marker, "part1.search_matches_reference")


def test_the_reference_search_repeats_the_learners_search(tmp_path):
    """Same query text, the four registry sensors in both filters, never traffic_short or the learner's own clip."""
    ctx = build_context(tmp_path)
    marker = grade(ctx)
    assert "rain_eval.mp4 20.0 to 25.0 s first, the window you reported" in detail(marker, "part1.search_matches_reference")
    embeds = [body for _, url, body in ctx.http.calls if url.endswith("/rtvi-embed/v1/generate_text_embeddings")]
    query = json.loads((FIXTURES / "pass" / "answers.json").read_text())["t5"]["query"]
    assert embeds == [{"text_input": [query], "model": common.EMBED_MODEL}]
    searches = [body for _, url, body in ctx.http.calls if url == f"http://nginx/es/{common.EMBED_INDEX}/_search"]
    assert len(searches) == 1
    registry = load_world()["registry"]
    ids = [registry[name]["sensor_id"] for name in ctx.rubric["part1"]["search_files"]]
    vector = load_world()["rtvi_embed"]["text_embeddings"][query]
    assert searches[0] == grade_part1.embedding_search_body(vector, ids, ctx.rubric["part1"]["thresholds"]["search_k"])
    assert searches[0]["query"]["bool"]["filter"] == [{"terms": {"sensor.id.keyword": ids}}]


@pytest.mark.parametrize("case_name,shift", [("window_before_the_clip", "-86,400"), ("window_on_the_vios_timeline", "+86,400")])
def test_the_wrong_anchor_is_named(tmp_path, case_name, shift):
    marker = grade(build_context(tmp_path, FIXTURES / "cases" / case_name))
    text = detail(marker, "part1.search_matches_reference")
    assert f"Your times are {shift} s off" in text and common.EMBEDDING_ANCHOR in text
    assert "does not fit inside rain_eval.mp4" in detail(marker, "part1.search_hit_overlaps_collision")


@pytest.mark.parametrize("case_name", ["window_misses_collision", "window_half_a_second_off"])
def test_another_window_is_not_blamed_on_the_anchor(tmp_path, case_name):
    marker = grade(build_context(tmp_path, FIXTURES / "cases" / case_name))
    assert "s off" not in detail(marker, "part1.search_matches_reference")


def test_a_window_that_is_not_the_top_hit_is_shown_the_top_hit(tmp_path):
    marker = grade(build_context(tmp_path, FIXTURES / "cases" / "window_not_the_top_hit"))
    text = detail(marker, "part1.search_matches_reference")
    assert text == ("Searching the four videos for 'two cars collide at a crossing' returns rain_eval.mp4 20.0 to "
                    "25.0 s first, not your window (normal_eval.mp4 45 to 50 s). Report the top hit of your own search.")


def test_missing_answer_key_fails_the_questions_only(tmp_path):
    ctx = build_context(tmp_path, answer_key=None)
    marker = grade(ctx)
    assert failed_ids(marker) == {"part1.questions"}
    assert "part1.json is missing" in detail(marker, "part1.questions")


def test_malformed_answer_key_is_reported(tmp_path):
    bad = tmp_path / "bad_key.json"
    bad.write_text(json.dumps({"version": 1, "questions": {"p1-q1": "maybe"}, "min_correct": 3}))
    ctx = build_context(tmp_path, answer_key=bad)
    marker = grade(ctx)
    text = detail(marker, "part1.questions")
    assert "one capital letter" in text and "min_correct" in text


def test_wrong_and_missing_questions_are_named(tmp_path):
    ctx = build_context(tmp_path, FIXTURES / "cases" / "six_questions_right")
    marker = grade(ctx)
    text = detail(marker, "part1.questions")
    assert text.startswith("6 of 8 answers are correct (you need 6)")
    assert "Not correct yet: p1-q7" in text and "Not answered: p1-q8" in text


def test_thresholds_come_from_the_rubric(tmp_path):
    rubric = common.load_rubric()
    case = FIXTURES / "cases" / "throughput_off"   # 86,400 busy seconds where 8,640 x 4,200 ms gives 36,288
    assert "part1.throughput_estimate" in failed_ids(grade(build_context(tmp_path / "strict", case, rubric=rubric)))
    loose = copy.deepcopy(rubric)
    loose["part1"]["thresholds"]["throughput_tolerance"] = 1.5
    assert "part1.throughput_estimate" not in failed_ids(grade(build_context(tmp_path / "loose", case, rubric=loose)))


def test_a_rubric_check_without_a_grader_fails_as_not_evaluated(tmp_path):
    rubric = copy.deepcopy(common.load_rubric())
    rubric["part1"]["checks"].append({"id": "part1.future_check", "task": "p1-t8", "must": True, "text": "not written yet"})
    ctx = build_context(tmp_path, rubric=rubric)
    marker = grade_part1.grade(ctx)
    assert not marker["passed"] and detail(marker, "part1.future_check") == "not evaluated"


def test_t5_search_needs_the_services_and_the_overlap_does_not(tmp_path):
    """The must-check repeats the search live; the reported overlap reads only the grader's ground truth."""
    marker = grade(build_context(tmp_path, services="down"))
    assert "part1.search_matches_reference" in failed_ids(marker)
    assert "part1.search_hit_overlaps_collision" not in failed_ids(marker)


def test_chunk_counts_come_from_the_rubric(tmp_path):
    """chunks_at_10_s may be one number or a list; a single 3 refuses the fourth short chunk."""
    case = FIXTURES / "cases" / "four_chunks_at_10"
    assert "part1.captions_indexed" not in failed_ids(grade(build_context(tmp_path / "list", case)))
    strict = copy.deepcopy(common.load_rubric())
    strict["part1"]["thresholds"]["chunks_at_10_s"] = 3
    marker = grade(build_context(tmp_path / "scalar", case, rubric=strict))
    assert "part1.captions_indexed" in failed_ids(marker)
    assert detail(marker, "part1.captions_indexed").startswith("At chunk_duration 10 you recorded 4 chunks. The 30-second clip gives 3.")


def test_every_request_goes_through_nginx_and_reads_only(tmp_path):
    ctx = build_context(tmp_path)
    grade(ctx)
    assert ctx.http.calls
    embed = "http://nginx/rtvi-embed/v1/generate_text_embeddings"
    for method, url, _ in ctx.http.calls:
        assert url.startswith("http://nginx/vios/") or url.startswith("http://nginx/es/") or url == embed, url
        assert method in ("GET", "POST"), (method, url)
        if method == "POST":
            assert url.endswith("/_count") or url.endswith("/_search") or url == embed, url


def test_the_appearance_search_stays_inside_the_clip(tmp_path):
    ctx = build_context(tmp_path, FIXTURES / "cases" / "own_track_is_nearest")
    marker = grade(ctx)
    assert "Object 7 in frame 1 is the closest match" in detail(marker, "part1.appearance_match"), common.summarize(marker)
    knn_bodies = [body for _, url, body in ctx.http.calls if body and "objects.embedding.vector" in json.dumps(body)]
    assert len(knn_bodies) == 1
    knn = knn_bodies[0]["query"]["nested"]["query"]["knn"]
    assert knn["filter"] == {"bool": {"filter": [grade_part1.stem_or_suffix_filter("rain_check")],
                                      "must_not": [{"term": {"timestamp": "2025-01-01T00:00:00.000Z"}}]}}
    assert knn_bodies[0]["query"]["nested"]["inner_hits"] == {"size": 1, "_source": False}


@pytest.mark.parametrize("text", [
    "2025-01-01T00:00:01Z",
    "2025-01-01T00:00:01.000Z",
    "2025-01-01T00:00:01.000000000Z",
    "2025-01-01T00:00:01.0+00:00",
    "2025-01-01T00:00:01",
])
def test_parse_iso_accepts_protobuf_and_elasticsearch_forms(text):
    assert common.fmt_iso(common.parse_iso(text)) == "2025-01-01T00:00:01.000Z"


def test_parse_iso_rejects_garbage():
    for bad in ("", "yesterday", None, 12):
        with pytest.raises(common.ArtefactError):
            common.parse_iso(bad)
