"""Hardening proofs for the tail-fold and cross-chunk yield relation.

Ported verbatim (only import paths changed) from tscribe-transcription-tool's
tests/test_tail_chunk_and_yield.py -- this file tests pure functions with no
config coupling, so nothing else needed to change.

1. A too-short final remainder must be folded into the previous chunk. A real
   2h30m05s recording split into ten 900s chunks plus a **5-second** sliver
   holding two words. Per-chunk rate bounds are nonsense at that scale -- a
   320 wpm ceiling permits 4800 words over 900s but only 27 over 5s, fewer
   than a normal sign-off.

2. Chunks of one recording must yield words at a consistent rate per second of
   detected speech. The absolute speech floor is only as good as the speech
   measurement beneath it, and on a real class that measurement was visibly
   unreliable (one chunk implied over 1700 wpm). A relative comparison across
   siblings survives that miscalibration, because whatever bias the detector
   has, it applies to every chunk of the same recording.

Point 2 became load-bearing: it is the *only* rate guard on a chunk whose
speech measurement saturated. Two consequences live here. It has to report
*which* chunks it actually judged, so ``rate_guard.coverage`` can name the
ones nothing covered. And it has to compare within cohorts of chunks whose
denominators mean the same thing -- the "same bias applies to every chunk"
argument stops holding the moment some chunks saturate and others don't.

Run: pytest -q  (ffmpeg + ffprobe must be on PATH)
"""

from __future__ import annotations

import pytest

from tscribe.core.class_pipeline.splitter import ChunkSpec, plan_boundaries
from tscribe.core.class_pipeline.verify import (
    Contract,
    VerifyReport,
    assert_speech_yield_consistent,
    assert_transcript_earned,
)

# --------------------------------------------------------------- the tail fix


def _durations(spans):
    return [round(e - s, 3) for s, e in spans]


def test_a_long_recording_no_longer_emits_a_sliver():
    """2h30m05s at 15-minute chunks. The regression, in one assertion."""
    spans = plan_boundaries(9005.0, 900.0, [], 10.0)
    assert len(spans) == 10, _durations(spans)
    assert spans[0][0] == 0.0
    assert spans[-1][1] == 9005.0  # still tiles the whole timeline
    assert _durations(spans)[-1] == pytest.approx(905.0)
    assert min(_durations(spans)) >= 60.0  # no chunk too short to judge


def test_short_tail_is_folded_into_the_previous_chunk():
    spans = plan_boundaries(1810.0, 900.0, [], 10.0)  # 10s remainder
    assert len(spans) == 2
    assert _durations(spans) == [900.0, 910.0]


def test_adequate_tail_is_left_alone():
    spans = plan_boundaries(2100.0, 900.0, [], 10.0)  # 300s remainder, fine
    assert len(spans) == 3
    assert _durations(spans) == [900.0, 900.0, 300.0]


def test_min_tail_is_clamped_for_small_chunks():
    """Fine-grained callers keep their chunk counts: the clamp is chunk_s/2, so
    a 60s default cannot swallow a 30s recording split into 10s chunks."""
    spans = plan_boundaries(30.0, 10.0, [(8.0, 9.0), (18.0, 19.0)], 3.0)
    assert len(spans) == 3
    assert spans[-1][1] == 30.0


def test_tail_fold_preserves_coverage_and_determinism():
    for total in (905.0, 1805.0, 3601.0, 9005.0, 12345.6):
        a = plan_boundaries(total, 900.0, [], 10.0)
        b = plan_boundaries(total, 900.0, [], 10.0)
        assert a == b, total
        assert a[0][0] == 0.0 and a[-1][1] == total
        for x, y in zip(a, a[1:]):  # contiguous, no gap or overlap
            assert x[1] == y[0]


def test_a_whole_file_shorter_than_one_chunk_is_untouched():
    spans = plan_boundaries(42.0, 900.0, [], 10.0)
    assert spans == [(0.0, 42.0)]


# ------------------------------------------------- the cross-chunk yield relation


def _levels(**speech_by_index):
    return {int(i): (-28.0, s) for i, s in speech_by_index.items()}


def _chunks(n, dur=900.0):
    return [
        ChunkSpec(index=i, path=f"/nonexistent/{i}.wav", start_s=i * dur, end_s=(i + 1) * dur)
        for i in range(n)
    ]


def test_real_class_yield_profile_passes():
    """Actual words/speech-second ratios from a real run. One chunk is a wild
    high outlier (bad denominator); none are starved. Must pass."""
    speech = [478, 629, 418, 106, 487, 407, 618, 298, 645, 458]
    words = [1870, 2366, 1510, 3023, 1987, 1654, 2158, 1742, 2268, 2230]
    report = VerifyReport()
    assert_speech_yield_consistent(
        report,
        _chunks(10),
        [" ".join(["w"] * n) for n in words],
        {i: (-28.0, float(s)) for i, s in enumerate(speech)},
        Contract(),
    )
    assert report.ok, [(c.name, c.detail) for c in report.failures]


