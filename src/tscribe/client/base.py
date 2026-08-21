"""Shared client protocol and result types for mode: client.

Three implementations (SPEC.md Unit 04's interface contract), one shared
in-process base for the two that don't touch a network job queue:

- LocalClient           — backend: local, runs Unit 02 in this process.
- RemoteInferenceClient — backend: remote (kind: inference_endpoint), also
  runs Unit 02 in this process; only the whisper call inside it goes over
  the network (this is "carnyx today," minus the HTTP job layer).
- RemoteServerClient    — backend: remote (kind: tscribe_server), talks HTTP
  to a real Unit 03 server instead of running the pipeline itself.

LocalClient and RemoteInferenceClient share an implementation (_InProcessClient)
because the actual difference between them lives entirely in `cfg` — which
Unit 02/Transcriber already branches on — not in how this layer calls them.
Keeping them as separate, named classes (rather than one class doing both)
matches the documented interface contract and keeps each one's docstring
honest about which config shape it expects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class AudioSource:
    path: str


@dataclass
class JobHandle:
    id: str


@dataclass
class JobResult:
    status: str  # "done" | "failed" | "running" | "queued"
    verified: bool
    transcript: Optional[str] = None
    report: Optional[dict] = None
    error: Optional[str] = None


class TscribeClient(Protocol):
    def submit(self, source: AudioSource) -> JobHandle: ...
    def poll(self, handle: JobHandle) -> JobResult: ...


class ClientBase:
    """Adds a synchronous convenience wrapper on top of submit/poll — every
    client (in-process or HTTP) ends up needing "just run this and wait,"
    so it lives once here instead of three times."""

    def submit(self, source: AudioSource) -> JobHandle:  # pragma: no cover - overridden
        raise NotImplementedError

    def poll(self, handle: JobHandle) -> JobResult:  # pragma: no cover - overridden
        raise NotImplementedError

    def submit_and_wait(
        self, source: AudioSource, poll_interval_s: float = 1.0, timeout_s: float = 3600.0
    ) -> JobResult:
        handle = self.submit(source)
        deadline = time.monotonic() + timeout_s
        result = self.poll(handle)
        while result.status not in ("done", "failed"):
            if time.monotonic() > deadline:
                return JobResult(status="failed", verified=False,
                                  error=f"timed out after {timeout_s:.0f}s waiting on job {handle.id}")
            time.sleep(poll_interval_s)
            result = self.poll(handle)
        return result
