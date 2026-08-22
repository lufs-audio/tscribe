"""Shared fixtures for tests/capture/.

The whole point of this test suite (matching tscribe-capture-rig's own
tests/selftest.sh) is proving the rig without a real microphone or a real
Windows box. Every test here drives the actual capture_start/verify_capture/
capture_finalize functions through the test seam
(input_override + duration_s, the Python equivalent of the source rig's
RIG_INPUT_OVERRIDE/CAPTURE_DURATION env vars) against synthetic ffmpeg
`lavfi` sources -- never a real device.
"""

from __future__ import annotations

import shutil

import pytest

from tscribe.config import CaptureConfig


@pytest.fixture(autouse=True, scope="session")
def _require_ffmpeg():
    """Every test in this package needs a real ffmpeg/ffprobe to synthesize
    and probe test fixtures -- this is the one real (non-mocked) external
    dependency the whole suite has, matching the source rig's own tests
    (which also shell out to real ffmpeg to build synthetic sources)."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not found on PATH -- required to run tests/capture/")


@pytest.fixture
def capture_cfg(tmp_path):
    """A CaptureConfig pointed at a throwaway tmp_path out_dir, with short
    tolerances suited to multi-second test fixtures rather than real
    15-minute class segments."""
    return CaptureConfig(
        out_dir=str(tmp_path / "class_audio"),
        sample_rate=16000,
        channels=1,
        segment_seconds=2,
    )
