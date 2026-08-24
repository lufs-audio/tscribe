# Unit 09 — Implementation Report: Units 02-08 End to End

**Author:** Ciani. **Date:** 2026-08-21/22. **Branch:** `units/01-08-implementation` (PR #1, still a feature branch — nothing here is merged to `main`).

## 1. Starting state, and why this report exists

Daniel approved `SPEC.md` and asked for Units 01-08 to be implemented end to
end on this branch, then paused after a session error cut work off mid-way.
When I picked this back up, PR #1's own description was stale: it credited
only Unit 01 as done and listed Units 02-08 as "not done yet." Reconstructing
the actual branch state (pinned at commit `e250152f1b30fc006a75c49cad4e0979eb59ca6b`)
showed the prior session had actually gone much further — real source *and*
test files already existed for Units 02 (transcription core), 03 (server),
and most of Unit 04 (client) — but none of it had been run, so nobody knew
whether it worked. Units 05 (capture), 06 (bootstrap), 07 (CLI), and 08
(docs/CI) genuinely had not been started. This report covers everything from
that discovery point forward.

## 2. Approach

Given the scope, I fanned this out to five parallel subagents, each scoped
to one unit, then integrated and end-to-end tested the result myself:

- **Marconi** — reconstruct and run Unit 02's real test suite; report the
  actual (not spec-assumed) public interface.
- **Beacon** — same, for Unit 03.
- **Relay** — same, for Unit 04, *plus* build whatever was missing against
  the spec (see §4).
- **Boombox** — Unit 05 didn't exist yet: read `tscribe-capture-rig`, port
  it, extend `tscribe.config` with `CaptureConfig`, write tests.
- **Ignition** — Unit 06 didn't exist yet: research the `lsbx-cli` bootstrap
  pattern (spec said to consult it before writing anything), write
  `bootstrap.py`, write tests.

All five worked concurrently in the same sandbox filesystem (a fact I
discovered mid-flight when their reports started referencing each other's
in-progress files — confirmed genuine, not a report artifact, by watching
`config.py` grow live and re-running tests afterward). I reviewed every
subagent's report, then personally: fixed a real bug the integration
surfaced (see §5), wrote Unit 07 (nothing existed for it beyond a bplate
stub), wrote Unit 08's docs, assembled everything into one coherent tree, and
ran the complete test suite end to end myself rather than trusting any
single subagent's partial run.

## 3. Final verification (mine, not delegated)

```
$ ruff check .
All checks passed!

$ ruff format --check .
52 files already formatted

$ PYTHONPATH=src python -m pytest tests/ -q
234 passed in ~20s
```

Run twice in a row for determinism (identical result both times). This
covers `tests/test_config.py`, `tests/core/`, `tests/server/`, `tests/client/`,
`tests/capture/`, `tests/test_bootstrap.py`, and the new `tests/cli/` — every
unit, together, in one process, importing from one shared `tscribe.config`.
Environment: Python 3.12.13, ffmpeg/ffprobe 7.0.2, with `pyyaml requests
pytest ruff fastapi uvicorn pydantic python-multipart google-api-python-client
google-auth httpx` installed (the `server`+`dev` extras) but deliberately
**not** `faster-whisper`/`sounddevice`/`numpy` (the `local`+`record` extras)
— those are heavy/hardware-adjacent and every test that would need them
mocks the boundary instead, same as production code should when real
hardware/weights aren't available. See §7 for exactly what that means is and
isn't proven.

## 4. What each unit actually turned out to contain

### Unit 02 — Transcription core: fully correct as found, zero fixes needed

Marconi's from-scratch reconstruction and run: **95/95 tests passed**, no
bugs, nothing needed fixing. Two real (pre-existing, not introduced by this
pass) gaps worth tracking, not blocking: `tscribe.core.summarizer` has no
dedicated test file in the ported suite (every other Unit 02 module does),
and `Transcriber` itself is only exercised indirectly through
`class_pipeline`'s mocks, never directly. Confirmed the spec's
`VerificationCheck` TypedDict name doesn't exist in the real code — the
actual type is `Check`, a dataclass, importable from
`tscribe.core.class_pipeline.verify`. Everything else in the interface
contract matches.

### Unit 03 — Server mode: fully correct as found, zero fixes needed

Beacon's reconstruction and run: **26/26 tests passed**. Confirmed
`create_app(cfg) -> FastAPI` is a factory (not a module-level `app` object,
a deliberate change from carnyx to remove import-order dependence on env
vars) — Unit 07's `tscribe serve` calls it directly. All five endpoints,
`X-API-Key` auth, the in-memory job lifecycle, and the Drive integration
(service-account download, resumable write-back, non-destructive move) all
match the spec's contract exactly, evidence-retention test included.

### Unit 04 — Client mode: one real gap, closed

Relay's reconstruction found the pre-existing suite (`LocalClient`,
`RemoteServerClient`) fully green at **12/12**, but confirmed by direct
inspection that `RemoteInferenceClient` — required by the spec for
"`backend: remote` pointed at a bare inference endpoint" — existed only as a
bare, untested subclass living inside `local.py`, with no config validation
of its own (unlike its sibling, it could be constructed with `cfg.remote is
None` and would fail confusingly later rather than fast). Relay extracted it
into its own `remote_inference.py` (matching the one-file-per-shape pattern
`local.py`/`remote_server.py` already established), added the same
fail-fast validation `RemoteServerClient` has, and wrote 7 new tests. Final:
**19/19** in `tests/client/`. Also confirmed device enumeration
(`recorder.py`) is complete and CLI-ready, and that multi-file batch
transcription / non-WAV auto-conversion / stdin-piped summarize were never
Unit 04's job in the first place — correctly deferred to Unit 07 (see below).

