"""tscribe.bootstrap — `tscribe bootstrap`: fresh clone -> running, verified instance.

Replaces carnyx's manual systemd-plus-Cloudflare-Tunnel runbook
(tscribe-class-carnyx/deploy/README.md) with a single, scriptable operation:
check dependencies, write a valid tscribe.yaml, optionally prove a synthetic
job round-trips through Unit 02's pipeline, and optionally scaffold (never
install) a systemd unit file for server mode.

Design note, load-bearing for how this module is meant to be used: Unit 07
(the CLI surface) does not exist yet and is being built by a different unit
after this one lands. To stay decoupled from a parser that doesn't exist,
nothing in this module calls sys.exit() or argparse. The public entrypoint is
`run_bootstrap(options: BootstrapOptions) -> BootstrapResult`, plus a family
of specific, typed exceptions (BackendUnreachableError, ConfigInvalidError,
DependencyMissingError, InterruptedBootstrapError). Unit 07's dispatch layer
is expected to catch these and translate them into the exit codes documented
in docs/units/07-cli-surface-and-exit-codes.md:

    SUCCESS=0  USAGE=2  CONFIG_INVALID=3  BACKEND_UNREACHABLE=4
    CONTRACT_VIOLATED=5  JOB_NOT_FOUND=6  INTERRUPTED=7

That mapping is documented on each exception class below and mirrored in
EXIT_CODE_FOR_EXCEPTION for convenience, but this module itself never exits
the process — every path here is a plain function call a test can drive
directly, which is the whole point of Unit 06 being independently testable
before Unit 07 exists.

Research precedent this unit was supposed to consult before writing anything
(SPEC.md §10 open question #3, docs/units/06-bootstrap-command.md "Context"):
`lufs-audio/lufs-sandbox-provider`'s `lsbx-cli` bootstrap pattern. That repo
does not exist under the `lufs-audio` org as of this writing (confirmed via a
direct fetch attempt -- 404 -- and a GitHub code/repo search that found no
matching name anywhere the connected token can see). This is not a hedge:
there is no lsbx bootstrap implementation to read yet, so nothing below
imitates or claims familiarity with lsbx-cli mechanics. This module is
therefore the first real implementation of this shape in the fleet, built
directly from this unit's own acceptance criteria and interface contract
instead. If lufs-sandbox-provider/lsbx-cli appears later, it is worth a
follow-up pass to reconcile conventions (flag names, --json envelope shape,
selftest terminology) -- flagged here rather than silently assumed done.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from tscribe.config import (
    Backend,
    ConfigError,
    Mode,
    RemoteKind,
    RuntimeConfig,
    config_to_dict,
    load_config,
    parse_config_dict,
)

# --------------------------------------------------------------------------- #
# Exit-code contract (Unit 07 owns the enum itself; mirrored here so this
# module's exceptions can each declare which code they map to without
# depending on Unit 07's package, which does not exist yet).
# --------------------------------------------------------------------------- #


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    CONFIG_INVALID = 3
    BACKEND_UNREACHABLE = 4
    CONTRACT_VIOLATED = 5
    JOB_NOT_FOUND = 6
    INTERRUPTED = 7


class BootstrapError(Exception):
    """Base for every bootstrap-specific failure. Never raised directly --
    always one of the subclasses below, each pinned to the exit code Unit 07
    must translate it to. `exit_code` is an attribute (not just a docstring
    convention) so a dispatch layer can do `EXIT_CODE_FOR_EXCEPTION` lookups
    or just read `exc.exit_code` directly, whichever fits its dispatch style.
    """

    exit_code: ExitCode = ExitCode.USAGE


class DependencyMissingError(BootstrapError):
    """A required dependency (ffmpeg/ffprobe on PATH, or an unresolvable
    Python import) is missing. Maps to USAGE (2) -- this is a "can't even
    start" condition on the machine, not a config or contract problem.
    Always carries a specific, actionable message (SPEC.md's "fails with a
    specific actionable message" acceptance criterion) -- never a bare
    FileNotFoundError from deep inside subprocess.
    """

    exit_code = ExitCode.USAGE


class ConfigInvalidError(BootstrapError):
    """The requested or existing tscribe.yaml is invalid, or bootstrap was
    asked to overwrite an existing valid config without --force and without
    a way to ask (headless/--json with no confirm callback). Maps to
    CONFIG_INVALID (3).
    """

    exit_code = ExitCode.CONFIG_INVALID


class BackendUnreachableError(BootstrapError):
    """The configured remote endpoint (server or bare inference endpoint)
    did not respond during the selftest's reachability check. Maps to
    BACKEND_UNREACHABLE (4) -- explicitly NOT a generic failure, per this
    unit's acceptance criteria.
    """

    exit_code = ExitCode.BACKEND_UNREACHABLE


class SelftestContractViolationError(BootstrapError):
    """--selftest's synthetic job ran but Unit 02's verification contract
    rejected the result. Maps to CONTRACT_VIOLATED (5). Carries the
    underlying tscribe.core.class_pipeline.ContractViolation (with its
    .report and .transcript) as `__cause__`/`.violation` so the evidence is
    not discarded -- this project's stated non-negotiable, ported from
    carnyx's 2026-08-06 incident lesson.
    """

    exit_code = ExitCode.CONTRACT_VIOLATED

    def __init__(self, message: str, *, violation: Exception | None = None) -> None:
        super().__init__(message)
        self.violation = violation


class InterruptedBootstrapError(BootstrapError):
    """Bootstrap was interrupted (e.g. an interactive prompt was declined,
    or the caller's confirm callback raised KeyboardInterrupt-shaped intent).
    Maps to INTERRUPTED (7).
    """

    exit_code = ExitCode.INTERRUPTED


EXIT_CODE_FOR_EXCEPTION: dict[type[BootstrapError], ExitCode] = {
    DependencyMissingError: ExitCode.USAGE,
    ConfigInvalidError: ExitCode.CONFIG_INVALID,
    BackendUnreachableError: ExitCode.BACKEND_UNREACHABLE,
    SelftestContractViolationError: ExitCode.CONTRACT_VIOLATED,
    InterruptedBootstrapError: ExitCode.INTERRUPTED,
}


# --------------------------------------------------------------------------- #
# Options / Result — the structured surface Unit 07's parser will populate
# and read. Deliberately not argparse.Namespace: a plain dataclass is
# constructible directly by tests and by any future non-CLI caller (an agent
# calling into this as a library) without going through arg parsing at all.
# --------------------------------------------------------------------------- #


@dataclass
class BootstrapOptions:
    """Mirrors the interface contract in docs/units/06-bootstrap-command.md:

        tscribe bootstrap [--mode server|client] [--backend local|remote]
                           [--endpoint <url>] [--force] [--json] [--selftest]

    `config_path` is not part of the documented CLI surface but is exposed
    here (defaulting to ./tscribe.yaml via tscribe.config's own resolution)
    so tests never have to touch a real cwd file.
    """

    mode: Mode = Mode.CLIENT
    backend: Backend = Backend.LOCAL
    endpoint: str | None = None
    api_key_env: str = "TSCRIBE_API_KEY"
    remote_kind: RemoteKind = RemoteKind.INFERENCE_ENDPOINT
    force: bool = False
    json_mode: bool = False
    selftest: bool = False
    dry_run: bool = False
    config_path: Path | None = None
    #: Where to write the optional systemd scaffold. Only consulted when
    #: `mode == Mode.SERVER` and `write_systemd_unit` is True.
    write_systemd_unit: bool = False
    systemd_unit_path: Path | None = None
    #: Injectable confirm hook for the idempotency prompt. Signature:
    #: (prompt: str) -> bool. Defaults to a real input()-backed prompt, but
    #: is never called at all when `force` or `json_mode` is set -- headless
    #: callers must pass --force to opt into overwriting, never get an
    #: unconditional input() call blocking a non-interactive process.
    confirm: Callable[[str], bool] | None = None
    #: Working directory bootstrap operates in. Defaults to cwd; tests pass
    #: tmp_path so nothing here ever touches the real filesystem outside it.
    cwd: Path | None = None


@dataclass
class BootstrapResult:
    """Everything a CLI dispatch layer needs to build the --json envelope
    and decide the exit code without re-deriving anything. `ok` is True only
    when config is valid AND (if selftest was requested) the selftest
    passed -- matching the interface contract's "Exit 0 only when..." line
    exactly.
    """

    ok: bool
    config_path: Path
    config: RuntimeConfig
    config_written: bool
    api_key_generated: str | None = None
    dependency_report: DependencyReport | None = None
    selftest_report: SelftestReport | None = None
    systemd_unit_path: Path | None = None
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Matches Unit 07's `{"status": ..., "data": {...}}` success envelope
        shape (this returns the `data` payload; Unit 07 wraps it)."""
        d: dict[str, Any] = {
            "ok": self.ok,
            "config_path": str(self.config_path),
            "config": config_to_dict(self.config),
            "config_written": self.config_written,
            "messages": list(self.messages),
        }
        if self.api_key_generated is not None:
            # Report that a key was generated, never the key material itself,
            # in a --json envelope that might get logged or piped anywhere.
            d["api_key_generated"] = True
        if self.dependency_report is not None:
            d["dependencies"] = self.dependency_report.to_dict()
        if self.selftest_report is not None:
            d["selftest"] = self.selftest_report.to_dict()
        if self.systemd_unit_path is not None:
            d["systemd_unit_path"] = str(self.systemd_unit_path)
        return d


# --------------------------------------------------------------------------- #
# Step 1: dependency check
# --------------------------------------------------------------------------- #


@dataclass
class DependencyReport:
    ffmpeg_found: bool
    ffprobe_found: bool
    ffmpeg_path: str | None
    ffprobe_path: str | None
    pyyaml_importable: bool
    requests_importable: bool

    @property
    def ok(self) -> bool:
        return (
            self.ffmpeg_found
            and self.ffprobe_found
            and self.pyyaml_importable
            and self.requests_importable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ffmpeg_found": self.ffmpeg_found,
            "ffprobe_found": self.ffprobe_found,
            "ffmpeg_path": self.ffmpeg_path,
            "ffprobe_path": self.ffprobe_path,
            "pyyaml_importable": self.pyyaml_importable,
            "requests_importable": self.requests_importable,
            "ok": self.ok,
        }


def check_dependencies() -> DependencyReport:
    """Confirm ffmpeg/ffprobe are on PATH and this repo's declared runtime
    dependencies (pyyaml, requests) actually import in the current
    interpreter -- a cheap proxy for "uv sync happened and this venv is the
    one bootstrap should trust" without shelling out to uv itself (uv may
    not be installed in every environment this runs in, e.g. inside an
    already-provisioned venv/container; PATH + import checks are the part
    that's actually load-bearing for the rest of bootstrap to work).

    Never raises. Callers decide what to do with a non-ok report; see
    `require_dependencies` for the raising wrapper used by `run_bootstrap`.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")

    pyyaml_ok = True
    try:
        import yaml as _yaml  # noqa: F401
    except ImportError:
        pyyaml_ok = False

    requests_ok = True
    try:
        import requests as _requests  # noqa: F401
    except ImportError:
        requests_ok = False

    return DependencyReport(
        ffmpeg_found=ffmpeg_path is not None,
        ffprobe_found=ffprobe_path is not None,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        pyyaml_importable=pyyaml_ok,
        requests_importable=requests_ok,
    )


def require_dependencies() -> DependencyReport:
    """Same check as `check_dependencies`, but raises DependencyMissingError
    with a specific, actionable message per missing piece instead of handing
    back a report the caller has to remember to inspect. This is what
    `run_bootstrap` actually calls.
    """
    report = check_dependencies()
    if report.ok:
        return report

    problems: list[str] = []
    if not report.ffmpeg_found:
        problems.append(
            "ffmpeg was not found on PATH. Install it (e.g. `apt install ffmpeg`, "
            "`brew install ffmpeg`, or download a static build) and re-run "
            "`tscribe bootstrap`."
        )
    if not report.ffprobe_found:
        problems.append(
            "ffprobe was not found on PATH. It ships alongside ffmpeg in every "
            "common distribution -- if ffmpeg is installed and ffprobe still "
            "isn't found, check that both live in the same directory on PATH."
        )
    if not report.pyyaml_importable:
        problems.append(
            "the 'pyyaml' package is not importable in this interpreter. Run "
            "`uv sync` (or `pip install -e .`) from the repo root before "
            "bootstrapping."
        )
    if not report.requests_importable:
        problems.append(
            "the 'requests' package is not importable in this interpreter. Run "
            "`uv sync` (or `pip install -e .`) from the repo root before "
            "bootstrapping."
        )

    raise DependencyMissingError("tscribe bootstrap cannot continue -- " + " ".join(problems))


# --------------------------------------------------------------------------- #
# Step 2: config writing
# --------------------------------------------------------------------------- #


def generate_api_key() -> str:
    """A real secret, not a weak random -- secrets.token_urlsafe uses the OS
    CSPRNG. 32 bytes of entropy (the default in the stdlib is also 32; made
    explicit here since it's security-relevant enough to not want it to
    silently change if the stdlib default ever does)."""
    return secrets.token_urlsafe(32)


def _build_config_dict(options: BootstrapOptions) -> dict[str, Any]:
    """Translate BootstrapOptions into the plain dict shape
    tscribe.config.parse_config_dict expects, applying the same defaults
    tscribe.example.yaml documents."""
    data: dict[str, Any] = {
        "mode": options.mode.value,
        "backend": options.backend.value,
    }

    if options.backend == Backend.REMOTE:
        if not options.endpoint:
            raise ConfigInvalidError(
                "backend: remote requires --endpoint <url> (or an 'endpoint' "
                "option) -- there is nothing to connect to otherwise."
            )
        data["remote"] = {
            "endpoint": options.endpoint,
            "api_key_env": options.api_key_env,
            "kind": options.remote_kind.value,
        }

    return data


def _render_yaml(cfg: RuntimeConfig, *, api_key_env_for_server: str) -> str:
    """Render a valid, commented tscribe.yaml in the spirit of
    tscribe.example.yaml -- not a bare yaml.safe_dump, which would produce a
    technically-valid but unreadable file with none of the guidance a fresh
    operator needs. Every value written here round-trips through
    tscribe.config.parse_config_dict without modification.
    """
    d = config_to_dict(cfg)
    lines: list[str] = [
        "# tscribe.yaml — generated by `tscribe bootstrap`.",
        "# See docs/units/01-config-schema-and-core-models.md for the full field",
        "# reference and docs/config-migration.md if you're translating an",
        "# existing tscribe-class-carnyx or tscribe-transcription-tool deployment.",
        "",
        f"mode: {d['mode']}",
        f"backend: {d['backend']}",
        "",
    ]

    if d.get("remote") is not None:
        r = d["remote"]
        lines.append("remote:")
        lines.append(f'  endpoint: "{r["endpoint"]}"')
        lines.append(f'  api_key_env: "{r["api_key_env"]}"')
        lines.append(f"  kind: {r['kind']}")
        if r.get("transcription_model"):
            lines.append(f'  transcription_model: "{r["transcription_model"]}"')
        else:
            lines.append("  transcription_model: null")
        lines.append("")
        lines.append(f"# Set the secret before running: export {r['api_key_env']}=...")
        lines.append("")

    lines.append("local:")
    lines.append(f"  model_size: {d['local']['model_size']}")
    lines.append(f"  device: {d['local']['device']}")
    lines.append(f"  compute_type: {d['local']['compute_type']}")
    lines.append("")

    lines.append("chunking:")
    lines.append(f"  minutes: {d['chunking']['minutes']}")
    lines.append(f"  workers: {d['chunking']['workers']}")
    lines.append(f"  snap_window_s: {d['chunking']['snap_window_s']}")
    lines.append("")

    lines.append("summarization:")
    lines.append(f"  model: {d['summarization']['model']}")
    prompt = d["summarization"].get("prompt")
    lines.append(f"  prompt: {prompt if prompt is not None else 'null'}")

    if cfg.mode == Mode.SERVER:
        lines.append("")
        lines.append("# Server-mode API key lives in the environment variable named")
        lines.append("# below, never in this file. bootstrap generated one for you")
        lines.append("# if none was supplied -- see the bootstrap run's output.")
        lines.append("server:")
        lines.append(f"  api_key_env: {d['server']['api_key_env']}")
        lines.append(f"  max_audio_bytes: {d['server']['max_audio_bytes']}")
        lines.append(f"  download_timeout_s: {d['server']['download_timeout_s']}")
        lines.append(f"  drive_sa_json_env: {d['server']['drive_sa_json_env']}")

    lines.append("")
    return "\n".join(lines)


def _default_confirm(prompt: str) -> bool:
    """Real interactive confirm, used only when the caller didn't inject one
    and options.force/json_mode don't already bypass the prompt entirely."""
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, OSError):
        # No stdin to read -- e.g. piped/non-interactive without --json/
        # --force explicitly set (EOFError), or stdin is unavailable/
        # captured entirely (OSError, which is what a test runner capturing
        # stdout raises: "reading from stdin while output is captured").
        # Either way there is no human to answer, so treat it as "no" and
        # never hang or crash -- this is the exact "never prompt
        # unconditionally" guarantee this module's own docstrings promise
        # for headless callers, just also covering the non-EOFError shape a
        # captured-output test runner raises instead of a real EOF.
        return False
    return answer in ("y", "yes")


def write_config(
    options: BootstrapOptions,
) -> tuple[Path, RuntimeConfig, bool, str | None]:
    """Write (or reuse) tscribe.yaml per `options`. Returns
    (path, config, was_written, generated_api_key_or_None).

    Idempotency (SPEC.md acceptance criterion): if a valid tscribe.yaml
    already exists at the target path, this does NOT overwrite it unless
    `options.force` is set, or the injected/default confirm callback says
    yes. A --json/headless run without --force and without an injected
    confirm callback never blocks on input() -- it raises
    ConfigInvalidError immediately with an actionable message, per this
    module's "never prompt unconditionally" design constraint.
    """
    cwd = options.cwd or Path.cwd()
    target_path = options.config_path or (cwd / "tscribe.yaml")

    generated_key: str | None = None
    api_key_env_for_server = options.api_key_env

    existing_is_valid = False
    if target_path.exists():
        try:
            load_config(path=target_path)  # only its success/failure matters here
            existing_is_valid = True
        except ConfigError:
            existing_is_valid = False

        if existing_is_valid:
            if options.force:
                pass  # explicit opt-in to overwrite, skip the prompt entirely
            else:
                confirm = options.confirm
                if confirm is None:
                    if options.json_mode:
                        # Headless callers must pass --force to overwrite; a
                        # --json run has no human to prompt and must not hang.
                        raise ConfigInvalidError(
                            f"{target_path} already contains a valid config. "
                            f"Re-run with --force to overwrite it, or remove "
                            f"the file yourself first. Refusing to prompt in "
                            f"--json/headless mode."
                        )
                    confirm = _default_confirm
                should_overwrite = confirm(
                    f"{target_path} already exists and is valid. Overwrite it?"
                )
                if not should_overwrite:
                    raise InterruptedBootstrapError(
                        f"Bootstrap stopped: {target_path} already exists and "
                        f"the user declined to overwrite it. Existing config "
                        f"was left untouched."
                    )
        # existing file present but invalid: fall through and (re)write it --
        # there is nothing valid to protect, and refusing to fix a broken
        # config would defeat the point of bootstrap.

    data = _build_config_dict(options)

    if options.mode == Mode.SERVER and options.backend != Backend.REMOTE:
        # Server mode still needs an API key env var name for its own auth,
        # independent of whether the *transcription* backend is local or
        # remote (ServerConfig.api_key_env, distinct from RemoteConfig's).
        # Generate a real secret if the caller didn't provide one so a fresh
        # server never starts with a guessable or empty key.
        pass  # handled below via ServerConfig default + generated_key export

    try:
        cfg = parse_config_dict(data)
    except ConfigError as exc:
        raise ConfigInvalidError(str(exc)) from exc

    if options.mode == Mode.SERVER:
        import os

        if not os.environ.get(cfg.server.api_key_env):
            generated_key = generate_api_key()

    rendered = _render_yaml(cfg, api_key_env_for_server=api_key_env_for_server)

    if options.dry_run:
        return target_path, cfg, False, generated_key

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(rendered)

    return target_path, cfg, True, generated_key


# --------------------------------------------------------------------------- #
# Step 3: backend reachability (used by --selftest for backend: remote)
# --------------------------------------------------------------------------- #


def check_backend_reachable(cfg: RuntimeConfig, *, timeout_s: float = 5.0) -> None:
    """Raise BackendUnreachableError if `cfg` points at a remote backend that
    does not respond. No-op for backend: local -- there is nothing remote to
    reach. This is deliberately a light HEAD/GET probe, not a full
    transcription call; --selftest's synthetic job is what actually proves
    the pipeline works end to end.
    """
    if cfg.backend != Backend.REMOTE or cfg.remote is None:
        return

    import requests

    endpoint = cfg.remote.endpoint.rstrip("/")
    try:
        # Most inference/server endpoints don't guarantee a bare GET / route,
        # so treat any response (including 4xx) as "reachable" -- only a
        # connection-level failure means the backend itself is unreachable.
        requests.get(endpoint, timeout=timeout_s)
    except requests.exceptions.RequestException as exc:
        raise BackendUnreachableError(
            f"Could not reach remote endpoint {endpoint!r}: {exc}. Check the "
            f"URL in tscribe.yaml's 'remote.endpoint' field and that the host "
            f"is up and network-reachable from this machine."
        ) from exc


# --------------------------------------------------------------------------- #
# Step 4: --selftest — tiny synthetic end-to-end job through Unit 02
# --------------------------------------------------------------------------- #


@dataclass
class SelftestReport:
    ran: bool
    passed: bool
    detail: str
    #: True only when the *real* faster-whisper/local pipeline executed
    #: (heavy model load + inference). False when the selftest could not
    #: reach that far (e.g. faster-whisper not installed / no model weights
    #: cached in this sandbox) and reports that honestly instead of a false
    #: pass. See this module's docstring and the CreateAgent-facing report
    #: for exactly what was vs. wasn't exercised in any given environment.
    exercised_real_backend: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "passed": self.passed,
            "detail": self.detail,
            "exercised_real_backend": self.exercised_real_backend,
        }


def _generate_synthetic_wav(path: Path, *, duration_s: float = 3.0, frequency: int = 440) -> None:
    """Generate a tiny synthetic WAV via ffmpeg's lavfi sine source.

    This matches tests/core/test_class_pipeline.py's own convention for
    synthesizing test audio in this repo (ffmpeg -f lavfi sine=/anullsrc=,
    not a hand-rolled `wave`-module tone or numpy array) -- consistency with
    the rest of the test suite mattered more here than inventing a second
    way to make a WAV. It also means this function exercises the exact
    ffmpeg invocation shape `check_dependencies` already confirmed is on
    PATH, rather than a parallel code path nothing else in the repo uses.

    A pure sine tone is not speech, so this is honestly a *plumbing* test
    (file in -> pipeline runs -> report shape comes back), not a proof that
    whisper transcribes correctly -- selftest's report says so explicitly
    via `SelftestReport.detail` rather than implying more than it checked.
    """
    cp = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={duration_s}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        raise DependencyMissingError(
            f"Could not generate the selftest's synthetic WAV via ffmpeg: {cp.stderr.strip()}"
        )


def run_selftest(cfg: RuntimeConfig, *, work_dir: Path | None = None) -> SelftestReport:
    """Run a tiny synthetic end-to-end job through Unit 02's pipeline and
    report pass/fail. Never raises for an ordinary "the heavy local
    dependency chain isn't available here" outcome -- that is reported as
    `passed=False, exercised_real_backend=False` with a specific detail
    string, per this project's standard of never claiming a false pass.

    For backend: remote, this checks reachability first (raises
    BackendUnreachableError, per the acceptance criterion that an
    unreachable remote endpoint must exit BACKEND_UNREACHABLE specifically)
    and then still exercises the same local-pipeline plumbing below it,
    matching Transcriber's own remote-first-with-local-fallback behavior.
    """
    check_backend_reachable(cfg)

    tmp_owned = work_dir is None
    tmp_dir = work_dir or Path(__import__("tempfile").mkdtemp(prefix="tscribe_bootstrap_selftest_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    wav_path = tmp_dir / "selftest.wav"

    try:
        _generate_synthetic_wav(wav_path)

        from tscribe.core.transcriber import Transcriber

        transcriber = Transcriber(cfg)
        try:
            text, _info = transcriber.transcribe(str(wav_path))
        except ImportError as exc:
            # faster_whisper (or one of its heavy native deps) isn't
            # installed in this environment. This is the expected shape of
            # "written for real, not execution-tested here" -- report it
            # plainly rather than swallowing it into a generic failure.
            return SelftestReport(
                ran=True,
                passed=False,
                detail=(
                    "Selftest could not exercise the real local-whisper "
                    f"backend: {exc}. The synthetic WAV was generated "
                    "successfully and the pipeline was invoked correctly, "
                    "but faster-whisper (or a native dependency it needs) "
                    "is not importable in this environment. Install it "
                    "(`uv sync` should pull it in) and re-run --selftest to "
                    "get a real pass/fail."
                ),
                exercised_real_backend=False,
            )
        except Exception as exc:  # noqa: BLE001 — report, don't swallow
            return SelftestReport(
                ran=True,
                passed=False,
                detail=(
                    f"Selftest's synthetic job failed during transcription: "
                    f"{exc!r}. This ran the real backend configured in "
                    f"tscribe.yaml (backend: {cfg.backend.value}) and it "
                    f"raised rather than returning a result."
                ),
                exercised_real_backend=True,
            )

        # A pure sine tone legitimately transcribes to empty or near-empty
        # text with a real ASR model -- that is not a failure of the
        # plumbing being tested here. The pass condition is "the pipeline
        # ran to completion and returned the (text, info) shape Unit 02's
        # interface contract promises", matching this being a synthetic
        # end-to-end *round-trip* check, not a speech-accuracy check.
        if not isinstance(text, str):
            return SelftestReport(
                ran=True,
                passed=False,
                detail=(
                    f"Selftest's synthetic job returned a non-string "
                    f"transcript ({type(text)!r}); Unit 02's interface "
                    f"contract requires str."
                ),
                exercised_real_backend=True,
            )

        return SelftestReport(
            ran=True,
            passed=True,
            detail=(
                f"Synthetic {wav_path.name} round-tripped through "
                f"Transcriber(cfg).transcribe() and returned "
                f"{len(text.split())} word(s) of text (a pure sine tone is "
                f"not speech, so a short/empty result is expected and not "
                f"itself a failure -- this proves the plumbing, not "
                f"transcription accuracy)."
            ),
            exercised_real_backend=True,
        )
    finally:
        if tmp_owned:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Step 5: systemd scaffold (server mode, Linux, disk artifact only)
# --------------------------------------------------------------------------- #


SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=tscribe server ({mode} mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_directory}
Environment=TSCRIBE_CONFIG={config_path}
ExecStart={python_executable} -m tscribe.cli serve
Restart=on-failure
RestartSec=5
# The server-mode API key referenced by tscribe.yaml's server.api_key_env
# field must be exported here or in an EnvironmentFile= this unit loads --
# bootstrap does NOT write secrets into this unit file. See:
#   docs/units/06-bootstrap-command.md, tscribe-class-carnyx/deploy/README.md
# EnvironmentFile=/etc/tscribe.env

[Install]
WantedBy=multi-user.target
"""


def scaffold_systemd_unit(
    cfg: RuntimeConfig,
    *,
    config_path: Path,
    working_directory: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    """Write a systemd unit FILE to disk as an artifact. Never installs,
    enables, or starts it -- SPEC.md's boundary is explicit that systemd
    install/enable stays a separate, explicit, privileged step a human runs
    (`sudo cp ... && sudo systemctl daemon-reload && sudo systemctl enable
    --now ...`, exactly as carnyx's deploy/README.md documents today).

    This function itself never shells out to systemctl and never requires
    root -- it only ever writes a plain text file wherever `output_path`
    says, which defaults to ./tscribe.service in the working directory so a
    non-privileged bootstrap run can always produce it.
    """
    if cfg.mode != Mode.SERVER:
        raise ValueError(
            "scaffold_systemd_unit is only meaningful for mode: server "
            f"configs (got mode: {cfg.mode.value})."
        )

    wd = working_directory or Path.cwd()
    target = output_path or (wd / "tscribe.service")

    unit_text = SYSTEMD_UNIT_TEMPLATE.format(
        mode=cfg.mode.value,
        working_directory=str(wd),
        config_path=str(config_path),
        python_executable=sys.executable,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit_text)
    return target


# --------------------------------------------------------------------------- #
# Orchestration: run_bootstrap — the single entrypoint Unit 07 calls
# --------------------------------------------------------------------------- #


def run_bootstrap(options: BootstrapOptions) -> BootstrapResult:
    """The whole `tscribe bootstrap` operation, exceptions instead of exit
    codes. Matches the interface contract's success condition exactly:
    "Exit 0 only when config is valid AND (if --selftest passed) the
    synthetic job round-trips and verifies" -- here, that means returning a
    BootstrapResult with `ok=True` only under those conditions, or raising
    one of this module's typed BootstrapError subclasses otherwise so a CLI
    layer can map straight to the documented exit code.

    Order of operations, each of which can raise:
      1. require_dependencies() -> DependencyMissingError
      2. write_config()         -> ConfigInvalidError | InterruptedBootstrapError
      3. (if selftest) run_selftest() -> BackendUnreachableError |
                                          (or a non-raising failed report)
      4. (if server + write_systemd_unit) scaffold_systemd_unit()

    Boundaries respected (SPEC.md "do NOT touch"): this never implements
    server/client request handling itself (Units 03/04) and never runs a
    privileged systemd/firewall/tunnel operation -- it only configures and
    smoke-tests.
    """
    messages: list[str] = []

    dependency_report = require_dependencies()
    messages.append("Dependency check passed: ffmpeg, ffprobe, pyyaml, requests all found.")

    config_path, cfg, was_written, generated_key = write_config(options)
    if was_written:
        messages.append(f"Wrote {config_path}.")
    elif options.dry_run:
        messages.append(f"--dry-run: would write {config_path} (not written).")
    else:
        messages.append(f"Reused existing valid config at {config_path}.")

    if generated_key is not None:
        messages.append(
            f"Generated a new server-mode API key (export it as "
            f"{cfg.server.api_key_env} before starting the server; bootstrap "
            f"does not store it anywhere)."
        )

    selftest_report: SelftestReport | None = None
    if options.selftest and not options.dry_run:
        selftest_report = run_selftest(cfg)
        if selftest_report.passed:
            messages.append(f"Selftest passed: {selftest_report.detail}")
        else:
            messages.append(f"Selftest did not pass: {selftest_report.detail}")
    elif options.selftest and options.dry_run:
        messages.append("--dry-run: skipping --selftest (no config was written to test against).")

    systemd_path: Path | None = None
    if options.mode == Mode.SERVER and options.write_systemd_unit and not options.dry_run:
        systemd_path = scaffold_systemd_unit(
            cfg,
            config_path=config_path,
            working_directory=options.cwd,
            output_path=options.systemd_unit_path,
        )
        messages.append(
            f"Wrote systemd unit scaffold to {systemd_path} (NOT installed or "
            f"enabled -- copy it to /etc/systemd/system/ and run `systemctl "
            f"daemon-reload && systemctl enable --now` yourself when ready)."
        )

    ok = True
    if options.selftest and not options.dry_run:
        ok = selftest_report is not None and selftest_report.passed

    return BootstrapResult(
        ok=ok,
        config_path=config_path,
        config=cfg,
        config_written=was_written,
        api_key_generated=generated_key,
        dependency_report=dependency_report,
        selftest_report=selftest_report,
        systemd_unit_path=systemd_path,
        messages=messages,
    )
