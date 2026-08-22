"""Proof for the speaking-rate contract: it must forgive quiet *classes* without
forgiving dropped *content* -- and it must not accuse a transcript on the
strength of a measurement that never measured anything.

Ported verbatim (only import paths changed) from tscribe-transcription-tool's
tests/test_speech_rate_contract.py -- this file tests pure functions with no
config coupling, so nothing else needed to change.

Regression origin, part one: a real class failed the gate on a chunk with
``26 wpm outside [40,320]``. The transcript was fine. The chunk was fifteen
minutes of wall clock holding about three minutes of talking while the room
worked quietly, and the verifier divided the words by the wall clock. The fix
was to divide by *detected speech time* instead.

Regression origin, part two: the same check failed the same way through a
different door -- ``wpm_above_floor: 19 wpm across 900s of detected speech``.
Note the denominator: 900s of "speech" in a 900s chunk. ``silencedetect`` had
found *zero* silence, because that stretch was peer mock interviews and a room
in simultaneous paired conversation never drops below the -30 dBFS level gate
for a third of a second. With the denominator pinned to the chunk duration,
"words per minute of speech" silently became words per minute of wall clock
and reproduced the first regression exactly.

So the contract has to distinguish four things, not three:

  * little speech present, few words          -> PASS (honest quiet room)
  * lots of speech present, few words         -> FAIL (content really was dropped)
  * more words than the wall clock allows     -> FAIL (looping/garbled output)
  * speech time *unmeasured* (saturated)      -> ABSTAIN, hand off to the relation

The fixtures are synthetic but the levels are literal: talk sits around
-21 dBFS, room tone around -54 dBFS, and a workshop chunk's whole-file mean lands
near -28 dBFS -- comfortably over the -45 dBFS "is there audio at all" floor,
which is exactly why the old gate engaged its rate floor in the first place. The
crosstalk fixture is pink noise at -27 dBFS mean: audible, continuous, and never
quiet enough for the level gate to call it a pause. It measures
speech/wall-clock = 1.000, just like the real incident chunk.

Run: pytest -q  (ffmpeg + ffprobe must be on PATH)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tscribe.core.class_pipeline.splitter import ChunkSpec, probe_duration
from tscribe.core.class_pipeline.verify import (
    ADVISORY_PREFIX,
    RATE_GUARD_FLOOR_APPLIED,
    RATE_GUARD_SATURATED,
    Contract,
    VerifyReport,
    _chunk_levels,
    assert_rate_guard_coverage,
    assert_transcript_earned,
)


def _gen(spec: str, out: Path, af: str | None = None) -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", spec]
    if af:
        cmd += ["-af", af]
    cmd += ["-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(out)]
    subprocess.run(cmd, check=True)
    return out


def _concat(parts: list[Path], out: Path, tmp: Path) -> Path:
    listfile = tmp / f"{out.stem}_list.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listfile),
            "-c",
            "copy",
            str(out),
        ],
        check=True,
    )
    return out


def _talk(tmp: Path, seconds: float) -> Path:
    """Speech-level audio: ~-21 dBFS mean, peaks ~-18 dBFS, well over any
    reasonable speech threshold."""
    return _gen(f"sine=frequency=200:duration={seconds:.3f}", tmp / f"talk_{seconds * 10:.0f}.wav")


def _room(tmp: Path, seconds: float) -> Path:
    """Empty-room tone: audible to a meter, silence to a speech detector."""
    return _gen(
        f"anoisesrc=r=16000:c=pink:duration={seconds:.3f}",
        tmp / f"room_{seconds * 10:.0f}.wav",
        af="volume=-40dB",
    )


def _crosstalk(tmp: Path, seconds: float) -> Path:
    """A room where everyone is talking at once.

    Continuous, speech-level (~-27 dBFS mean), and never below the -30 dBFS gate
    for long enough to register as a pause. This is what real simultaneous
    conversation looks like to ``silencedetect``, and it is the whole reason the
    speech-time denominator cannot be trusted blindly.
    """
    return _gen(
        f"anoisesrc=r=16000:c=pink:duration={seconds:.3f}",
        tmp / f"crosstalk_{seconds * 10:.0f}.wav",
        af="volume=-13dB",
    )


@pytest.fixture
def workshop_chunk(tmp_path):
    """15 min wall clock: 8 x (22.5s of talking + 90s of quiet work) = 180s speech."""
    talk, room = _talk(tmp_path, 22.5), _room(tmp_path, 90.0)
    return _concat([talk, room] * 8, tmp_path / "workshop.wav", tmp_path)


@pytest.fixture
def lecture_chunk(tmp_path):
    """15 min of a dense lecture: 30 x (25.5s of talking + 4.5s of breath).

    Deliberately *not* an unbroken tone. Real speech, however continuous it
    sounds, is punctuated -- a lecturer breathes, pauses between sentences, waits
    out a question -- so a real 900s lecture chunk measures somewhere around
    765s of detected speech (85%), not 900s.

    This matters for what the fixture proves. A pure 900s tone would measure
    100% speech, which is the *saturated* case the floor now abstains on, so it
    could no longer demonstrate that the floor still catches dropped content.
    Making the fixture behave like speech keeps the teeth in this test where they
    belong.
    """
    talk, breath = _talk(tmp_path, 25.5), _room(tmp_path, 4.5)
    return _concat([talk, breath] * 30, tmp_path / "lecture.wav", tmp_path)


@pytest.fixture
def crosstalk_chunk(tmp_path):
    """15 min of unbroken simultaneous conversation."""
    return _concat([_crosstalk(tmp_path, 90.0)] * 10, tmp_path / "crosstalk.wav", tmp_path)


# Deterministic non-repetitive filler -- see test_class_pipeline.py for why.
_VOCAB = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
    "lima mike november oscar papa quebec romeo sierra tango uniform "
    "victor whiskey xray yankee zulu"
).split()


def _words(n: int, seed: int = 1234) -> str:
    import random

    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCAB) for _ in range(n))


def _judge(chunk_path: Path, words: int, index: int = 10) -> VerifyReport:
    dur = probe_duration(str(chunk_path))
    chunk = ChunkSpec(index=index, path=str(chunk_path), start_s=0.0, end_s=dur)
    report = VerifyReport()
    assert_transcript_earned(report, chunk, _words(words), Contract())
    return report


def _outcome(chunk_path: Path, words: int, index: int = 10) -> str:
    dur = probe_duration(str(chunk_path))
    chunk = ChunkSpec(index=index, path=str(chunk_path), start_s=0.0, end_s=dur)
    return assert_transcript_earned(VerifyReport(), chunk, _words(words), Contract())


def _names(report: VerifyReport) -> set[str]:
    return {c.name.split("].", 1)[-1] for c in report.checks}


# ---------------------------------------------------------------- measurement


def test_chunk_levels_separates_audio_presence_from_speech_time(workshop_chunk):
    """The two questions the old single mean-volume test conflated."""
    mean_db, speech_s = _chunk_levels(str(workshop_chunk), Contract())
    # There is plainly audio here -- this is why the old rate floor engaged.
    assert mean_db is not None and mean_db > Contract().silence_floor_db
    # But only ~180s of it is anybody talking.
    assert speech_s == pytest.approx(180.0, abs=5.0)


def test_chunk_levels_measures_most_but_not_all_of_a_dense_lecture(lecture_chunk):
    """A talkative chunk still leaves room to breathe -- ~85%, not 100%."""
    mean_db, speech_s = _chunk_levels(str(lecture_chunk), Contract())
    assert mean_db is not None
    assert speech_s == pytest.approx(765.0, abs=10.0)
    assert speech_s / 900.0 < Contract().max_speech_fraction_for_floor


def test_chunk_levels_saturates_on_continuous_room_sound(crosstalk_chunk):
    """THE SATURATION MECHANISM, measured directly.

    The level gate cannot tell a room of simultaneous conversation from one
    person talking without pause, so it reports the entire chunk as speech. The
    measurement is not slightly off -- it has no information left in it.
    """
    mean_db, speech_s = _chunk_levels(str(crosstalk_chunk), Contract())
    dur = probe_duration(str(crosstalk_chunk))
    assert mean_db is not None and mean_db > Contract().silence_floor_db
    assert speech_s == pytest.approx(dur, abs=1.0)
    assert speech_s / dur >= Contract().max_speech_fraction_for_floor


# ------------------------------------------------------------------ the fix


def test_sparse_speech_chunk_is_not_a_failure(workshop_chunk):
    """THE FIRST REGRESSION. 390 words = 26 wpm on the wall clock, but
    130 wpm across the speech that actually happened. Must pass."""
    report = _judge(workshop_chunk, words=390)
    assert report.ok, [(c.name, c.detail) for c in report.failures]
    assert "sparse_speech_ok" not in _names(report)  # 180s is enough to judge
    assert "wpm_above_floor" in _names(report)


def test_sparse_speech_floor_is_skipped_when_speech_is_negligible(tmp_path):
    """A chunk with only a few seconds of talking: a rate is meaningless, so the
    floor is skipped rather than guessed at."""
    chunk = _concat(
        [_talk(tmp_path, 10.0), _room(tmp_path, 90.0)], tmp_path / "almost_empty.wav", tmp_path
    )
    report = _judge(chunk, words=20)
    assert report.ok, [(c.name, c.detail) for c in report.failures]
    assert "sparse_speech_ok" in _names(report)
    assert "wpm_above_floor" not in _names(report)


def test_the_crosstalk_chunk_is_not_accused(crosstalk_chunk):
    """THE SECOND REGRESSION, in one assertion.

    292 words in a 900s chunk of unbroken crosstalk. The old check computed
    19 wpm "across 900s of detected speech" and called it dropped content. It had
    measured nothing: with silence_s = 0 the speech denominator equals the wall
    clock, so this is the first regression's wall-clock floor wearing a new name.
    """
    report = _judge(crosstalk_chunk, words=292, index=8)
    assert report.ok, [(c.name, c.detail) for c in report.failures]
    assert "wpm_above_floor" not in _names(report), (
        "the floor must not assert on a denominator that measured nothing"
    )
    assert "speech_measure_saturated" in _names(report)


def test_saturation_is_reported_as_an_advisory_not_a_silent_skip(crosstalk_chunk):
    """Abstaining is only honest if the report says so, in the machine-readable
    way the rest of this module uses for 'passed, but read this'."""
    report = _judge(crosstalk_chunk, words=292, index=8)
    note = next(c for c in report.checks if c.name.endswith("speech_measure_saturated"))
    assert note.ok
    assert note.detail.startswith(ADVISORY_PREFIX)
    assert "no silence detected" in note.detail
    assert "1.000" in note.detail  # the ratio that gave it away
    assert "abstains" in note.detail
    assert "yield relation" in note.detail  # names the guard taking over


def test_saturated_outcome_is_reported_to_the_caller(crosstalk_chunk, lecture_chunk):
    """verify_run needs to know which chunks went unjudged, so the outcome is a
    return value rather than something to be re-derived by parsing prose."""
    assert _outcome(crosstalk_chunk, words=292, index=8) == RATE_GUARD_SATURATED
    assert _outcome(lecture_chunk, words=1500) == RATE_GUARD_FLOOR_APPLIED


# ------------------------------------------------------- still has its teeth


def test_dropped_content_in_a_real_lecture_still_fails(lecture_chunk):
    """15 minutes of solid talking that transcribed to 390 words IS a failure --
    the same word count that must pass on the workshop chunk. 390 words over
    765s of measured speech is 31 wpm, and this time the denominator is real."""
    report = _judge(lecture_chunk, words=390)
    assert not report.ok
    assert any("wpm_above_floor" in c.name for c in report.failures)
    detail = next(c.detail for c in report.failures if "wpm_above_floor" in c.name)
    assert "of detected speech" in detail  # denominator is reported honestly
    assert "765s" in detail or "764s" in detail or "766s" in detail


def test_the_ceiling_still_applies_to_a_saturated_chunk(crosstalk_chunk):
    """Only the floor abstains. The ceiling is measured over wall clock, which
    saturation does not touch -- 15 minutes still cannot hold 20000 words."""
    report = _judge(crosstalk_chunk, words=20000, index=8)
    assert not report.ok
    assert any("wpm_under_ceiling" in c.name for c in report.failures)


def test_an_empty_transcript_still_fails_on_a_saturated_chunk(crosstalk_chunk):
    """Saturation excuses a *low* rate, never a missing transcript."""
    report = _judge(crosstalk_chunk, words=0, index=8)
    assert not report.ok
    assert any("transcript_nonempty" in c.name for c in report.failures)


def test_empty_transcript_on_audible_chunk_still_fails(workshop_chunk):
    report = _judge(workshop_chunk, words=0)
    assert not report.ok
    assert any("transcript_nonempty" in c.name for c in report.failures)


def test_looping_output_still_hits_the_ceiling(workshop_chunk):
    """Wall clock is the right denominator for the ceiling: 15 minutes cannot
    contain 20000 words no matter how the speech is arranged."""
    report = _judge(workshop_chunk, words=20000)
    assert not report.ok
    assert any("wpm_under_ceiling" in c.name for c in report.failures)


def test_silent_chunk_still_allowed_to_be_empty(tmp_path):
    chunk = _room(tmp_path, 120.0)
    dur = probe_duration(str(chunk))
    spec = ChunkSpec(index=0, path=str(chunk), start_s=0.0, end_s=dur)
    report = VerifyReport()
    assert_transcript_earned(report, spec, "", Contract())
    assert report.ok
    assert "silence_ok" in _names(report)


# ------------------------------- abstention must be disclosed, not papered over


def test_an_uncoverable_abstention_is_disclosed_not_invented_as_a_failure():
    """A chunk the floor abstained on, with no cohort to compare it against, is
    *unproven* -- and the report has to say so. What it must not do is fail the
    class: that would manufacture a verdict the evidence does not support, which
    is the same mistake the floor made before. The gap is named; the rest of the
    contract still covers the chunk."""
    report = VerifyReport()
    assert_rate_guard_coverage(
        report, [8], judged_by_relation=set(), floor_applied_indices=[0, 1, 2], contract=Contract()
    )
    assert report.ok
    row = next(c for c in report.checks if c.name == "rate_guard.coverage")
    assert row.detail.startswith(ADVISORY_PREFIX)  # passed, but read this
    assert "chunk[8]" in row.detail
    assert "no speaking-rate check covered it" in row.detail
    assert "the rest of the contract still applies" in row.detail


def test_abstention_is_clean_when_the_relation_took_over():
    report = VerifyReport()
    assert_rate_guard_coverage(
        report,
        [8],
        judged_by_relation={4, 6, 8, 9},
        floor_applied_indices=[0, 1],
        contract=Contract(),
    )
    assert report.ok
    row = next(c for c in report.checks if c.name == "rate_guard.coverage")
    assert row.detail.startswith(ADVISORY_PREFIX)
    assert "judged" in row.detail and "yield relation" in row.detail


def test_a_recording_with_no_rate_check_at_all_does_fail():
    """The one indefensible case. If the floor abstained everywhere and no cohort
    ever reached a quorum, the rate contract proved nothing about this recording,
    and calling that verified would overstate the result rather than merely leave
    a gap in it."""
    report = VerifyReport()
    assert_rate_guard_coverage(
        report, [0, 1], judged_by_relation=set(), floor_applied_indices=[], contract=Contract()
    )
    assert not report.ok
    failure = next(c for c in report.failures if c.name == "rate_guard.coverage")
    assert "proved nothing at all" in failure.detail
    assert not failure.detail.startswith(ADVISORY_PREFIX)


def test_no_abstention_reports_a_clean_pass():
    report = VerifyReport()
    assert_rate_guard_coverage(
        report,
        [],
        judged_by_relation={0, 1, 2, 3},
        floor_applied_indices=[0, 1, 2, 3],
        contract=Contract(),
    )
    assert report.ok
    row = next(c for c in report.checks if c.name == "rate_guard.coverage")
    assert not row.detail.startswith(ADVISORY_PREFIX)


# -------------------------------------------------------- the report must not lie


@pytest.mark.parametrize("words", [390, 20, 20000, 0])
def test_passing_checks_never_carry_failure_prose(workshop_chunk, words):
    """A passing row that reads like a failure misleads the agent deciding whether
    this transcript may become a study guide. Details state measurements; the
    accusation is appended only on failure.

    Advisory rows are the deliberate exception -- they passed but carry a caveat
    -- and they must announce that in a machine-readable way rather than by tone.
    """
    report = _judge(workshop_chunk, words=words)
    accusations = ("exceeds", "below", "likely dropped", "is empty", "garbled")
    for c in report.checks:
        if c.ok and not c.detail.startswith(ADVISORY_PREFIX):
            assert not any(a in c.detail for a in accusations), (
                f"passing check {c.name!r} reads like a failure: {c.detail!r}"
            )


@pytest.mark.parametrize("words", [292, 20000, 0])
def test_saturated_chunk_rows_also_never_read_like_failures(crosstalk_chunk, words):
    report = _judge(crosstalk_chunk, words=words, index=8)
    accusations = ("exceeds", "below", "likely dropped", "is empty", "garbled")
    for c in report.checks:
        if c.ok and not c.detail.startswith(ADVISORY_PREFIX):
            assert not any(a in c.detail for a in accusations), (
                f"passing check {c.name!r} reads like a failure: {c.detail!r}"
            )


def test_a_report_row_is_always_one_of_three_unambiguous_states(workshop_chunk):
    """failed / passed-with-caveat / passed-clean, distinguishable without
    reading the prose."""
    report = _judge(workshop_chunk, words=20000)
    states = {(c.ok, c.detail.startswith(ADVISORY_PREFIX)) for c in report.checks}
    assert (False, True) not in states, "a failure must never be dressed as advisory"
    advisories = [c for c in report.checks if c.detail.startswith(ADVISORY_PREFIX)]
    assert advisories, "this fixture should raise the unreliable-denominator caveat"
    assert all(c.ok for c in advisories)


def test_a_failed_coverage_check_is_never_dressed_as_advisory():
    """The one place a caveat and a failure could get confused, pinned down."""
    report = VerifyReport()
    assert_rate_guard_coverage(
        report, [0], judged_by_relation=set(), floor_applied_indices=[], contract=Contract()
    )
    failure = next(c for c in report.failures if c.name == "rate_guard.coverage")
    assert not failure.detail.startswith(ADVISORY_PREFIX)


def test_failing_checks_report_both_measurement_and_diagnosis(lecture_chunk):
    report = _judge(lecture_chunk, words=390)
    failure = next(c for c in report.failures if "wpm_above_floor" in c.name)
    assert "31 wpm" in failure.detail or "30 wpm" in failure.detail  # the measurement
    assert "of detected speech" in failure.detail  # the denominator
    assert "below 40 wpm" in failure.detail  # the diagnosis


def test_unmeasurable_speech_time_fails_closed(workshop_chunk, monkeypatch):
    """If speech time cannot be measured we fall back to the old wall-clock
    floor -- never wave the chunk through. Unmeasurable is not the same as
    saturated: here we have no number at all, so the conservative check is the
    honest one."""
    import tscribe.core.class_pipeline.verify as v

    monkeypatch.setattr(v, "_chunk_levels", lambda path, contract, **kw: (-28.0, None))
    dur = probe_duration(str(workshop_chunk))
    spec = ChunkSpec(index=10, path=str(workshop_chunk), start_s=0.0, end_s=dur)
    report = VerifyReport()
    v.assert_transcript_earned(report, spec, _words(390), Contract())
    assert not report.ok
    assert any("wpm_above_floor" in c.name for c in report.failures)
    assert "unmeasurable" in next(c.detail for c in report.failures if "wpm_above_floor" in c.name)


# -------------------------------------------------- the threshold is calibrated


def test_the_saturation_threshold_clears_every_real_measurement_we_have():
    """Ten chunks from a real class, as speech fractions of a 900s chunk. None
    comes close to the threshold, because human speech has pauses in it. A
    chunk that measures 1.000 is categorically a different animal, and the
    threshold has to sit above the real data with room to spare or it would
    start excusing genuinely starved chunks."""
    real_speech_s = [478, 629, 418, 106, 487, 407, 618, 298, 645, 458]
    fractions = [s / 900.0 for s in real_speech_s]
    assert max(fractions) < 0.75
    assert Contract().max_speech_fraction_for_floor > max(fractions) + 0.2


# ------------------------------------------ the whole contract, end to end


def _mixed_recording_chunks(tmp_path, n_normal: int, n_saturated: int, chunk_s: float = 180.0):
    """Build real chunk files: ``n_normal`` dense-lecture chunks followed by
    ``n_saturated`` crosstalk chunks, tiling one timeline contiguously."""
    specs, texts = [], []
    talk, breath = _talk(tmp_path, 25.5), _room(tmp_path, 4.5)
    cycles = int(chunk_s // 30)
    for i in range(n_normal + n_saturated):
        out = tmp_path / f"c{i}.wav"
        if i < n_normal:
            _concat([talk, breath] * cycles, out, tmp_path)
            words = 300  # ~118 wpm over ~153s of speech: healthy
        else:
            _concat([_crosstalk(tmp_path, chunk_s)], out, tmp_path)
            words = 58  # 19 wpm over wall clock: the real crosstalk rate
        specs.append(
            ChunkSpec(index=i, path=str(out), start_s=i * chunk_s, end_s=(i + 1) * chunk_s)
        )
        texts.append(_words(words))
    return specs, texts


def _run_contract(specs, texts):
    from tscribe.core.class_pipeline.pipeline import reassemble
    from tscribe.core.class_pipeline.verify import verify_run

    assembled = reassemble(specs, texts, "class.m4a")
    return verify_run(
        input_path="class.m4a",
        chunks=specs,
        chunk_texts=texts,
        assembled=assembled,
        total_s=specs[-1].end_s,
    )


def test_a_class_with_one_crosstalk_chunk_passes_the_whole_contract(tmp_path):
    """A real-shaped class in miniature: mostly lecture, one stretch where the
    whole room talks at once. That stretch is judged by its siblings instead of
    by an absolute floor, and the class ships."""
    specs, texts = _mixed_recording_chunks(tmp_path, n_normal=4, n_saturated=1)
    report = _run_contract(specs, texts)
    assert report.ok, [(c.name, c.detail) for c in report.failures]

    names = {c.name for c in report.checks}
    assert "chunk[4].speech_measure_saturated" in names
    assert "chunk[4].wpm_above_floor" not in names  # abstained
    assert "chunk[0].wpm_above_floor" in names  # siblings still judged

    coverage = next(c for c in report.checks if c.name == "rate_guard.coverage")
    assert coverage.ok
    assert coverage.detail.startswith(ADVISORY_PREFIX)
    assert "chunk[4]" in coverage.detail


def test_a_lone_saturated_chunk_is_disclosed_as_unproven_and_the_class_ships(tmp_path):
    """One noisy stretch, no crosstalk peers to compare it against.

    Nothing can honestly judge its speaking rate: a room in simultaneous
    conversation and a chunk that dropped content look the same to every
    measurement we have. So the report names it as unchecked and the class ships
    on the strength of everything else. Inventing a failure here is what a real
    incident cost.
    """
    specs, texts = _mixed_recording_chunks(tmp_path, n_normal=4, n_saturated=1)
    report = _run_contract(specs, texts)
    assert report.ok, [(c.name, c.detail) for c in report.failures]

    coverage = next(c for c in report.checks if c.name == "rate_guard.coverage")
    assert coverage.detail.startswith(ADVISORY_PREFIX)
    assert "chunk[4]" in coverage.detail
    assert "no speaking-rate check covered it" in coverage.detail


def test_a_starved_chunk_among_crosstalk_peers_is_still_caught(tmp_path):
    """The guard is not a hiding place once a cohort exists -- which is the real
    incident shape, where a whole half of the class was mock interviews.

    One crosstalk chunk transcribes to almost nothing despite being crosstalk
    like its peers. It is compared against the other saturated chunks, whose
    denominators mean the same thing as its own, and it fails there.
    """
    specs, texts = _mixed_recording_chunks(tmp_path, n_normal=4, n_saturated=4)
    texts[7] = _words(5)
    report = _run_contract(specs, texts)
    assert not report.ok
    failure = next(c for c in report.failures if c.name == "speech_yield.consistent_saturated")
    assert "chunk[7]" in failure.detail
    assert "content likely dropped" in failure.detail


def test_a_healthy_crosstalk_half_passes_on_its_own_terms(tmp_path):
    """A real class shape: a lecture half and a mock-interview half. The
    crosstalk chunks yield far fewer words per second than the lecture chunks
    and that is *not* a defect -- so they are judged against each other, and
    the class passes."""
    specs, texts = _mixed_recording_chunks(tmp_path, n_normal=4, n_saturated=4)
    report = _run_contract(specs, texts)
    assert report.ok, [(c.name, c.detail) for c in report.failures]

    names = {c.name for c in report.checks}
    assert "speech_yield.consistent" in names  # the lecture cohort
    assert "speech_yield.consistent_saturated" in names  # the crosstalk cohort

    coverage = next(c for c in report.checks if c.name == "rate_guard.coverage")
    assert coverage.ok
    assert "judged" in coverage.detail  # every abstention was covered


def test_pooling_the_two_cohorts_would_have_false_accused(tmp_path):
    """Why cohorts exist, stated as a measurement.

    The crosstalk chunks' words-per-second-of-speech is far below the lecture
    chunks' -- entirely because their denominator is inflated to the full chunk
    duration. Pooled into one median they look starved, which is the same false
    accusation the floor was just taught not to make. The numbers that would have
    caused it are asserted here so the reason this design exists cannot quietly
    rot away.
    """
    from tscribe.core.class_pipeline.verify import _chunk_levels, word_count

    specs, texts = _mixed_recording_chunks(tmp_path, n_normal=4, n_saturated=4)
    ratios = []
    for spec, txt in zip(specs, texts):
        _, speech_s = _chunk_levels(spec.path, Contract())
        ratios.append(word_count(txt) / speech_s)

    lecture, crosstalk = ratios[:4], ratios[4:]
    pooled = sorted(ratios)
    pooled_median = (pooled[3] + pooled[4]) / 2.0

    assert min(lecture) > max(crosstalk) * 3  # incommensurable, by a lot
    # Pooled, every crosstalk chunk trips the 30%-of-median outlier threshold.
    assert all(r < 0.30 * pooled_median for r in crosstalk)
    # Judged against its own cohort, none of them does.
    own_median = sorted(crosstalk)[len(crosstalk) // 2]
    assert all(r >= 0.30 * own_median for r in crosstalk)
