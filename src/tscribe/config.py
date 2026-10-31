"""tscribe.config — the tscribe config schema, loader, and validator.

Every other unit that needs configuration imports RuntimeConfig from here.
This module owns exactly one thing: turning a config file — TOML
(``.toml``, via the stdlib ``tomllib``) or legacy YAML (``.yaml``/``.yml``),
optionally layered as a user-global base plus a cwd project override — into a
validated, typed RuntimeConfig object (plus CLI overrides). It does not know
about transcription, HTTP, or the CLI's argument parser — see SPEC.md's
"Boundaries" note for each unit that consumes this one.

Precedence order (documented, not implicit): CLI-flag overrides > explicit
``--config`` path > env var (TSCRIBE_CONFIG for the file path only) > user
base config + cwd project overlay > dataclass default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILENAME = "tscribe.yaml"
CONFIG_PATH_ENV_VAR = "TSCRIBE_CONFIG"

# Cwd/base default filenames, in precedence order — `.toml` wins over the
# legacy `.yaml` when both are present (Unit 01's acceptance criterion).
DEFAULT_CONFIG_FILENAMES: list[str] = [
    "tscribe.toml",
    "tscribe.yaml",
]

# Project-local override filenames searched in the current working directory
# (Unit 02). `.tscribe.*` (dot-prefixed, i.e. hidden) is preferred over the
# non-dot form, and TOML over YAML within each, matching the lrex precedent.
PROJECT_CONFIG_FILENAMES: list[str] = [
    ".tscribe.toml",
    "tscribe.toml",
    ".tscribe.yaml",
    "tscribe.yaml",
]

# User-global base config filenames searched in ~/.config/tscribe/ (Unit 02).
# `config.toml` is the documented primary; the legacy `tscribe.yaml` name is
# still honored so an existing global YAML setup keeps working.
BASE_CONFIG_FILENAMES: list[str] = [
    "config.toml",
    "tscribe.toml",
    "config.yaml",
    "tscribe.yaml",
]


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
    """CLI-flag path > TSCRIBE_CONFIG env var > first existing
    DEFAULT_CONFIG_FILENAMES in cwd > ./tscribe.toml.

    `tscribe.toml` is checked before `tscribe.yaml` (Unit 01's acceptance
    criterion), so a project that ships both gets the TOML one. When neither
    exists yet, the TOML name is returned as the documented default so the
    resulting "no config file found" message names the format we now prefer.
    """
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)
    cwd = Path.cwd()
    for name in DEFAULT_CONFIG_FILENAMES:
        candidate = cwd / name
        if candidate.exists():
            return candidate
    return cwd / DEFAULT_CONFIG_FILENAMES[0]


def default_base_config_path() -> Path | None:
    """Return ~/.config/tscribe/config.toml (or the legacy
    ~/.config/tscribe/tscribe.yaml) if it exists, else None.

    This is the user-global base of Unit 02's two-tier resolution: a config
    placed here applies to every project without exporting $TSCRIBE_CONFIG.
    """
    base_dir = Path.home() / ".config" / "tscribe"
    for name in BASE_CONFIG_FILENAMES:
        candidate = base_dir / name
        if candidate.exists():
            return candidate
    return None


def project_override_path(cwd: Path | None = None) -> Path | None:
    """Return the first PROJECT_CONFIG_FILENAMES entry present in `cwd`
    (defaults to the process cwd), else None.

    A project-local override is a *partial* config: its keys are deep-merged
    on top of the resolved base, so it only needs to state what it changes.
    """
    directory = Path(cwd) if cwd is not None else Path.cwd()
    for name in PROJECT_CONFIG_FILENAMES:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """overlay wins on conflicts; nested dicts are merged, not replaced wholesale."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config_text(raw_text: str, *, filename_hint: str = "tscribe.toml") -> dict[str, Any]:
    """Parse raw config text into a plain dict, choosing the parser from the
    filename extension: `.toml` via the stdlib `tomllib` (zero new
    dependencies), `.yaml`/`.yml` via `yaml.safe_load` for backwards
    compatibility. Anything else is rejected rather than guessed at.

    Raises ConfigError (never a bare parser exception) for invalid syntax, a
    non-mapping top level, or — for `.toml` specifically — a field name the
    schema does not define, so a typo in a new TOML config fails loudly
    instead of being silently ignored.
    """
    suffix = Path(filename_hint).suffix.lower()
    if suffix == ".toml":
        try:
            data = tomllib.loads(raw_text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{filename_hint} is not valid TOML: {exc}") from exc
        _reject_unknown_fields(data, filename_hint)
    elif suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{filename_hint} is not valid YAML: {exc}") from exc
        data = data or {}
    else:
        raise ConfigError(
            f"Unsupported config file extension {suffix!r} for {filename_hint} — "
            f"use .toml, .yaml, or .yml."
        )

    if not isinstance(data, dict):
        raise ConfigError(f"{filename_hint} must contain a mapping at the top level.")
    return data


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read + parse one config file, translating I/O errors to ConfigError."""
    try:
        raw_text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    return load_config_text(raw_text, filename_hint=str(path))


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


# Section name -> the dataclass that owns its fields. Used only to validate
# `.toml` field names (see load_config_text); parse_config_dict itself stays
# tolerant of extra keys, matching its long-shipped behavior.
_SECTION_DATACLASSES: dict[str, type] = {
    "remote": RemoteConfig,
    "local": LocalConfig,
    "chunking": ChunkingConfig,
    "summarization": SummarizationConfig,
    "server": ServerConfig,
    "capture": CaptureConfig,
}


def _reject_unknown_fields(data: dict[str, Any], filename_hint: str) -> None:
    """Raise ConfigError for any field name the schema does not define.

    Applied to `.toml` only: TOML is the new format, so a mistyped key there
    should fail loudly. Legacy `.yaml` keeps the historical lenient behavior
    (unknown keys ignored) because strict backwards compatibility with
    existing YAML configs is a hard requirement.
    """
    known_top = {f.name for f in fields(RuntimeConfig)}
    unknown_top = sorted(set(data) - known_top)
    if unknown_top:
        raise ConfigError(
            f"{filename_hint}: unknown top-level field(s) {unknown_top} — "
            f"known fields: {sorted(known_top)}."
        )
    for section, section_cls in _SECTION_DATACLASSES.items():
        section_data = data.get(section)
        if not isinstance(section_data, dict):
            continue
        known = {f.name for f in fields(section_cls)}
        unknown = sorted(set(section_data) - known)
        if unknown:
            raise ConfigError(
                f"{filename_hint}: unknown field(s) {unknown} in '{section}' — "
                f"known fields: {sorted(known)}."
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
    *,
    cwd: Path | None = None,
    allow_project_layering: bool = True,
) -> RuntimeConfig:
    """Resolve, layer, and validate a tscribe config, applying runtime
    overrides last.

    Resolution order (highest precedence first):

    1. An explicit `path` (the CLI's ``--config``) — authoritative; when
       `allow_project_layering` is False (the CLI's default for an explicit
       path) the cwd project override is *not* merged on top.
    2. ``$TSCRIBE_CONFIG`` — also authoritative and never layered, preserving
       the historical "this exact file is the config" contract.
    3. The user-global base (``~/.config/tscribe/config.toml`` or the legacy
       ``tscribe.yaml``), with the cwd project override deep-merged on top
       when `allow_project_layering` is True.
    4. CLI `overrides` are deep-merged last, on top of whatever the above
       produced.

    Raises ConfigError with a specific, human-readable reason for every
    documented failure mode — never a bare traceback from deep inside
    tomllib/yaml.safe_load or dataclass construction.
    """
    directory = Path(cwd) if cwd is not None else Path.cwd()
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)

    if path is not None or env_path:
        base_path = _resolve_config_path(path)
        if not base_path.exists():
            raise ConfigError(
                f"No config file found at {base_path}. "
                f"Copy tscribe.example.yaml to {DEFAULT_CONFIG_FILENAME} and edit it, "
                f"or pass --config <path>."
            )
        data = _read_config_file(base_path)
        # An explicit file is authoritative unless the caller opted into
        # project layering (the CLI's --with-project-config). $TSCRIBE_CONFIG
        # never layers, so a legacy single-file invocation is unchanged.
        if allow_project_layering and path is not None:
            overlay_path = project_override_path(directory)
            if overlay_path is not None and overlay_path != base_path:
                data = _deep_merge(data, _read_config_file(overlay_path))
    else:
        base_path = default_base_config_path()
        data = _read_config_file(base_path) if base_path is not None else {}
        overlay_path = project_override_path(directory) if allow_project_layering else None
        if overlay_path is not None:
            data = _deep_merge(data, _read_config_file(overlay_path))
        if not data:
            base_dir = Path.home() / ".config" / "tscribe"
            raise ConfigError(
                f"No config file found. Looked for {DEFAULT_CONFIG_FILENAMES} in "
                f"{directory} and for {BASE_CONFIG_FILENAMES} in {base_dir}. "
                f"Copy tscribe.example.yaml to {DEFAULT_CONFIG_FILENAME} and edit it, "
                f"or pass --config <path>."
            )

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
