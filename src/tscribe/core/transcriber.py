"""tscribe.core.transcriber — dual local/remote whisper transcription.

Ported from tscribe-transcription-tool's Transcriber. Behavior preserved
exactly: remote-first via a LiteLLM-compatible OpenAI transcription endpoint,
automatic fallback to local faster-whisper on any remote failure. The only
real change is *where configuration comes from* — RuntimeConfig (Unit 01)
instead of individual constructor kwargs / raw os.environ reads.

Known gap carried over from this port (not fixed, flagged honestly): transcription
language is hardcoded to "en" here, matching this port's scope. The source
tool exposed a --language CLI flag; RuntimeConfig has no language field yet.
Worth adding to Unit 01's schema as a follow-up if non-English audio comes up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests

from tscribe.config import Backend, RuntimeConfig

_DEFAULT_LANGUAGE = "en"


class _RemoteResultInfo:
    """Stand-in for faster-whisper's TranscriptionInfo when the remote path
    is used — the remote endpoint doesn't return per-segment language-detection
    metadata the way faster-whisper does, so this fills the same two fields
    the original tool relied on (language, language_probability)."""

    def __init__(self, language: str) -> None:
        self.language = language
        self.language_probability = 1.0


class Transcriber:
    """Owns the (expensive, lazily-loaded) local whisper model, if used."""

    def __init__(self, cfg: RuntimeConfig, language: str = _DEFAULT_LANGUAGE) -> None:
        self.cfg = cfg
        self.language = language
        self._model: Any = None

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy import, deferred

            self._model = WhisperModel(
                self.cfg.local.model_size,
                device=self.cfg.local.device,
                compute_type=self.cfg.local.compute_type,
            )
        return self._model

    def _transcribe_remote(self, audio_path: str | Path) -> tuple[str, Any]:
        """Transcribe using an OpenAI-compatible /audio/transcriptions endpoint."""
        remote = self.cfg.remote
        assert remote is not None  # caller (transcribe()) already checked this
        api_key = os.environ.get(remote.api_key_env, "")
        url = f"{remote.endpoint.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}

        with open(audio_path, "rb") as f:
            files = {"file": (Path(audio_path).name, f, "audio/wav")}
            data = {"model": remote.transcription_model, "language": self.language}
            response = requests.post(url, headers=headers, files=files, data=data, timeout=300)
            response.raise_for_status()
            result = response.json()
            text = result.get("text", "")
            return text, _RemoteResultInfo(self.language)

    def transcribe(self, audio_path: str | Path) -> tuple[str, Any]:
        """Remote-first (if backend: remote and a remote section is configured),
        with automatic fallback to local faster-whisper on any failure."""
        should_try_remote = self.cfg.backend == Backend.REMOTE and self.cfg.remote is not None
        if should_try_remote:
            try:
                return self._transcribe_remote(audio_path)
            except Exception as e:  # noqa: BLE001 — fallback path, not a hard failure
                print(f"Remote transcription failed: {e}", file=sys.stderr)
                print("Falling back to local Whisper...", file=sys.stderr)

        model = self._load_model()
        segments, info = model.transcribe(str(audio_path), language=self.language)
        text = " ".join(seg.text for seg in segments)
        return text, info


def transcribe(audio_path: str | Path, cfg: RuntimeConfig) -> str:
    """Matches SPEC.md Unit 02's simple interface contract — text only, no
    per-call Transcriber reuse. Use the Transcriber class directly (as Units
    03/04 do) when transcribing many files, so the local model loads once."""
    text, _info = Transcriber(cfg).transcribe(audio_path)
    return text
