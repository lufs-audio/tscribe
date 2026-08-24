"""tscribe.capture — verified segmented recording (Unit 05).

Ported from `danialrami/tscribe-capture-rig`: a USB-mic-and-flash-drive
recording rig that captures long-form audio to disk in fixed-length
segments and proves the capture is real, audible speech before anything
downstream (in particular `tscribe transcribe`, Unit 02) is asked to trust
it. See docs/units/05-capture-integration.md for the full unit spec.

Three functions make up the whole surface, matching the unit's interface
contract exactly:

    capture_start(cfg: CaptureConfig) -> CaptureSession
        Start (or, with dry_run=True, just plan) a segmented recording.

    verify_capture(path, expected_seconds, final=False) -> VerificationReport
        Prove one WAV file (a segment, or the finalized file) is real,
        correctly-shaped, non-silent audio. The `not_silence_floor` check is
        the load-bearing one: it is what catches a mic-privacy toggle or an
        unplugged mic silently producing four hours of digital silence.

    capture_finalize(session_dir: str) -> str
        Losslessly concatenate every segment in a session directory into one
        WAV, verify the result, and return its path.

This package does not implement transcription (Unit 02) — `capture_finalize`
hands back a plain file path, which is a valid `audio_path` for
`tscribe.core.transcribe`/`transcribe_class`. It also does not modify
`tscribe-capture-rig`'s original files; this is a from-scratch Python port.
"""

from .finalize import FinalizeError, capture_finalize, capture_finalize_report
from .session import CaptureError, CaptureSession, capture_start
from .verify import Check, VerificationReport, verify_capture

__all__ = [
    "CaptureError",
    "CaptureSession",
    "capture_start",
    "Check",
    "VerificationReport",
    "verify_capture",
    "FinalizeError",
    "capture_finalize",
    "capture_finalize_report",
]
