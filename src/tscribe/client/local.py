"""LocalClient — backend: local, the simplest of the three client shapes.

Runs tscribe.core.class_pipeline.transcribe_class directly in this process
(no HTTP, no job queue). The shared machinery for "run the pipeline
in-process and cache the result against a handle" lives in _InProcessClient
below because RemoteInferenceClient (remote_inference.py) needs the exact
same machinery — the only difference between the two is which RuntimeConfig
they're constructed with, and that distinction is resolved inside Unit 02's
Transcriber, not here. See base.py's module docstring for why they are kept
as separate, named classes rather than one class doing both.

Since transcribe_class is itself synchronous, `submit()` here runs the whole
job before returning — there's no separate "poll until done" the way
RemoteServerClient needs. The result is cached against the returned handle so
`poll()` can still be called (fulfilling the TscribeClient protocol) without
re-running anything.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from tscribe.config import RuntimeConfig
from tscribe.core.class_pipeline import ContractViolation, transcribe_class

from .base import AudioSource, ClientBase, JobHandle, JobResult


def _report_to_dict(report) -> dict:
    return {
        "ok": report.ok,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in report.checks],
        "failures": [c.name for c in report.failures],
    }


class _InProcessClient(ClientBase):
    """Shared submit/poll machinery for the two client shapes that run
    Unit 02's pipeline in this process rather than talking HTTP to a job
    queue. Imported by both local.py (LocalClient) and remote_inference.py
    (RemoteInferenceClient) — see each module's docstring for what, if
    anything, it adds on top of this.
    """

    def __init__(self, cfg: RuntimeConfig) -> None:
        self.cfg = cfg
        self._results: dict[str, JobResult] = {}

    def submit(self, source: AudioSource) -> JobHandle:
        job_id = uuid.uuid4().hex
        out_path = Path(tempfile.mkdtemp(prefix="tscribe_client_")) / "transcript.md"
        try:
            result = transcribe_class(source.path, str(out_path), self.cfg)
            self._results[job_id] = JobResult(
                status="done",
                verified=True,
                transcript=result.transcript,
                report=_report_to_dict(result.report),
            )
        except ContractViolation as e:
            # Same evidence-retention principle as Units 02/03: a failed
            # in-process job still returns its transcript and report.
            self._results[job_id] = JobResult(
                status="failed",
                verified=False,
                transcript=e.transcript,
                report=_report_to_dict(e.report) if e.report else None,
                error=f"ContractViolation: {e}",
            )
        except Exception as e:  # noqa: BLE001
            self._results[job_id] = JobResult(
                status="failed", verified=False, error=f"{type(e).__name__}: {e}"
            )
        return JobHandle(id=job_id)

    def poll(self, handle: JobHandle) -> JobResult:
        result = self._results.get(handle.id)
        if result is None:
            return JobResult(status="failed", verified=False, error="unknown job id")
        return result


class LocalClient(_InProcessClient):
    """backend: local — calls Unit 02 in-process, no network."""
