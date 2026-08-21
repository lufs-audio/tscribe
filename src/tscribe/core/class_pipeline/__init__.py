"""Verified long-audio (class-length) transcription pipeline.

Ported from tscribe-transcription-tool.class_pipeline, verbatim in spirit and
near-verbatim in code: splits a long recording into chunks, transcribes them
in parallel through tscribe's Transcriber (remote-first, local fallback),
reassembles a single timestamped transcript, and *proves the result is
correct* against a declared contract before reporting success.

The contract is the point. A transcription that "ran" but silently dropped a
chunk or returned empty text is worse than one that failed loudly, because it
lies to whoever (or whatever agent) consumes it. Nothing in here reports
success it did not earn — see :mod:`tscribe.core.class_pipeline.verify`.

The corollary, learned the hard way in the source repo: a check must not
report *failure* it did not earn either. See verify.py's module docstring —
that history is exactly why this port changes as little of the algorithm as
possible.
"""

from .splitter import ChunkSpec, plan_boundaries, split_audio, probe_duration
from .verify import (
    ADVISORY_PREFIX,
    Contract,
    ContractViolation,
    VerifyReport,
    VerifyReport as Report,  # SPEC.md Unit 02 names it "Report"; alias, same type
    verify_run,
)
from .pipeline import transcribe_class, ClassResult

__all__ = [
    "ChunkSpec",
    "plan_boundaries",
    "split_audio",
    "probe_duration",
    "ADVISORY_PREFIX",
    "Contract",
    "ContractViolation",
    "VerifyReport",
    "Report",
    "verify_run",
    "transcribe_class",
    "ClassResult",
]
