"""A failed contract must yield MORE information than a passing one, not less.

Ported from tscribe-transcription-tool's tests/test_failed_contract_keeps_evidence.py.
This is the regression proof for the 2026-08-06 incident referenced throughout
SPEC.md and this repo's Unit 02/03 docs: a check misfired, the report and
transcript were both discarded, and the only way to look at the evidence was
to re-run the whole job. So the contract keeps its teeth AND its receipts:

  * the exception still stops the pipeline (the gate is unchanged)
  * ``output_path`` is still not written (nothing downstream can mistake an
    unproven transcript for a proven one)
  * ``.report`` and ``.transcript`` ride along on the exception, so the failure
    can be read, argued with, and -- if the check was wrong -- recovered

Adaptation from the source: transcribe_class() now takes a RuntimeConfig
(Unit 01) instead of individual kwargs. The algorithm under test is unchanged.

Run: pytest -q  (ffmpeg + ffprobe must be on PATH)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tscribe.config import Backend, ChunkingConfig, Mode, RuntimeConfig
from tscribe.core.class_pipeline import ContractViolation, transcribe_class
from tscribe.core.class_pipeline.verify import Contract, VerifyReport


# Deterministic non-repetitive filler -- see test_class_pipeline.py for why.
_VOCAB = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
          "lima mike november oscar papa quebec romeo sierra tango uniform "
          "victor whiskey xray yankee zulu").split()


def _words(n: int, seed: int = 1234) -> str:
    import random
    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCAB) for _ in range(n))


def _make_recording(path: Path) -> None:
    """~30s: tone(0-8) sil(8-9) tone(9-18) sil(18-19) tone(19-30)."""
    segs = [
        ("sine=frequency=220:duration=8", path.parent / "a.wav"),
        ("anullsrc=r=16000:cl=mono:duration=1", path.parent / "b.wav"),
        ("sine=frequency=330:duration=9", path.parent / "c.wav"),
        ("anullsrc=r=16000:cl=mono:duration=1", path.parent / "d.wav"),
        ("sine=frequency=440:duration=11", path.parent / "e.wav"),
    ]
    for f, out in segs:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f,
             "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(out)],
            check=True,
        )
    listfile = path.parent / "list.txt"
    listfile.write_text("".join(f"file '{out}'\n" for _, out in segs))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", str(path)],
        check=True,
    )


class DroppingTranscriber:
    """Returns real text for every chunk except one, which comes back empty --
    the genuine silent failure the contract is built to catch."""

    def __init__(self, drop_index: int = 0, wpm: float = 120.0):
        self.drop_index = drop_index
        self.wpm = wpm
        self._seen = 0

    def transcribe(self, audio_path: str):
        from tscribe.core.class_pipeline.splitter import probe_duration
        idx = self._seen
        self._seen += 1
        if idx == self.drop_index:
            return "", None
        dur = probe_duration(audio_path)
        return _words(max(1, int(self.wpm * dur / 60.0))), None


@pytest.fixture
def recording(tmp_path):
    p = tmp_path / "class.wav"
    _make_recording(p)
    return p


def _violation(recording, tmp_path):
    out = tmp_path / "out.md"
    cfg = RuntimeConfig(
        mode=Mode.CLIENT, backend=Backend.LOCAL,
        chunking=ChunkingConfig(minutes=10 / 60.0, snap_window_s=3.0, workers=1),
    )
    with pytest.raises(ContractViolation) as exc:
        transcribe_class(
            str(recording), str(out), cfg,
            transcriber=DroppingTranscriber(drop_index=0),
        )
    return exc.value, out


# ------------------------------------------------------- the gate is unchanged

def test_the_gate_still_closes(recording, tmp_path):
    err, out = _violation(recording, tmp_path)
    assert "transcript_nonempty" in str(err)
    assert not out.exists(), "an unproven transcript must never reach output_path"


# ------------------------------------------------------ the evidence survives

def test_the_report_survives_the_failure(recording, tmp_path):
    err, _ = _violation(recording, tmp_path)
    assert isinstance(err.report, VerifyReport)
    assert not err.report.ok
    assert err.report.failures
    # The whole check list, not just the failures -- the passing rows are how a
    # reader tells "one chunk was dropped" from "the checker misfired".
    assert len(err.report.checks) > len(err.report.failures)
    assert any("transcript_nonempty" in c.name for c in err.report.failures)


def test_the_unverified_transcript_survives_the_failure(recording, tmp_path):
    err, _ = _violation(recording, tmp_path)
    assert err.transcript
    assert "# Class Transcript" in err.transcript
    assert any(w in err.transcript for w in _VOCAB)  # the chunks that did work
    assert "## [00:00:00]" in err.transcript  # still a readable, stamped document


def test_the_failure_message_still_names_every_violation(recording, tmp_path):
    err, _ = _violation(recording, tmp_path)
    for c in err.report.failures:
        assert c.name in str(err)


# ------------------------------- attaching evidence does not soften the verdict

def test_raise_if_failed_still_raises_with_a_transcript_attached():
    report = VerifyReport()
    report.add("some.check", False, "measurement", fail_hint="diagnosis")
    with pytest.raises(ContractViolation) as exc:
        report.raise_if_failed(transcript="a transcript we are not going to trust")
    assert exc.value.transcript == "a transcript we are not going to trust"
    assert exc.value.report is report
    assert not exc.value.report.ok


def test_raise_if_failed_is_a_no_op_on_a_clean_report():
    report = VerifyReport()
    report.add("some.check", True, "measurement")
    assert report.raise_if_failed(transcript="fine") is report


def test_a_violation_without_evidence_is_still_a_valid_exception():
    """Backwards compatible: older call sites raise with no payload and must not
    blow up on attribute access."""
    err = ContractViolation("something failed")
    assert err.report is None
    assert err.transcript is None
    assert "something failed" in str(err)
