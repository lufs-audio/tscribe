"""RemoteServerClient — an HTTP client of Unit 03's exact /jobs contract.

Used when `backend: remote` and `remote.kind: tscribe_server`: this client
does not run any transcription itself. It submits jobs to a real tscribe
server (possibly this same codebase running `mode: server` elsewhere,
including a future migrated tscribe-class-carnyx) and polls for the result,
byte-for-byte against the contract documented in SPEC.md Unit 03.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from tscribe.config import RuntimeConfig

from .base import AudioSource, ClientBase, JobHandle, JobResult


class RemoteServerClient(ClientBase):
    """backend: remote, endpoint is a tscribe server — HTTP against Unit 03's
    /jobs contract."""

    def __init__(self, cfg: RuntimeConfig, timeout_s: float = 60.0) -> None:
        remote = cfg.remote
        if remote is None:
            raise ValueError("RemoteServerClient requires a 'remote:' section in tscribe.yaml.")
        self.cfg = cfg
        self.base_url = remote.endpoint.rstrip("/")
        self.api_key_env = remote.api_key_env
        self.timeout_s = timeout_s

    def _headers(self) -> dict:
        return {"X-API-Key": os.environ.get(self.api_key_env, "")}

    def submit(self, source: AudioSource) -> JobHandle:
        """Uploads the local file directly via POST /jobs/upload.

        (POST /jobs with drive_file_id/audio_url is also part of Unit 03's
        contract, but a local client submitting a local file has no Drive
        file id or public URL to give it — upload is the only source shape
        that makes sense from this side.)
        """
        with open(source.path, "rb") as f:
            resp = requests.post(
                f"{self.base_url}/jobs/upload",
                headers=self._headers(),
                files={"file": (Path(source.path).name, f, "audio/wav")},
                timeout=self.timeout_s,
            )
        resp.raise_for_status()
        data = resp.json()
        return JobHandle(id=data["id"])

    def poll(self, handle: JobHandle) -> JobResult:
        resp = requests.get(
            f"{self.base_url}/jobs/{handle.id}",
            headers=self._headers(),
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        return JobResult(
            status=data["status"],
            verified=data["verified"],
            transcript=data.get("transcript"),
            report=data.get("report"),
            error=data.get("error"),
        )
