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

from tscribe.cli.main import _build_transcribe_overrides, build_parser, main
from tscribe.cli.output import ExitCode
from tscribe.config import Backend


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


# --------------------------------------------------------------------------- #
# Unit 03 — runtime config overrides on `tscribe transcribe`
# --------------------------------------------------------------------------- #


def _capture_transcribe_cfg(monkeypatch):
    """Patch the client factory cmd_transcribe calls so tests can inspect the
    RuntimeConfig the CLI actually resolved, without touching whisper/ffmpeg."""
    from tscribe.client.base import JobResult

    captured: dict = {}

    class _FakeClient:
        def submit_and_wait(self, source, *args, **kwargs):
            return JobResult(status="done", verified=True, transcript="[00:00:00] ok")

    def _fake_make_client(cfg):
        captured["cfg"] = cfg
        return _FakeClient()

    monkeypatch.setattr("tscribe.cli.commands.make_client", _fake_make_client)
    return captured


def _write_cfg(tmp_path, body):
    path = tmp_path / "tscribe.yaml"
    path.write_text(body)
    return path


def test_transcribe_overrides_local_device_and_model_propagate(tmp_path, monkeypatch):
    cfg_path = _write_cfg(
        tmp_path, "mode: client\nbackend: local\nlocal:\n  model_size: small\n  device: cpu\n"
    )
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"")  # .wav suffix -> no ffmpeg conversion needed
    captured = _capture_transcribe_cfg(monkeypatch)

    code = main(
        ["--config", str(cfg_path), "transcribe", "--device", "cuda", "--model", "medium", str(wav)]
    )
    assert code == ExitCode.SUCCESS
    assert captured["cfg"].local.device == "cuda"
    assert captured["cfg"].local.model_size == "medium"


def test_transcribe_overrides_backend_and_endpoint_propagate(tmp_path, monkeypatch):
    cfg_path = _write_cfg(
        tmp_path,
        "mode: client\nbackend: local\nremote:\n"
        "  endpoint: http://old:1\n  api_key_env: K\n  kind: tscribe_server\n",
    )
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"")
    captured = _capture_transcribe_cfg(monkeypatch)

    code = main(
        [
            "--config",
            str(cfg_path),
            "transcribe",
            "--backend",
            "remote",
            "--endpoint",
            "http://carnyx:6390",
            str(wav),
        ]
    )
    assert code == ExitCode.SUCCESS
    assert captured["cfg"].backend == Backend.REMOTE
    assert captured["cfg"].remote.endpoint == "http://carnyx:6390"


def test_transcribe_overrides_model_sets_remote_transcription_model(tmp_path, monkeypatch):
    cfg_path = _write_cfg(
        tmp_path,
        "mode: client\nbackend: remote\nremote:\n"
        "  endpoint: http://carnyx:6280/v1\n  api_key_env: K\n  kind: inference_endpoint\n",
    )
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"")
    captured = _capture_transcribe_cfg(monkeypatch)

    code = main(["--config", str(cfg_path), "transcribe", "--model", "large-v3", str(wav)])
    assert code == ExitCode.SUCCESS
    assert captured["cfg"].remote.transcription_model == "large-v3"
    assert captured["cfg"].local.model_size == "small"  # untouched


def test_transcribe_overrides_unset_flags_leave_config_untouched(tmp_path, monkeypatch):
    cfg_path = _write_cfg(
        tmp_path, "mode: client\nbackend: local\nlocal:\n  model_size: medium\n  device: cpu\n"
    )
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"")
    captured = _capture_transcribe_cfg(monkeypatch)

    assert main(["--config", str(cfg_path), "transcribe", str(wav)]) == ExitCode.SUCCESS
    assert captured["cfg"].local.model_size == "medium"
    assert captured["cfg"].local.device == "cpu"
    assert captured["cfg"].backend == Backend.LOCAL


def test_transcribe_overrides_invalid_choice_is_usage_error(tmp_path):
    cfg_path = _write_cfg(tmp_path, "mode: client\nbackend: local\n")
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"")
    assert (
        main(["--config", str(cfg_path), "transcribe", "--backend", "invalid", str(wav)])
        == ExitCode.USAGE
    )
    assert (
        main(["--config", str(cfg_path), "transcribe", "--device", "tpu", str(wav)])
        == ExitCode.USAGE
    )


def test_transcribe_overrides_builder_translates_only_set_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "transcribe",
            "--backend",
            "remote",
            "--endpoint",
            "http://x:1",
            "--device",
            "cuda",
            "f.wav",
        ]
    )
    assert _build_transcribe_overrides(args) == {
        "backend": "remote",
        "remote": {"endpoint": "http://x:1"},
        "local": {"device": "cuda"},
    }
    assert _build_transcribe_overrides(parser.parse_args(["transcribe", "f.wav"])) == {}


