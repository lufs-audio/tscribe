"""Unit 04 acceptance tests: RemoteInferenceClient.

RemoteInferenceClient runs tscribe.core.class_pipeline in-process, exactly
like LocalClient (see tests/client/test_local.py) -- the only things this
class adds on top of the shared _InProcessClient machinery are the
constructor checks that `cfg` actually describes a bare inference endpoint
(remote.kind == RemoteKind.INFERENCE_ENDPOINT) before any work starts. These
tests exercise both: the in-process pipeline behavior (shared with
LocalClient, re-verified here so RemoteInferenceClient is proven on its own
rather than only via isinstance() checks in test_local.py's make_client
tests), and the constructor validation that is unique to this class.

Same fixture pattern as test_local.py: a real synthetic recording (no network,
no real inference endpoint) with a mocked transcriber, so a genuine
ContractViolation exercises the evidence-retention path end to end without
needing faster-whisper installed or a live LiteLLM/whisper endpoint.

`RemoteInferenceClient.submit()` is inherited from `_InProcessClient` in
`tscribe.client.local`, which imports `transcribe_class` by name (`from
tscribe.core.class_pipeline import ... transcribe_class`). That import
binding lives in `local.py`, not `remote_inference.py`, so tests here
monkeypatch it at `tscribe.client.local.transcribe_class` -- patching
`tscribe.client.remote_inference.transcribe_class` would be patching a name
that module never imports, and the mock would silently never be seen. Same
import-binding lesson test_local.py documents, and Unit 03 hit with
`transcriber_factory`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tscribe.client.local as local_mod
from tscribe.client import AudioSource, JobHandle, RemoteInferenceClient
from tscribe.config import (
    Backend,
    ChunkingConfig,
    Mode,
    RemoteConfig,
    RemoteKind,
    RuntimeConfig,
)
from tscribe.core.class_pipeline.pipeline import transcribe_class as real_transcribe_class


def _make_recording(path: Path, seconds: int = 6) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={seconds}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(path),
        ],
        check=True,
    )


@pytest.fixture
def tone_wav(tmp_path):
    p = tmp_path / "tone.wav"
    _make_recording(p)
    return p


def _remote_inference_cfg(**remote_overrides) -> RuntimeConfig:
    remote_kwargs = dict(
        endpoint="http://gpu-host.example.com:8000/v1",
        api_key_env="TSCRIBE_TEST_INFERENCE_KEY",
        kind=RemoteKind.INFERENCE_ENDPOINT,
        transcription_model="Systran/faster-distil-whisper-large-v3",
    )
    remote_kwargs.update(remote_overrides)
    return RuntimeConfig(
        mode=Mode.CLIENT,
        backend=Backend.REMOTE,
        remote=RemoteConfig(**remote_kwargs),
        chunking=ChunkingConfig(minutes=10 / 60.0, snap_window_s=3.0, workers=1),
    )


class _GoodTranscriber:
    """Word count proportional to actual chunk duration (~120 wpm) so the
    speaking-rate contract passes regardless of fixture length -- same
    fixture-independence rationale as test_local.py's _GoodTranscriber.

    Standing in for what would otherwise be Unit 02's Transcriber actually
    calling out to `cfg.remote.endpoint` -- that whisper-call logic belongs
    to Unit 02 (tscribe.core.transcriber.Transcriber) and is deliberately not
    re-tested or reimplemented here; RemoteInferenceClient never talks to the
    network directly, so there is no HTTP call in this module for a test to
    mock in the first place (see remote_inference.py's module docstring).
    """

    def transcribe(self, audio_path: str):
        from tscribe.core.class_pipeline.splitter import probe_duration

        dur = probe_duration(audio_path)
        words = ("alpha bravo charlie delta echo foxtrot golf hotel " * 20).split()
        n = max(1, int(120 * dur / 60.0))
        return " ".join(words[i % len(words)] for i in range(n)), None


class _DropsEverything:
    def transcribe(self, audio_path: str):
        return "", None  # audible chunk, empty transcript -> contract violation


# --------------------------------------------------------------------------- #
# Constructor validation -- the one thing genuinely unique to this class.
# --------------------------------------------------------------------------- #


def test_requires_a_remote_section():
    """Mirrors RemoteServerClient's identical fail-fast check (see
    test_remote_server.py::test_requires_a_remote_section) -- constructing a
    RemoteInferenceClient with no endpoint to call is a config error, not
    something that should surface later as an opaque AttributeError deep in
    Unit 02's Transcriber."""
    cfg = RuntimeConfig(mode=Mode.CLIENT, backend=Backend.LOCAL)
    with pytest.raises(ValueError, match="remote"):
        RemoteInferenceClient(cfg)


