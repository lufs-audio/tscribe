# Unit 04 — Client Mode

## Objective
Implement `mode: client` — the same clone, pointed at either a remote `tscribe` server (thin HTTP client) or running the transcription pipeline entirely in-process (no server involved) — restoring the standalone personal CLI that `tscribe-transcription-tool` provided (`record`, `transcribe`, `summarize`, `devices`).

## Context
- Spec: `SPEC.md` §3, §5
- Source (read-only): `danialrami/tscribe-transcription-tool` `cli.py`, `recorder.py`
- Unit 02 — in-process paths call it directly
- Unit 03 — remote-server path is an HTTP client of its contract
- Proposal doc item 1 ("same clone, different mode") and item 3 ("remote mode speaks the existing API contract")

## Acceptance criteria
- [ ] `mode: client` + `backend: local`: runs in-process via Unit 02, no HTTP, no server.
- [ ] `mode: client` + `backend: remote` pointed at a `tscribe` server: submits/polls via Unit 03's exact `/jobs` contract.
- [ ] `mode: client` + `backend: remote` pointed directly at a bare LiteLLM/whisper endpoint: runs Unit 02's pipeline in-process, remote endpoint used only for the whisper call (this is "carnyx today," minus the HTTP job layer).
- [ ] Config must explicitly disambiguate these two "remote" flavors (Open Question #1 in SPEC.md §10) — resolved together with Unit 01, not silently picked.
- [ ] Mic recording (`tscribe record`) with device enumeration (`tscribe devices`), matching source tool's device-id selection.
- [ ] Batch file transcription (`tscribe transcribe <files...>`), auto-converting non-WAV formats.
- [ ] `tscribe summarize` restores pipe-friendly stdin behavior.

## Interface contract
```python
class TscribeClient(Protocol):
    def submit(self, source: "AudioSource") -> "JobHandle": ...
    def poll(self, handle: "JobHandle") -> "JobResult": ...


class LocalClient(TscribeClient):
    """backend: local — calls Unit 02 in-process, no network."""


class RemoteServerClient(TscribeClient):
    """backend: remote, endpoint is a tscribe server — HTTP against Unit 03's contract."""


class RemoteInferenceClient(TscribeClient):
    """backend: remote, endpoint is a bare inference endpoint — Unit 02 in-process, remote inference only."""
```

## Boundaries — do NOT touch
- Does not reimplement transcription/verification (Unit 02) or the server contract (Unit 03).
- Does not implement `tscribe capture` (Unit 05) — live mic-to-transcript is this unit's; the verified multi-hour classroom rig is Unit 05's.
- Does not define the CLI argument parser itself (Unit 07 owns it) — exposes the three client classes for Unit 07 to wire up.

## Output
- `src/tscribe/client/{local.py,remote_server.py,remote_inference.py,recorder.py}`, `tests/client/`
- Commit style: `feat(client): implement local/remote-server/remote-inference client modes and restore recorder CLI`

## Verification
- `pytest tests/client/`
- Manual: `tscribe transcribe sample.wav` works locally with only `backend: local` configured; `tscribe devices` lists a real input device