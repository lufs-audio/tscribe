"""Unit 07 acceptance tests: `tscribe --help` lists every subcommand, and
every documented exit code is reachable and shows up correctly in both
human and --json output. Each command function is exercised through the
real CLI entrypoint (main(argv)) rather than called directly, so this also
proves the argparse wiring itself, not just tscribe.cli.commands in
isolation.

Heavy/hardware-bound paths (real mic recording, real PipeWire capture, real
faster-whisper inference) are mocked at the same seam the unit-level tests
already established (see tests/client/test_local.py, tests/capture/, and
tests/test_bootstrap.py) — this file's job is proving the CLI plumbs
arguments and exceptions through correctly, not re-proving each unit's own
internals.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types

from tscribe.cli.main import main
from tscribe.cli.output import ExitCode


def _inject_fake_recorder_module(monkeypatch, *, list_input_devices):
    """`cmd_devices`/`cmd_record` do a deferred `from tscribe.client.recorder
    import ...` inside the function body (so importing tscribe.cli.commands
    itself never requires numpy/sounddevice). Monkeypatching a dotted string
    path like "tscribe.client.recorder.list_input_devices" would force
    pytest to actually import that real module first to resolve the target
    -- which fails in any environment that hasn't installed this project's
    optional `record` extra (numpy + sounddevice, the latter needing a real
    PortAudio). Pre-seeding sys.modules with a fake module sidesteps that
    entirely: Python's import machinery checks sys.modules before ever
    touching the real file, so `from tscribe.client.recorder import
    list_input_devices` picks up the fake straight away."""
    fake = types.ModuleType("tscribe.client.recorder")
    fake.list_input_devices = list_input_devices
    monkeypatch.setitem(sys.modules, "tscribe.client.recorder", fake)


def _make_tone_wav(path, seconds: int = 6, frequency: int = 220) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={seconds}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(path),
        ],
        check=True,
    )


# --------------------------------------------------------------------------- #
# Parser-level basics
# --------------------------------------------------------------------------- #


def test_version_flag_returns_success(capsys):
    code = main(["--version"])
    assert code == ExitCode.SUCCESS
    assert "tscribe" in capsys.readouterr().out


def test_help_flag_returns_success_and_lists_every_subcommand(capsys):
    code = main(["--help"])
    assert code == ExitCode.SUCCESS
    out = capsys.readouterr().out
    for cmd in [
        "serve",
        "transcribe",
        "record",
        "devices",
        "summarize",
        "capture",
        "bootstrap",
        "jobs",
    ]:
        assert cmd in out


def test_no_subcommand_is_usage_error():
    assert main([]) == ExitCode.USAGE


def test_unknown_subcommand_is_usage_error():
    assert main(["not-a-real-command"]) == ExitCode.USAGE


# --------------------------------------------------------------------------- #
# CONFIG_INVALID (3)
# --------------------------------------------------------------------------- #


def test_missing_config_file_is_config_invalid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["devices"]) == ExitCode.CONFIG_INVALID


def test_missing_config_file_json_envelope_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main(["--json", "devices"])
    assert code == ExitCode.CONFIG_INVALID
    out = json.loads(capsys.readouterr().out)
    assert out == {"status": "error", "code": ExitCode.CONFIG_INVALID, "message": out["message"]}
    assert "tscribe.yaml" in out["message"]


def test_serve_requires_server_mode_config(local_client_config):
    """local_client_config is mode: client -- `serve` must refuse before it
    ever imports uvicorn/fastapi (so this test needs neither installed)."""
    assert main(["--config", str(local_client_config), "serve"]) == ExitCode.CONFIG_INVALID


# --------------------------------------------------------------------------- #
# USAGE (2)
# --------------------------------------------------------------------------- #


def test_transcribe_missing_file_is_usage_error(local_client_config):
    code = main(["--config", str(local_client_config), "transcribe", "/no/such/file.wav"])
    assert code == ExitCode.USAGE


def test_transcribe_missing_file_json_shape(local_client_config, capsys):
    code = main(["--config", str(local_client_config), "--json", "transcribe", "/no/such/file.wav"])
    out = json.loads(capsys.readouterr().out)
    assert code == ExitCode.USAGE
    assert out["status"] == "error"
    assert out["code"] == ExitCode.USAGE


def test_jobs_status_on_a_local_config_is_usage_error(local_client_config):
    """jobs status only makes sense against backend: remote / kind:
    tscribe_server -- a local config has no server to poll."""
    code = main(["--config", str(local_client_config), "jobs", "status", "abc123"])
    assert code == ExitCode.USAGE


def test_capture_start_without_pipewire_is_usage_error(local_client_config):
    """This sandbox has no PipeWire/pactl (or no source matching the default
    capture.mic_match) -- capture_start's own preflight fails fast with
    CaptureError, which main() maps to USAGE. This is the honest, expected
    outcome in any environment without real audio hardware, matching Unit
    05's own report that real-mic capture is code-reviewed, not
    execution-tested, here."""
    code = main(["--config", str(local_client_config), "capture", "start", "--dry-run"])
    assert code == ExitCode.USAGE


# --------------------------------------------------------------------------- #
# SUCCESS (0) -- devices, capture verify, bootstrap, transcribe
# --------------------------------------------------------------------------- #


def test_devices_success_json(local_client_config, monkeypatch, capsys):
    _inject_fake_recorder_module(
        monkeypatch,
        list_input_devices=lambda: [{"index": 0, "name": "Fake Mic", "max_input_channels": 1}],
    )
    code = main(["--config", str(local_client_config), "--json", "devices"])
    assert code == ExitCode.SUCCESS
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["devices"][0]["name"] == "Fake Mic"


def test_devices_human_output(local_client_config, monkeypatch, capsys):
    _inject_fake_recorder_module(
        monkeypatch,
        list_input_devices=lambda: [{"index": 0, "name": "Fake Mic", "max_input_channels": 1}],
    )
    code = main(["--config", str(local_client_config), "devices"])
    assert code == ExitCode.SUCCESS
    assert "Fake Mic" in capsys.readouterr().out


def test_capture_verify_real_synthetic_wav(local_client_config, tmp_path, capsys):
    """No mocking -- real ffmpeg, real capture verification contract,
    exercised through the actual CLI."""
    wav = tmp_path / "seg.wav"
    _make_tone_wav(wav, seconds=3)
    code = main(
        [
            "--config",
            str(local_client_config),
            "--json",
            "capture",
            "verify",
            str(wav),
            "--expected-seconds",
            "3",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == ExitCode.SUCCESS
    assert out["data"]["report"]["ok"] is True


def test_bootstrap_writes_config_and_reports_success(tmp_path, capsys):
    cfg_path = tmp_path / "tscribe.yaml"
    code = main(["--config", str(cfg_path), "--json", "bootstrap"])
    assert code == ExitCode.SUCCESS
    assert cfg_path.exists()
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["config_written"] is True


def test_transcribe_verified_success_end_to_end(local_client_config, tmp_path, monkeypatch, capsys):
    """Real ffmpeg-synthesized tone -> real CLI parsing -> real make_client
    -> real transcribe_class/verification pipeline; only the whisper call
    itself is mocked, same fixture pattern as tests/client/test_local.py."""
    wav = tmp_path / "tone.wav"
    _make_tone_wav(wav)

    import tscribe.client.local as local_mod
    from tscribe.core.class_pipeline.pipeline import transcribe_class as real_transcribe_class
    from tscribe.core.class_pipeline.splitter import probe_duration

    class _GoodTranscriber:
        def transcribe(self, audio_path):
            dur = probe_duration(audio_path)
            words = ("alpha bravo charlie delta echo foxtrot golf hotel " * 20).split()
            n = max(1, int(120 * dur / 60.0))
            return " ".join(words[i % len(words)] for i in range(n)), None

    monkeypatch.setattr(
        local_mod,
        "transcribe_class",
        lambda input_path, output_path, cfg: real_transcribe_class(
            input_path, output_path, cfg, transcriber=_GoodTranscriber()
        ),
    )

    code = main(["--config", str(local_client_config), "--json", "transcribe", str(wav)])
    out = json.loads(capsys.readouterr().out)
    assert code == ExitCode.SUCCESS
    assert out["data"]["results"][0]["ok"] is True
    assert "[00:00:00]" in out["data"]["results"][0]["transcript"]


# --------------------------------------------------------------------------- #
# CONTRACT_VIOLATED (5)
# --------------------------------------------------------------------------- #


def test_transcribe_contract_violation_is_reported_with_evidence(
    local_client_config, tmp_path, monkeypatch, capsys
):
    wav = tmp_path / "tone.wav"
    _make_tone_wav(wav)

    import tscribe.client.local as local_mod
    from tscribe.core.class_pipeline.pipeline import transcribe_class as real_transcribe_class

    class _DropsEverything:
        def transcribe(self, audio_path):
            return "", None  # audible chunk, empty transcript -> ContractViolation

    monkeypatch.setattr(
        local_mod,
        "transcribe_class",
        lambda input_path, output_path, cfg: real_transcribe_class(
            input_path, output_path, cfg, transcriber=_DropsEverything()
        ),
    )

    code = main(["--config", str(local_client_config), "--json", "transcribe", str(wav)])
    out = json.loads(capsys.readouterr().out)
    assert code == ExitCode.CONTRACT_VIOLATED
    assert out["status"] == "error"


# --------------------------------------------------------------------------- #
# BACKEND_UNREACHABLE (4) / JOB_NOT_FOUND (6) -- via `jobs status`
# --------------------------------------------------------------------------- #


def _remote_server_config(tmp_path):
    cfg_path = tmp_path / "tscribe.yaml"
    cfg_path.write_text(
        "mode: client\nbackend: remote\nremote:\n"
        '  endpoint: "http://example.invalid"\n'
        '  api_key_env: "TSCRIBE_TEST_KEY"\n'
        "  kind: tscribe_server\n"
    )
    return cfg_path


def test_jobs_status_unreachable_backend(tmp_path, monkeypatch):
    import requests

    cfg_path = _remote_server_config(tmp_path)

    def _boom(*_a, **_k):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("tscribe.client.remote_server.requests.get", _boom)
    code = main(["--config", str(cfg_path), "jobs", "status", "abc"])
    assert code == ExitCode.BACKEND_UNREACHABLE


def test_jobs_status_not_found(tmp_path, monkeypatch, capsys):
    import requests

    cfg_path = _remote_server_config(tmp_path)

    class _FakeResponse:
        status_code = 404

        def raise_for_status(self):
            err = requests.exceptions.HTTPError("404 Client Error")
            err.response = self
            raise err

    monkeypatch.setattr(
        "tscribe.client.remote_server.requests.get", lambda *a, **k: _FakeResponse()
    )
    code = main(["--config", str(cfg_path), "--json", "jobs", "status", "missing-id"])
    out = json.loads(capsys.readouterr().out)
    assert code == ExitCode.JOB_NOT_FOUND
    assert out["code"] == ExitCode.JOB_NOT_FOUND


# --------------------------------------------------------------------------- #
# INTERRUPTED (7)
# --------------------------------------------------------------------------- #


def test_bootstrap_declined_overwrite_is_interrupted(tmp_path):
    """Real (not synthetically-thrown) path: a second, non-json, non-force
    bootstrap run against an existing valid config calls the real
    _default_confirm, whose input() has no attached stdin under pytest and
    raises EOFError -- treated as "no", which write_config turns into
    InterruptedBootstrapError, which cmd_bootstrap maps to INTERRUPTED."""
    cfg_path = tmp_path / "tscribe.yaml"
    assert main(["--config", str(cfg_path), "--json", "bootstrap"]) == ExitCode.SUCCESS
    assert main(["--config", str(cfg_path), "bootstrap"]) == ExitCode.INTERRUPTED


def test_summarize_empty_input_is_usage_error(local_client_config, monkeypatch):
    monkeypatch.setattr("sys.stdin.read", lambda: "")
    code = main(["--config", str(local_client_config), "summarize"])
    assert code == ExitCode.USAGE


def test_summarize_missing_file_is_usage_error(local_client_config):
    code = main(["--config", str(local_client_config), "summarize", "/nonexistent/path/file.txt"])
    assert code == ExitCode.USAGE


def test_summarize_missing_remote_config_is_config_invalid(local_client_config, monkeypatch):
    monkeypatch.setattr("sys.stdin.read", lambda: "Some transcript text to summarize.")
    code = main(["--config", str(local_client_config), "summarize"])
    assert code == ExitCode.CONFIG_INVALID


def test_summarize_success_with_remote(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "tscribe.yaml"
    cfg_path.write_text(
        "mode: client\n"
        "backend: local\n"
        "remote:\n"
        "  endpoint: https://api.example.com/v1\n"
        "  api_key_env: TEST_KEY\n"
    )
    monkeypatch.setenv("TEST_KEY", "secret")
    monkeypatch.setattr(
        "tscribe.cli.commands.core_summarize",
        lambda text, cfg, obsidian_format=False: f"Summary: {text[:10]}",
    )
    monkeypatch.setattr("sys.stdin.read", lambda: "Lecture notes transcript")
    code = main(["--config", str(cfg_path), "--json", "summarize"])
    assert code == ExitCode.SUCCESS
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "success"
    assert out["data"]["summary"] == "Summary: Lecture no"


def test_unhandled_keyboard_interrupt_maps_to_interrupted(local_client_config, monkeypatch):
    """A generic safety net at main()'s dispatch level, independent of any
    specific command's own handling -- simulates an interrupt raised from
    inside a command function that doesn't have its own translation."""
    monkeypatch.setattr(
        "tscribe.cli.commands.cmd_devices",
        lambda cfg: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    code = main(["--config", str(local_client_config), "devices"])
    assert code == ExitCode.INTERRUPTED
