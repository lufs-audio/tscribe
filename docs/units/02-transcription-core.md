# Unit 02 — Transcription Core

## Objective
Port `tscribe-transcription-tool`'s `Transcriber`, `class_pipeline` (chunk/reassemble/verify), and `summarizer` into `tscribe` as its core library — the single place transcription and verification logic lives, consumed by both server mode (Unit 03) and client mode (Unit 04).

## Context
- Spec: `SPEC.md` §5
- Source (read-only): `danialrami/tscribe-transcription-tool` — `transcriber.py`, `class_pipeline/pipeline.py`, `class_pipeline/verify.py`, `summarizer.py`
- Unit 01's `RuntimeConfig`
- Carnyx's "evidence retained on failure, never discarded" principle (2026-08-06 incident) — a hard requirement

## Acceptance criteria
- [ ] `Transcriber` ported with both paths intact: remote-first via LiteLLM (config-driven, not hardcoded), automatic fallback to local faster-whisper.
- [ ] `class_pipeline` ported: silence-aware chunking, parallel transcription, reassembly.
- [ ] Verification (`Report`/`ContractViolation`) ported with existing checks intact — completeness, coverage, reassembly word-count integrity, speech-rate band — passing the source repo's tests (ported, not just the code).
- [ ] On verification failure, the unverified transcript AND full report are retained, never discarded — explicit test required, mirroring `test_failed_contract_keeps_evidence.py`.
- [ ] `summarizer` ported with LiteLLM summarization + Obsidian-frontmatter output intact.
- [ ] All existing metamorphic/property tests (`test_class_pipeline.py`, `test_speech_rate_contract.py`, `test_degenerate_output.py`, `test_tail_chunk_and_yield.py`) ported and passing.

## Interface contract
```python
class VerificationCheck(TypedDict):
    name: str
    ok: bool
    detail: str


class Report:
    ok: bool
    checks: list[VerificationCheck]
    failures: list[str]


class ContractViolation(Exception):
    transcript: str  # unverified, retained for audit
    report: Report


def transcribe(audio_path: str, cfg: RuntimeConfig) -> str: ...


def transcribe_class(input_path: str, output_path: str, cfg: RuntimeConfig) -> "ClassResult": ...


def summarize(transcript: str, cfg: RuntimeConfig, obsidian_format: bool = False) -> str: ...
```
Units 03 and 04 both call these — neither reimplements transcription or verification logic.

## Boundaries — do NOT touch
- Does not implement the HTTP server, job lifecycle, or Drive integration (Unit 03).
- Does not implement the CLI (Unit 07).
- Does not modify `danialrami/tscribe-transcription-tool` itself.

## Output
- `src/tscribe/core/transcriber.py`, `src/tscribe/core/class_pipeline/{pipeline.py,verify.py}`, `src/tscribe/core/summarizer.py`, `tests/core/`
- Commit style: `feat(core): port transcription, chunked-verify pipeline, and summarizer from tscribe-transcription-tool`

## Verification
- `pytest tests/core/` — full ported suite green, including evidence-retention test
- Manual: a deliberately corrupted-chunk run raises `ContractViolation` carrying both transcript and report