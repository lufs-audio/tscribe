# Unit 03 — Server Mode (HTTP API)

## Objective
Port `tscribe-class-carnyx`'s FastAPI server, job lifecycle, and Drive integration into `tscribe`'s `mode: server` path, preserving the existing `/jobs` API contract exactly.

## Context
- Spec: `SPEC.md` §5, §6
- Source (read-only, carnyx stays untouched production): `danialrami/tscribe-class-carnyx` — `server/app.py`, `server/jobs.py`, Drive integration modules
- Unit 02's core library — this unit calls it, never reimplements it
- Unit 01's `RuntimeConfig` — `mode: server` gates this path
- Proposal doc: "The API contract is solid and reusable as-is... should be kept, not redesigned."

## Acceptance criteria
- [ ] All five endpoints ported with identical shapes: `GET /healthz`, `GET /version`, `POST /jobs`, `POST /jobs/upload`, `GET /jobs/{id}`.
- [ ] `X-API-Key` auth enforcement preserved (key sourced from Unit 01's config/env).
- [ ] Job lifecycle preserved: in-memory store, one thread per job, `queued -> running -> done|failed`.
- [ ] `verified` bool remains the sole authoritative gate; on `failed`, `transcript`/`report` still populated — evidence never discarded (port the explicit test for this).
- [ ] Drive integration ported: service-account download, public-URL pull, resumable write-back, non-destructive reparenting, retry-with-reset resilience.
- [ ] `tscribe serve` CLI invocation (wired in Unit 07) starts this server when `mode: server`.
- [ ] Existing carnyx test suite (`test_app.py`, `test_drive.py`, `test_failed_job_keeps_evidence.py`) ported and green.

## Interface contract
```text
POST /jobs
{ "drive_file_id"?: str, "audio_url"?: str, "dest_folder_id"?: str,
  "move_file_ids"?: [str], "source_name"?: str }
-> { "id": str, "status": "queued" }

GET /jobs/{id}
-> { "id", "status", "verified", "source", "created_at", "updated_at",
     "transcript", "report": {"ok", "checks", "failures"}, "error",
     "transcript_file_id", "upload_error", "moved", "move_errors" }
```
Byte-for-byte the same JSON shapes carnyx serves today. Any client written against carnyx's current API must work against this server unmodified — the hard constraint of the whole unit.

## Boundaries — do NOT touch
- Does not implement transcription/verification logic itself — calls Unit 02.
- Does not implement `mode: client` (Unit 04).
- Does not modify `danialrami/tscribe-class-carnyx`.

## Output
- `src/tscribe/server/{app.py,jobs.py,drive.py}`, `tests/server/`
- Commit style: `feat(server): port carnyx HTTP API, job lifecycle, and Drive integration to server mode`

## Verification
- `pytest tests/server/`
- Side-by-side smoke test against carnyx: identical job submitted to both, diff `GET /jobs/{id}` responses for shape parity