"""Orchestrator: split -> parallel transcribe -> reassemble -> VERIFY.

Ported from tscribe-transcription-tool.class_pipeline.pipeline. This is the
only place the steps are wired together, and it ends with the contract. If
verification fails it raises — it never hands back a transcript it could not
prove. That is the whole point.

What it does *not* do is destroy the evidence on the way out. The raised
:class:`ContractViolation` carries the full report and the unverified
transcript, so a failed class can be audited without spending another
multi-hour run to see what happened. ``output_path`` is still only written on
success, so nothing downstream can mistake an unproven transcript for a
proven one.

Signature change from the source repo: ``transcribe_class`` now takes
``cfg: RuntimeConfig`` (Unit 01) as its third positional argument, matching
SPEC.md's Unit 02 interface contract, and derives ``chunk_minutes`` /
``workers`` / ``snap_window_s`` from ``cfg.chunking`` instead of individual
kwargs. ``transcriber`` is now optional — when omitted, a real
``tscribe.core.transcriber.Transcriber(cfg)`` is constructed — so callers
that just want "transcribe this file, verified" don't have to know the
Transcriber class exists, while tests can still inject a mock exactly as the
source repo's tests did.
"""

from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol

from tscribe.config import RuntimeConfig

from .splitter import ChunkSpec, probe_duration, split_audio
from .verify import Contract, VerifyReport, verify_run


class TranscriberLike(Protocol):
    def transcribe(self, audio_path: str): ...


@dataclass
class ClassResult:
    output_path: str
    chunks: List[ChunkSpec]
    chunk_texts: List[str]
    transcript: str
    report: VerifyReport
    total_s: float


def _fmt_stamp(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def reassemble(chunks: List[ChunkSpec], texts: List[str], source: str) -> str:
    """Concatenate chunk transcripts under real per-chunk timestamp headers.

    Headers use each chunk's actual start time (not an assumed N*chunk_minutes),
    so silence-snapped boundaries are reflected honestly.
    """
    lines = [f"# Class Transcript", f"", f"**Source:** {source}", f""]
    for ch, txt in zip(sorted(chunks, key=lambda c: c.index), texts):
        lines.append(f"## [{_fmt_stamp(ch.start_s)}]")
        lines.append("")
        lines.append(txt.strip())
        lines.append("")
    return "\n".join(lines)


def transcribe_class(
    input_path: str,
    output_path: str,
    cfg: RuntimeConfig,
    *,
    transcriber: Optional[TranscriberLike] = None,
    silence_aware: bool = True,
    contract: Optional[Contract] = None,
    keep_chunks: bool = False,
    chunk_dir: Optional[str] = None,
) -> ClassResult:
    """Run the full verified pipeline. Raises ContractViolation if the result
    cannot be proven correct.

    On violation the exception carries ``.report`` and ``.transcript`` — the
    gate is unchanged, but the failure is auditable instead of opaque.
    """
    if transcriber is None:
        from tscribe.core.transcriber import Transcriber  # avoid import cycle

        transcriber = Transcriber(cfg)

    input_path = str(input_path)
    total_s = probe_duration(input_path)

    tmp_dir = Path(chunk_dir) if chunk_dir else Path(tempfile.mkdtemp(prefix="tscribe_class_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        chunks = split_audio(
            input_path, tmp_dir,
            chunk_minutes=cfg.chunking.minutes,
            snap_window_s=cfg.chunking.snap_window_s,
            silence_aware=silence_aware,
        )

        texts: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, cfg.chunking.workers)) as pool:
            futures = {
                pool.submit(transcriber.transcribe, ch.path): ch.index
                for ch in chunks
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                result = fut.result()  # let exceptions propagate -> hard fail
                text = result[0] if isinstance(result, tuple) else result
                texts[idx] = text or ""

        ordered_texts = [texts[i] for i in range(len(chunks))]
        transcript = reassemble(chunks, ordered_texts, Path(input_path).name)

        report = verify_run(
            input_path=input_path,
            chunks=chunks,
            chunk_texts=ordered_texts,
            assembled=transcript,
            total_s=total_s,
            contract=contract,
        )
        # Proven, or it doesn't ship. The transcript rides along on the exception
        # so the failure can be read; it is still not written to output_path.
        report.raise_if_failed(transcript=transcript)

        Path(output_path).write_text(transcript)
        return ClassResult(
            output_path=output_path,
            chunks=chunks,
            chunk_texts=ordered_texts,
            transcript=transcript,
            report=report,
            total_s=total_s,
        )
    finally:
        if not keep_chunks and not chunk_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
