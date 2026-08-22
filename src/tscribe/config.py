"""tscribe.config — the tscribe.yaml schema, loader, and validator.

Every other unit that needs configuration imports RuntimeConfig from here.
This module owns exactly one thing: turning a tscribe.yaml file (plus CLI
overrides) into a validated, typed RuntimeConfig object. It does not know
about transcription, HTTP, or the CLI's argument parser — see SPEC.md's
"Boundaries" note for each unit that consumes this one.

Precedence order (documented, not implicit): CLI-flag overrides > env var
(TSCRIBE_CONFIG for the file path only) > config file > dataclass default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILENAME = "tscribe.yaml"
CONFIG_PATH_ENV_VAR = "TSCRIBE_CONFIG"


class ConfigError(Exception):
    """Raised for anything wrong with tscribe.yaml — always with a specific,
    actionable message. Never let a bad config file surface as a bare
    KeyError/TypeError from deep inside dataclass construction; that is
    exactly the kind of "exited nonzero for an unreadable reason" failure
    this project exists to close.
    """


class Mode(str, Enum):
    SERVER = "server"
    CLIENT = "client"


class Backend(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class RemoteKind(str, Enum):
    """Disambiguates SPEC.md §10 Open Question #1: a `backend: remote`
    config can point at two genuinely different things, and conflating them
    was flagged in the spec as something Units 01 and 04 must resolve
    together rather than silently pick one interpretation of. This is that
    resolution: an explicit field, not an inferred one.
    """

    # remote.endpoint is another tscribe server (mode: server elsewhere) —
    # client submits jobs via POST /jobs and polls GET /jobs/{id} (Unit 03's
    # contract), never touches the transcription pipeline directly.
    TSCRIBE_SERVER = "tscribe_server"

    # remote.endpoint is a bare LiteLLM/whisper-compatible inference
    # endpoint (e.g. carnyx's GPU host today) — Unit 02's pipeline runs
    # in-process locally and only the whisper call itself goes over the
    # network. This is what tscribe-class-carnyx's Transcriber does today.
    INFERENCE_ENDPOINT = "inference_endpoint"


@dataclass
class RemoteConfig:
    endpoint: str
    # Name of an environment variable holding the secret — never the
    # secret itself in the file. Read at call time by whichever unit
    # actually makes the request (Unit 02 or Unit 04), not here.
    api_key_env: str
    # Which of the two "remote" shapes this is — see RemoteKind.
    kind: RemoteKind = RemoteKind.INFERENCE_ENDPOINT
    # Whisper model name for remote inference, e.g.
    # "Systran/faster-distil-whisper-large-v3". Only meaningful when
    # kind == INFERENCE_ENDPOINT; ignored (but harmless) otherwise.
    transcription_model: str | None = None


@dataclass
class LocalConfig:
    model_size: str = "small"  # tiny|base|small|medium|large
    device: str = "cpu"
    compute_type: str = "int8"


@dataclass
class ChunkingConfig:
    minutes: int = 15
    workers: int = 2
    snap_window_s: int = 10


@dataclass
class SummarizationConfig:
    model: str = "lightweight"
    prompt: str | None = None


@dataclass
class ServerConfig:
    """mode: server settings. Added in Unit 03 — flagged as "not yet modeled"
    in docs/config-migration.md when Unit 01 shipped; this is that follow-up.
    Fields map 1:1 to carnyx's env vars (see the migration doc for the table),
    now sourced from tscribe.yaml like everything else instead of living in a
    second, server-only config surface.
    """

    api_key_env: str = "TSCRIBE_API_KEY"
    max_audio_bytes: int = 2 * 1024**3  # 2 GiB
    download_timeout_s: int = 600
    # Google Drive service-account credential resolution stays independent of
    # this env var by design (see server/drive.py's own docstring) — this is
    # only consulted as the first, explicit override in that chain.
    drive_sa_json_env: str = "CARNYX_DRIVE_SA_JSON"


@dataclass
class CaptureConfig:
    """`tscribe capture start|verify|finalize` settings (Unit 05). Ported from
    tscribe-capture-rig's `rig.conf` (KEY=VALUE) and the hardcoded contract
    constants in its `verify_capture.py` — both are retired in favor of this
    one config-file section. Fields map onto the source rig 1:1:

        rig.conf MIC_MATCH          -> mic_match
        rig.conf MIC_NAME           -> mic_name
        rig.conf SAMPLE_RATE        -> sample_rate
        rig.conf CHANNELS           -> channels
        rig.conf SEGMENT_SECONDS    -> segment_seconds
        rig.conf OUT_DIR            -> out_dir
        verify_capture.py DUR_TOL_SEG    -> duration_tolerance_segment_s
        verify_capture.py DUR_TOL_FINAL  -> duration_tolerance_final_s
        verify_capture.py MEAN_WARN_DB   -> silence_warn_db
        verify_capture.py MEAN_FAIL_DB   -> silence_fail_db

    The dB thresholds were conservative starting points in the source rig,
    calibrated on the first real classroom capture — now config-exposed
    (rather than hardcoded) specifically so that recalibration is a
    tscribe.yaml edit, not a code change.
    """

    # Linux (PipeWire/Pulse): substring match against `pactl list sources
    # short` to pick the input device. Windows uses `mic_name` instead (an
    # exact DirectShow device name — substring auto-match in that API is
    # brittle, same rationale the source rig's capture.bat documented).
    mic_match: str = "Tula"
    mic_name: str = ""

    # Capture shape — carnyx/tscribe.core ingest 16 kHz / mono / 16-bit;
    # capture native, no resample.
    sample_rate: int = 16000
    channels: int = 1

    # Segment length in seconds. 900 = 15 min, aligned to the class breaks
    # (matches SPEC.md's Unit 05 acceptance criterion default).
    segment_seconds: int = 900

    # Output directory for segmented WAVs (e.g. on a USB flash drive).
    out_dir: str = "class_audio"

    # duration_in_band tolerances, seconds: mid-run segment vs segment_seconds,
    # and final concatenated file vs measured wall-clock sum of segments.
    duration_tolerance_segment_s: float = 2.0
    duration_tolerance_final_s: float = 5.0

    # not_silence_floor thresholds, dBFS mean_volume (ffmpeg volumedetect).
    # Quieter than silence_warn_db: WARN (gain/placement). Quieter than
    # silence_fail_db: FAIL — dead capture (mic blocked/unplugged/privacy
    # toggle). silence_warn_db must stay >= silence_fail_db.
    silence_warn_db: float = -50.0
    silence_fail_db: float = -60.0

    # Optional explicit ffmpeg/ffprobe binary paths — same "zero PATH setup"
    # resolution order the source verifier used (explicit override here >
    # FFMPEG/FFPROBE env vars > PATH), so a portable ffmpeg dropped next to a
    # deployment still works with no shell configuration.
    ffmpeg_bin: str | None = None
    ffprobe_bin: str | None = None

    def __post_init__(self) -> None:
        if self.silence_warn_db < self.silence_fail_db:
            raise ConfigError(
                "capture.silence_warn_db must be >= capture.silence_fail_db "
                f"(got warn={self.silence_warn_db}, fail={self.silence_fail_db})."
            )
        if self.segment_seconds <= 0:
            raise ConfigError("capture.segment_seconds must be positive.")
        if self.sample_rate <= 0:
            raise ConfigError("capture.sample_rate must be positive.")
        if self.channels <= 0:
            raise ConfigError("capture.channels must be positive.")


@dataclass
class RuntimeConfig:
    mode: Mode
    backend: Backend
    remote: RemoteConfig | None = None
    local: LocalConfig = field(default_factory=LocalConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    summarization: SummarizationConfig = field(default_factory=SummarizationConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)

    def __post_init__(self) -> None:
        if self.backend == Backend.REMOTE and self.remote is None:
            raise ConfigError(
                "backend: remote requires a 'remote:' section (endpoint + api_key_env)."
            )


def _resolve_config_path(path: str | Path | None) -> Path:
    """CLI-flag path > TSCRIBE_CONFIG env var > ./tscribe.yaml."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)
    return Path.cwd() / DEFAULT_CONFIG_FILENAME


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """overlay wins on conflicts; nested dicts are merged, not replaced wholesale."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _require(d: dict[str, Any], key: str, section: str) -> Any:
    if key not in d or d[key] is None:
        raise ConfigError(f"Missing required field '{key}' in '{section}' section of tscribe.yaml.")
    return d[key]


def _enum_from(enum_cls: type[Enum], raw: Any, field_name: str) -> Any:
    try:
        return enum_cls(raw)
    except ValueError:
        valid = ", ".join(m.value for m in enum_cls)
        raise ConfigError(
            f"Invalid value {raw!r} for '{field_name}' — must be one of: {valid}."
        ) from None


def _build_remote_config(raw: dict[str, Any] | None) -> RemoteConfig | None:
    if raw is None:
        return None
    endpoint = _require(raw, "endpoint", "remote")
    api_key_env = _require(raw, "api_key_env", "remote")
    kind = (
        _enum_from(RemoteKind, raw["kind"], "remote.kind")
        if raw.get("kind") is not None
        else RemoteKind.INFERENCE_ENDPOINT
    )
    return RemoteConfig(
        endpoint=endpoint,
        api_key_env=api_key_env,
        kind=kind,
        transcription_model=raw.get("transcription_model"),
    )


def _build_local_config(raw: dict[str, Any] | None) -> LocalConfig:
    raw = raw or {}
    defaults = LocalConfig()
    return LocalConfig(
        model_size=raw.get("model_size", defaults.model_size),
        device=raw.get("device", defaults.device),
        compute_type=raw.get("compute_type", defaults.compute_type),
    )


def _build_chunking_config(raw: dict[str, Any] | None) -> ChunkingConfig:
    raw = raw or {}
    defaults = ChunkingConfig()
    return ChunkingConfig(
        minutes=raw.get("minutes", defaults.minutes),
        workers=raw.get("workers", defaults.workers),
        snap_window_s=raw.get("snap_window_s", defaults.snap_window_s),
    )


def _build_summarization_config(raw: dict[str, Any] | None) -> SummarizationConfig:
    raw = raw or {}
    defaults = SummarizationConfig()
    return SummarizationConfig(
        model=raw.get("model", defaults.model),
        prompt=raw.get("prompt", defaults.prompt),
    )


def _build_server_config(raw: dict[str, Any] | None) -> ServerConfig:
    raw = raw or {}
    defaults = ServerConfig()
    return ServerConfig(
        api_key_env=raw.get("api_key_env", defaults.api_key_env),
        max_audio_bytes=raw.get("max_audio_bytes", defaults.max_audio_bytes),
        download_timeout_s=raw.get("download_timeout_s", defaults.download_timeout_s),
        drive_sa_json_env=raw.get("drive_sa_json_env", defaults.drive_sa_json_env),
    )


def _build_capture_config(raw: dict[str, Any] | None) -> CaptureConfig:
    raw = raw or {}
    defaults = CaptureConfig()
    return CaptureConfig(
        mic_match=raw.get("mic_match", defaults.mic_match),
        mic_name=raw.get("mic_name", defaults.mic_name),
        sample_rate=raw.get("sample_rate", defaults.sample_rate),
        channels=raw.get("channels", defaults.channels),
        segment_seconds=raw.get("segment_seconds", defaults.segment_seconds),
        out_dir=raw.get("out_dir", defaults.out_dir),
        duration_tolerance_segment_s=raw.get(
            "duration_tolerance_segment_s", defaults.duration_tolerance_segment_s
        ),
        duration_tolerance_final_s=raw.get(
            "duration_tolerance_final_s", defaults.duration_tolerance_final_s
        ),
        silence_warn_db=raw.get("silence_warn_db", defaults.silence_warn_db),
        silence_fail_db=raw.get("silence_fail_db", defaults.silence_fail_db),
        ffmpeg_bin=raw.get("ffmpeg_bin", defaults.ffmpeg_bin),
        ffprobe_bin=raw.get("ffprobe_bin", defaults.ffprobe_bin),
    )


def parse_config_dict(data: dict[str, Any]) -> RuntimeConfig:
    """The pure, testable core: dict in, RuntimeConfig out (or ConfigError).

    Kept separate from load_config so tests exercise validation logic
    directly, without needing a real file on disk for every case.
    """
    if not isinstance(data, dict):
        raise ConfigError("tscribe.yaml must contain a YAML mapping at the top level.")

    mode = _enum_from(Mode, _require(data, "mode", "top-level"), "mode")
    backend = _enum_from(Backend, _require(data, "backend", "top-level"), "backend")

    return RuntimeConfig(
        mode=mode,
        backend=backend,
        remote=_build_remote_config(data.get("remote")),
        local=_build_local_config(data.get("local")),
        chunking=_build_chunking_config(data.get("chunking")),
        summarization=_build_summarization_config(data.get("summarization")),
        server=_build_server_config(data.get("server")),
        capture=_build_capture_config(data.get("capture")),
    )


def load_config(
    path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> RuntimeConfig:
    """Load and validate tscribe.yaml, applying CLI-flag overrides on top.

    Raises ConfigError with a specific, human-readable reason for every
    documented failure mode — never a bare traceback from deep inside
    yaml.safe_load or dataclass construction.
    """
    resolved_path = _resolve_config_path(path)

    if not resolved_path.exists():
        raise ConfigError(
            f"No config file found at {resolved_path}. "
            f"Copy tscribe.example.yaml to {DEFAULT_CONFIG_FILENAME} and edit it, "
            f"or pass --config <path>."
        )

    try:
        raw_text = resolved_path.read_text()
    except OSError as exc:
        raise ConfigError(f"Could not read {resolved_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved_path} is not valid YAML: {exc}") from exc

    if overrides:
        data = _deep_merge(data, overrides)

    return parse_config_dict(data)


def _dataclass_to_dict(obj: Any) -> Any:
    """Minimal recursive dataclass->dict for debugging/--json surfaces.
    Enums serialize to their .value, not their Python repr."""
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {f.name: _dataclass_to_dict(getattr(obj, f.name)) for f in fields(obj)}
    return obj


def config_to_dict(cfg: RuntimeConfig) -> dict[str, Any]:
    return _dataclass_to_dict(cfg)
