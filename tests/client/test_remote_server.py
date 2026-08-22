"""Unit 04 acceptance tests: RemoteServerClient against Unit 03's exact contract.

No real network or server: monkeypatches `requests.post`/`requests.get` inside
`tscribe.client.remote_server` with fakes that assert the right URLs/headers
and return response shapes lifted directly from Unit 03's documented contract
(SPEC.md's Interface contract section for that unit).
"""

from __future__ import annotations

import os

import pytest

import tscribe.client.remote_server as remote_server_mod
from tscribe.client import AudioSource, JobHandle, RemoteServerClient
from tscribe.config import Backend, Mode, RemoteConfig, RemoteKind, RuntimeConfig


class _FakeResponse:
    def __init__(self, json_body: dict, status_code: int = 200):
        self._json = json_body
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _cfg(endpoint="http://tscribe.example.com", api_key_env="TSCRIBE_TEST_KEY") -> RuntimeConfig:
    return RuntimeConfig(
        mode=Mode.CLIENT,
        backend=Backend.REMOTE,
        remote=RemoteConfig(
            endpoint=endpoint, api_key_env=api_key_env, kind=RemoteKind.TSCRIBE_SERVER
        ),
    )


def test_requires_a_remote_section():
    cfg = RuntimeConfig(mode=Mode.CLIENT, backend=Backend.LOCAL)
    with pytest.raises(ValueError, match="remote"):
        RemoteServerClient(cfg)


def test_submit_posts_to_jobs_upload_with_api_key_header(monkeypatch, tmp_path):
    os.environ["TSCRIBE_TEST_KEY"] = "secret-123"
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")  # content doesn't matter, never decoded here

    captured = {}

    def fake_post(url, headers=None, files=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["filename"] = files["file"][0]
        return _FakeResponse({"id": "job-abc", "status": "queued"})

    monkeypatch.setattr(remote_server_mod.requests, "post", fake_post)

    client = RemoteServerClient(_cfg())
    handle = client.submit(AudioSource(path=str(audio)))

    assert handle == JobHandle(id="job-abc")
    assert captured["url"] == "http://tscribe.example.com/jobs/upload"
    assert captured["headers"] == {"X-API-Key": "secret-123"}
    assert captured["filename"] == "clip.wav"


def test_poll_gets_jobs_id_and_maps_the_full_contract_shape(monkeypatch):
    os.environ["TSCRIBE_TEST_KEY"] = "secret-123"

    def fake_get(url, headers=None, timeout=None):
        assert url == "http://tscribe.example.com/jobs/job-abc"
        assert headers == {"X-API-Key": "secret-123"}
        return _FakeResponse(
            {
                "id": "job-abc",
                "status": "done",
                "verified": True,
                "source": "upload",
                "created_at": 1.0,
                "updated_at": 2.0,
                "transcript": "# Class Transcript\n...",
                "report": {"ok": True, "checks": [], "failures": []},
                "error": None,
                "transcript_file_id": None,
                "upload_error": None,
                "moved": [],
                "move_errors": [],
            }
        )

    monkeypatch.setattr(remote_server_mod.requests, "get", fake_get)

    client = RemoteServerClient(_cfg())
    result = client.poll(JobHandle(id="job-abc"))
    assert result.status == "done"
    assert result.verified is True
    assert result.transcript.startswith("# Class Transcript")
    assert result.report == {"ok": True, "checks": [], "failures": []}


def test_poll_surfaces_failed_jobs_with_evidence_intact(monkeypatch):
    """Mirrors Unit 03's evidence-retention contract: a failed job still
    carries transcript + report over the wire, and this client must not drop
    either on the way through."""
    os.environ["TSCRIBE_TEST_KEY"] = "secret-123"

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(
            {
                "id": "job-xyz",
                "status": "failed",
                "verified": False,
                "source": "upload",
                "created_at": 1.0,
                "updated_at": 2.0,
                "transcript": "partial transcript",
                "report": {"ok": False, "checks": [], "failures": ["chunk[0].transcript_nonempty"]},
                "error": "ContractViolation: 1 contract check(s) failed",
                "transcript_file_id": None,
                "upload_error": None,
                "moved": [],
                "move_errors": [],
            }
        )

    monkeypatch.setattr(remote_server_mod.requests, "get", fake_get)

    client = RemoteServerClient(_cfg())
    result = client.poll(JobHandle(id="job-xyz"))
    assert result.status == "failed"
    assert result.verified is False
    assert result.transcript == "partial transcript"
    assert result.report["failures"] == ["chunk[0].transcript_nonempty"]
    assert "ContractViolation" in result.error


def test_submit_and_wait_polls_until_terminal(monkeypatch, tmp_path):
    os.environ["TSCRIBE_TEST_KEY"] = "secret-123"
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"fake")

    monkeypatch.setattr(
        remote_server_mod.requests,
        "post",
        lambda *a, **k: _FakeResponse({"id": "job-poll", "status": "queued"}),
    )

    call_count = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        call_count["n"] += 1
        status = "running" if call_count["n"] < 3 else "done"
        return _FakeResponse(
            {
                "id": "job-poll",
                "status": status,
                "verified": status == "done",
                "source": "upload",
                "created_at": 1.0,
                "updated_at": 2.0,
                "transcript": "done text" if status == "done" else None,
                "report": {"ok": True, "checks": [], "failures": []} if status == "done" else None,
                "error": None,
                "transcript_file_id": None,
                "upload_error": None,
                "moved": [],
                "move_errors": [],
            }
        )

    monkeypatch.setattr(remote_server_mod.requests, "get", fake_get)

    client = RemoteServerClient(_cfg())
    result = client.submit_and_wait(AudioSource(path=str(audio)), poll_interval_s=0.01)
    assert result.status == "done"
    assert call_count["n"] == 3  # polled twice while running, then got done
