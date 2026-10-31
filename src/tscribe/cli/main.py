"""tscribe.cli.main — the single `tscribe` entrypoint (Unit 07).

Wires every other unit's functionality behind one argument parser with a
consistent --json envelope and the documented exit-code contract:

    0 SUCCESS  2 USAGE  3 CONFIG_INVALID  4 BACKEND_UNREACHABLE
    5 CONTRACT_VIOLATED  6 JOB_NOT_FOUND  7 INTERRUPTED

This module owns parsing, config loading, and exception-to-exit-code
translation only — it never reimplements a unit's logic. See
tscribe.cli.commands for the one-function-per-subcommand glue this dispatches
to, and each docs/units/NN-*.md for what each unit itself owns.

`main()` takes an explicit `argv` (defaulting to `sys.argv[1:]`) rather than
reading `sys.argv` directly, specifically so tests can drive it with a known
argument list instead of inheriting whatever argv the test runner itself was
invoked with — the previous bplate-scaffold stub's test
(`assert main() == 0`, calling with no argv at all) broke exactly this way
under pytest, which is why that test is being replaced rather than kept.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from tscribe import bootstrap as bootstrap_mod
from tscribe.capture.finalize import FinalizeError
from tscribe.capture.session import CaptureError
from tscribe.config import Backend, ConfigError, Mode, RemoteKind, load_config
from tscribe.core import ContractViolation

from . import commands
from .output import CliResponse, ExitCode, emit, error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tscribe", description="Agent-first transcription primitive."
    )
    parser.add_argument("--version", action="version", version="tscribe 0.1.0")
    parser.add_argument(
        "--json", action="store_true", help="Emit a single JSON envelope instead of human text."
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to a tscribe config file (.toml/.yaml). Default: $TSCRIBE_CONFIG, "
        "else ~/.config/tscribe/config.toml plus any cwd project override "
        "(.tscribe.toml / tscribe.toml). For `bootstrap`, this is the path "
        "bootstrap writes to instead of reads from.",
    )
    parser.add_argument(
        "--with-project-config",
        action="store_true",
        help="With --config, also deep-merge the cwd project override "
        "(.tscribe.toml / tscribe.toml) on top of it.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run mode: server (Unit 03's FastAPI app).")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)

    p_transcribe = sub.add_parser("transcribe", help="Transcribe one or more audio files.")
    p_transcribe.add_argument(
        "files", nargs="+", help="Audio file paths (non-WAV is auto-converted via ffmpeg)."
    )
    p_transcribe.add_argument(
        "--summarize", action="store_true", help="Also summarize each verified transcript."
    )
    p_transcribe.add_argument(
        "--backend",
        choices=["local", "remote"],
        default=None,
        help="Override the configured backend for this invocation.",
    )
    p_transcribe.add_argument(
        "--endpoint",
        type=str,
        default=None,
        help="Override remote.endpoint for this invocation.",
    )
    p_transcribe.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the model (remote.transcription_model, or local.model_size locally).",
    )
    p_transcribe.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default=None,
        help="Override local.device for this invocation.",
    )

    p_monitor = sub.add_parser(
        "monitor", help="Watch a transcription job live in the terminal monitor (TUI)."
    )
    p_monitor.add_argument("id", help="Job ID returned from POST /jobs or POST /jobs/upload")
    p_monitor.add_argument(
        "--host", default=None, help="Server host (default: derived from remote.endpoint)"
    )
    p_monitor.add_argument(
        "--port", type=int, default=None, help="Server port (default: derived from remote.endpoint)"
    )
    p_monitor.add_argument(
        "--theme",
        choices=["lufs", "catppuccin", "mono"],
        default="lufs",
        help="TUI theme palette",
    )

    sub.add_parser("devices", help="List available microphone input devices.")

    p_record = sub.add_parser("record", help="Record from the microphone to a WAV file.")
    p_record.add_argument("--seconds", type=float, default=10.0)
    p_record.add_argument(
        "--device", type=int, default=None, help="Device index from `tscribe devices`."
    )
    p_record.add_argument(
        "--out", type=str, default=None, help="Output path (default: a temp file)."
    )

    p_summarize = sub.add_parser(
        "summarize", help="Summarize a transcript (file arg, or stdin if omitted)."
    )
    p_summarize.add_argument("file", nargs="?", default=None)
    p_summarize.add_argument(
        "--obsidian", action="store_true", help="Emit Obsidian-frontmatter output."
    )

    p_capture = sub.add_parser("capture", help="Verified segmented recording (Unit 05).")
    capture_sub = p_capture.add_subparsers(dest="capture_command", required=True)

    p_cap_start = capture_sub.add_parser(
        "start", help="Start (or plan) a segmented capture session."
    )
    p_cap_start.add_argument("--dry-run", action="store_true")
    p_cap_start.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Hard time cap in seconds (mainly for tests/ops).",
    )
    p_cap_start.add_argument(
        "--list", action="store_true", help="List PipeWire input sources instead of starting."
    )

    p_cap_verify = capture_sub.add_parser("verify", help="Verify a captured WAV file.")
    p_cap_verify.add_argument("file")
    p_cap_verify.add_argument(
        "--final", action="store_true", help="Use the wider finalize-time duration tolerance."
    )
    p_cap_verify.add_argument("--expected-seconds", type=float, default=None)

    p_cap_finalize = capture_sub.add_parser(
        "finalize", help="Concatenate a session's segments and verify the result."
    )
    p_cap_finalize.add_argument("session_dir")
    p_cap_finalize.add_argument("--out", type=str, default=None)

    p_bootstrap = sub.add_parser(
        "bootstrap", help="Take a fresh clone to a running, verified instance (Unit 06)."
    )
    p_bootstrap.add_argument("--mode", choices=["server", "client"], default="client")
    p_bootstrap.add_argument("--backend", choices=["local", "remote"], default="local")
    p_bootstrap.add_argument("--endpoint", type=str, default=None)
    p_bootstrap.add_argument(
        "--remote-kind",
        choices=["tscribe_server", "inference_endpoint"],
        default="inference_endpoint",
    )
    p_bootstrap.add_argument("--api-key-env", type=str, default="TSCRIBE_API_KEY")
    p_bootstrap.add_argument("--force", action="store_true")
    p_bootstrap.add_argument("--selftest", action="store_true")
    p_bootstrap.add_argument("--dry-run", action="store_true")
    p_bootstrap.add_argument("--write-systemd-unit", action="store_true")

    p_jobs = sub.add_parser("jobs", help="Poll a remote tscribe server for a job's status.")
    jobs_sub = p_jobs.add_subparsers(dest="jobs_command", required=True)
    p_jobs_status = jobs_sub.add_parser(
        "status", help="GET /jobs/{id} on a configured remote tscribe server."
    )
    p_jobs_status.add_argument("id")
    p_jobs_monitor = jobs_sub.add_parser(
        "monitor", help="Alias for `tscribe monitor <id>` — launch the TUI for a job."
    )
    p_jobs_monitor.add_argument("id", help="Job ID returned from POST /jobs or POST /jobs/upload")
    p_jobs_monitor.add_argument(
        "--host", default=None, help="Server host (default: derived from remote.endpoint)"
    )
    p_jobs_monitor.add_argument(
        "--port", type=int, default=None, help="Server port (default: derived from remote.endpoint)"
    )
    p_jobs_monitor.add_argument(
        "--theme",
        choices=["lufs", "catppuccin", "mono"],
        default="lufs",
        help="TUI theme palette",
    )

    return parser


def _load_cfg_or_error(
    config_path: str | None,
    *,
    overrides: dict[str, Any] | None = None,
    allow_project_layering: bool = True,
):
    try:
        return (
            load_config(
                path=config_path,
                overrides=overrides,
                allow_project_layering=allow_project_layering,
            ),
            None,
        )
    except ConfigError as exc:
        return None, error(str(exc), ExitCode.CONFIG_INVALID)


def _build_transcribe_overrides(
    args: argparse.Namespace, *, backend: Backend | None = None
) -> dict[str, Any]:
    """Translate non-None `tscribe transcribe` flags into the nested dict
    `load_config(overrides=...)` expects.

    `--backend`, `--endpoint`, and `--device` map straight onto their config
    sections. `--model` is ambiguous on its own — it means
    `remote.transcription_model` for a remote backend and `local.model_size`
    for a local one — so the caller passes the already-resolved `backend`
    (explicit `--backend`, else the config's own value). Without it, `--model`
    is applied to whichever section the explicit `--backend` names; if neither
    is available it is omitted from this pass and the caller re-resolves once
    the effective backend is known.
    """
    overrides: dict[str, Any] = {}
    if args.backend is not None:
        overrides["backend"] = args.backend
    if args.endpoint is not None:
        overrides.setdefault("remote", {})["endpoint"] = args.endpoint
    if args.device is not None:
        overrides.setdefault("local", {})["device"] = args.device

    if args.model is not None:
        effective = backend
        if effective is None and args.backend is not None:
            effective = Backend(args.backend)
        if effective == Backend.REMOTE:
            overrides.setdefault("remote", {})["transcription_model"] = args.model
        elif effective == Backend.LOCAL:
            overrides.setdefault("local", {})["model_size"] = args.model
    return overrides


def _dispatch(args: argparse.Namespace):
    """Returns a CliResponse. May raise — main() maps exceptions to exit codes."""
    if args.command == "bootstrap":
        options = bootstrap_mod.BootstrapOptions(
            mode=Mode(args.mode),
            backend=Backend(args.backend),
            endpoint=args.endpoint,
            api_key_env=args.api_key_env,
            remote_kind=RemoteKind(args.remote_kind),
            force=args.force,
            json_mode=args.json,
            selftest=args.selftest,
            dry_run=args.dry_run,
            config_path=Path(args.config) if args.config else None,
            write_systemd_unit=args.write_systemd_unit,
        )
        return commands.cmd_bootstrap(options)

    # Explicit --config is authoritative: the cwd project override is layered
    # on top only when --with-project-config is passed (Unit 02). With no
    # --config, the user-global base + cwd project overlay resolve normally.
    allow_project_layering = args.with_project_config or args.config is None

    if args.command == "transcribe":
        overrides = _build_transcribe_overrides(args)
        cfg, load_error = _load_cfg_or_error(
            args.config, overrides=overrides, allow_project_layering=allow_project_layering
        )
        if load_error is not None:
            return load_error
        if args.model is not None:
            # --model is backend-dependent; now that the effective backend is
            # known, re-resolve so it lands in the right section.
            overrides = _build_transcribe_overrides(args, backend=cfg.backend)
            cfg, load_error = _load_cfg_or_error(
                args.config, overrides=overrides, allow_project_layering=allow_project_layering
            )
            if load_error is not None:
                return load_error
        return commands.cmd_transcribe(cfg, args.files, do_summarize=args.summarize)

    # Every other command needs an existing, valid tscribe.yaml.
    cfg, load_error = _load_cfg_or_error(args.config, allow_project_layering=allow_project_layering)
    if load_error is not None:
        return load_error

    if args.command == "serve":
        return commands.cmd_serve(cfg, host=args.host, port=args.port)
    if args.command == "devices":
        return commands.cmd_devices(cfg)
    if args.command == "record":
        return commands.cmd_record(cfg, seconds=args.seconds, device=args.device, out=args.out)
    if args.command == "summarize":
        return commands.cmd_summarize(cfg, file=args.file, obsidian=args.obsidian)
    if args.command == "capture":
        if args.capture_command == "start":
            if args.list:
                return commands.cmd_capture_list(cfg)
            return commands.cmd_capture_start(cfg, dry_run=args.dry_run, duration=args.duration)
        if args.capture_command == "verify":
            return commands.cmd_capture_verify(
                cfg, file=args.file, final=args.final, expected_seconds=args.expected_seconds
            )
        if args.capture_command == "finalize":
            return commands.cmd_capture_finalize(cfg, session_dir=args.session_dir, out=args.out)
    if args.command == "jobs" and args.jobs_command == "status":
        return commands.cmd_jobs_status(cfg, job_id=args.id)
    if args.command == "monitor":
        return commands.cmd_monitor(
            cfg, job_id=args.id, host=args.host, port=args.port, theme=args.theme
        )
    if args.command == "jobs" and args.jobs_command == "monitor":
        return commands.cmd_monitor(
            cfg, job_id=args.id, host=args.host, port=args.port, theme=args.theme
        )

    return error(
        f"unrecognized command: {args.command}", ExitCode.USAGE
    )  # pragma: no cover - argparse prevents this


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit as exc:
        # argparse's own --help/--version/usage-error paths already call
        # sys.exit with the right code (0 for --help/--version, 2 for a
        # usage error) -- respect that instead of re-deriving it.
        return exc.code if isinstance(exc.code, int) else ExitCode.USAGE

    json_mode = bool(getattr(args, "json", False))

    try:
        response = _dispatch(args)
    except KeyboardInterrupt:
        emit(error("interrupted", ExitCode.INTERRUPTED), json_mode=json_mode)
        return ExitCode.INTERRUPTED
    except ConfigError as exc:
        emit(error(str(exc), ExitCode.CONFIG_INVALID), json_mode=json_mode)
        return ExitCode.CONFIG_INVALID
    except (CaptureError, FinalizeError) as exc:
        # A capture preflight/environment problem (no ffmpeg, no matching
        # mic, no segments to concat) — an operator/environment condition,
        # not a usage typo, but USAGE (2) is the closest documented code and
        # matches how tscribe-capture-rig's own verifier treated an
        # unresolvable ffmpeg (its exit 64) as distinct from a normal
        # contract violation. See docs/units/05-capture-integration.md.
        emit(error(str(exc), ExitCode.USAGE), json_mode=json_mode)
        return ExitCode.USAGE
    except ContractViolation as exc:
        # Safety net: every command that can raise this already converts it
        # to an error envelope itself (see commands.py), so reaching here
        # would mean a new call site forgot to. Still handled correctly
        # either way, with the evidence preserved on the exception.
        emit(
            error(
                f"ContractViolation: {exc} (transcript and report retained on "
                f"the exception, not discarded)",
                ExitCode.CONTRACT_VIOLATED,
            ),
            json_mode=json_mode,
        )
        return ExitCode.CONTRACT_VIOLATED
    except Exception as exc:  # noqa: BLE001 - last-resort translation, never silent
        import requests

        if isinstance(exc, requests.exceptions.RequestException):
            emit(
                error(f"backend unreachable: {exc}", ExitCode.BACKEND_UNREACHABLE),
                json_mode=json_mode,
            )
            return ExitCode.BACKEND_UNREACHABLE
        # Deliberately NOT one of the seven documented codes: this is an
        # unhandled condition, and claiming one of the specific documented
        # codes for it would misrepresent what happened. Plain `1` signals
        # "not a documented condition, check the message/traceback" without
        # colliding with a meaning this project promises elsewhere.
        emit(error(f"{type(exc).__name__}: {exc}", ExitCode.USAGE), json_mode=json_mode)
        return 1

    emit(response, json_mode=json_mode, human=_human_summary(args.command, response))
    return (
        response.get("code", ExitCode.SUCCESS)
        if response.get("status") == "error"
        else ExitCode.SUCCESS
    )


def _human_summary(command: str, response: CliResponse) -> str | None:
    """A friendlier human-mode rendering for the commands whose payload
    doesn't read well as a flat key: value dump (output.py's default). Only
    consulted for a success envelope in non-JSON mode; returning None falls
    back to that default. Purely cosmetic — every field used here also
    exists, unchanged, in the --json envelope's `data`."""
    if response.get("status") != "success":
        return None
    data = response.get("data") or {}

    if command == "transcribe":
        parts = []
        for r in data.get("results", []):
            header = f"=== {r['file']} ==="
            body = r.get("transcript") or "(no transcript)"
            parts.append(f"{header}\n{body}")
            if r.get("summary"):
                parts.append(f"--- summary ---\n{r['summary']}")
        return "\n\n".join(parts) if parts else "(no files)"

    if command == "summarize":
        return data.get("summary", "")

    if command == "devices":
        lines = [
            f"[{d['index']}] {d['name']} (in: {d['max_input_channels']})"
            for d in data.get("devices", [])
        ]
        return "\n".join(lines) if lines else "(no input devices found)"

    if command == "bootstrap":
        return "\n".join(data.get("messages", [])) or "bootstrap complete"

    return None
