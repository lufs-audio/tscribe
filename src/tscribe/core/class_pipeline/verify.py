"""The contract enforcer for the class pipeline.

Ported verbatim (algorithm unchanged) from
tscribe-transcription-tool.class_pipeline.verify — this is the most carefully
calibrated part of the whole system (see the incident notes below), so this
port changes nothing about how a check decides pass/fail. Only the module
path changed.

"Works" means *proven correct*, not merely exited 0. This module turns "ran"
into "proven". It declares a contract for the chunked-transcribe operation as
a set of reusable assertion primitives plus numeric post-conditions with
sensible, audio-literate tolerances, and refuses to report success on any
violation.

Two layers, mirroring the Workchain verifier philosophy:

  * Per-execution checks (cheap, run every time): outputs exist, decode, carry
    audio at the right sample rate, no chunk silently produced empty text, the
    chunk plan tiles the whole timeline with no gap or overlap, and reassembly
    dropped no words.

  * Metamorphic relations (assert invariants, not exact outputs — transcription
    has no single right answer): the split plan is deterministic; the chunked
    word count stays within tolerance of a whole-file run. The expensive
    whole-file relation runs at test/certify time, not on every class.

A third discipline runs through both layers and is worth naming, because this
module keeps relearning it: **a check may only assert what its measurement can
actually support.** The speaking-rate floor produced three false accusations
against three good transcripts in the source repo's production history, each
time because it asserted an absolute over a denominator that could not bear
one. When the measurement under a check is not trustworthy, the honest move is
to abstain, say so in the report, and hand off to a check that does not depend
on it — never to guess, and never to quietly fall back to a denominator
already known to be wrong. See :func:`assert_transcript_earned` and
:func:`assert_rate_guard_coverage`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .splitter import WAV_CHANNELS, WAV_SR, ChunkSpec, plan_boundaries

#: Marks a check that passed but carries a caveat worth surfacing. A reader can
#: split "passed clean" from "passed, but read this" without parsing prose.
ADVISORY_PREFIX = "advisory: "

# Outcomes of the per-chunk speaking-rate guard, returned by
# :func:`assert_transcript_earned` so the caller can tell which chunks ended up
# with no absolute guard and therefore *must* be covered by the relative one.
RATE_GUARD_FLOOR_APPLIED = "floor_applied"
RATE_GUARD_WALL_CLOCK_FALLBACK = "wall_clock_fallback"
RATE_GUARD_SATURATED = "speech_measure_saturated"
RATE_GUARD_SPARSE = "sparse_speech"
RATE_GUARD_SILENCE = "silence"
RATE_GUARD_NO_TEXT = "no_text"


class ContractViolation(Exception):
    """Raised when a verified run fails its contract. Never swallow this.

    Carries the evidence: ``report`` (the full check list, failures included)
    and ``transcript`` (the unverified output, when one was produced).

    A failed contract must yield *more* information than a passing one, not
    less. Without the report a reader cannot tell a genuinely dropped chunk from
    a check that misfired — and this module's own history says misfires happen.
    Without the transcript, hours of compute are discarded over a verdict
    nobody can audit, and the only way to look at the evidence is to run the
    whole class again.

    Holding them here does not weaken the gate. The exception still stops the
    pipeline, nothing is written to ``output_path``, nothing is written back to
    Drive, and no consumer may treat this payload as verified. It only means the
    failure is auditable and, if the check was wrong, recoverable.
    """

    def __init__(
        self,
        message: str,
        *,
        report: VerifyReport | None = None,
        transcript: str | None = None,
    ) -> None:
        super().__init__(message)
        self.report = report
        self.transcript = transcript


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def add(self, name: str, ok: bool, detail: str = "", fail_hint: str = "") -> None:
        """Record a check.

        ``detail`` must be a statement of the *measurement* that is true whether
        the check passed or failed; ``fail_hint`` is the accusation, appended only
        when it actually failed. A report whose passing rows read like failures
        misleads whoever reads it — and for this pipeline the reader is an agent
        deciding whether a transcript may become a study guide.

        A row therefore reports one of three states, distinguishable without
        reading prose: **failed** (``ok`` is False), **passed with a caveat**
        (``ok`` is True and ``detail`` starts with ``ADVISORY_PREFIX``), and
        **passed clean** (everything else). The middle state exists because some
        things we learn are true but not disqualifying — an unreliable
        measurement, say — and collapsing them into either of the other two
        would be a small lie in one direction or the other.
        """
        if not ok and fail_hint:
            detail = f"{detail} -- {fail_hint}" if detail else fail_hint
        self.checks.append(Check(name=name, ok=bool(ok), detail=detail))

    def raise_if_failed(self, transcript: str | None = None) -> VerifyReport:
        """Raise :class:`ContractViolation` if any check failed.

        ``transcript`` is attached to the exception so a caller can retain the
        unverified output for audit without re-running the job. Passing it does
        not make the transcript verified — the raise is what matters.
        """
        if not self.ok:
            lines = "\n".join(f"  - {c.name}: {c.detail}" for c in self.failures)
            raise ContractViolation(
                f"{len(self.failures)} contract check(s) failed:\n{lines}",
                report=self,
                transcript=transcript,
            )
        return self


@dataclass
class Contract:
    """Numeric post-conditions for a class transcription. Tolerances are
    musical/technical, not arbitrary — a lecture runs ~90-180 wpm; a chunk that
    decodes but yields 5 wpm is a partial failure even though it's non-empty.

    A speaking rate is words per minute *of speech*, so the rate floor is
    measured over detected speech time, not wall clock — see
    :func:`assert_transcript_earned`."""

    sample_rate: int = WAV_SR
    channels: int = WAV_CHANNELS
    duration_tolerance_s: float = 0.75  # sum(chunks) vs whole-file
    coverage_tolerance_s: float = 0.05  # tiling gap/overlap (~1 frame)
    silence_floor_db: float = -45.0  # below this, a chunk may be empty
    speech_floor_db: float = -30.0  # below this, audio isn't speech
    min_speech_s_for_rate: float = 60.0  # need this much speech to judge a rate
    min_wpm_if_speech: float = 40.0  # floor, over detected speech time
    max_wpm: float = 320.0  # ceiling, over wall clock
    implausible_speech_wpm: float = 400.0  # above this, the denominator is suspect
    #: Above this ratio of detected speech to wall clock, ``silencedetect`` found
    #: (essentially) no silence and has therefore discriminated nothing: the
    #: "speech time" denominator has collapsed into wall clock and the floor
    #: would silently re-become the wall-clock floor that was already proven
    #: wrong. Real recordings sit far below this. A ratio at 1.0 is a failed
    #: measurement, not a talkative room. See :func:`assert_transcript_earned`.
    max_speech_fraction_for_floor: float = 0.98
    min_chunks_for_yield_relation: int = 4  # need this many peers to compare
    #: Outlier threshold for the cross-chunk yield relation, as a fraction of the
    #: cohort median. A genuinely starved chunk sits ~100x below its siblings,
    #: not a small multiple, so the threshold belongs in that gap rather than at
    #: the boundary of normal variation. This relation is the blunt backstop;
    #: :func:`assert_not_degenerate` is the precise instrument.
    min_yield_fraction_of_median: float = 0.10
    #: Degeneracy: fraction of a chunk's tokens allowed to sit inside back-to-back
    #: repeated n-grams.
    max_loop_fraction: float = 0.05
    loop_ngram: int = 4  # window for the repeat detector
    min_loop_runs: int = 3  # back-to-back repeats before it counts
    whole_vs_chunked_tolerance: float = 0.03  # 3% word-count drift, certify-time


# --------------------------------------------------------------------------- #
# Reusable assertion primitives — authors compose these, rarely hand-write.
# --------------------------------------------------------------------------- #


def _ffprobe_field(path: str, stream_entry: str) -> str | None:
    cp = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            stream_entry,
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def _probe_duration(path: str) -> float | None:
    val = _ffprobe_field(path, "format=duration")
    try:
        return float(val) if val is not None else None
    except ValueError:
        return None


def _mean_volume_db(path: str) -> float | None:
    """Mean volume of a chunk via ffmpeg volumedetect (stdlib-free RMS proxy)."""
    cp = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", cp.stderr)
    return float(m.group(1)) if m else None


def _chunk_levels(
    path: str, contract: Contract, min_silence_s: float = 0.35
) -> tuple[float | None, float | None]:
    """Return ``(mean_volume_db, speech_seconds)`` for a chunk in ONE decode pass.

    ``volumedetect`` answers "is there any audio here at all" (whole-chunk RMS);
    ``silencedetect`` answers "for how long was anyone actually talking". Chaining
    them means this costs the same single decode the old mean-volume-only check
    already paid.

    Why this parses silences here instead of calling
    :func:`splitter.detect_silences`: that function serves a different job and
    must not change. It finds *cut points* across the whole file, and the chunk
    plan's determinism depends on its exact behaviour — including its heuristic
    of ending an unterminated trailing silence at ``start + min_silence_s``.
    Here we need *speech duration inside a bounded chunk*, so a trailing silence
    is correctly terminated at the chunk's end. Same ffmpeg primitive, different
    question; conflating them would silently alter how classes get split.

    **This is a level gate, not voice activity.** It answers "for how long was
    this chunk louder than ``speech_floor_db``", which is a decent proxy for
    talking in a room that falls quiet between utterances and a bad one in a room
    that never does. It fails in both directions, and callers must handle both:
    it *under*-counts quiet speech (inflating any rate computed from it) and it
    *over*-counts continuous non-speech sound such as a whole class talking at
    once, which pins the result at the chunk duration and destroys the
    measurement entirely. :func:`assert_transcript_earned` guards each direction.

    Returns ``(None, None)`` for mean/speech that could not be measured. Callers
    must fail closed on ``None``, never treat it as "fine".
    """
    cp = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(path),
            "-af",
            f"volumedetect,silencedetect=noise={contract.speech_floor_db}dB:d={min_silence_s}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    err = cp.stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", err)
    mean_db = float(m.group(1)) if m else None

    dur = _probe_duration(path)
    if dur is None or dur <= 0:
        return mean_db, None

    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", err)]
    silence_s = 0.0
    for i, s in enumerate(starts):
        # An unterminated trailing silence runs to the end of the chunk.
        e = ends[i] if i < len(ends) else dur
        lo, hi = max(0.0, s), min(dur, e)
        if hi > lo:
            silence_s += hi - lo
    speech_s = max(0.0, dur - silence_s)
    return mean_db, speech_s


def word_count(text: str) -> int:
    return len(text.split())


def _loop_coverage(text: str, n: int = 4, min_runs: int = 3) -> float:
    """Fraction of tokens sitting inside a run of back-to-back identical n-grams.

    Deliberately measures *immediate* repetition — "ABCD ABCD ABCD" — and not
    how often a phrase recurs across the chunk. A lecturer who says the same
    short phrase several times in fifteen minutes is talking; a transcriber
    that emits it dozens of times in a row has stopped transcribing and started
    looping. Only the second is a defect, and only the second is contiguous.

    Token-normalised (lowercase, punctuation dropped) so capitalization
    variance doesn't split what should read as one gram.
    """
    toks = re.findall(r"[a-z0-9']+", text.lower())
    if len(toks) < n * min_runs:
        return 0.0
    covered = [False] * len(toks)
    i = 0
    while i <= len(toks) - n:
        gram = toks[i : i + n]
        runs, j = 1, i + n
        while j + n <= len(toks) and toks[j : j + n] == gram:
            runs += 1
            j += n
        if runs >= min_runs:
            for k in range(i, j):
                covered[k] = True
            i = j
        else:
            i += 1
    return sum(covered) / len(toks)


def assert_not_degenerate(
    report: VerifyReport,
    chunk: ChunkSpec,
    text: str,
    contract: Contract,
    *,
    speech_measure_saturated: bool = False,
) -> float:
    """Catch the failure the rate bounds were quietly being asked to cover.

    Whisper, handed audio it cannot resolve, does not go quiet — it loops. A
    looping chunk is slow: hundreds of words in fifteen minutes passes every
    rate bound there is. So the defect kept landing on the speaking-rate
    checks, which diagnosed it as "content likely dropped" — close enough to be
    actionable, wrong enough to mislead. Content was not dropped; it was
    replaced with fabricated repetition. This measures that directly instead of
    inferring it from a rate.

    **Whether a loop is fatal depends on what was underneath it**, which is the
    same discipline the rest of this module runs on — assert only what the
    measurement supports:

    * **Speech measure saturated** (continuous room sound, nobody individually
      intelligible): advisory. A room of many people talking at once contains no
      single-speaker content to transcribe, so a loop over it destroys nothing.
    * **Speech measure clean**: a failure. Real, measurable speech was present and
      the transcriber returned repetition instead, which means that stretch of the
      class is genuinely gone.

    Returns the measured coverage so callers can report it.
    """
    coverage = _loop_coverage(text, contract.loop_ngram, contract.min_loop_runs)
    tag = f"chunk[{chunk.index}]"
    pct = coverage * 100.0
    measurement = (
        f"{pct:.1f}% of tokens inside back-to-back repeated "
        f"{contract.loop_ngram}-grams (limit {contract.max_loop_fraction * 100:.0f}%)"
    )

    if coverage < contract.max_loop_fraction:
        report.add(f"{tag}.no_degenerate_output", True, measurement)
    elif speech_measure_saturated:
        report.add(
            f"{tag}.no_degenerate_output",
            True,
            f"{ADVISORY_PREFIX}{measurement} -- the transcriber looped, but this "
            f"chunk's speech measure saturated, so the audio underneath is "
            f"continuous room sound with no individually intelligible speaker. "
            f"There was nothing here to lose, so this is recorded rather than "
            f"treated as a defect",
        )
    else:
        report.add(
            f"{tag}.no_degenerate_output",
            False,
            measurement,
            fail_hint="the transcriber looped over audio whose speech measure is "
            "clean, so real speech was present and this stretch of the "
            "class was replaced with fabricated repetition",
        )
    return coverage


def assert_audio_valid(report: VerifyReport, chunk: ChunkSpec, contract: Contract) -> None:
    p = chunk.path
    exists = Path(p).exists() and Path(p).stat().st_size > 0
    report.add(
        f"chunk[{chunk.index}].exists_nonempty", exists, "" if exists else f"missing/empty: {p}"
    )
    if not exists:
        return
    sr = _ffprobe_field(p, "stream=sample_rate")
    report.add(
        f"chunk[{chunk.index}].sample_rate",
        sr == str(contract.sample_rate),
        f"got {sr}, want {contract.sample_rate}",
    )
    ch = _ffprobe_field(p, "stream=channels")
    report.add(
        f"chunk[{chunk.index}].channels",
        ch == str(contract.channels),
        f"got {ch}, want {contract.channels}",
    )
    dur = _probe_duration(p)
    report.add(
        f"chunk[{chunk.index}].decodes",
        dur is not None and dur > 0,
        "did not decode" if not dur else "",
    )


def assert_transcript_earned(
    report: VerifyReport,
    chunk: ChunkSpec,
    text: str,
    contract: Contract,
    levels: tuple[float | None, float | None] | None = None,
) -> str:
    """The critical silent-failure guard: a chunk that *had audio* must produce
    non-empty text, and at a plausible speaking rate.

    Returns which speaking-rate guard actually ran (one of the ``RATE_GUARD_*``
    constants), so the caller can verify that a chunk the absolute floor declined
    to judge was still covered by something — see
    :func:`assert_rate_guard_coverage`.

    The rate assertion is deliberately asymmetric, because the two bounds fail
    for opposite reasons and each is measured over the denominator that cannot
    produce a false accusation:

    * **Ceiling** (garbled/looping output) over **wall clock** — nobody can say
      more words than the chunk's span allows, however the speech is distributed.
    * **Floor** (dropped content) over **detected speech time** — "words per
      minute" is a property of speaking, not of elapsed time. A workshop chunk
      where the class works quietly for most of the chunk is *not* a
      transcription failure, and a wall-clock floor would call it one.

    The floor abstains in three situations, and it is worth being precise about
    why, because each was learned from a real production incident in the source
    repo:

    * **Not enough speech to judge** (below ``min_speech_s_for_rate``): the rate
      is dominated by measurement granularity, so asserting on it would be noise
      rather than verification.
    * **The speech measure saturated** (at or above
      ``max_speech_fraction_for_floor``): ``silencedetect`` found no silence at
      all, so "speech time" is not a measurement — it is the chunk duration
      wearing a different name, and dividing by it reproduces the wall-clock
      floor this design deliberately removed. A room of simultaneous
      conversation never dips below ``speech_floor_db``, so a level gate reports
      the full chunk duration as "speech" while yielding few intelligible
      single-speaker words. Continuing to assert there would not be strictness,
      it would be a coin flip dressed as a contract.
    * **Speech time unmeasurable** is the one case that does *not* abstain: we
      fall back to the wall-clock floor rather than wave a chunk through with no
      check at all.

    Abstention is only honest because it hands off. The absolute floor rests on a
    level gate that is a weak proxy for voice activity; the cross-chunk yield
    relation in :func:`assert_speech_yield_consistent` compares siblings measured
    by the *same* proxy and so survives its miscalibration. That relation is the
    operative guard whenever this one steps back, and
    :func:`assert_rate_guard_coverage` fails the run if it was not available.
    """
    wc = word_count(text)
    mean_db, speech_s = levels if levels is not None else _chunk_levels(chunk.path, contract)
    has_audio = mean_db is not None and mean_db > contract.silence_floor_db
    tag = f"chunk[{chunk.index}]"

    if not has_audio:
        # Silence: empty is acceptable, non-empty is fine too (don't penalize).
        report.add(f"{tag}.silence_ok", True, f"silence ({mean_db} dB), empty transcript allowed")
        return RATE_GUARD_SILENCE

    report.add(
        f"{tag}.transcript_nonempty",
        wc > 0,
        f"{wc} words, audio at {mean_db:.1f} dB",
        fail_hint="audio was present but the transcript is empty",
    )
    if wc == 0 or chunk.duration_s <= 0:
        return RATE_GUARD_NO_TEXT

    wpm_wall = wc / (chunk.duration_s / 60.0)
    report.add(
        f"{tag}.wpm_under_ceiling",
        wpm_wall <= contract.max_wpm,
        f"{wpm_wall:.0f} wpm over {chunk.duration_s:.0f}s wall clock",
        fail_hint=f"exceeds {contract.max_wpm:.0f} wpm; looping or garbled output",
    )

    if speech_s is None:
        # Fail closed: if we cannot measure speech time we fall back to the old
        # wall-clock floor rather than waving the chunk through unchecked.
        report.add(
            f"{tag}.wpm_above_floor",
            wpm_wall >= contract.min_wpm_if_speech,
            f"speech duration unmeasurable; fell back to wall clock: "
            f"{wpm_wall:.0f} wpm over {chunk.duration_s:.0f}s",
            fail_hint=f"below {contract.min_wpm_if_speech:.0f} wpm",
        )
        return RATE_GUARD_WALL_CLOCK_FALLBACK

    if speech_s >= contract.min_speech_s_for_rate:
        speech_fraction = speech_s / chunk.duration_s
        if speech_fraction >= contract.max_speech_fraction_for_floor:
            # The denominator saturated: no silence was detected anywhere in the
            # chunk, so speech time *is* wall clock and the floor has nothing of
            # its own to divide by. Abstain and say so, rather than re-run the
            # wall-clock floor under a name that implies it measured speech.
            report.add(
                f"{tag}.speech_measure_saturated",
                True,
                f"{ADVISORY_PREFIX}no silence detected at "
                f"{contract.speech_floor_db:.0f} dBFS anywhere in a "
                f"{chunk.duration_s:.0f}s chunk, so detected speech "
                f"({speech_s:.0f}s) equals wall clock "
                f"(ratio {speech_fraction:.3f} >= "
                f"{contract.max_speech_fraction_for_floor:.2f}) -- continuous "
                f"room sound such as simultaneous conversation reads this way. "
                f"The speech-time denominator is not a measurement here, so the "
                f"speaking-rate floor abstains ({wc} words, "
                f"{wpm_wall:.0f} wpm over wall clock); the cross-chunk yield "
                f"relation is the operative guard for this chunk",
            )
            return RATE_GUARD_SATURATED

        wpm_speech = wc / (speech_s / 60.0)
        report.add(
            f"{tag}.wpm_above_floor",
            wpm_speech >= contract.min_wpm_if_speech,
            f"{wpm_speech:.0f} wpm across {speech_s:.0f}s of detected speech "
            f"({wc} words in a {chunk.duration_s:.0f}s chunk)",
            fail_hint=f"below {contract.min_wpm_if_speech:.0f} wpm; content likely dropped",
        )
        if wpm_speech > contract.implausible_speech_wpm:
            # Nobody talks this fast, so the denominator -- not the transcript --
            # is what's wrong: level-thresholded silence detection missed quiet
            # speech. Say so, rather than banking a pass this check didn't earn.
            # Deliberately non-fatal: the transcript may be perfectly fine. The
            # cross-chunk yield relation is what still has teeth here.
            report.add(
                f"{tag}.speech_measure_unreliable",
                True,
                f"{ADVISORY_PREFIX}{wpm_speech:.0f} wpm exceeds "
                f"{contract.implausible_speech_wpm:.0f} -- only {speech_s:.0f}s "
                f"of speech detected in a {chunk.duration_s:.0f}s chunk yielding "
                f"{wc} words, so the speech floor is weak for this chunk",
            )
        return RATE_GUARD_FLOOR_APPLIED

    report.add(
        f"{tag}.sparse_speech_ok",
        True,
        f"only {speech_s:.0f}s of speech in a {chunk.duration_s:.0f}s chunk "
        f"({wc} words) -- speaking-rate floor not applicable",
    )
    return RATE_GUARD_SPARSE


def _yield_cohort(
    report: VerifyReport,
    check_name: str,
    ratios: dict,
    contract: Contract,
    what: str,
) -> set:
    """Run the outlier relation over one cohort of comparable chunks.

    Returns the set of chunk indices actually judged (empty when the cohort was
    too small to establish a median).
    """
    if len(ratios) < contract.min_chunks_for_yield_relation:
        report.add(
            check_name,
            True,
            f"only {len(ratios)} chunk(s) carry enough speech to compare "
            f"(need {contract.min_chunks_for_yield_relation}) -- relation "
            f"not applicable",
        )
        return set()

    vals = sorted(ratios.values())
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    threshold = contract.min_yield_fraction_of_median * median
    low = {i: r for i, r in ratios.items() if r < threshold}

    report.add(
        check_name,
        not low,
        f"median {median:.2f} words per second of speech across {n} chunks "
        f"({what}; outlier threshold {threshold:.2f})",
        fail_hint="chunk(s) far below their siblings: "
        + ", ".join(f"chunk[{i}] at {r:.2f}" for i, r in sorted(low.items()))
        + " -- content likely dropped",
    )
    return set(ratios)


def assert_speech_yield_consistent(
    report: VerifyReport,
    chunks: Sequence[ChunkSpec],
    chunk_texts: Sequence[str],
    levels: dict,
    contract: Contract,
) -> set:
    """Metamorphic relation: chunks of one recording must yield words at a
    broadly consistent rate per second of detected speech.

    Returns the set of chunk indices this relation actually judged, so the caller
    can tell which chunks the absolute floor declined to judge were nonetheless
    covered here — and which ended up covered by nothing.

    This exists because the absolute speech floor is only as trustworthy as the
    speech measurement under it, and level-thresholded silence detection is a
    weak proxy — it undercounts quiet speech, which inflates words-per-minute
    and quietly blunts the floor, and it overcounts continuous room sound, which
    pins the denominator at the chunk duration. A *relative* comparison
    sidesteps both: whatever bias the detector has, it applies to every chunk of
    the same recording, so a chunk that yields dramatically fewer words per
    second of speech than its siblings is anomalous regardless of how the
    detector is calibrated. Same spirit as the rest of the metamorphic layer --
    assert a relation, not an absolute, when the absolute has no single right
    value.

    **Cohorts.** That argument has a precondition which is easy to miss: the bias
    must be the *same* for every chunk being compared. It is not, once some
    chunks saturate. A saturated chunk's denominator is its entire duration while
    a normally-measured sibling's is some fraction of it, so the saturated chunk's
    words-per-second-of-speech is systematically depressed — by the measurement,
    not by anything the transcriber did. Pooling them makes a crosstalk chunk look
    starved next to a lecture chunk and hands us back the same false accusation
    the floor was just taught not to make, wearing a relative disguise.

    So the comparison runs within cohorts of chunks whose denominators mean the
    same thing: normally-measured chunks against each other, saturated chunks
    against each other. Each cohort needs its own quorum. This gives up one
    genuinely invalid comparison and keeps every valid one.

    Only chunks with enough speech to judge take part at all.
    """
    measured: dict = {}
    saturated: dict = {}
    for ch, txt in zip(sorted(chunks, key=lambda c: c.index), chunk_texts):
        _, speech_s = levels.get(ch.index, (None, None))
        if not speech_s or speech_s < contract.min_speech_s_for_rate:
            continue
        ratio = word_count(txt) / speech_s
        if ch.duration_s > 0 and speech_s / ch.duration_s >= contract.max_speech_fraction_for_floor:
            saturated[ch.index] = ratio
        else:
            measured[ch.index] = ratio

    judged = _yield_cohort(
        report,
        "speech_yield.consistent",
        measured,
        contract,
        "speech time measured",
    )
    if saturated:
        # Only emit the second row when there is actually a saturated cohort, so
        # an ordinary class's report doesn't grow a row about a situation it never
        # encountered.
        judged |= _yield_cohort(
            report,
            "speech_yield.consistent_saturated",
            saturated,
            contract,
            "speech measure saturated, so this is words per second of wall clock",
        )
    return judged


def assert_rate_guard_coverage(
    report: VerifyReport,
    saturated_indices: Sequence[int],
    judged_by_relation: set | Sequence[int],
    floor_applied_indices: Sequence[int],
    contract: Contract,
) -> None:
    """Say plainly which chunks nobody checked the speaking rate of.

    The absolute floor abstains when its denominator saturates, and the relative
    relation only judges a chunk when its cohort had a quorum. Between them a
    chunk can fall through and receive no speaking-rate check at all. That fact
    must appear in the report — an unproven chunk that reads as clean is exactly
    the lie this module exists to prevent.

    What it deliberately does *not* do is fail the class for it. Manufacturing a
    verdict the evidence does not support was tried and was wrong, for the same
    reason the floor itself was wrong more than once: one noisy stretch in an
    otherwise sound recording is not proof that anything was dropped, and turning
    "we could not measure this" into "this is broken" costs real material to buy
    nothing. A disclosed unknown is strictly more useful than an invented
    failure, and the chunk is still covered by every other check — audio present
    implies non-empty text, the wall-clock ceiling, coverage, duration
    preservation, lossless reassembly.

    The one condition that *is* a failure: no chunk in the whole recording
    received any speaking-rate check whatsoever. Then the rate contract got no
    purchase on this class at all, and reporting it as verified would overstate
    what was proven rather than merely leave a gap in it.
    """
    if not saturated_indices:
        report.add(
            "rate_guard.coverage", True, "speaking-rate floor applied wherever it was applicable"
        )
        return

    judged = set(judged_by_relation)
    listed = ", ".join(f"chunk[{i}]" for i in sorted(saturated_indices))
    unproven = sorted(set(saturated_indices) - judged)

    if not unproven:
        report.add(
            "rate_guard.coverage",
            True,
            f"{ADVISORY_PREFIX}speaking-rate floor abstained on {listed} "
            f"(saturated speech measure); every one of them was judged "
            f"instead by the cross-chunk yield relation",
        )
        return

    unproven_listed = ", ".join(f"chunk[{i}]" for i in unproven)
    nothing_checked = not judged and not list(floor_applied_indices)
    report.add(
        "rate_guard.coverage",
        not nothing_checked,
        f"{'' if nothing_checked else ADVISORY_PREFIX}"
        f"speaking-rate floor abstained on {listed} (saturated speech measure) "
        f"and the cross-chunk relation had no quorum for {unproven_listed}, so "
        f"no speaking-rate check covered {'it' if len(unproven) == 1 else 'them'} "
        f"-- the rest of the contract still applies there",
        fail_hint="and no chunk anywhere in this recording received a "
        "speaking-rate check, so the rate contract proved nothing at all",
    )


def assert_coverage(
    report: VerifyReport, chunks: Sequence[ChunkSpec], total_s: float, contract: Contract
) -> None:
    """Chunk spans must tile [0, total_s] with no gap and no overlap."""
    ordered = sorted(chunks, key=lambda c: c.index)
    ok_start = abs(ordered[0].start_s) <= contract.coverage_tolerance_s
    report.add("coverage.starts_at_zero", ok_start, f"first start={ordered[0].start_s}")
    ok_end = abs(ordered[-1].end_s - total_s) <= contract.coverage_tolerance_s
    report.add(
        "coverage.ends_at_total", ok_end, f"last end={ordered[-1].end_s}, total={total_s:.3f}"
    )
    contiguous = True
    detail = ""
    for a, b in zip(ordered, ordered[1:]):
        gap = b.start_s - a.end_s
        if abs(gap) > contract.coverage_tolerance_s:
            contiguous = False
            detail = f"gap/overlap of {gap:.3f}s between chunk {a.index} and {b.index}"
            break
    report.add("coverage.contiguous", contiguous, detail)


def assert_reassembly_lossless(
    report: VerifyReport, chunk_texts: Sequence[str], assembled_body: str
) -> None:
    """No words may be dropped or duplicated when concatenating chunks."""
    expected = sum(word_count(t) for t in chunk_texts)
    got = word_count(assembled_body)
    report.add(
        "reassembly.word_count_conserved",
        expected == got,
        f"sum(chunks)={expected} but assembled body={got}",
    )


def assert_headers_monotonic(report: VerifyReport, assembled: str, n_chunks: int) -> None:
    stamps = re.findall(r"^##\s*\[(\d{2}):(\d{2}):(\d{2})\]", assembled, re.MULTILINE)
    report.add(
        "headers.count",
        len(stamps) == n_chunks,
        f"found {len(stamps)} headers, expected {n_chunks}",
    )
    secs = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in stamps]
    report.add("headers.monotonic", secs == sorted(secs), f"non-monotonic: {secs}")


# --------------------------------------------------------------------------- #
# Per-execution verification entrypoint
# --------------------------------------------------------------------------- #


def verify_run(
    *,
    input_path: str,
    chunks: Sequence[ChunkSpec],
    chunk_texts: Sequence[str],
    assembled: str,
    total_s: float,
    contract: Contract | None = None,
) -> VerifyReport:
    """Run the full per-execution contract. Returns a report; caller decides
    whether to ``raise_if_failed()``. The pipeline always raises."""
    contract = contract or Contract()
    report = VerifyReport()

    if not chunks:
        report.add("plan.nonempty", False, "no chunks produced")
        return report
    report.add("plan.nonempty", True)

    # Measure each chunk once; both the per-chunk rate checks and the
    # cross-chunk yield relation read from the same measurement.
    ordered = sorted(chunks, key=lambda c: c.index)
    levels = {ch.index: _chunk_levels(ch.path, contract) for ch in ordered}

    saturated: list[int] = []
    floor_applied: list[int] = []
    for ch, txt in zip(ordered, chunk_texts):
        assert_audio_valid(report, ch, contract)
        outcome = assert_transcript_earned(report, ch, txt, contract, levels=levels[ch.index])
        if outcome == RATE_GUARD_SATURATED:
            saturated.append(ch.index)
        elif outcome in (RATE_GUARD_FLOOR_APPLIED, RATE_GUARD_WALL_CLOCK_FALLBACK):
            floor_applied.append(ch.index)
        # Looping is a different disease from a bad rate and needs its own
        # diagnosis; whether it is fatal depends on what the audio underneath
        # actually was, which the rate guard has just told us.
        assert_not_degenerate(
            report,
            ch,
            txt,
            contract,
            speech_measure_saturated=(outcome == RATE_GUARD_SATURATED),
        )

    judged = assert_speech_yield_consistent(report, chunks, chunk_texts, levels, contract)
    # Whatever the absolute floor declined to judge, say whether anything else
    # did. A gap is disclosed, not invented into a failure -- see the docstring.
    assert_rate_guard_coverage(report, saturated, judged, floor_applied, contract)

    assert_coverage(report, chunks, total_s, contract)

    # Sum of chunk durations should equal the whole file (no time lost).
    summed = sum(c.duration_s for c in chunks)
    report.add(
        "duration.preserved",
        abs(summed - total_s) <= contract.duration_tolerance_s,
        f"sum(chunks)={summed:.3f}s vs whole={total_s:.3f}s",
    )

    # Reassembly integrity: count only transcript body words, excluding the
    # structural scaffolding -- markdown headers (^#...) and bold-metadata lines
    # like "**Source:** file.m4a" -- which are added by reassemble(), not chunks.
    body = re.sub(r"^\s*#.*$", "", assembled, flags=re.MULTILINE)
    body = re.sub(r"^\s*\*\*.*\*\*.*$", "", body, flags=re.MULTILINE)
    assert_reassembly_lossless(report, chunk_texts, body)
    assert_headers_monotonic(report, assembled, len(chunks))
    return report


# --------------------------------------------------------------------------- #
# Metamorphic relations (invariants, not exact outputs)
# --------------------------------------------------------------------------- #


def metamorphic_split_deterministic(
    total_s: float, chunk_s: float, silences, snap_window_s: float = 10.0
) -> bool:
    """Same input + params -> identical plan. Determinism is a contract."""
    a = plan_boundaries(total_s, chunk_s, silences, snap_window_s)
    b = plan_boundaries(total_s, chunk_s, list(silences), snap_window_s)
    return a == b


def metamorphic_whole_vs_chunked(
    whole_words: int, chunked_words: int, contract: Contract | None = None
) -> Check:
    """Chunked transcription must stay within tolerance of a whole-file run.
    Certify-time only — it needs an expensive whole-file transcription."""
    contract = contract or Contract()
    if whole_words == 0:
        return Check("metamorphic.whole_vs_chunked", False, "whole-file word count is 0")
    drift = abs(whole_words - chunked_words) / max(whole_words, chunked_words)
    ok = drift <= contract.whole_vs_chunked_tolerance
    return Check(
        "metamorphic.whole_vs_chunked",
        ok,
        f"drift {drift * 100:.2f}% (tol {contract.whole_vs_chunked_tolerance * 100:.0f}%)",
    )
