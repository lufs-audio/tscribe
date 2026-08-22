"""tscribe.cli.commands — one function per subcommand.

Every function here is glue: it takes already-parsed arguments (and usually
a loaded RuntimeConfig), calls exactly one owning unit's public interface,
and shapes the result into the {"status", "data"} / {"status", "code",
"message"} envelope via tscribe.cli.output. No subcommand here reimplements
transcription (Unit 02), the server contract (Unit 03), a client shape
(Unit 04), capture (Unit 05), or bootstrap (Unit 06) — see each
docs/units/NN-*.md file's own "Boundaries" section for why that split
exists. A command function may raise; main.py's dispatch loop is the single
place that turns an exception into an exit code, so this module never calls
sys.exit itself.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tscribe import bootstrap as bootstrap_mod
from tscribe.capture import (
    capture_finalize,
    capture_finalize_report,
    capture_start,
)
from tscribe.capture.session import list_pipewire_sources
from tscribe.capture.verify import verify_capture as capture_verify_fn
from tscribe.client import AudioSource, JobHandle, RemoteServerClient, make_client
from tscribe.config import Mode, RemoteKind, RuntimeConfig
from tscribe.core import summarize as core_summarize

from .output import CliResponse, ExitCode, error, success

# --------------------------------------------------------------------------- #
# transcribe / devices / record / summarize — Unit 02 + Unit 04
# --------------------------------------------------------------------------- #


def _report_dict(report: Any) -> dict | None:
    if report is None:
        return None
    return {
        "ok": report.ok,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in report.checks],
        "failures": [c.name for c in report.failures],
    }


def _ensure_wav(path: Path, tmp_dir: Path) -> Path:
    """Batch transcribe must auto-convert non-WAV inputs — Unit 04's own
    report on this repo found the single-file submit/poll primitive but
    confirmed "nothing loops over multiple files or does format
    auto-conversion... squarely Unit 07's job" (see docs/units/04's
    acceptance criteria). One ffmpeg subprocess call, matching how every
    other unit here shells out to ffmpeg rather than adding a second, heavier
    Python audio-decoding dependency.
    """
    if path.suffix.lower() == ".wav":
        return path
    out = tmp_dir / (path.stem + ".wav")
    cp = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-ac", "1", str(out)],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"could not convert {path} to WAV via ffmpeg: {cp.stderr.strip()}")
    return out


def cmd_transcribe(cfg: RuntimeConfig, files: list[str], *, do_summarize: bool) -> CliResponse:
    """`tscribe transcribe <files...> [--summarize]`.

    Uses the client abstraction (Unit 04's make_client) rather than calling
    tscribe.core directly, so `backend: remote` (either flavor) is honored
    the same way it would be for `tscribe record`/a future non-CLI caller —
    this command does not special-case backend: local.
    """
    client = make_client(cfg)
    results: list[dict[str, Any]] = []
    any_violation = False
    any_hard_error = False

    with tempfile.TemporaryDirectory(prefix="tscribe_cli_convert_") as tmp:
        tmp_dir = Path(tmp)
        for f in files:
            src = Path(f)
            if not src.exists():
                any_hard_error = True
                results.append({"file": f, "ok": False, "error": f"file not found: {f}"})
                continue
            try:
                wav_path = _ensure_wav(src, tmp_dir)
            except RuntimeError as exc:
                any_hard_error = True
                results.append({"file": f, "ok": False, "error": str(exc)})
                continue

            job_result = client.submit_and_wait(AudioSource(path=str(wav_path)))
            entry: dict[str, Any] = {
                "file": f,
                "ok": job_result.verified,
                "transcript": job_result.transcript,
            }
            if job_result.report is not None:
                entry["report"] = job_result.report
            if job_result.error:
                entry["error"] = job_result.error
            if not job_result.verified:
                any_violation = True
            elif do_summarize and job_result.transcript:
                entry["summary"] = core_summarize(job_result.transcript, cfg)
            results.append(entry)

    if any_hard_error and not any_violation:
        return error(
            "one or more files could not be read or converted (see per-file errors)",
            ExitCode.USAGE,
        )
    if any_violation:
        return error(
            "one or more files failed transcription verification "
            "(evidence retained in the per-file results above)",
            ExitCode.CONTRACT_VIOLATED,
        )
    return success({"results": results})


def cmd_devices(_cfg: RuntimeConfig) -> CliResponse:
    """`tscribe devices` — Unit 04's recorder.py already exposes exactly the
    function this needs; nothing to build here beyond formatting."""
    from tscribe.client.recorder import list_input_devices

    return success({"devices": list_input_devices()})


def cmd_record(
    _cfg: RuntimeConfig, *, seconds: float, device: int | None, out: str | None
) -> CliResponse:
    """`tscribe record [--seconds N] [--device N] [--out path]`."""
    import time as _time

    from tscribe.client.recorder import Recorder

    if seconds <= 0:
        return error("--seconds must be positive", ExitCode.USAGE)

    rec = Recorder()
    if device is not None:
        rec.set_device(device)
    rec.start()
    _time.sleep(seconds)
    audio = rec.stop()
    if audio is None:
        return error("no audio was captured (check --device / mic permissions)", ExitCode.USAGE)

    tmp_path = rec.save_temp_wav(audio)
    if out:
        final_path = Path(out)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.replace(final_path)
    else:
        final_path = tmp_path
    return success({"path": str(final_path), "seconds": seconds})


def cmd_summarize(cfg: RuntimeConfig, *, file: str | None, obsidian: bool) -> CliResponse:
    """`tscribe summarize [file] [--obsidian]` — reads stdin when no file is
    given, restoring the source tool's pipe-friendly behavior
    (`tscribe transcribe x.wav | tscribe summarize`)."""
    if file:
        path = Path(file)
        if not path.exists():
            return error(f"file not found: {file}", ExitCode.USAGE)
        text = path.read_text()
    else:
        text = sys.stdin.read()

    if not text.strip():
        return error("no input text (pass a file argument or pipe text via stdin)", ExitCode.USAGE)

    summary = core_summarize(text, cfg, obsidian_format=obsidian)
    return success({"summary": summary})


# --------------------------------------------------------------------------- #
# serve — Unit 03
# --------------------------------------------------------------------------- #


def cmd_serve(cfg: RuntimeConfig, *, host: str, port: int) -> CliResponse:
    """`tscribe serve` — starts the Unit 03 FastAPI app. Blocks until killed;
    the config check happens before uvicorn is even imported so a bad-mode
    config never pays for that import."""
    if cfg.mode != Mode.SERVER:
        return error(
            "tscribe serve requires 'mode: server' in tscribe.yaml "
            f"(current mode: {cfg.mode.value})",
            ExitCode.CONFIG_INVALID,
        )
    import uvicorn

    from tscribe.server.app import create_app

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port)  # blocks for the life of the process
    return success({})  # pragma: no cover - unreachable while serving


# --------------------------------------------------------------------------- #
# capture start|verify|finalize|list — Unit 05
# --------------------------------------------------------------------------- #


def cmd_capture_list(cfg: RuntimeConfig) -> CliResponse:
    """`tscribe capture start --list` — enumerate real PipeWire sources so an
    operator can pick a `capture.mic_match` value. Linux-only; matches
    `capture.sh --list`'s original purpose."""
    sources = list_pipewire_sources()
    return success({"sources": [{"name": name, "raw": raw} for name, raw in sources]})


def cmd_capture_start(cfg: RuntimeConfig, *, dry_run: bool, duration: float | None) -> CliResponse:
    session = capture_start(cfg.capture, dry_run=dry_run, duration_s=duration)
    data = {
        "session_dir": session.session_dir,
        "segment_pattern": session.segment_pattern,
        "command": session.command,
        "source": session.source_description,
        "dry_run": dry_run,
    }
    if not dry_run:
        exit_code = session.stop() if duration is not None else None
        if exit_code is not None:
            data["ffmpeg_exit_code"] = exit_code
    return success(data)


def cmd_capture_verify(
    cfg: RuntimeConfig, *, file: str, final: bool, expected_seconds: float | None
) -> CliResponse:
    report = capture_verify_fn(file, expected_seconds, final=final, cfg=cfg.capture)
    if not report["ok"]:
        return error(
            f"capture verification failed for {file} (see checks in the data payload "
            f"of the equivalent --json call, or re-run with --json)",
            ExitCode.CONTRACT_VIOLATED,
        )
    return success({"report": report})


def cmd_capture_finalize(cfg: RuntimeConfig, *, session_dir: str, out: str | None) -> CliResponse:
    from tscribe.capture.finalize import FinalizeError

    try:
        out_path = capture_finalize(session_dir, out_file=out, cfg=cfg.capture)
    except FinalizeError as exc:
        return error(str(exc), ExitCode.USAGE)
    report = capture_finalize_report(out_path)
    data: dict[str, Any] = {"path": out_path}
    if report is not None:
        data["report"] = report
        if not report["ok"]:
            return error(
                f"finalize produced {out_path} but it failed verification "
                f"(file retained, not discarded — see the report)",
                ExitCode.CONTRACT_VIOLATED,
            )
    return success(data)


# --------------------------------------------------------------------------- #
# bootstrap — Unit 06
# --------------------------------------------------------------------------- #

_BOOTSTRAP_EXIT_CODE = {
    bootstrap_mod.DependencyMissingError: ExitCode.USAGE,
    bootstrap_mod.ConfigInvalidError: ExitCode.CONFIG_INVALID,
    bootstrap_mod.BackendUnreachableError: ExitCode.BACKEND_UNREACHABLE,
    bootstrap_mod.SelftestContractViolationError: ExitCode.CONTRACT_VIOLATED,
    bootstrap_mod.InterruptedBootstrapError: ExitCode.INTERRUPTED,
}


def cmd_bootstrap(options: bootstrap_mod.BootstrapOptions) -> CliResponse:
    """`tscribe bootstrap [...]` — translates Unit 06's typed exceptions into
    this unit's exit-code contract; this is the one piece of glue Unit 06's
    own module docstring explicitly deferred to Unit 07 (see bootstrap.py's
    top-of-file design note)."""
    try:
        result = bootstrap_mod.run_bootstrap(options)
    except bootstrap_mod.BootstrapError as exc:
        code = _BOOTSTRAP_EXIT_CODE.get(type(exc), ExitCode.USAGE)
        return error(str(exc), code)

    if not result.ok:
        return error(
            "bootstrap completed but did not reach a clean, verified state "
            "(see selftest details in the data payload of a --json re-run)",
            ExitCode.CONTRACT_VIOLATED,
        )
    return success(result.to_dict())


# --------------------------------------------------------------------------- #
# jobs status — a polling convenience over Unit 03's contract
# --------------------------------------------------------------------------- #


def cmd_jobs_status(cfg: RuntimeConfig, *, job_id: str) -> CliResponse:
    """`tscribe jobs status <id>` — polls a remote tscribe server for a job's
    status. Only meaningful for `mode: client` + `backend: remote` +
    `remote.kind: tscribe_server`; a bare `mode: server` config has no
    separate process to ask (its job store is in-memory in the server
    process itself, not reachable from a second CLI invocation)."""
    if (
        cfg.backend.value != "remote"
        or cfg.remote is None
        or cfg.remote.kind != RemoteKind.TSCRIBE_SERVER
    ):
        return error(
            "tscribe jobs status requires a client config with backend: remote "
            "and remote.kind: tscribe_server (pointed at a running tscribe "
            "server) — this config does not describe one.",
            ExitCode.USAGE,
        )

    import requests

    client = RemoteServerClient(cfg)
    try:
        result = client.poll(JobHandle(id=job_id))
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return error(f"job not found: {job_id}", ExitCode.JOB_NOT_FOUND)
        return error(f"server returned an error: {exc}", ExitCode.BACKEND_UNREACHABLE)
    except requests.exceptions.RequestException as exc:
        return error(f"could not reach remote server: {exc}", ExitCode.BACKEND_UNREACHABLE)

    data = {
        "id": job_id,
        "status": result.status,
        "verified": result.verified,
        "transcript": result.transcript,
        "report": result.report,
        "error": result.error,
    }
    if result.status == "failed" and result.error and "unknown job id" in result.error:
        return error(f"job not found: {job_id}", ExitCode.JOB_NOT_FOUND)
    return success(data)
