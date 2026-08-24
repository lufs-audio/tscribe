"""Proves the Unit 05 acceptance criterion: "A captured-and-finalized WAV is
a valid input to `tscribe transcribe` -- proving the two halves compose end
to end."

What this test actually exercises, precisely, and why: it runs the REAL
capture_start -> capture_finalize path (synthetic source, no real
microphone) to produce a real, on-disk, ffmpeg-concatenated WAV file, then
hands that exact path to the REAL tscribe.core.transcriber.Transcriber /
transcribe() call chain. The one thing it does NOT exercise is the actual
faster-whisper speech-to-text model itself, because:

  1. faster_whisper is a heavy, GPU-capable ML dependency that belongs to
     Unit 02 (transcription core), not Unit 05 (capture) -- Unit 05's own
     "Boundaries" section is explicit that it "does not implement
     transcription (Unit 02) -- hands off a file path only," and pulling in
     a large model download here would blur that boundary and slow every
     future capture-only test run for no benefit to what this unit is
     actually proving.
  2. This sandbox was not confirmed to have faster_whisper installed or a
     model cached, and installing+downloading a real whisper model is not a
     reasonable cost for a composition test whose job is proving a FILE PATH
     is accepted, not proving speech recognition accuracy.

So the model-loading boundary (WhisperModel) is monkeypatched to a stub that
records what it was called with and returns a trivial deterministic
transcript -- this is the one seam between "capture produced a real file"
and "the transcription library accepted that file as its audio_path,
resolved backend: local, and got as far as invoking the model on that exact
path." Everything upstream of that seam (capture_start, capture_finalize,
verify_capture, Transcriber.__init__, Transcriber.transcribe's backend
branch, RuntimeConfig plumbing) is real, unmocked code.

Honesty note for the port's report: this proves file-path/interface
composition, not "faster-whisper can actually transcribe rig audio" -- that
second claim is Unit 02's to prove, with real audio content, not this unit's.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from tscribe.capture import capture_finalize, capture_start
from tscribe.config import Backend, Mode, RuntimeConfig


def _wait_for_exit(session, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while session.poll() is None:
        if time.monotonic() > deadline:
            session.stop()
            raise TimeoutError("capture session did not exit in time")
        time.sleep(0.1)


class _StubSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubWhisperModel:
    """Records the audio_path it was asked to transcribe, so the test can
    assert the exact capture_finalize output flowed all the way through
    Transcriber unmodified."""

    last_audio_path: str | None = None

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def transcribe(self, audio_path: str, language: str = "en"):
        _StubWhisperModel.last_audio_path = audio_path
        info = SimpleNamespace(language=language, language_probability=1.0)
        return [_StubSegment("stub transcript from a captured-and-finalized WAV")], info


def test_finalized_capture_composes_into_transcribe(capture_cfg, monkeypatch):
    # 1. REAL capture: synthetic source, no microphone, through the exact
    #    same test seam as tests/capture/test_session_and_finalize.py's T4.
    session = capture_start(
        capture_cfg,
        input_override="-f lavfi -i sine=frequency=300:sample_rate=16000",
        duration_s=4,
    )
    _wait_for_exit(session)

    # 2. REAL finalize: lossless concat + verify, producing an actual WAV
    #    file on disk that must independently decode and verify.
    finalized_path = capture_finalize(session.session_dir, cfg=capture_cfg)

    import os

    assert os.path.isfile(finalized_path)
    assert os.path.getsize(finalized_path) > 1024

    # 3. Hand that exact path to the REAL transcription entrypoint.
    #    Only the deepest boundary (the actual ML model class) is stubbed --
    #    see this module's docstring for why.
    monkeypatch.setattr("tscribe.core.transcriber.WhisperModel", _StubWhisperModel, raising=False)

    def _fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            return SimpleNamespace(WhisperModel=_StubWhisperModel)
        return _real_import(name, *args, **kwargs)

    import builtins

    _real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _fake_import)

    cfg = RuntimeConfig(mode=Mode.CLIENT, backend=Backend.LOCAL, capture=capture_cfg)

    from tscribe.core.transcriber import transcribe

    text = transcribe(finalized_path, cfg)

    assert text == "stub transcript from a captured-and-finalized WAV"
    # The critical assertion: the transcription call chain received the
    # EXACT path capture_finalize produced -- proving the two halves compose
    # via a plain file path, with no capture-specific knowledge leaking into
    # tscribe.core and no transcription-specific knowledge leaking into
    # tscribe.capture.
    assert _StubWhisperModel.last_audio_path == finalized_path
