"""Proof for the degeneracy check, and the calibration behind its threshold.

Ported verbatim (only import paths changed) from tscribe-transcription-tool's
tests/test_degenerate_output.py -- this file tests pure functions with no
config coupling, so nothing else needed to change.

Learned in the source repo's production history: a chunk that "ran" but
replaced real speech with fabricated repetition would previously misdiagnose
as "content likely dropped" via the speaking-rate checks, close enough to be
actionable, wrong enough to mislead. This measures looping directly instead
of inferring it from a rate.

CALIBRATION, from a real incident's chunks:

    healthy chunks   0.0% - 1.3%   (twelve of fourteen)
    one chunk        8.5%          (loop over crosstalk)
    another chunk   18.7%          (loop over real instruction)

The threshold sits at 5%: well above the noisiest healthy chunk and well
below the quietest defect.

Run: pytest -q
"""

from __future__ import annotations

import pytest

from tscribe.core.class_pipeline.splitter import ChunkSpec
from tscribe.core.class_pipeline.verify import (
    ADVISORY_PREFIX,
    Contract,
    VerifyReport,
    _loop_coverage,
    assert_not_degenerate,
)


def _chunk(index: int = 7) -> ChunkSpec:
    return ChunkSpec(index=index, path="/nonexistent.wav", start_s=0.0, end_s=900.0)


def _judge(text: str, *, saturated: bool = False, index: int = 7) -> VerifyReport:
    report = VerifyReport()
    assert_not_degenerate(
        report, _chunk(index), text, Contract(), speech_measure_saturated=saturated
    )
    return report


# Reconstructions of the real chunks, at their real proportions.
LOOPED_TAIL = (  # ~700 words of instruction, then 41 straight repeats
    (
        "you are going to be evicted you are no longer allowed to be in the same "
        "neighborhood i want you to find a new neighborhood pair up with somebody "
        "different not from your block "
    )
    * 26
    + "it's a big part. " * 41
)
CROSSTALK_LOOP = (  # two sentences on repeat over unintelligible audio
    "i've always been the issue but it's because i'm not in the face of the police " * 6
    + "i'll go back to the real world what's up everybody i don't know "
    + "it's a great place. " * 7
    + "i i i i i i i"
)
CLEAN_LECTURE = (
    "so when we talk about a mobile device the first thing you check is whether "
    "the battery is swollen because that is the definitive sign it has to come "
    "out of service right away and never gets punctured or charged again "
) * 40


# ------------------------------------------------------------ the measurement


def test_loop_coverage_separates_looping_from_talking():
    """A phrase repeated across a chunk is speech; the same phrase repeated
    back-to-back is a loop. Only the second is contiguous, and only the second
    is measured."""
    talking = "it's a big part of the job and you will see that " * 12
    looping = "it's a big part. " * 41
    assert _loop_coverage(talking) < 0.05
    assert _loop_coverage(looping) > 0.90


def test_loop_coverage_matches_the_real_chunks():
    assert _loop_coverage(CLEAN_LECTURE) < 0.05
    assert _loop_coverage(LOOPED_TAIL) >= 0.05
    assert _loop_coverage(CROSSTALK_LOOP) >= 0.05


def test_a_short_stutter_is_not_a_loop():
    """Transcribers repeat a few words at chunk seams routinely. Two or three
    repeats is noise, not a defect -- the run threshold exists for this."""
    assert _loop_coverage("okay so the next thing " * 2 + CLEAN_LECTURE) < 0.05


def test_empty_and_tiny_inputs_do_not_explode():
    for t in ("", "   ", "one two three", "a b c d a b c d"):
        assert _loop_coverage(t) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------- disposition by what's underneath


def test_a_loop_over_real_speech_is_a_failure():
    """The speech measure was clean, so real instruction was present and the
    transcriber returned repetition instead. That stretch of the class is
    genuinely gone, and the contract must say so."""
    report = _judge(LOOPED_TAIL, saturated=False)
    assert not report.ok
    failure = next(c for c in report.failures if c.name.endswith("no_degenerate_output"))
    assert "back-to-back repeated" in failure.detail
    assert "real speech was present" in failure.detail
    assert "fabricated repetition" in failure.detail


def test_a_loop_over_saturated_crosstalk_is_an_advisory():
    """A room where everyone is talking at once contains no individually
    intelligible speaker, so a loop over it destroys nothing. Recorded, not
    punished -- failing here would be the manufactured verdict this module
    keeps learning not to produce."""
    report = _judge(CROSSTALK_LOOP, saturated=True, index=8)
    assert report.ok
    row = next(c for c in report.checks if c.name.endswith("no_degenerate_output"))
    assert row.detail.startswith(ADVISORY_PREFIX)
    assert "nothing here to lose" in row.detail


def test_a_clean_chunk_passes_either_way():
    for sat in (True, False):
        report = _judge(CLEAN_LECTURE, saturated=sat)
        assert report.ok
        row = next(c for c in report.checks if c.name.endswith("no_degenerate_output"))
        assert not row.detail.startswith(ADVISORY_PREFIX)


def test_saturation_excuses_the_loop_but_not_a_clean_chunks_loop():
    """The whole point of the split, in one assertion: identical text, opposite
    verdicts, decided by what the audio underneath actually was."""
    assert _judge(CROSSTALK_LOOP, saturated=True).ok
    assert not _judge(CROSSTALK_LOOP, saturated=False).ok


# ------------------------------------------------------ the report must not lie


def test_every_row_is_one_of_the_three_states():
    for text, sat in ((CLEAN_LECTURE, False), (CROSSTALK_LOOP, True), (LOOPED_TAIL, False)):
        for c in _judge(text, saturated=sat).checks:
            if not c.ok:
                assert not c.detail.startswith(ADVISORY_PREFIX)


def test_a_passing_row_reports_the_measurement_not_an_accusation():
    row = next(c for c in _judge(CLEAN_LECTURE).checks if c.name.endswith("no_degenerate_output"))
    for word in ("fabricated", "looped", "defect"):
        assert word not in row.detail


# ------------------------------------------------------------- the calibration


def test_the_threshold_clears_every_healthy_chunk_we_have_measured():
    """The twelve healthy chunks measured in the source incident, as loop
    coverages. The threshold has to sit above all of them with room, or it
    starts calling a talkative lecturer a defect."""
    healthy = [0.000, 0.008, 0.000, 0.000, 0.013, 0.000, 0.005, 0.007, 0.006, 0.000, 0.000, 0.000]
    assert max(healthy) < Contract().max_loop_fraction / 3.0


def test_the_threshold_catches_both_real_defects():
    """The two real defects measured, at 8.5% and 18.7%."""
    for measured in (0.085, 0.187):
        assert measured >= Contract().max_loop_fraction