def test_rejects_tscribe_server_kind():
    """A RemoteConfig pointed at a real tscribe server (kind:
    tscribe_server) should go through RemoteServerClient, not this class --
    constructing RemoteInferenceClient directly with that kind means the
    caller's intent and the class it picked have drifted apart, which is
    exactly the ambiguity RemoteKind exists to catch (see config.py)."""
    cfg = _remote_inference_cfg(kind=RemoteKind.TSCRIBE_SERVER)
    with pytest.raises(ValueError, match="INFERENCE_ENDPOINT|inference_endpoint"):
        RemoteInferenceClient(cfg)


# --------------------------------------------------------------------------- #
# In-process pipeline behavior -- shared with LocalClient via _InProcessClient,
# re-verified here so RemoteInferenceClient is proven directly rather than only
# through make_client() isinstance() checks.
# --------------------------------------------------------------------------- #


def test_remote_inference_client_submit_and_wait_returns_verified_result(tone_wav, monkeypatch):
    monkeypatch.setattr(
        local_mod,
        "transcribe_class",
        lambda input_path, output_path, cfg: real_transcribe_class(
            input_path, output_path, cfg, transcriber=_GoodTranscriber()
        ),
    )
    client = RemoteInferenceClient(_remote_inference_cfg())
    result = client.submit_and_wait(AudioSource(path=str(tone_wav)), poll_interval_s=0.05)
    assert result.status == "done"
    assert result.verified is True
    assert "## [00:00:00]" in result.transcript


def test_remote_inference_client_poll_unknown_job_id_is_reported_not_raised():
    client = RemoteInferenceClient(_remote_inference_cfg())
    result = client.poll(JobHandle(id="does-not-exist"))
    assert result.status == "failed"
    assert "unknown job id" in result.error


def test_remote_inference_client_keeps_evidence_on_contract_violation(tone_wav, monkeypatch):
    """Same silent-failure guard as LocalClient's equivalent test: a chunk
    with real audio but an empty transcript must surface transcript + report
    on failure, not swallow them -- whether the (mocked) whisper call was
    "local" or "remote" makes no difference to this client's evidence-
    retention contract."""
    monkeypatch.setattr(
        local_mod,
        "transcribe_class",
        lambda input_path, output_path, cfg: real_transcribe_class(
            input_path, output_path, cfg, transcriber=_DropsEverything()
        ),
    )
    client = RemoteInferenceClient(_remote_inference_cfg())
    handle = client.submit(AudioSource(path=str(tone_wav)))  # runs synchronously
    result = client.poll(handle)
    assert result.status == "failed"
    assert result.verified is False
    assert result.error and "ContractViolation" in result.error
    assert result.report is not None
    assert result.report["ok"] is False
    assert any("transcript_nonempty" in n for n in result.report["failures"])


def test_remote_inference_client_surfaces_unexpected_errors_without_raising(tone_wav, monkeypatch):
    """If Unit 02's Transcriber blows up for a reason that has nothing to do
    with the verification contract (e.g. the bare inference endpoint is
    unreachable and there is no local fallback model installed), submit()
    must still report a failed JobResult rather than propagate -- the same
    contract LocalClient upholds, exercised here with a non-ContractViolation
    exception standing in for "the remote endpoint call blew up"."""

    def _boom(input_path, output_path, cfg):
        raise RuntimeError("connection refused: gpu-host.example.com:8000")

    monkeypatch.setattr(local_mod, "transcribe_class", _boom)
    client = RemoteInferenceClient(_remote_inference_cfg())
    handle = client.submit(AudioSource(path=str(tone_wav)))
    result = client.poll(handle)
    assert result.status == "failed"
    assert result.verified is False
    assert "RuntimeError" in result.error
    assert "connection refused" in result.error


def test_make_client_selects_remote_inference_client_from_this_module():
    """make_client() (tscribe/client/__init__.py) must resolve to the
    RemoteInferenceClient defined in remote_inference.py -- not a
    same-named class living somewhere else -- for backend: remote configs
    that don't specify kind: tscribe_server."""
    import tscribe.client.remote_inference as remote_inference_mod
    from tscribe.client import make_client

    cfg = _remote_inference_cfg()
    client = make_client(cfg)
    assert isinstance(client, remote_inference_mod.RemoteInferenceClient)
    assert type(client).__module__ == "tscribe.client.remote_inference"