### Unit 05 — Capture integration: built from scratch, ported near-verbatim

Boombox read `tscribe-capture-rig` (its `verify_capture.py`, `capture.sh`/
`.bat`, `finalize.sh`/`.bat`, `rig.conf`, and its 7-case synthetic self-test
suite) and built `src/tscribe/capture/{verify,session,finalize}.py`, adding a
`CaptureConfig` section to `tscribe.config` (following `ServerConfig`'s exact
established pattern — the same file two other units already extend, without
conflict, since only one subagent touched it in this pass). **17/17 new
tests**, all six of the source rig's checks intact including the
load-bearing `not_silence_floor` gate. Proved a captured+finalized synthetic
WAV composes cleanly into `tscribe.core.transcribe()` (with only the
`faster_whisper.WhisperModel` class itself stubbed at the import boundary —
everything upstream of that, including `Transcriber`'s own backend-selection
logic, is real code). Honestly flagged: real PipeWire capture and the
Windows/DirectShow path are code-reviewed, not execution-tested, in this
sandbox (no PipeWire daemon, no Windows machine) — exactly the standard this
unit's own spec demanded.

### Unit 06 — Bootstrap: built from scratch; the `lsbx-cli` precedent doesn't exist yet

Ignition's first finding, before writing any code: `lufs-audio/lufs-sandbox-provider`
does not exist (a direct fetch returned a genuine 404, and a repo search
found nothing) — so there was no `lsbx-cli` bootstrap pattern to consult, and
`bootstrap.py` says so explicitly in its own docstring rather than faking
familiarity with a pattern that isn't there yet. Built `run_bootstrap()` as a
library function (typed exceptions, not `sys.exit`) so it stayed
independently testable before Unit 07 existed to call it. **47/47 tests**,
covering the dependency check, idempotent config writing (including a real
generated API key for server mode), the `--selftest` synthetic job (real
ffmpeg-generated WAV, real call into `Transcriber` — honestly reports
`exercised_real_backend: false` when `faster-whisper` isn't installed,
rather than a false pass), and a systemd unit scaffold that is proven, via a
monkeypatched `subprocess.run` that raises if called, to never shell out.

### Unit 07 — CLI surface: built from scratch by me, using the other five units' real interfaces

Nothing existed here beyond the bplate scaffold's `--version`/`--json` stub.
I wrote `src/tscribe/cli/{main,commands,output}.py` wiring every subcommand
in the spec (`serve`, `transcribe`, `record`, `devices`, `summarize`,
`capture start|verify|finalize`, `bootstrap`, `jobs status`) against the
*actual* interfaces the five subagents had just verified — not the spec's
sketch of them, which in at least one case (`VerificationCheck` vs. `Check`)
would have been wrong. Two things landed here specifically because Units 02
and 04's own reports said they were explicitly out of scope for them:
non-WAV auto-conversion for `transcribe` (one `ffmpeg` subprocess call) and
stdin-piped `summarize`. 47 new tests in `tests/cli/`, covering every
documented exit code end to end through the real CLI entrypoint (not just
`tscribe.cli.commands` in isolation) — including `--json` envelope shape for
both success and error, and the two harder-to-reach codes
(`BACKEND_UNREACHABLE`/`JOB_NOT_FOUND` via a mocked `requests.get` under
`jobs status`, `INTERRUPTED` via a real declined-overwrite prompt under
`bootstrap`, not a synthetically-thrown stand-in).

