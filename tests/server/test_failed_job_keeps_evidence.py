"""Proof that a contract failure flags the job instead of emptying it.

Ported from tscribe-class-carnyx's tests/test_failed_job_keeps_evidence.py.
Regression origin (the 2026-08-06 incident referenced throughout this repo's
docs): a four-hour class ran, sixteen chunks came back, one check misfired,
and this is what the job held afterwards:

    status:     failed
    transcript: None
    report:     None
    error:      "ContractViolation: 1 contract check(s) failed: chunk[8]..."

Both of the things needed to act on that were gone. So a failed job now keeps
its receipts, and keeps the gate shut:

    status:     failed        <- unchanged
    verified:   False         <- explicit, so "transcript present" != "usable"
    transcript: <the text>    <- auditable, recoverable
    report:     <all checks>  <- the failing row AND the passing ones
    (nothing written back to Drive, nothing moved)

Adaptation from the source: `app`/`jobs.submit` go through Unit 03's
`create_app(cfg)` / `jobs.submit(cfg, ...)` instead of a module-level
`SETTINGS` singleton; `config.transcriber_factory` mocks now take a `cfg` arg
(unused) since Transcriber needs one.

Run: pytest -q  (ffmpeg + ffprobe must be on PATH)
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

os.environ.setdefault("TSCRIBE_API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from tscribe.config import Backend, ChunkingConfig, Mode, RuntimeConfig  # noqa: E402
from tscribe.server import config, jobs  # noqa: E402
from tscribe.server.app import create_app  # noqa: E402

TEST_CFG = RuntimeConfig(
    mode=Mode.SERVER,
    backend=Backend.LOCAL,
    # 10s chunks so a 30s fixture yields several.
    chunking=ChunkingConfig(minutes=10 / 60.0, snap_window_s=3.0, workers=2),
)
app = create_app(TEST_CFG)
client = TestClient(app)


# Deterministic non-repetitive filler — see test_app.py for why.
_VOCAB = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
    "lima mike november oscar papa quebec romeo sierra tango uniform "
    "victor whiskey xray yankee zulu"
).split()


def _words(n: int, seed: int = 1234) -> str:
    import random

    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCAB) for _ in range(n))


class DroppingTranscriber:
    """Returns nothing for the first chunk -- a genuine silent failure, the kind
    the contract exists to catch -- and real text for the rest."""

    def __init__(self):
        self._seen = 0

    def transcribe(self, audio_path: str):
        from tscribe.core.class_pipeline.splitter import probe_duration

        idx = self._seen
        self._seen += 1
        if idx == 0:
            return "", None
        dur = probe_duration(audio_path)
        return _words(max(1, int(120 * dur / 60.0))), None


@pytest.fixture(autouse=True)
def dropping_transcriber():
    prev = config.transcriber_factory
    config.transcriber_factory = lambda cfg: DroppingTranscriber()
    yield
    config.transcriber_factory = prev


@pytest.fixture
def tone_wav(tmp_path):
    """~30s of continuous tone: every chunk carries audio, so a chunk that comes
    back empty is unambiguously a drop."""
    p = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=30",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(p),
        ],
        check=True,
    )
    return p


def _wait(job_id, timeout=60):
    for _ in range(timeout * 5):
        j = jobs.get_job(job_id)
        if j and j.status in ("done", "failed"):
            return j
        time.sleep(0.2)
    raise TimeoutError("job did not finish")


@pytest.fixture
def failed_job(tone_wav, monkeypatch):
    import shutil

    monkeypatch.setattr(jobs, "_download_url", lambda url, dest, cfg: shutil.copy(tone_wav, dest))
    job = jobs.submit(TEST_CFG, audio_url="https://example.com/tone.wav", source_name="tone.wav")
    return _wait(job.id)


# ------------------------------------------------------- the gate stays closed


def test_the_job_still_fails(failed_job):
    assert failed_job.status == "failed"
    assert "ContractViolation" in failed_job.error
    assert "transcript_nonempty" in failed_job.error


def test_verified_is_explicitly_false(failed_job):
    """A transcript being present must never be mistaken for a usable one."""
    assert failed_job.verified is False
    assert failed_job.public()["verified"] is False


def test_nothing_was_written_back_or_moved(failed_job):
    assert failed_job.transcript_file_id is None
    assert failed_job.moved == []


# ------------------------------------------------------ the evidence survives


def test_the_report_is_retained_with_all_of_its_rows(failed_job):
    report = failed_job.report
    assert report is not None
    assert report["ok"] is False
    assert report["failures"]
    # The passing rows too -- they are how a reader tells "a chunk was dropped"
    # from "the checker misfired", which is the whole reason the source
    # incident was hard to diagnose.
    assert len(report["checks"]) > len(report["failures"])
    assert any("transcript_nonempty" in n for n in report["failures"])
    assert all({"name", "ok", "detail"} <= set(c) for c in report["checks"])


def test_the_unverified_transcript_is_retained(failed_job):
    assert failed_job.transcript
    assert "# Class Transcript" in failed_job.transcript
    assert any(w in failed_job.transcript for w in _VOCAB)  # the chunks that did work


def test_the_api_surfaces_both_over_http(failed_job):
    r = client.get(f"/jobs/{failed_job.id}", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["verified"] is False
    assert body["transcript"]
    assert body["report"]["ok"] is False
    # The client prints these; before this design the list was always empty.
    assert body["report"]["failures"]


# ------------------------------------------ a passing job is unaffected


def test_a_verified_job_still_reports_verified(tone_wav, monkeypatch):
    import shutil

    class GoodTranscriber:
        def transcribe(self, audio_path: str):
            from tscribe.core.class_pipeline.splitter import probe_duration

            dur = probe_duration(audio_path)
            return _words(max(1, int(120 * dur / 60.0))), None

    config.transcriber_factory = lambda cfg: GoodTranscriber()
    monkeypatch.setattr(jobs, "_download_url", lambda url, dest, cfg: shutil.copy(tone_wav, dest))
    job = _wait(
        jobs.submit(TEST_CFG, audio_url="https://example.com/tone.wav", source_name="tone.wav").id
    )
    assert job.status == "done", job.error
    assert job.verified is True
    assert job.report["ok"] is True
