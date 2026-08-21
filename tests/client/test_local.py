"""Unit 04 acceptance tests: LocalClient and RemoteInferenceClient.

Both run tscribe.core.class_pipeline in-process; these tests use a real
synthetic recording (same fixture pattern as Units 02/03's tests) with a
mocked transcriber (no faster-whisper install needed), so a genuine
ContractViolation exercises the evidence-retention path end to end.

`tscribe.client.local` imports `transcribe_class` by name (`from
tscribe.core.class_pipeline import ... transcribe_class`), so tests
monkeypatch it at `tscribe.client.local.transcribe_class` — patching the
original module's export would not be seen here, the same import-binding
lesson Unit 03 hit with `transcriber_factory`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tscribe.client.local as local_mod
from tscribe.client import AudioSource, JobHandle, LocalClient, RemoteInferenceClient, make_client
from tscribe.client.local import _report_to_dict
from tscribe.config import Backend, ChunkingConfig, Mode, RemoteConfig, RemoteKind, RuntimeConfig
from tscribe.core.class_pipeline.pipeline import transcribe_class as real_transcribe_class


def _make_recording(path: Path, seconds: int = 6) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"sine=frequency=220:duration={seconds}", "-ac", "1", "-ar", "16000",
         "-sample_fmt", "s16", str(path)],
        check=True,
    )


@pytest.fixture
def tone_wav(tmp_path):
    p = tmp_path / "tone.wav"
    _make_recording(p)
    return p


def _local_cfg() -> RuntimeConfig:
    return RuntimeConfig(
        mode=Mode.CLIENT, backend=Backend.LOCAL,
        chunking=ChunkingConfig(minutes=10 / 60.0, snap_window_s=3.0, workers=1),
    )


class _GoodTranscriber:
    """Word count proportional to actual chunk duration (~120 wpm) so the
    speaking-rate contract passes regardless of fixture length -- a fixed
    word count (as several early drafts of this test used) trips the wpm
    ceiling/floor the moment the fixture duration changes."""

    def transcribe(self, audio_path: str):
        from tscribe.core.class_pipeline.splitter import probe_duration

        dur = probe_duration(audio_path)
        words = ("alpha bravo charlie delta echo foxtrot golf hotel " * 20).split()
        n = max(1, int(120 * dur / 60.0))
        return " ".join(words[i % len(words)] for i in range(n)), None


class _DropsEverything:
    def transcribe(self, audio_path: str):
        return "", None  # audible chunk, empty transcript -> contract violation


def test_local_client_submit_and_wait_returns_verified_result(tone_wav, monkeypatch):
    monkeypatch.setattr(
        local_mod, "transcribe_class",
        lambda input_path, output_path, cfg: real_transcribe_class(
            input_path, output_path, cfg, transcriber=_GoodTranscriber()
        ),
    )
    client = LocalClient(_local_cfg())
    result = client.submit_and_wait(AudioSource(path=str(tone_wav)), poll_interval_s=0.05)
    assert result.status == "done"
    assert result.verified is True
    assert "## [00:00:00]" in result.transcript


def test_local_client_poll_unknown_job_id_is_reported_not_raised():
    client = LocalClient(_local_cfg())
    result = client.poll(JobHandle(id="does-not-exist"))
    assert result.status == "failed"
    assert "unknown job id" in result.error


def test_local_client_keeps_evidence_on_contract_violation(tone_wav, monkeypatch):
    """A chunk with real audio but an empty transcript is the exact
    silent-failure the contract exists to catch -- and the client must
    surface the transcript + report on the failure, not swallow them."""
    monkeypatch.setattr(
        local_mod, "transcribe_class",
        lambda input_path, output_path, cfg: real_transcribe_class(
            input_path, output_path, cfg, transcriber=_DropsEverything()
        ),
    )
    client = LocalClient(_local_cfg())
    handle = client.submit(AudioSource(path=str(tone_wav)))  # runs synchronously
    result = client.poll(handle)
    assert result.status == "failed"
    assert result.verified is False
    assert result.error and "ContractViolation" in result.error
    assert result.report is not None
    assert result.report["ok"] is False
    assert any("transcript_nonempty" in n for n in result.report["failures"])


def test_report_to_dict_shape_matches_units_02_03():
    """Same {ok, checks, failures} shape jobs.py (Unit 03) already produces --
    keeping it identical means a CLI (Unit 07) formats both the same way."""
    from tscribe.core.class_pipeline.verify import Check, VerifyReport

    report = VerifyReport(checks=[Check("a", True, "fine"), Check("b", False, "bad")])
    d = _report_to_dict(report)
    assert d == {
        "ok": False,
        "checks": [{"name": "a", "ok": True, "detail": "fine"}, {"name": "b", "ok": False, "detail": "bad"}],
        "failures": ["b"],
    }


def test_make_client_selects_local_for_backend_local():
    assert isinstance(make_client(_local_cfg()), LocalClient)


def test_make_client_selects_remote_inference_by_default():
    cfg = RuntimeConfig(
        mode=Mode.CLIENT, backend=Backend.REMOTE,
        remote=RemoteConfig(endpoint="http://example.com", api_key_env="X"),
    )
    assert isinstance(make_client(cfg), RemoteInferenceClient)


def test_make_client_selects_remote_server_when_kind_is_tscribe_server():
    from tscribe.client import RemoteServerClient

    cfg = RuntimeConfig(
        mode=Mode.CLIENT, backend=Backend.REMOTE,
        remote=RemoteConfig(endpoint="http://example.com", api_key_env="X", kind=RemoteKind.TSCRIBE_SERVER),
    )
    assert isinstance(make_client(cfg), RemoteServerClient)
