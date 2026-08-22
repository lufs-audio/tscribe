"""Unit 01 acceptance tests: config schema, loader, validation.

Run with: pytest tests/test_config.py -v
"""

from __future__ import annotations

import pytest

from tscribe.config import (
    Backend,
    ConfigError,
    Mode,
    RemoteKind,
    config_to_dict,
    load_config,
    parse_config_dict,
)

# ---------------------------------------------------------------------------
# parse_config_dict — pure validation logic, no filesystem involved
# ---------------------------------------------------------------------------


def test_minimal_local_config_loads_with_defaults():
    cfg = parse_config_dict({"mode": "client", "backend": "local"})
    assert cfg.mode == Mode.CLIENT
    assert cfg.backend == Backend.LOCAL
    assert cfg.remote is None
    assert cfg.local.model_size == "small"
    assert cfg.local.device == "cpu"
    assert cfg.chunking.minutes == 15
    assert cfg.chunking.workers == 2
    assert cfg.summarization.model == "lightweight"


def test_full_remote_server_config_loads():
    cfg = parse_config_dict(
        {
            "mode": "server",
            "backend": "remote",
            "remote": {
                "endpoint": "https://tscribe.example.com",
                "api_key_env": "TSCRIBE_API_KEY",
                "kind": "tscribe_server",
            },
        }
    )
    assert cfg.mode == Mode.SERVER
    assert cfg.backend == Backend.REMOTE
    assert cfg.remote is not None
    assert cfg.remote.endpoint == "https://tscribe.example.com"
    assert cfg.remote.api_key_env == "TSCRIBE_API_KEY"
    assert cfg.remote.kind == RemoteKind.TSCRIBE_SERVER


def test_remote_inference_endpoint_is_the_default_kind():
    """Matches 'carnyx today': backend remote with no explicit kind should
    mean 'bare inference endpoint', not 'another tscribe server' — that was
    the actual production shape being ported, so it's the safer default."""
    cfg = parse_config_dict(
        {
            "mode": "client",
            "backend": "remote",
            "remote": {
                "endpoint": "http://100.89.168.11:6280/v1",
                "api_key_env": "LITELLM_API_KEY",
                "transcription_model": "Systran/faster-distil-whisper-large-v3",
            },
        }
    )
    assert cfg.remote.kind == RemoteKind.INFERENCE_ENDPOINT
    assert cfg.remote.transcription_model == "Systran/faster-distil-whisper-large-v3"


def test_missing_mode_raises_specific_error():
    with pytest.raises(ConfigError, match="mode"):
        parse_config_dict({"backend": "local"})


def test_missing_backend_raises_specific_error():
    with pytest.raises(ConfigError, match="backend"):
        parse_config_dict({"mode": "client"})


def test_invalid_mode_value_raises_specific_error():
    with pytest.raises(ConfigError, match="mode"):
        parse_config_dict({"mode": "bogus", "backend": "local"})


def test_invalid_backend_value_raises_specific_error():
    with pytest.raises(ConfigError, match="backend"):
        parse_config_dict({"mode": "client", "backend": "bogus"})


def test_remote_backend_without_remote_section_raises():
    with pytest.raises(ConfigError, match="remote"):
        parse_config_dict({"mode": "client", "backend": "remote"})


def test_remote_section_missing_endpoint_raises():
    with pytest.raises(ConfigError, match="endpoint"):
        parse_config_dict(
            {
                "mode": "client",
                "backend": "remote",
                "remote": {"api_key_env": "X"},
            }
        )


def test_remote_section_missing_api_key_env_raises():
    with pytest.raises(ConfigError, match="api_key_env"):
        parse_config_dict(
            {
                "mode": "client",
                "backend": "remote",
                "remote": {"endpoint": "https://x"},
            }
        )


def test_top_level_must_be_a_mapping():
    with pytest.raises(ConfigError, match="mapping"):
        parse_config_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_partial_chunking_overrides_only_specified_fields():
    cfg = parse_config_dict({"mode": "client", "backend": "local", "chunking": {"workers": 4}})
    assert cfg.chunking.workers == 4
    assert cfg.chunking.minutes == 15  # default preserved
    assert cfg.chunking.snap_window_s == 10  # default preserved