def test_a_starved_chunk_is_caught_even_with_a_miscalibrated_detector():
    """THE POINT. One chunk had plenty of speech and produced almost nothing.
    No absolute threshold is consulted -- only its siblings."""
    speech = [478, 629, 418, 487, 600, 407, 618, 645]
    words = [1870, 2366, 1510, 1987, 40, 1654, 2158, 2268]  # chunk[4] starved
    report = VerifyReport()
    assert_speech_yield_consistent(
        report,
        _chunks(8),
        [" ".join(["w"] * n) for n in words],
        {i: (-28.0, float(s)) for i, s in enumerate(speech)},
        Contract(),
    )
    assert not report.ok
    failure = next(c for c in report.failures if c.name == "speech_yield.consistent")
    assert "chunk[4]" in failure.detail
    assert "content likely dropped" in failure.detail


def test_relation_is_skipped_without_enough_peers():
    report = VerifyReport()
    assert_speech_yield_consistent(
        report,
        _chunks(2),
        ["w " * 100, "w " * 100],
        {0: (-28.0, 300.0), 1: (-28.0, 300.0)},
        Contract(),
    )
    assert report.ok
    assert "not applicable" in report.checks[0].detail


def test_chunks_without_enough_speech_do_not_skew_the_median():
    """Short/quiet chunks are excluded from the comparison rather than dragging
    the median down and masking a genuinely starved sibling."""
    speech = [478, 629, 418, 487, 600, 2.0]  # last one is a tail sliver
    words = [1870, 2366, 1510, 1987, 2100, 2]
    report = VerifyReport()
    assert_speech_yield_consistent(
        report,
        _chunks(6),
        [" ".join(["w"] * n) for n in words],
        {i: (-28.0, float(s)) for i, s in enumerate(speech)},
        Contract(),
    )
    assert report.ok
    assert "across 5 chunks" in report.checks[0].detail


def test_the_relation_reports_which_chunks_it_actually_judged():
    """``rate_guard.coverage`` depends on knowing exactly which chunks were
    covered, so it is a return value rather than something to be scraped back out
    of the report prose."""
    speech = [478, 629, 418, 487, 600, 2.0]
    words = [1870, 2366, 1510, 1987, 2100, 2]
    report = VerifyReport()
    compared = assert_speech_yield_consistent(
        report,
        _chunks(6),
        [" ".join(["w"] * n) for n in words],
        {i: (-28.0, float(s)) for i, s in enumerate(speech)},
        Contract(),
    )
    assert compared == {0, 1, 2, 3, 4}  # the sliver did not take part

    report2 = VerifyReport()
    too_few = assert_speech_yield_consistent(
        report2,
        _chunks(2),
        ["w " * 100, "w " * 100],
        {0: (-28.0, 300.0), 1: (-28.0, 300.0)},
        Contract(),
    )
    assert too_few == set()  # below the quorum: nothing was judged


# ------------------------------------------- honest reporting of a bad denominator


def test_implausible_speech_rate_is_flagged_not_silently_banked(tmp_path, monkeypatch):
    """One chunk from a real run implied 1707 wpm. The transcript may be fine, so
    this must not fail the class -- but the report must not pretend the floor
    did meaningful work either."""
    import tscribe.core.class_pipeline.verify as v

    chunk = ChunkSpec(index=3, path="/nonexistent/3.wav", start_s=0.0, end_s=901.0)
    report = VerifyReport()
    v.assert_transcript_earned(
        report,
        chunk,
        " ".join(["w"] * 3023),
        Contract(),
        levels=(-28.0, 106.0),
    )
    names = {c.name for c in report.checks}
    assert "chunk[3].speech_measure_unreliable" in names
    assert report.ok  # informational, not fatal
    note = next(c for c in report.checks if c.name.endswith("speech_measure_unreliable"))
    assert "1711 wpm" in note.detail or "wpm exceeds 400" in note.detail
    assert "floor is weak" in note.detail


def test_normal_speech_rate_raises_no_flag():
    chunk = ChunkSpec(index=0, path="/nonexistent/0.wav", start_s=0.0, end_s=900.0)
    report = VerifyReport()
    assert_transcript_earned(
        report,
        chunk,
        " ".join(["w"] * 1870),
        Contract(),
        levels=(-28.0, 478.0),
    )
    assert not any("speech_measure_unreliable" in c.name for c in report.checks)
    assert report.ok


def test_the_two_denominator_failures_are_distinguished():
    """Under-counted speech (rate implausibly high) and over-counted speech
    (denominator pinned to the chunk) are different diseases with different
    treatments: the first is a caveat on an applied floor, the second means the
    floor never ran. They must never be reported as the same thing."""
    under = VerifyReport()
    assert_transcript_earned(
        under,
        ChunkSpec(index=3, path="/x.wav", start_s=0.0, end_s=901.0),
        " ".join(["w"] * 3023),
        Contract(),
        levels=(-28.0, 106.0),
    )
    over = VerifyReport()
    assert_transcript_earned(
        over,
        ChunkSpec(index=8, path="/x.wav", start_s=0.0, end_s=900.0),
        " ".join(["w"] * 292),
        Contract(),
        levels=(-28.0, 900.0),
    )

    under_names = {c.name for c in under.checks}
    over_names = {c.name for c in over.checks}

    assert "chunk[3].speech_measure_unreliable" in under_names
    assert "chunk[3].wpm_above_floor" in under_names  # floor still applied

    assert "chunk[8].speech_measure_saturated" in over_names
    assert "chunk[8].wpm_above_floor" not in over_names  # floor abstained
    assert over.ok
