"""Silence-aware audio splitting for the class pipeline.

Ported near-verbatim from tscribe-transcription-tool.class_pipeline.splitter.
Why this exists: a hard time-boundary cut slices straight through whatever
word is being spoken at the cut point. Whisper at a hard cut drops or garbles
the boundary word, and you can't see it happen — the word count still looks
fine. We fix it at the source: cut in the *pauses*. We find silences with
ffmpeg and snap each ideal cut to the nearest pause within a window, so chunk
seams land where nobody is talking. Reassembly is then losslessly just
concatenation — no overlap/dedupe needed.

ffmpeg/ffprobe only — no pydub, no full-file load into RAM (a 4-hour WAV is
~440 MB; we stream-cut it instead). Everything is deterministic given the
same input + parameters, which the contract checks.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

WAV_SR = 16000
WAV_CHANNELS = 1
WAV_SAMPLE_WIDTH = 2  # 16-bit PCM, matches the carnyx-native chunk format


@dataclass(frozen=True)
class ChunkSpec:
    """One chunk's place in the timeline. Times are seconds from file start."""

    index: int
    path: str
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(path: str | Path) -> float:
    """Return audio duration in seconds via ffprobe. Raises on undecodable input."""
    cp = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if cp.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {cp.stderr.strip()}")
    data = json.loads(cp.stdout or "{}")
    dur = float(data.get("format", {}).get("duration", 0.0))
    if dur <= 0:
        raise RuntimeError(f"ffprobe reported non-positive duration for {path}")
    return dur


def detect_silences(
    path: str | Path,
    noise_db: float = -30.0,
    min_silence_s: float = 0.35,
) -> List[Tuple[float, float]]:
    """Return [(start_s, end_s), ...] silence intervals via ffmpeg silencedetect."""
    cp = _run(
        [
            "ffmpeg",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
            "-f",
            "null",
            "-",
        ]
    )
    text = cp.stderr  # silencedetect writes to stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[\d.]+)", text)]
    intervals: List[Tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else s + min_silence_s
        if e > s:
            intervals.append((s, e))
    return intervals


def _nearest_silence_midpoint(
    ideal: float, silences: List[Tuple[float, float]], window_s: float
) -> float:
    """Snap ``ideal`` to the midpoint of the closest silence within ``window_s``.

    Falls back to the ideal time if no silence is close enough — a hard cut is
    the worst case, not the default.
    """
    best = None
    best_dist = window_s
    for s, e in silences:
        mid = (s + e) / 2.0
        dist = abs(mid - ideal)
        if dist <= best_dist:
            best_dist = dist
            best = mid
    return best if best is not None else ideal


def plan_boundaries(
    total_s: float,
    chunk_s: float,
    silences: List[Tuple[float, float]] | None = None,
    snap_window_s: float = 10.0,
    min_tail_s: float = 60.0,
) -> List[Tuple[float, float]]:
    """Deterministically plan non-overlapping (start, end) spans tiling [0, total_s].

    Interior cuts are snapped to the nearest silence within ``snap_window_s``.
    Pure function of its inputs — the contract asserts it is idempotent.

    A recording almost never divides evenly into ``chunk_s``, so the last span
    is a remainder that can be arbitrarily short. ``min_tail_s`` folds a
    too-short tail into the preceding chunk instead of emitting a sliver — a
    stub chunk is a menace to any per-chunk rate assertion, because rate
    bounds meaningful over fifteen minutes are absurd over five seconds.

    ``min_tail_s`` is clamped to half of ``chunk_s`` so that short-chunk
    callers (tests, fine-grained splits) keep their expected chunk counts.
    """
    if chunk_s <= 0:
        raise ValueError("chunk_s must be positive")
    silences = silences or []

    # Ideal interior cut points: chunk_s, 2*chunk_s, ...
    ideal_cuts = []
    t = chunk_s
    while t < total_s - 1e-6:
        ideal_cuts.append(t)
        t += chunk_s

    snapped = []
    for c in ideal_cuts:
        snapped.append(_nearest_silence_midpoint(c, silences, snap_window_s))

    # Keep cuts strictly increasing and inside (0, total).
    cuts = []
    prev = 0.0
    for c in snapped:
        c = min(max(c, prev + 1.0), total_s)  # never produce a sub-1s sliver
        if c > prev and c < total_s:
            cuts.append(c)
            prev = c

    bounds = [0.0] + cuts + [total_s]

    # Fold a too-short final remainder into the chunk before it.
    if len(bounds) > 2:
        effective_min_tail = min(min_tail_s, chunk_s / 2.0)
        if (bounds[-1] - bounds[-2]) < effective_min_tail:
            del bounds[-2]

    spans = [(round(bounds[i], 3), round(bounds[i + 1], 3)) for i in range(len(bounds) - 1)]
    return spans


def split_audio(
    input_path: str | Path,
    out_dir: str | Path,
    chunk_minutes: float = 15.0,
    snap_window_s: float = 10.0,
    silence_aware: bool = True,
    min_tail_s: float = 60.0,
) -> List[ChunkSpec]:
    """Split ``input_path`` into 16k/mono/16-bit WAV chunks, cutting in pauses.

    Returns the chunk plan as a list of :class:`ChunkSpec`. Uses ffmpeg stream
    copy of decoded PCM per span; the whole file is never loaded into memory.

    A final remainder shorter than ``min_tail_s`` is folded into the previous
    chunk rather than emitted as a sliver — see :func:`plan_boundaries`.
    """
    input_path = Path(input_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_s = probe_duration(input_path)
    chunk_s = float(chunk_minutes) * 60.0

    silences = detect_silences(input_path) if silence_aware else []
    spans = plan_boundaries(total_s, chunk_s, silences, snap_window_s, min_tail_s)

    chunks: List[ChunkSpec] = []
    for i, (start_s, end_s) in enumerate(spans):
        chunk_path = out_dir / f"chunk_{i:04d}.wav"
        cp = _run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(input_path),
                "-ss",
                f"{start_s:.3f}",
                "-to",
                f"{end_s:.3f}",
                "-ac",
                str(WAV_CHANNELS),
                "-ar",
                str(WAV_SR),
                "-sample_fmt",
                "s16",
                str(chunk_path),
            ]
        )
        if cp.returncode != 0:
            raise RuntimeError(f"ffmpeg split failed on chunk {i}: {cp.stderr.strip()}")
        chunks.append(ChunkSpec(index=i, path=str(chunk_path), start_s=start_s, end_s=end_s))
    return chunks
