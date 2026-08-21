# Unit 06 — Bootstrap Command

## Objective
Implement `tscribe bootstrap` — a single command that takes a fresh clone to a running, verified instance (server or client), replacing carnyx's manual systemd-plus-Cloudflare-Tunnel runbook.

## Context
- Spec: `SPEC.md` §3
- Precedent to consult, do NOT re-derive: the `lsbx` bootstrap pattern in `lufs-audio/lufs-sandbox-provider`/`lsbx-cli`. This unit's author must read that implementation before writing this one — its mechanics were not part of this spec's recon pass, and this document does not fake familiarity with them.
- Source (read-only): `danialrami/tscribe-class-carnyx/deploy/README.md` — what bootstrap replaces
- Unit 01 (config bootstrap writes/validates), Unit 05 (self-test pattern to borrow)

## Acceptance criteria
- [ ] Installs/verifies dependencies (`uv sync`), confirms `ffmpeg`/`ffprobe` on PATH, fails with a specific actionable message if missing.
- [ ] Interactively or via flags (`--json` for headless/agent use) writes a valid `tscribe.yaml`; generates an API key for server mode if none provided.
- [ ] `--selftest` runs a tiny synthetic end-to-end job and reports pass/fail.
- [ ] Server mode on Linux: optionally scaffolds a systemd unit file as a disk artifact (doesn't install/enable it — that stays an explicit, privileged, separate step).
- [ ] Idempotent: re-running doesn't corrupt an existing valid config; asks before overwriting, or `--force` skips the prompt.
- [ ] Uses Unit 07's exit codes — unreachable remote endpoint exits `BACKEND_UNREACHABLE`, not a generic failure.

## Interface contract
```text
tscribe bootstrap [--mode server|client] [--backend local|remote] [--endpoint <url>]
                   [--force] [--json] [--selftest]
```
Exit 0 only when config is valid AND (if `--selftest` passed) the synthetic job round-trips and verifies.

## Boundaries — do NOT touch
- Does not implement server/client logic itself (Units 03/04) — configures and smoke-tests only.
- Does not automate privileged system operations (systemd install, firewall, Cloudflare Tunnel provisioning).

## Output
- `src/tscribe/bootstrap.py`, `tests/test_bootstrap.py`
- Commit style: `feat(bootstrap): implement self-provisioning bootstrap command with selftest`

## Verification
- `pytest tests/test_bootstrap.py`
- Manual: `tscribe bootstrap --backend local --selftest` on a clean checkout ends in exit 0 with a working config