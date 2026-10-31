# tscribe-tui

A terminal monitor for a `tscribe` transcription job. Polls `GET /jobs/{id}` and
shows its status (queued → running → done/failed), elapsed time, and — once
it lands — the verification report (which checks passed, which failed, and
why). **Read-only.** There is no code path in here that creates, cancels, or
retries a job; start one with `curl` or the `tscribe` CLI, then point this at
the id it gives you.

## Why this is a separate Rust crate in a Python repo

`tscribe` itself is Python (`uv`/`pyproject.toml`, per `SPEC.md` §6's decision
to reuse the existing Python ecosystem rather than rewrite it). This TUI
follows the cross-tool TUI style guide locked for LUFS's terminal tools
(`lufs-audio/bplate` → `references/studies/tui-style-directions/`), which is
`ratatui`-only — there's no ratatui-for-Python. Rather than force a mismatched
stack onto either side, this lives in its own `tui/` subdirectory as an
independent Rust crate: its own `Cargo.toml`, built and run with `cargo`
directly, not part of `uv sync` or the `tscribe` package. `SPEC.md` §7
explicitly defers TUI design to this same style guide, so this addition
doesn't touch anything Units 01-08 own (config, transcription core, server,
client, capture, bootstrap, CLI, docs/CI) — see "Scope" below.

`lufs-audio/lrex`'s own `tui` (a `ratatui` monitor of `serve`'s HTTP+SSE API)
is the closest sibling: same locked interaction shape, same three themes,
same "the TUI watches, it doesn't drive" rule. The two were built the same
way for the same reason — read `lrex`'s `src/tui/` for the pattern this
followed.

## Build & run

```sh
cd tui
cargo build --release
./target/release/tscribe-tui --port 8777 --job-id <id> --api-key <key>
# or, to avoid a secret in your shell history / `ps` output:
export TSCRIBE_API_KEY=<key>
./target/release/tscribe-tui --port 8777 --job-id <id>
```

`--host` defaults to `127.0.0.1`. `--port` is required — there's no single
default that would be correct across deployments (it's whatever the server
was actually started with). `--theme lufs|catppuccin|mono` (default `lufs`)
picks the palette; `mono` is the accessibility-focused standard and never
signals state by color alone.

Press `q` or Esc to quit. Nothing you do in this TUI affects the job.

## Launch it from the `tscribe` CLI (preferred)

You normally don't invoke the binary directly. `tscribe monitor <job_id>` (and
its alias `tscribe jobs monitor <job_id>`) resolves the host, port, and API
key from the active config and launches this TUI for you:

```sh
tscribe monitor <job_id>
tscribe jobs monitor <job_id>          # identical
tscribe monitor <job_id> --theme mono  # explicit overrides still win
```

It derives `--host`/`--port` from `remote.endpoint` in `tscribe.yaml` /
`tscribe.toml` (e.g. `http://carnyx:6390` → `--host carnyx --port 6390`) and
sends the key from `os.environ[remote.api_key_env]`, so you never repeat
connection details you already configured. `--host`, `--port`, and
`--theme lufs|catppuccin|mono` override the derived values explicitly.

The binary is located on `$PATH` first, then at the relative build path
`tui/target/release/tscribe-tui`. If neither is found, `tscribe monitor`
exits `2` (USAGE) with instructions to build it. The direct `cargo`
invocation above remains supported for development.

## What it targets today, and what changes later

This client is built directly against `tscribe-class-carnyx`'s real,
currently-live `server/app.py` + `server/jobs.py` source (read, not guessed):
`GET /jobs/{id}` returns `{id, status, verified, source, created_at,
updated_at, transcript, report, error, transcript_file_id, upload_error,
moved, move_errors}`, auth is an `X-API-Key` header (`401` wrong key, `503`
server has no key configured at all — fails closed), and an unknown id is
`404`.

Per `SPEC.md` §5, tscribe's own Unit 03 ports that exact `/jobs` contract
**verbatim, byte-for-byte** as the new server's surface ("NOT redesigned").
So this client should keep working unchanged once Unit 03 lands — point
`--host`/`--port` at a real `tscribe --mode server` instance instead of
carnyx, same shape either way. If Unit 03 ends up deviating from that
byte-for-byte commitment, this is the client that will need to change.

## Security note

The API key is never rendered on screen. The footer's `curl`-equivalent
label shows `X-API-Key: ***` — the real key is used for the actual request
(see `src/client.rs`) but a terminal screen (recordings, shoulder-surfing,
scrollback capture) is not a safe place to echo a live secret back.

## Verification

No live `tscribe` or `tscribe-class-carnyx` server was reachable from the
build sandbox this was written in (no deployed instance, no GPU/whisper
backend to run one against locally). Two tiers, same approach as
`lufs-audio/lrex`'s `tui`:

- **Unit tests** (`cargo test`, run from `tui/`): HTTP status-line/response
  parsing, the real `Job.public()` JSON shape (including a failed-but-still-
  carries-a-transcript job, matching `jobs.py`'s documented "flag, don't
  delete" behavior), poll-interval selection, and the rendering/state logic
  (per-outcome text is distinguishable even where colors intentionally
  collapse under the `mono` theme, the API key is structurally excluded from
  the one function that builds the displayed command string, a job id
  mismatch between what was requested and what the server returned is
  refused rather than silently rendered).
- **A local fixture HTTP server** (not part of this repo — sandbox-only test
  tooling) implementing the exact real response shapes above, PTY-driving
  the actual compiled binary against **real HTTP traffic over a real
  socket**: a job genuinely transitioning queued → running → done/verified
  over real wall-clock time and being watched live, a failed job with a
  populated report, a wrong API key, an unknown job id, a server-wide
  `503` misconfiguration, and an outright unreachable host -- all producing
  the correct, distinct on-screen state and a clean `q`/exit-0.

**Independent audit before this was proposed for merge:** a second review
(fetching this branch's real pushed files fresh, not the author's local copy)
caught two real issues, both fixed in a follow-up commit: an unbounded
`Content-Length`/no-length response body could trigger an attempted
allocation of an attacker-chosen size (a real crash, not just a slow path —
now capped); and the "running" elapsed figure was originally computed from
the server's own `updated_at - created_at`, which the real server only
advances at the START and END of a run (`jobs.py`'s `touch()`), never during
it — so for the entire multi-hour job this tool exists to watch, that number
would have stayed frozen near zero. It's now tracked locally (wall-clock
time since this TUI first observed the job as `running`) and labeled "since
first observed" rather than implying it's the job's true total runtime.

**Known gap, flagged rather than assumed away:** none of the above touches a
*real* `tscribe-class-carnyx` or `tscribe` server, real Google Drive
write-back, or a real multi-hour transcription run. The fixture server
proves this client's HTTP/rendering logic is correct against the documented
contract; it does not prove the real server still honors that contract today,
or will once Unit 03 lands. First real run against a live instance should
confirm: the job actually reaches `done`/`failed` as expected, `verified`
matches the real contract outcome, and Drive write-back fields
(`transcript_file_id`/`upload_error`/`moved`/`move_errors`) populate as
documented.
