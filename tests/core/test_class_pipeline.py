"""Proof that the class pipeline's contract actually catches the bugs it claims to.

Ported from tscribe-transcription-tool's tests/test_class_pipeline.py. We
synthesize a recording with a known speech/silence structure (tones =
"speech", gaps = silence), drive it through the pipeline with a mock
transcriber, and assert: (1) a good run passes the contract, and (2) each
failure mode -- dropped/empty chunk, coverage gap, word drop in reassembly --
is caught.

Adaptation from the source: transcribe_class() now takes a RuntimeConfig
(Unit 01) instead of individual chunk_minutes/snap_window_s/workers kwargs;
the `_cfg()` helper below builds one. The algorithm under test is unchanged.

Run: pytest -q  (ffmpeg + ffprobe must be on PATH)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tscribe.config import Backend, ChunkingConfig, Mode, RuntimeConfig
from tscribe.core.class_pipeline import (
    ContractViolation,
    transcribe_class,
)
from tscribe.core.class_pipeline.splitter import (
    plan_boundaries,
    probe_duration,
    split_audio,
)
from tscribe.core.class_pipeline.verify import (
    metamorphic_split_deterministic,
    metamorphic_whole_vs_chunked,
    verify_run,
)


def _cfg(chunk_minutes: float, snap_window_s: float, workers: int = 2) -> RuntimeConfig:
    return RuntimeConfig(
        mode=Mode.CLIENT,
        backend=Backend.LOCAL,
        chunking=ChunkingConfig(
            minutes=chunk_minutes, snap_window_s=snap_window_s, workers=workers
        ),
    )


# Deterministic non-repetitive filler. " ".join(["lorem"] * n) is 100%
# back-to-back repetition -- indistinguishable from a transcriber loop, and
# the degeneracy check correctly refuses it. Synthetic text standing in for
# speech has to look like speech in the ways the contract measures.
_VOCAB = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
    "lima mike november oscar papa quebec romeo sierra tango uniform "
    "victor whiskey xray yankee zulu"
).split()


def _words(n: int, seed: int = 1234) -> str:
    import random

    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCAB) for _ in range(n))


def _make_recording(path: Path) -> None:
    """~30s: tone(0-8) sil(8-9) tone(9-18) sil(18-19) tone(19-30).

    Concatenated with ffmpeg so silencedetect finds real pauses at ~8.5 / ~18.5.
    """
    segs = [
        ("sine=frequency=220:duration=8", path.parent / "a.wav"),
        ("anullsrc=r=16000:cl=mono:duration=1", path.parent / "b.wav"),
        ("sine=frequency=330:duration=9", path.parent / "c.wav"),
        ("anullsrc=r=16000:cl=mono:duration=1", path.parent / "d.wav"),
        ("sine=frequency=440:duration=11", path.parent / "e.wav"),
    ]
    for f, out in segs:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f,
                "-ac",
                "1",
                "-ar",
                "16000",
                "-sample_fmt",
                "s16",
                str(out),
            ],
            check=True,
        )
    listfile = path.parent / "list.txt"
    listfile.write_text("".join(f"file '{out}'\n" for _, out in segs))
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
            str(path),
        ],
        check=True,
    )


class MockTranscriber:
    """Deterministic stand-in for a real backend: words proportional to chunk
    duration, so word-per-minute lands in a plausible band. Optionally drops
    one chunk."""

    def __init__(self, wpm: float = 120.0, drop_index: int | None = None):
        self.wpm = wpm
        self.drop_index = drop_index
        self._seen = 0

    def transcribe(self, audio_path: str):
        # Silence chunks legitimately transcribe to empty.
        from tscribe.core.class_pipeline.verify import _mean_volume_db

        mean_db = _mean_volume_db(audio_path)
        idx = self._seen
        self._seen += 1
        if self.drop_index is not None and idx == self.drop_index:
            return "", None
        if mean_db is None or mean_db <= -45.0:
            return "", None
        dur = probe_duration(audio_path)
        n = max(1, int(self.wpm * dur / 60.0))
        return _words(n), None


@pytest.fixture
def recording(tmp_path):
    p = tmp_path / "class.wav"
    _make_recording(p)
    return p


def test_good_run_passes_contract(recording, tmp_path):
    out = tmp_path / "out.md"
    result = transcribe_class(
        str(recording),
        str(out),
        _cfg(10 / 60.0, 3.0, workers=2),
        transcriber=MockTranscriber(),
    )
    assert result.report.ok, result.report.failures
    assert out.exists()
    assert "## [00:00:00]" in result.transcript
    # Cuts should have snapped into the silences (~8.5s, ~18.5s), not 10/20.
    interior = [c.start_s for c in result.chunks[1:]]
    assert any(abs(s - 8.5) < 2.0 for s in interior)


def test_dropped_chunk_is_caught(recording, tmp_path):
    out = tmp_path / "out.md"
    with pytest.raises(ContractViolation) as exc:
        transcribe_class(
            str(recording),
            str(out),
            _cfg(10 / 60.0, 3.0, workers=1),
            transcriber=MockTranscriber(drop_index=0),  # first chunk has speech
        )
    assert "transcript_nonempty" in str(exc.value)
    assert not out.exists()  # nothing shipped


def test_coverage_gap_is_caught(recording, tmp_path):
    """Tamper the chunk plan so it no longer tiles the timeline; verify catches it."""
    chunk_dir = tmp_path / "chunks"
    chunks = split_audio(str(recording), chunk_dir, chunk_minutes=10 / 60.0, snap_window_s=3.0)
    total_s = probe_duration(str(recording))
    # Drop the last chunk -> coverage no longer reaches total_s.
    broken = chunks[:-1]
    texts = ["lorem lorem lorem"] * len(broken)
    report = verify_run(
        input_path=str(recording),
        chunks=broken,
        chunk_texts=texts,
        assembled="# t\n## [00:00:00]\nlorem lorem lorem",
        total_s=total_s,
    )
    assert not report.ok
    assert any("coverage.ends_at_total" in c.name for c in report.failures)


def test_reassembly_word_drop_is_caught(recording, tmp_path):
    chunk_dir = tmp_path / "chunks"
    chunks = split_audio(str(recording), chunk_dir, chunk_minutes=10 / 60.0, snap_window_s=3.0)
    total_s = probe_duration(str(recording))
    texts = ["alpha beta gamma" for _ in chunks]
    # Assembled body is missing words from the chunks -> must fail.
    bad_assembled = "# Class Transcript\n" + "".join(
        f"## [00:0{i}:00]\nalpha\n" for i in range(len(chunks))
    )
    report = verify_run(
        input_path=str(recording),
        chunks=chunks,
        chunk_texts=texts,
        assembled=bad_assembled,
        total_s=total_s,
    )
    assert not report.ok
    assert any("reassembly.word_count_conserved" in c.name for c in report.failures)


def test_split_plan_is_deterministic():
    silences = [(8.0, 9.0), (18.0, 19.0)]
    assert metamorphic_split_deterministic(30.0, 10.0, silences, 3.0)
    a = plan_boundaries(30.0, 10.0, silences, 3.0)
    b = plan_boundaries(30.0, 10.0, silences, 3.0)
    assert a == b


def test_metamorphic_whole_vs_chunked_tolerance():
    assert metamorphic_whole_vs_chunked(7699, 7705).ok  # the real numbers
    assert not metamorphic_whole_vs_chunked(7699, 5000).ok  # a real regression