# ---------------------------------------------------------------------------
# load_config — filesystem + precedence behavior
# ---------------------------------------------------------------------------


def test_load_config_missing_file_raises_actionable_error(tmp_path):
    missing = tmp_path / "tscribe.yaml"
    with pytest.raises(ConfigError, match="tscribe.example.yaml"):
        load_config(path=missing)


def test_load_config_reads_valid_yaml_file(tmp_path):
    config_file = tmp_path / "tscribe.yaml"
    config_file.write_text("mode: client\nbackend: local\n")
    cfg = load_config(path=config_file)
    assert cfg.mode == Mode.CLIENT
    assert cfg.backend == Backend.LOCAL


def test_load_config_malformed_yaml_raises_specific_error(tmp_path):
    config_file = tmp_path / "tscribe.yaml"
    config_file.write_text("mode: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path=config_file)


def test_load_config_env_var_points_at_file(tmp_path, monkeypatch):
    config_file = tmp_path / "custom-name.yaml"
    config_file.write_text("mode: server\nbackend: local\n")
    monkeypatch.setenv("TSCRIBE_CONFIG", str(config_file))
    cfg = load_config()
    assert cfg.mode == Mode.SERVER


def test_cli_override_takes_precedence_over_file(tmp_path):
    """Documented precedence: CLI flag > env var > config file > default."""
    config_file = tmp_path / "tscribe.yaml"
    config_file.write_text("mode: client\nbackend: local\n")
    cfg = load_config(path=config_file, overrides={"mode": "server"})
    assert cfg.mode == Mode.SERVER  # override wins
    assert cfg.backend == Backend.LOCAL  # untouched field preserved from file


def test_cli_override_merges_nested_sections_rather_than_replacing():
    cfg = parse_config_dict(
        {
            "mode": "client",
            "backend": "local",
            "local": {"model_size": "medium", "device": "cpu"},
        }
    )
    assert cfg.local.model_size == "medium"
    assert cfg.local.device == "cpu"
    assert cfg.local.compute_type == "int8"  # default, not clobbered


# ---------------------------------------------------------------------------
# config_to_dict — used by Unit 07's --json envelope
# ---------------------------------------------------------------------------


def test_config_to_dict_serializes_enums_to_their_values():
    cfg = parse_config_dict({"mode": "client", "backend": "local"})
    d = config_to_dict(cfg)
    assert d["mode"] == "client"
    assert d["backend"] == "local"
    assert isinstance(d["local"], dict)
    assert d["local"]["model_size"] == "small"


# ---------------------------------------------------------------------------
# ServerConfig (Unit 03) — added after Unit 01 shipped, per the "not yet
# modeled" placeholders in docs/config-migration.md
# ---------------------------------------------------------------------------


def test_server_config_defaults_when_section_omitted():
    cfg = parse_config_dict({"mode": "server", "backend": "local"})
    assert cfg.server.api_key_env == "TSCRIBE_API_KEY"
    assert cfg.server.max_audio_bytes == 2 * 1024**3
    assert cfg.server.download_timeout_s == 600
    assert cfg.server.drive_sa_json_env == "CARNYX_DRIVE_SA_JSON"


def test_server_config_section_overrides_defaults():
    cfg = parse_config_dict(
        {
            "mode": "server",
            "backend": "local",
            "server": {
                "api_key_env": "MY_CUSTOM_KEY",
                "max_audio_bytes": 1024,
                "download_timeout_s": 30,
                "drive_sa_json_env": "MY_SA_JSON",
            },
        }
    )
    assert cfg.server.api_key_env == "MY_CUSTOM_KEY"
    assert cfg.server.max_audio_bytes == 1024
    assert cfg.server.download_timeout_s == 30
    assert cfg.server.drive_sa_json_env == "MY_SA_JSON"
