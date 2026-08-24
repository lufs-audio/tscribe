"""Port of tscribe-capture-rig's tests/selftest.sh T4: capture.sh
segment->concat round-trip, driven through the REAL code path
(capture_start + capture_finalize) via the synthetic-source test seam --
never a mocked stand-in for either function.

T4 in the source rig drove the real capture.sh through RIG_INPUT_OVERRIDE (a
synthetic lavfi source) + CAPTURE_DURATION (a hard time cap so the capture
stops on its own), producing 3 real 2-second segments from a 6-second
synthetic recording, then verified each segment independently, then ran the
real finalize.sh and asserted the concatenated duration landed at ~6s.

This file reproduces that exact shape: capture_start(..., input_override=...,
duration_s=...) in place of RIG_INPUT_OVERRIDE/CAPTURE_DURATION,
capture_finalize(...) in place of finalize.sh, verify_capture(...) in place
of the standalone verifier CLI.
"""

from __future__ import annotations

import glob
import time

import pytest

from tscribe.capture import (
    capture_finalize,
    capture_finalize_report,
    capture_start,
    verify_capture,
)


def _wait_for_process_exit(session, timeout_s: float = 30.0) -> int:
    deadline = time.monotonic() + timeout_s
    while session.poll() is None:
        if time.monotonic() > deadline:
            session.stop()
            pytest.fail("capture session did not exit on its own within timeout_s")
        time.sleep(0.1)
    return session.poll()


# --------------------------------------------------------------------------
# T4: segment -> concat round-trip (real capture_start + capture_finalize,
# synthetic source)
# --------------------------------------------------------------------------


def test_capture_start_produces_expected_segment_count(capture_cfg):
    """First half of T4: a 6s synthetic recording with 2s segments must
    produce exactly 3 segment files -- proving the real ffmpeg `-f segment
    -segment_time <n> -reset_timestamps 1` invocation built by capture_start
    behaves identically to the source rig's capture.sh."""
    session = capture_start(
        capture_cfg,
        input_override="-f lavfi -i sine=frequency=300:sample_rate=16000",
        duration_s=6,
    )
    exit_code = _wait_for_process_exit(session)
    assert exit_code == 0, "ffmpeg segmenter did not exit cleanly"

    segments = sorted(glob.glob(f"{session.session_dir}/seg_*.wav"))
    assert len(segments) == 3, f"expected 3 segments, got {len(segments)}: {segments}"


def test_every_segment_verifies_independently(capture_cfg):
    """Second half of T4: every produced segment must independently verify
    (2s each, real level, correct shape) -- matching selftest.sh's
    "all segments verify individually" assertion."""
    session = capture_start(
        capture_cfg,
        input_override="-f lavfi -i sine=frequency=300:sample_rate=16000",
        duration_s=6,
    )
    _wait_for_process_exit(session)

    segments = sorted(glob.glob(f"{session.session_dir}/seg_*.wav"))
    assert len(segments) == 3

    for seg in segments:
        report = verify_capture(seg, capture_cfg.segment_seconds, cfg=capture_cfg)
        assert report["ok"] is True, f"{seg} failed verification: {report['checks']}"


def test_finalize_concatenates_and_preserves_duration(capture_cfg):
    """Third part of T4: capture_finalize must concatenate the 3 segments
    losslessly into one file whose duration lands within
    duration_tolerance_final_s of the true ~6s total, and that file must
    pass a final verification -- matching selftest.sh's
    "concat round-trip preserved duration (~6s) and verified" assertion."""
    session = capture_start(
        capture_cfg,
        input_override="-f lavfi -i sine=frequency=300:sample_rate=16000",
        duration_s=6,
    )
    _wait_for_process_exit(session)

    out_path = capture_finalize(session.session_dir, cfg=capture_cfg)
    report = capture_finalize_report(out_path)

    assert report is not None
    assert report["ok"] is True, report["checks"]

    duration_check = next(c for c in report["checks"] if c["name"] == "duration_in_band")
    assert duration_check["ok"] is True, duration_check

    # Belt-and-suspenders: independently probe the finalized file's duration
    # via a fresh verify_capture call against the expected ~6s, rather than
    # only trusting capture_finalize's internal report.
    final_report = verify_capture(out_path, 6.0, final=True, cfg=capture_cfg)
    assert final_report["ok"] is True, final_report["checks"]


def test_finalize_raises_on_empty_session_dir(tmp_path):
    """finalize.sh's "FATAL: no segments in $OUT_DIR" hard failure, ported:
    capture_finalize on a directory with no seg_*.wav files must raise
    FinalizeError rather than silently producing a bogus empty output."""
    from tscribe.capture import FinalizeError

    empty_dir = tmp_path / "empty_session"
    empty_dir.mkdir()

    with pytest.raises(FinalizeError, match="no segments"):
        capture_finalize(str(empty_dir))


# --------------------------------------------------------------------------
# capture_start dry_run + --list-equivalent surfaces (static-review-adjacent:
# these don't touch real hardware, but do exercise the real function paths
# with dry_run=True / input_override, which IS executed here).
# --------------------------------------------------------------------------


def test_capture_start_dry_run_plans_without_spawning_ffmpeg(capture_cfg):
    """Unit 07's acceptance criterion "--dry-run supported at minimum for
    bootstrap and capture start": a dry-run session must build the correct
    ffmpeg command and create the output directory, but never actually spawn
    a process."""
    session = capture_start(
        capture_cfg,
        input_override="-f lavfi -i sine=frequency=300:sample_rate=16000",
        dry_run=True,
    )

    assert session.process is None
    assert session.poll() is None
    assert session.stop() == 0  # no-op on a dry-run session, must not raise
    assert "-reset_timestamps" in session.command
    assert "1" in session.command
    assert str(capture_cfg.segment_seconds) in session.command
    import os

    assert os.path.isdir(session.session_dir)
