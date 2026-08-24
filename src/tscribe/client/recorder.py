"""Microphone recording — ported from tscribe-transcription-tool's recorder.py.

Restores `tscribe record` / `tscribe devices` (Unit 04's CLI, wired by Unit 07)
against the standalone personal-use workflow the source tool provided. Ported
essentially verbatim — this module has no configuration coupling of its own;
the caller (Unit 07's CLI) decides what to do with the captured audio (usually
handing the saved WAV straight to a client's `submit()`).
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

CHANNELS = 2
SAMPLE_RATE = 48000
DTYPE = "float32"


class Recorder:
    def __init__(self, channels: int = CHANNELS, samplerate: int = SAMPLE_RATE) -> None:
        self.channels = channels
        self.samplerate = samplerate
        self.audio_buffers: list = []
        self._recording = False
        self._stream = None

    def start(self) -> None:
        self.audio_buffers = []
        self._recording = True

        def callback(indata, frames, time_info, status):
            if self._recording:
                self.audio_buffers.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype=DTYPE,
            callback=callback,
        )
        self._stream.start()

    def stop(self):
        self._recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if not self.audio_buffers:
            return None

        recording = np.concatenate(self.audio_buffers, axis=0)
        if recording.shape[1] > 1:
            recording = np.mean(recording, axis=1, keepdims=False)
        return recording

    def save_temp_wav(self, audio_array, samplerate: int | None = None) -> Path:
        if samplerate is None:
            samplerate = self.samplerate
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        path = Path(tmp.name)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(samplerate)
            wf.writeframes((audio_array * 32767).astype(np.int16).tobytes())
        return path

    def list_devices(self):
        return sd.query_devices()

    def set_device(self, device_id: int) -> None:
        sd.default.device = device_id


def list_input_devices() -> list[dict]:
    """Used by `tscribe devices` — only devices that can actually record."""
    devices = sd.query_devices()
    return [
        {"index": i, "name": d["name"], "max_input_channels": d["max_input_channels"]}
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]
