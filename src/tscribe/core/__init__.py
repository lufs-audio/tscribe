"""tscribe.core — the transcription + verification library.

Ported from tscribe-transcription-tool. This is the single place transcription
and verification logic lives; Unit 03 (server) and Unit 04 (client) both call
into this package and never reimplement any of it.
"""

from .class_pipeline import ClassResult, ContractViolation, Report, transcribe_class
from .summarizer import Summarizer, summarize
from .transcriber import Transcriber, transcribe

__all__ = [
    "Transcriber",
    "transcribe",
    "Summarizer",
    "summarize",
    "ClassResult",
    "ContractViolation",
    "Report",
    "transcribe_class",
]
