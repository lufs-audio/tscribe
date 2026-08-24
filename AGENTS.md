# Agent Integration & Architecture Guide — `tscribe`

> Operational guidelines, architectural contracts, and verification commands for AI coding agents.

`tscribe` is the agent-first transcription primitive that unifies
`tscribe-class-carnyx`, `tscribe-transcription-tool`, and
`tscribe-capture-rig` into one config-driven tool. If you are an agent
working on this repo, read `SPEC.md` first — it is the authoritative spec
this repo was built against, and `docs/units/01` through `07` are the
per-unit contracts each part of the codebase satisfies. This file is the
operating contract for *how* to work on the code; it does not repeat what
those documents already say.

## 1. Architecture, in one paragraph

`tscribe.config` (Unit 01) is the single source of truth for `tscribe.yaml`
— every other package imports `RuntimeConfig` from it and nothing else
re-derives config shape. `tscribe.core` (Unit 02) is the only place
transcription and verification logic lives — `tscribe.server` (Unit 03,
`mode: server`'s FastAPI app) and `tscribe.client` (Unit 04, `mode: client`'s
three shapes: local / remote-server / remote-inference) both call into it and
never reimplement it. `tscribe.capture` (Unit 05) is independent of
transcription — it hands off a verified file path, nothing more.
`tscribe.bootstrap` (Unit 06) configures and smoke-tests the other units; it
never implements their logic and never calls `sys.exit` itself (it raises
typed exceptions instead — see §3). `tscribe.cli` (Unit 07) is the only place
argument parsing, the `--json` envelope, and exit codes are decided — every
`cmd_*` function in `tscribe/cli/commands.py` is glue over exactly one other
unit's public interface. If you find yourself reimplementing something one
of these packages already does, you are almost certainly working around a
boundary rather than respecting it — check the relevant `docs/units/NN-*.md`
file's "Boundaries" section before proceeding.

## 2. Core rules for agents

1. **Contract-first.** Never break a public interface (the "Interface
   contract" section of each `docs/units/NN-*.md`) without updating both the
   contract doc and its tests in the same change.
2. **Verified, not exited 0.** This project's entire reason to exist is
   closing the gap between "the command ran" and "the output is correct."
   Every transcription is checked by `tscribe.core.class_pipeline.verify`;
   every capture is checked by `tscribe.capture.verify`. On failure, retain
   the evidence (the unverified transcript, the full report) — never
   silently discard it. If you add a new operation with no single
   objectively-correct output, write a **metamorphic** check (an invariant
   that must hold — idempotence, duration preservation, word-count
   proportionality) rather than skipping verification because "there's no
   ground truth."
3. **Deterministic exit codes.** `tscribe.cli.output.ExitCode` is the
   complete, closed set: `0 SUCCESS, 2 USAGE, 3 CONFIG_INVALID,
   4 BACKEND_UNREACHABLE, 5 CONTRACT_VIOLATED, 6 JOB_NOT_FOUND,
   7 INTERRUPTED`. A new failure mode should map onto one of these, not
   invent an eighth. Every subcommand supports `--json`
   (`{"status": "success", "data": {...}}` or
   `{"status": "error", "code": <int>, "message": "..."}`); `code` in an
   error envelope is always the same number the process actually exits with.
4. **Honest about what's tested.** Several paths in this repo need hardware
   or weights this repo's own sandbox/CI does not have: real PipeWire mic
   capture, the Windows DirectShow capture path, and real `faster-whisper`
   inference (heavy, GPU-friendly, not installed by default — see the
   `local` extra). When you touch code on one of these paths, say explicitly
   in your PR/commit what you actually executed vs. what you wrote and
   reviewed but could not run — never claim execution-tested status you
   don't have. `docs/units/05-capture-integration.md` and
   `docs/units/06-bootstrap-command.md` model this explicitly; follow the
   same standard for new work. See [`docs/hardware-capture-test-plan.md`](./docs/hardware-capture-test-plan.md)
   for the step-by-step physical hardware verification runbook.
5. **`import the module, not the name`** when you need a test seam. Several
   modules in this repo (`server/jobs.py`, `client/local.py`,
   `client/remote_inference.py`) deliberately do
   `from . import config` / call `local_mod.transcribe_class(...)` rather
   than `from .config import transcriber_factory` — binding a name at import
   time means a test's `monkeypatch.setattr(module, "name", fake)` is never
   seen by the caller. This has already caused a real, caught-by-testing bug
   once in this repo's history (see `server/jobs.py`'s docstring); don't
   reintroduce it.

## 3. Essential commands

```bash
# Environment (project targets Python >=3.12)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"   # everything, including dev tools

# Run tests -- PYTHONPATH=src is required (no editable-install path hooks
# beyond pyproject's own [project.scripts] entry point)
mise run test                         # -> PYTHONPATH=src uv run pytest
PYTHONPATH=src pytest tests/ -v       # equivalent, no mise required

# Lint / format (ruff.toml: line-length 100, py312, E/F/I/N/W/UP selected)
mise run check                        # ruff check .
mise run fmt                          # ruff format .
```

Two lint findings are intentionally suppressed in `ruff.toml` — read the
comments there before "fixing" them: `UP042` on `Mode`/`Backend`/`RemoteKind`
(a deliberate `str, Enum` choice, not an oversight) and `N818` on
`ContractViolation` (a wide, purely-cosmetic rename not worth the risk this
late). Don't silently re-enable either without discussing it — it means
touching Unit 01/02's already-reviewed, already-shipped code for a style nit.

Verification before hand-off: run the full suite (`pytest tests/ -v`) AND
`ruff check .` AND `ruff format --check .` before declaring any change done.
A green exit code alone is not "done" on this project — see rule 2 above.

## 4. If this grows into a family of operations

`tscribe` already *is* a small process registry in spirit — `kernel`
(`tscribe.config`), `process` (each of `core`/`server`/`client`/`capture`/
`bootstrap`), and `verification` (each unit's own contract) are the same
three nouns `bplate` and the wider LUFS ecosystem use. If a genuinely new
capability shows up that doesn't fit any existing unit (a new capture
source, a new backend), give it the same shape — a focused package with a
narrow public interface, a `docs/units/NN-*.md` spec, and tests that prove
the contract, not just that it runs — rather than growing an existing unit
past its stated boundary.