def test_with_project_config_flag_layers_cwd_overlay(tmp_path, monkeypatch):
    """Unit 02 criterion 4 at the CLI layer: an explicit --config ignores the
    cwd project override, unless --with-project-config is passed."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".tscribe.toml").write_text('[local]\nmodel_size = "large"\n')
    cfg_path = project / "base.yaml"
    cfg_path.write_text("mode: client\nbackend: local\nlocal:\n  model_size: tiny\n")
    wav = project / "tone.wav"
    wav.write_bytes(b"")
    captured = _capture_transcribe_cfg(monkeypatch)
    monkeypatch.chdir(project)

    assert main(["--config", str(cfg_path), "transcribe", str(wav)]) == ExitCode.SUCCESS
    assert captured["cfg"].local.model_size == "tiny"
    assert (
        main(["--config", str(cfg_path), "--with-project-config", "transcribe", str(wav)])
        == ExitCode.SUCCESS
    )
    assert captured["cfg"].local.model_size == "large"


# --------------------------------------------------------------------------- #
# Unit 04 — `tscribe monitor <id>` (and the `jobs monitor` alias)
# --------------------------------------------------------------------------- #


def _monitor_config(tmp_path, endpoint="http://carnyx:6390"):
    cfg_path = tmp_path / "tscribe.yaml"
    cfg_path.write_text(
        "mode: client\nbackend: remote\nremote:\n"
        f'  endpoint: "{endpoint}"\n'
        '  api_key_env: "TSCRIBE_TEST_KEY"\n'
        "  kind: tscribe_server\n"
    )
    return cfg_path


def _stub_tui(monkeypatch, *, which="/usr/bin/tscribe-tui"):
    """Patch binary lookup + the actual exec so no real TUI is launched; the
    captured argv is what the assertions inspect."""
    calls: list = []
    monkeypatch.setattr("shutil.which", lambda name: which)

    def _fake_run(argv, *args, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("tscribe.cli.commands.subprocess.run", _fake_run)
    return calls


def test_monitor_command_derives_host_port_and_api_key(tmp_path, monkeypatch):
    cfg_path = _monitor_config(tmp_path, "http://carnyx:6390")
    monkeypatch.setenv("TSCRIBE_TEST_KEY", "secret")
    calls = _stub_tui(monkeypatch)

    assert main(["--config", str(cfg_path), "monitor", "job-1"]) == ExitCode.SUCCESS
    argv = calls[0]
    assert argv[argv.index("--host") + 1] == "carnyx"
    assert argv[argv.index("--port") + 1] == "6390"
    assert argv[argv.index("--job-id") + 1] == "job-1"
    assert argv[argv.index("--api-key") + 1] == "secret"
    assert argv[argv.index("--theme") + 1] == "lufs"


def test_monitor_command_explicit_flags_override_defaults(tmp_path, monkeypatch):
    cfg_path = _monitor_config(tmp_path, "http://carnyx:6390")
    monkeypatch.setenv("TSCRIBE_TEST_KEY", "secret")
    calls = _stub_tui(monkeypatch)

    code = main(
        [
            "--config",
            str(cfg_path),
            "monitor",
            "job-2",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--theme",
            "mono",
        ]
    )
    assert code == ExitCode.SUCCESS
    argv = calls[0]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "8000"
    assert argv[argv.index("--theme") + 1] == "mono"


def test_monitor_command_jobs_alias_launches_the_tui(tmp_path, monkeypatch):
    cfg_path = _monitor_config(tmp_path)
    monkeypatch.setenv("TSCRIBE_TEST_KEY", "secret")
    calls = _stub_tui(monkeypatch)

    assert main(["--config", str(cfg_path), "jobs", "monitor", "job-3"]) == ExitCode.SUCCESS
    assert calls and calls[0][calls[0].index("--job-id") + 1] == "job-3"


def test_monitor_command_resolves_binary_from_relative_path(tmp_path, monkeypatch):
    cfg_path = _monitor_config(tmp_path)
    monkeypatch.setenv("TSCRIBE_TEST_KEY", "secret")
    fake_bin = tmp_path / "tscribe-tui"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("tscribe.cli.commands._tui_binary_candidates", lambda: [fake_bin])
    calls: list = []

    def _fake_run(argv, *args, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("tscribe.cli.commands.subprocess.run", _fake_run)

    assert main(["--config", str(cfg_path), "monitor", "job-4"]) == ExitCode.SUCCESS
    assert calls[0][0] == str(fake_bin)


def test_monitor_command_missing_binary_is_usage_error(tmp_path, monkeypatch, capsys):
    cfg_path = _monitor_config(tmp_path)
    monkeypatch.setenv("TSCRIBE_TEST_KEY", "secret")
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("tscribe.cli.commands._tui_binary_candidates", lambda: [tmp_path / "nope"])

    code = main(["--config", str(cfg_path), "--json", "monitor", "job-5"])
    assert code == ExitCode.USAGE
    out = json.loads(capsys.readouterr().out)
    assert "cargo build --release" in out["message"]


def test_monitor_command_without_remote_config_is_usage_error(local_client_config):
    assert main(["--config", str(local_client_config), "monitor", "job-6"]) == ExitCode.USAGE


def test_monitor_command_binary_candidates_include_repo_tui_build_path():
    """The spec's literal `parents[2]` shorthand resolves to `src/`, not the
    repo root, so the resolver must also check the real build location."""
    from pathlib import Path

    import tscribe.cli.commands as commands_mod

    repo_root = Path(commands_mod.__file__).resolve().parents[3]
    expected = repo_root / "tui" / "target" / "release" / "tscribe-tui"
    assert expected in commands_mod._tui_binary_candidates()