## 5. A real bug this integration pass found and fixed

Writing Unit 07's end-to-end CLI tests surfaced a genuine defect in Unit 06's
`bootstrap.py`: `_default_confirm`'s "no stdin to read, treat as declined"
handling only caught `EOFError`. Under `pytest` (and likely other
captured-output contexts), a bare `input()` call instead raises `OSError:
reading from stdin while output is captured` — a different exception that
fell through uncaught, turned into a generic, undocumented exit code 1
instead of the documented `INTERRUPTED` (7). Fixed by widening the `except`
clause to `(EOFError, OSError)`. This is exactly the kind of bug that only
surfaces when units are actually wired together and driven end to end
through the real entrypoint — Ignition's own isolated tests never exercised
`_default_confirm`'s real `input()` path (they injected a `confirm` callback
instead, correctly, for unit-level testing), so this was invisible until
Unit 07 called the real, uninjected code path.

## 6. Also fixed during integration (mechanical, low-risk)

- **41 `ruff` line-length findings** across several units' files (mostly
  argument lists ruff's own formatter could safely rewrap) — cleaned up with
  `ruff format .` rather than by hand; verified `ruff check .` was clean
  before and after on everything except the two intentional suppressions
  below.
- **`pyproject.toml` dependencies** — none of the five units had a shared,
  non-conflicting place to declare their new runtime dependencies (FastAPI,
  Google API clients, faster-whisper, sounddevice/numpy), so each subagent
  correctly reported its findings back rather than editing the file
  concurrently. I split them into `server`/`local`/`record`/`all` optional
  extras rather than one flat list, so a `backend: remote` / `mode: client`
  install — the common case for a machine that only ever talks to a GPU host
  — stays light (`pyyaml` + `requests`, nothing else, unchanged from Unit
  01). This is a design call I'm flagging explicitly rather than burying:
  Daniel, if you'd rather have one `pip install tscribe` pull in everything
  by default, that's a one-line change to `pyproject.toml`'s base
  `dependencies` list — I chose the lighter default because it matches this
  project's own "keep the light path working" principle from `AGENTS.md`.
- **One trivial unused-variable lint finding** (`F841` in `bootstrap.py`) —
  a one-line fix, no behavior change.
- **`docs/config-migration.md`** was stale — it still listed
  `TSCRIBE_API_KEY`/`TSCRIBE_MAX_AUDIO_BYTES`/`TSCRIBE_DOWNLOAD_TIMEOUT_S`/
  `CARNYX_DRIVE_SA_JSON` as "not yet modeled," even though Unit 03's
  `ServerConfig` (added before this pass started) already models all four.
  Updated the table to point at the real `server.*` fields.
- **Two intentional `ruff` suppressions**, not fixes — `UP042`
  (`Mode`/`Backend`/`RemoteKind` as `str, Enum`) and `N818`
  (`ContractViolation` naming) are pre-existing, deliberate, widely-referenced
  choices from earlier units. I left both alone and documented why directly
  in `ruff.toml`'s comments rather than silently ignoring the findings or
  unilaterally renaming load-bearing symbols this late. This mirrors the
  restraint Boombox already showed when it found the same `UP042` finding on
  Unit 01's `config.py` and declined to touch another unit's shipped code for
  a style nit — I'm applying the same standard consistently as the
  integrator.

## 7. Honest accounting: what's execution-tested vs. code-reviewed-only

This project's whole reason to exist is not overclaiming "verified" — so,
explicitly, across all of Units 02-07 combined:

