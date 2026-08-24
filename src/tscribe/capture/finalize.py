"""tscribe.capture.finalize — glue segments into one WAV and prove the joined
file.

Ported from tscribe-capture-rig's `bin/finalize.sh` / `bin/finalize.bat`:
lossless concat (`-c copy`, no re-encode) of every `seg_*.wav` in a session
directory, followed by the same `verify_capture` contract used per-segment,
run once more in `final=True` mode against the concatenated file. The
duration-preservation check (finalized file's duration must be within
`cfg.duration_tolerance_final_s`, default +/-5s, of the *measured* sum of the
input segments' durations — not a value blindly trusted from elsewhere) is
the load-bearing proof that the concat didn't silently drop or duplicate
audio.
"""

from __future__ import annotations

import glob
import os
import subprocess
import tempfile
from pathlib import Path

from tscribe.config import CaptureConfig

from .verify import VerificationReport, _resolve_bin, verify_capture


class FinalizeError(Exception):
    """Raised when finalize cannot even attempt to produce a joined file
    (no segments found, ffmpeg concat itself failed to run) — distinct from
    a *verified* failure, which is reported via the returned/attached
    VerificationReport instead of an exception. Mirrors CaptureError's
    "specific, actionable message" standard."""


def _discover_segments(session_dir: str) -> list[str]:
    """Return every `seg_*.wav` in `session_dir`, sorted so that lexical
    order is chronological order.

    This relies on the zero-padded `%03d` segment index `capture_start`
    produces (`seg_<stamp>_000.wav`, `_001.wav`, ...) — identical assumption
    to both source scripts' `sort`/`dir /b /o-n` handling. A session
    directory holding segments from more than one capture run (different
    `<stamp>` prefixes) still sorts correctly overall because the timestamp
    prefix itself sorts chronologically first; segments are then contiguous
    within each run's shared prefix.
    """
    pattern = str(Path(session_dir) / "seg_*.wav")
    return sorted(glob.glob(pattern))


def _probe_duration_s(ffprobe_bin: str, path: str) -> float:
    proc = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise FinalizeError(f"ffprobe failed to read duration of {path}: {proc.stderr.strip()}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise FinalizeError(f"ffprobe returned a non-numeric duration for {path}") from exc


def capture_finalize(
    session_dir: str,
    *,
    out_file: str | None = None,
    cfg: CaptureConfig | None = None,
) -> str:
    """Concatenate every segment in `session_dir` into one lossless WAV,
    verify the result, and return the path to that file.

    Matches the interface contract `capture_finalize(session_dir: str) ->
    str`. `out_file` defaults to `<session_dir>/class_full.wav` (the source
    rig's exact default filename). `cfg` defaults to `CaptureConfig()` when
    omitted, matching the pattern the rest of Unit 01's config-consuming
    modules use for a bare/no-config call.

    Raises FinalizeError if no segments are found or the concat itself fails
    to run (matching the source scripts' own hard failures: "no segments in
    $OUT_DIR", a nonzero ffmpeg exit). If the concat *runs* but the result
    fails verification (wrong shape, dead silence, duration drifted more
    than the tolerance), this does NOT raise — it returns the path anyway,
    exactly as the source `finalize.sh` did (it always produces
    `class_full.wav` and then reports the verifier's verdict via exit code,
    rather than deleting the evidence on failure). Call
    `tscribe.capture.verify.verify_capture(path, expected_seconds, final=True)`
    on the returned path — or read `capture_finalize_report` below — to get
    the actual pass/fail verdict; retaining the unverified file rather than
    silently discarding it is the same evidence-retention principle
    `tscribe.core.class_pipeline` uses for a failed transcription contract.
    """
    cfg = cfg or CaptureConfig()
    segments = _discover_segments(session_dir)
    if not segments:
        raise FinalizeError(f"no segments (seg_*.wav) found in {session_dir}")

    ffmpeg_bin = _resolve_bin("ffmpeg", cfg.ffmpeg_bin)
    ffprobe_bin = _resolve_bin("ffprobe", cfg.ffprobe_bin)
    if ffmpeg_bin is None or ffprobe_bin is None:
        raise FinalizeError(
            "could not find ffmpeg/ffprobe (checked cfg.ffmpeg_bin/ffprobe_bin, "
            "FFMPEG/FFPROBE env vars, PATH)."
        )

    # Sum of segment durations, measured (not assumed) -- this is the
    # denominator the duration-preservation check proves the concat against.
    segment_duration_sum = sum(_probe_duration_s(ffprobe_bin, s) for s in segments)

    resolved_out_file = out_file or str(Path(session_dir) / "class_full.wav")

    # ffmpeg concat demuxer needs a newline-delimited list file of
    # "file '<absolute path>'" lines -- identical mechanism to both source
    # scripts (a temp file for finalize.sh, %TEMP%\rig_concat_%RANDOM%.txt
    # for finalize.bat).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as list_file:
        for seg in segments:
            abs_path = str(Path(seg).resolve())
            escaped = abs_path.replace("'", "'\\''")
            list_file.write(f"file '{escaped}'\n")
        list_path = list_file.name

    try:
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
                "-y",
                resolved_out_file,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise FinalizeError(f"ffmpeg concat failed: {proc.stderr.strip()}")
    finally:
        os.unlink(list_path)

    # Final verify, in `final=True` mode: this widens the duration tolerance
    # to cfg.duration_tolerance_final_s (default +/-5s) instead of the
    # tighter per-segment cfg.duration_tolerance_segment_s, and checks the
    # concatenated file's duration against segment_duration_sum -- the
    # measured sum, not an assumed one. A failing report is still returned
    # (attached below), never discarded.
    report = verify_capture(resolved_out_file, segment_duration_sum, final=True, cfg=cfg)
    _LAST_FINALIZE_REPORTS[resolved_out_file] = report

    return resolved_out_file


# Small side-channel so a caller that only has the interface-contract
# signature (`capture_finalize(session_dir) -> str`) can still retrieve the
# verification report finalize just produced, without re-running ffprobe on
# every segment a second time. Keyed by output path; last-write-wins is fine
# since finalize is a one-shot, end-of-session operation. Prefer calling
# `capture_finalize_report` immediately after `capture_finalize` in new code.
_LAST_FINALIZE_REPORTS: dict[str, VerificationReport] = {}


def capture_finalize_report(out_file: str) -> VerificationReport | None:
    """The VerificationReport `capture_finalize` produced for `out_file`, if
    any. Returns None if `out_file` was never finalized in this process."""
    return _LAST_FINALIZE_REPORTS.get(out_file)
