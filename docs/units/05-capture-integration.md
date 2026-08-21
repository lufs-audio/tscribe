# Unit 05 — Capture Integration

## Objective
Port `tscribe-capture-rig`'s verified-recording rig — segmented capture, per-segment verification, lossless finalize — into `tscribe capture` as cross-platform subcommands, preserving its silence-floor verification gate.

## Context
- Spec: `SPEC.md` §5
- Source (read-only): `danialrami/tscribe-capture-rig` — `verify_capture.py` (stdlib-only, port near-verbatim), `capture.sh`/`capture.bat` (consolidate), `finalize.sh`/`.bat`, `rig.conf`
- Related, do NOT conflate: `danialrami/lufs-recorder` is a separate, more general field-recorder primitive. This unit does not depend on it. Whether the two should converge later is Open Question #2 in SPEC.md §10 — not decided here.

## Acceptance criteria
- [ ] `verify_capture.py`'s checks ported near-verbatim: exists_nonempty, decodes, sample_rate, channels, duration_in_band, not_silence_floor (-60dB fail / -50dB warn, now config-exposed).
- [ ] `tscribe capture start` replaces `capture.sh`/`.bat`: one cross-platform Python implementation (PipeWire on Linux, DirectShow-equivalent on Windows), same segmented-WAV-with-reset-timestamps pattern, configurable segment length (default 900s).
- [ ] `tscribe capture verify <file>` replaces the standalone verifier CLI, same exit codes (0/2/64) folded into Unit 07's overall contract.
- [ ] `tscribe capture finalize` replaces `finalize.sh`/`.bat`: lossless concat + final verify, duration-preservation check (±5s) intact.
- [ ] The synthetic-source self-test pattern (`RIG_INPUT_OVERRIDE`, `CAPTURE_DURATION`) is ported — all 7 source test cases pass without a real microphone.
- [ ] A captured-and-finalized WAV is a valid input to `tscribe transcribe` — proving the two halves compose end to end.

## Interface contract
```python
class VerificationReport(TypedDict):
    ok: bool
    checks: list[dict]

def verify_capture(path: str, expected_seconds: float | None, final: bool = False) -> VerificationReport: ...

def capture_start(cfg: "CaptureConfig") -> "CaptureSession": ...
def capture_finalize(session_dir: str) -> str: ...
```
`CaptureConfig` lives in `tscribe.yaml` under a `capture:` key — `rig.conf`'s KEY=VALUE format is retired, not carried forward as a second format.

## Boundaries — do NOT touch
- Does not implement transcription (Unit 02) — hands off a file path only.
- Does not modify `tscribe-capture-rig`'s original files in place.
- Windows: port what can be verified by static review; do NOT claim execution-tested status without an actual Windows run.

## Output
- `src/tscribe/capture/{verify.py,session.py,finalize.py}`, `tests/capture/`
- Commit style: `feat(capture): port verified segmented-capture rig from tscribe-capture-rig as cross-platform subcommands`

## Verification
- `pytest tests/capture/`
- `capture start` (test seam) → `finalize` → `verify --final` → `transcribe`, all green