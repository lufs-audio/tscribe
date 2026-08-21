# `tscribe` — Agent-First Transcription Primitive (Client + Server + Bootstrap)

> A single, config-driven tool that turns audio into a verified transcript — locally or against a remote GPU backend — replacing three hand-built, single-purpose repos with one LUFS Primitive.

---

## 1. Problem & Mission

Today, "transcribe some audio" is solved three different ways in three different repos, each hand-built for one context:

- **`tscribe-class-carnyx`** — a bespoke HTTP server, hardcoded to the nightly CompTIA class pipeline, remote-backend-only in practice, no CLI, no config file, manual systemd deploy.
- **`tscribe-transcription-tool`** — the actual transcription+verification library carnyx depends on, but ALSO a full standalone personal CLI (`record`/`transcribe`/`summarize`/`devices`) most people don't know exists because it's buried as a dependency.
- **`tscribe-capture-rig`** — a USB-mic-and-flash-drive recording rig, hand-built for one classroom, that solves the same "prove this actually captured sound" problem the other two solve for transcription — independently, without sharing code.

Three repos, one underlying shape (record → transcribe → verify → deliver), zero shared config, zero shared CLI. That gap is exactly what blocks reuse outside the CompTIA context — Daniel's stated first target is therapy-session audio (his mic + call audio, two channels), which today would mean hand-building a fourth bespoke integration.

`tscribe` (repo: `lufs-audio/tscribe`) is the merge: one clone, one config file, one CLI, that can run as a server (what carnyx does today) or a client (local or pointed at a remote server), with the same verified-transcript contract either way.

---

## 2. Source Material & Boundary Conditions

This spec is a direct continuation of `danialrami/tscribe-class-carnyx/docs/proposals/01-generic-tscribe-tool.md` (2026-08-21) — read that document's "Confirmed current state" section for the full carnyx architecture audit; it is not repeated in full here (point, don't paste).

**Hard boundary, non-negotiable:** `tscribe-class-carnyx` is live production code serving the nightly CompTIA SRS-flashcards pipeline (carnyx Run A/B). Nothing in this spec touches that repo. `tscribe` proves itself standalone; migrating carnyx's nightly job onto the new tool is an explicit, separate, future decision — see §7 Non-Goals.

**Also non-negotiable:** the other two source repos (`tscribe-transcription-tool`, `tscribe-capture-rig`) are read-only inputs to this port. `tscribe` is a NEW repo — nothing in this spec modifies the old ones. They stay exactly as they are until Daniel separately decides to archive/deprecate/keep them, which is out of scope here.

---

## 3. Core Nouns

- **Job** — one audio-in, verified-transcript-out unit of work. The thing carnyx calls a job today; the concept survives unchanged.
- **Mode** — `server` | `client`. What role this clone plays. Same clone, same entrypoint, different config.
- **Backend** — `local` | `remote`. Where the actual whisper/LLM compute happens. `remote` speaks LiteLLM to a carnyx-style GPU host; `local` runs faster-whisper in-process. Both paths already exist today in `tscribe-transcription-tool`'s `Transcriber` — this spec ports and exposes the choice via config instead of a code constant.
- **Config** (`tscribe.yaml`) — the file that sets mode, backend, endpoint, and secrets references. Does not exist today (carnyx is env-var-only); this is new.
- **Contract / Report** — the verification checks and their pass/fail evidence, ported from `class_pipeline`'s `Report` / `ContractViolation`. Evidence is retained on failure, never discarded — the load-bearing lesson from carnyx's 2026-08-06 incident, preserved exactly.
- **Bootstrap** — the self-provisioning command that takes a fresh clone to a running instance without a hand-written systemd runbook. New; mirrors the `lsbx` bootstrap pattern (Unit 06 points at it rather than re-deriving it).
- **Capture** — the optional pre-stage: mic/USB-rig recording that produces a verified raw audio file a Job can consume. Ported from `tscribe-capture-rig`.

---

## 4. Architecture & Data Flow