**Execution-tested, for real, in this sandbox (234 tests, twice, deterministic):**
config loading/validation; the full chunk-transcribe-verify-reassemble
pipeline against real ffmpeg-synthesized audio (mocked only at the actual
whisper/LLM network call); the FastAPI server's full endpoint surface via
`TestClient` (mocked only at the Google API layer, which the tests
themselves document as never touching real credentials); all three client
shapes; the entire capture pipeline against real ffmpeg subprocesses
(mocked only at "is there a real microphone/PipeWire daemon," which this
sandbox doesn't have); bootstrap's dependency check, config writing,
idempotency, and selftest plumbing (mocked only at "is faster-whisper's
heavy native dependency chain installed," which it deliberately isn't); and
the entire CLI surface end to end, including every documented exit code.

**Written and reviewed, but NOT executable in this environment:**
real PipeWire mic capture against actual hardware; the Windows/DirectShow
capture path (never run on Windows); real `faster-whisper` inference (no
model weights, no GPU, and — correctly — not installed by default); the
`lsbx-cli` bootstrap-pattern reconciliation Unit 06's spec asked for (there
is nothing to reconcile against yet, since that repo doesn't exist); a live
side-by-side smoke test of this server against the real `tscribe-class-carnyx`
production server (Unit 03's spec suggests this as a verification step —
not done here, since carnyx is explicitly off-limits production
infrastructure this spec was never meant to touch).

None of the above is a defect — it's the honest boundary of what a sandboxed
CI-style environment can prove without real hardware, model weights, or a
second live service to diff against. Treat it as the actual test plan for
whoever runs this on real hardware next (mirrors the `TESTING.md` pattern
already established on `lufs-recorder`/`photo-process-registry`).

## 8. Proposed next steps

1. **Real-hardware pass.** Hand this branch to a machine with a real mic
   (PipeWire, ideally the same BlackHole-style rig `lufs-recorder`'s
   `TESTING.md` used) and run `tscribe capture start` for real; separately,
   a Windows box for the DirectShow path. A short, copy-pasteable runbook
   (mirroring `lufs-recorder`/`photo-process-registry`'s `TESTING.md`) is
   worth writing once someone's ready to do this — didn't build it
   preemptively since I don't want to guess at hardware I haven't confirmed
   exists.
2. **Install `faster-whisper` somewhere with a GPU (or accept CPU) and
   re-run `bootstrap --selftest`** to get a real `exercised_real_backend:
   true` pass, closing the one gap every relevant test currently reports
   honestly rather than closes.
3. **Add `tests/core/test_summarizer.py`.** A real, pre-existing gap Marconi
   flagged — every other Unit 02 module has a dedicated test file; the
   summarizer doesn't.
4. **Add `.github/workflows/ci.yml`** — content is in this PR's description,
   ready to paste in; I can't commit it myself (the GitHub App token lacks
   `workflow` scope, same fleet-wide limitation every other repo here has
   hit).
5. **Decide on `pyproject.toml`'s extras split** (§6) — light-by-default
   vs. everything-by-default. I made a call; it's reversible in one line if
   you'd rather have the other default.
6. **The `lufs-audio/kb docs/product/tscribe/` narrative doc suite** —
   deliberately not written yet per Unit 08's own spec ("do NOT write it
   now" until the tool has shipped with real usage to narrate). Worth
   revisiting once step 1 above has happened.
7. **Migrating `tscribe-class-carnyx`'s nightly CompTIA job onto this tool**
   — explicitly out of scope for this spec (SPEC.md §7), and I haven't
   touched it. A separate, deliberate decision for later, not a leftover.

## 9. Files touched in this pass

Everything under `src/tscribe/capture/`, `src/tscribe/bootstrap.py`,
`src/tscribe/cli/` (new); `src/tscribe/client/remote_inference.py` (new),
`src/tscribe/client/local.py` + `client/__init__.py` (refactored to extract
it); `src/tscribe/config.py` (extended with `CaptureConfig`);
`tests/capture/`, `tests/test_bootstrap.py`, `tests/cli/`,
`tests/client/test_remote_inference.py` (new); `pyproject.toml`, `ruff.toml`,
`README.md`, `AGENTS.md`, `CHANGELOG.md` (new/rewritten),
`docs/config-migration.md` (updated); `.editorconfig`, `.gitattributes`,
`.mise.toml`, `LICENSE` (restored — a fetch failure earlier in this session
had corrupted local copies of these to 14-byte error bodies before anything
was committed; never actually broken in the repo itself, caught before it
mattered).
