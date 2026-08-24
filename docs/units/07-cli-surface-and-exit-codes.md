# Unit 07 — CLI Surface, JSON Envelope & Exit Codes

## Objective
Wire every other unit's functionality behind one `tscribe` CLI entrypoint with a consistent `--json` envelope and a deterministic, documented exit-code contract.

## Context
- Spec: `SPEC.md` §4, §6
- Precedent: `lufs-audio/bplate` `docs/units/04-agent-cli-surface-and-exit-codes.md` — same envelope/exit-code philosophy
- Units 01-06 all surface here; per SPEC.md §10's build order, this can start as thin stubs early and fill in incrementally

## Acceptance criteria
- [ ] Subcommands: `serve` (Unit 03); `transcribe`, `record`, `devices`, `summarize` (Unit 04); `capture start|verify|finalize` (Unit 05); `bootstrap` (Unit 06); `jobs status <id>` (poll convenience over Unit 03).
- [ ] Every subcommand supports `--json`: `{"status": "success", "data": {...}}` or `{"status": "error", "code": <int>, "message": "..."}` — matches bplate's envelope shape.
- [ ] Deterministic exit codes:
  - `0` SUCCESS
  - `2` USAGE
  - `3` CONFIG_INVALID
  - `4` BACKEND_UNREACHABLE
  - `5` CONTRACT_VIOLATED
  - `6` JOB_NOT_FOUND
  - `7` INTERRUPTED
- [ ] `--dry-run` supported at minimum for `bootstrap` and `capture start`.
- [ ] CLI integration tests cover `--json` shape (success + error) and exit-code correctness for every documented code.

## Interface contract
```python
class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    CONFIG_INVALID = 3
    BACKEND_UNREACHABLE = 4
    CONTRACT_VIOLATED = 5
    JOB_NOT_FOUND = 6
    INTERRUPTED = 7


class CliResponse(TypedDict, total=False):
    status: Literal["success", "error"]
    data: dict
    code: int
    message: str
```

## Boundaries — do NOT touch
- Does not reimplement any unit's core logic — pure parsing/dispatch/output-formatting glue.
- Does not decide what goes in `data` beyond what each unit's interface contract already returns.

## Output
- `src/tscribe/cli/{main.py,commands.py,output.py}`, `tests/cli/`
- Commit style: `feat(cli): wire all subcommands behind unified tscribe entrypoint with json envelope and exit codes`

## Verification
- `pytest tests/cli/`
- `tscribe --help` lists every subcommand; broken input on any subcommand returns a well-formed error envelope + correct exit code