```
                    ┌─────────────────────────────┐
                    │      tscribe.yaml (config)   │
                    │   mode: server | client      │
                    │   backend: local | remote    │
                    └───────────────┬─────────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     │             CLI               │
                     │   (Unit 07 — --json envelope, │
                     │    deterministic exit codes)  │
                     └──────────────┬──────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
     mode: server            mode: client            tscribe capture
      (Unit 03)             (Unit 04, any backend)       (Unit 05)
              │                     │                      │
              ▼                     │                      ▼
   ┌──────────────────┐             │            ┌──────────────────────┐
   │   HTTP SERVER      │             │            │    CAPTURE RIG        │
   │ (FastAPI, ported   │             │            │ mic -> segmented,     │
   │  from carnyx)       │             │            │ verified WAV          │
   │ /jobs /jobs/{id}    │             │            │ (ported from          │
   └─────────┬──────────┘             │            │  capture-rig)         │
             │                        │            └───────────┬───────────┘
             ▼                        ▼                        │
   ┌───────────────────────────────────────────┐               │
   │           TRANSCRIPTION CORE                │◀──────────────┘
   │        (Unit 02 — ported from               │   (a finalized capture
   │         tscribe-transcription-tool)          │    is valid job input)
   │  backend: local  -> faster-whisper           │
   │  backend: remote -> LiteLLM -> carnyx GPU    │
   │  chunk -> transcribe -> reassemble           │
   └────────────────────┬──────────────────────────┘
                         │
                         ▼
   ┌───────────────────────────────────────────┐
   │             VERIFICATION (Contract)         │
   │  coverage, completeness, WPM band,          │
   │  reassembly integrity                       │
   │  -> Report { ok, checks[], failures[] }     │
   └────────────────────┬──────────────────────────┘
                         │
                verified: true | false
             (evidence retained either way)
                         │
                         ▼
                Transcript + Report
         (returned via API, CLI, or file)
```

---

## 5. What Gets Ported, What Gets Decided

| From | What | Treatment |
|---|---|---|
| `tscribe-transcription-tool` | `Transcriber` (dual local/remote whisper), `class_pipeline` (chunk/reassemble/verify), `summarizer` | Ported as `tscribe`'s core library — single source of truth for transcription + verification (Unit 02). |
| `tscribe-transcription-tool` | standalone CLI (`record`/`transcribe`/`summarize`/`devices`) | Ported as `tscribe`'s local-client-mode CLI surface (Unit 04). |
| `tscribe-class-carnyx` | FastAPI server, job lifecycle, Drive integration, `/jobs` `/jobs/{id}` `/jobs/upload` `/healthz` `/version` contract | Ported verbatim as `tscribe`'s server-mode surface — NOT redesigned (Unit 03). |
| `tscribe-capture-rig` | `verify_capture.py`, segment/finalize logic, `rig.conf` pattern | Ported as `tscribe capture` subcommands (Unit 05). |
| `tscribe-capture-rig` | `capture.sh` / `capture.bat` (dual shell/batch) | Recommend replacing with one cross-platform Python implementation rather than carrying two scripts forward. |

---

## 6. Language & Runtime Decision

**Recommendation: Python, `uv`-managed, matching the existing ecosystem — not a rewrite.** All three source repos are Python (carnyx: FastAPI; transcription-tool: Click + faster-whisper/onnxruntime/sounddevice/pydub; capture-rig: stdlib-only verifier). None of that has a mature Rust equivalent — faster-whisper's ONNX runtime and sounddevice's PortAudio bindings are the two hard dependencies — and "reuse, don't reinvent" is explicit in the proposal doc. `bplate` being Rust does not mean `tscribe` must be; bplate already seeds a `python-cli` profile for exactly this case.

This is a **decision**, not an open question — flagged to Daniel as a call he can veto, not a question blocking speccing.

**Status 2026-08-21: EXECUTED.** This repo was scaffolded via `bplate new tscribe --profile python-cli --with mise,agent-core,ruff,editor-baseline --author "Daniel Ramirez" --license MIT` (verified end-to-end: bplate cloned, built from source with cargo, run for real — not a dry-run guess. All 32 of bplate's own contract checks passed: hash integrity, placeholder resolution, TOML/JSON syntax, git init with the pre-push hook installed and executable). `github-ci` trait's `.github/workflows/ci.yml` was intentionally left out of the automated push — the GitHub App token used here lacks `workflow` scope; that one file is included in the PR description for Daniel to add by hand, per the fleet's standing convention.

**bplate finding worth flagging back upstream:** `bplate new <target>` uses the raw `target` CLI argument as both the destination path AND the literal input to `PROJECT_NAME`/`PROJECT_NAME_SNAKE` slugification. Passing an absolute path (e.g. `/tmp/tscribe-scaffold`) silently produces a mangled package name (`tmp_tscribe_scaffold`) instead of failing loudly. Worked around here by invoking `bplate new tscribe` from within the parent directory (matching bplate's own AGENTS.md examples), but bplate itself would benefit from either rejecting path separators in `target` or deriving the project name from `Path::file_name()` instead of the raw string.

---

## 7. Non-Goals (this spec)

- **Migrating `tscribe-class-carnyx`'s nightly CompTIA job** onto the new tool. Explicitly deferred per the proposal — "non-breaking follow-up, not a prerequisite."
- **Deciding the fate of the two old repos** (archive? deprecate? keep as-is?) — Daniel's call, later.
- **The TUI.** Deferred to the cross-tool TUI style guide being scoped with Amacher; this spec only shapes the CLI/config/bootstrap surface a TUI would sit on top of.
- **Queue/batch job support** beyond what carnyx already does (one thread per job).
- **A formal JSON-Schema/YAML-Schema file** for `tscribe.yaml`. Unit 01 defines fields/types in prose + a dataclass sketch; a machine-checkable schema is an implementation detail.

---

## 8. Definition of Done (for the spec-approval gate, not the whole build)

Daniel reviewed this spec and approved it 2026-08-21: "good job with this! let's move forward with your suggestions... then add your specs to main, and then begin working on the units end to end on a feature branch." The scaffold + this spec are now on `main`; Units 01-08 land on a feature branch per the build order in §10, then a PR against `main`, merged only when `mergeable_state` is clean.

---

## 9. Units of Work Decomposition

- **Unit 01 — Config Schema, Core Models & Bootstrap-via-bplate** (`docs/units/01-config-schema-and-core-models.md`) — `tscribe.yaml` schema, loader/validator, `Mode`/`Backend`/`RuntimeConfig` models, scaffolding via `bplate`.
- **Unit 02 — Transcription Core** (`docs/units/02-transcription-core.md`) — port `Transcriber`, `class_pipeline`, `summarizer` from `tscribe-transcription-tool`.
- **Unit 03 — Server Mode (HTTP API)** (`docs/units/03-server-mode-http-api.md`) — port carnyx's FastAPI server, job lifecycle, Drive integration; contract preserved byte-for-byte.
- **Unit 04 — Client Mode** (`docs/units/04-client-mode.md`) — local in-process execution, remote-server client, remote-inference client; restores the standalone personal CLI.
- **Unit 05 — Capture Integration** (`docs/units/05-capture-integration.md`) — port the verified segmented-capture rig as cross-platform `tscribe capture` subcommands.
- **Unit 06 — Bootstrap Command** (`docs/units/06-bootstrap-command.md`) — `tscribe bootstrap`, self-provisioning + selftest.
- **Unit 07 — CLI Surface, JSON Envelope & Exit Codes** (`docs/units/07-cli-surface-and-exit-codes.md`) — the single `tscribe` entrypoint wiring every unit together.
- **Unit 08 — Documentation, CI & Release Readiness** (`docs/units/08-documentation-ci-and-release-readiness.md`) — CHANGELOG, fleet CI, README, KB-suite pointer.

---

## 10. Decomposition Rationale & Cross-Unit Open Questions

**Rationale.** Split on surface/discipline, not file count: config (01) / core library (02) / server (03) / client (04) / capture (05) / bootstrap (06) / CLI wiring (07) / docs+CI (08). No two units edit the same files. Suggested build order: **01 → 02 → (03, 04, 05 in parallel) → 06 → 07 (can start as stubs early) → 08 (last)**.

**Open questions:**
1. *(Unit 01/04)* Config needs to disambiguate "remote = another tscribe server" from "remote = a bare inference endpoint." Flagged for whoever picks up Units 01 and 04 to resolve together.
2. *(Unit 05)* Whether tscribe's capture path and lufs-recorder should converge later — not decided here.
3. *(Unit 06)* The lsbx bootstrap pattern wasn't in this spec's recon scope — its author must read lufs-sandbox-provider/lsbx-cli directly first.
4. *(§6)* Python-as-language: EXECUTED (see §6 status note above) — no longer open.
5. *(§7)* Fate of the two old repos is explicitly out of scope — Daniel's call, later.

**Self-grading against the speccing skill's checklist:** every unit points at specific files/repos rather than pasting content; acceptance criteria are checkbox-shaped and map to concrete checks; interface contracts give real signatures chosen so units compose without ad hoc coordination; boundaries name the unit that DOES own adjacent surface; both production repos are marked read-only throughout. Unit 06 is deliberately honest about a research gap rather than faking familiarity.