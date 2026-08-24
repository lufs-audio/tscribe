"""tscribe.capture.verify — prove a capture segment (or the final file) is
real speech, not "ffmpeg exited 0."

Ported near-verbatim from tscribe-capture-rig's `bin/verify_capture.py`. The
killer check is `not_silence_floor`: a mic-privacy toggle or an unplugged mic
produces a file that exists, decodes, and is the right length but is ~digital
silence — exit 0 and useless. This catches it.

What changed in this port, and why:

  * Thresholds (sample rate, channels, duration tolerances, the two dB
    floors) now come from `CaptureConfig` (Unit 01's `tscribe.yaml` schema)
    instead of module-level constants read from `RIG_SR`/`RIG_CH` env vars
    and hardcoded literals. This is the acceptance criterion "now
    config-exposed" — recalibrating the silence floor after the first real
    capture is a `tscribe.yaml` edit, not a code change.
  * `ffmpeg`/`ffprobe` resolution keeps the source's exact "zero PATH setup"
    order (explicit path -> env var -> sibling-of-script -> PATH), but the
    explicit-path slot is now `CaptureConfig.ffmpeg_bin`/`ffprobe_bin`
    instead of a script always looking next to itself — a library module has
    no natural "next to me" location the way a distributed drive-local
    script did, so an explicit config field replaces that slot.
  * The standalone CLI (`argv`/`sys.exit`) is intentionally NOT here — Unit
    07 owns the CLI surface (`tscribe capture verify <file>`) and wires exit
    codes 0/2/64 into its overall envelope/exit-code contract. This module
    exposes the checking logic as a plain function, matching the interface
    contract in docs/units/05-capture-integration.md:

        def verify_capture(path, expected_seconds, final=False) -> VerificationReport

    Everything else (check names, order, semantics, the dB floor logic, the
    "never trust exit 0 alone" ffprobe-based decode check) is unchanged from
    the source rig.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, TypedDict

from tscribe.config import CaptureConfig

# Minimum plausible file size for a real (even very short) WAV capture.
# Matches the source rig's exists_nonempty threshold exactly.
_MIN_NONEMPTY_BYTES = 1024


class Check(TypedDict):
    """One row of a VerificationReport — name, ok/fail, and a human detail.

    Named to align with tscribe.core.class_pipeline.verify.Check in spirit
    (a name + a boolean + a detail string), but this is capture's own type:
    Unit 02's Check is a dataclass, not a dict, and the two verifiers are
    intentionally independent (capture verification predates and does not
    depend on the transcription contract).
    """

    name: str
    ok: bool
    detail: str


class VerificationReport(TypedDict):
    """Matches docs/units/05-capture-integration.md's interface contract
    exactly: `{ok: bool, checks: list[dict]}`."""

    ok: bool
    checks: list[dict]


def _resolve_bin(name: str, explicit: str | None) -> str | None:
    """Find an ffmpeg-family binary with no PATH setup required.

    Resolution order (highest priority first): explicit config path (
    `CaptureConfig.ffmpeg_bin`/`ffprobe_bin`) -> `FFMPEG`/`FFPROBE` env var ->
    PATH. This preserves the source rig's "zero PATH setup" promise — a
    portable ffmpeg dropped anywhere and pointed to via config or env still
    works with no shell configuration — while replacing the rig's
    "sibling-of-this-script" slot (meaningless for an installed library) with
    the explicit config field.
    """
    if explicit and os.path.isfile(explicit):
        return explicit
    env = os.environ.get(name.upper())  # FFMPEG / FFPROBE
    if env and os.path.isfile(env):
        return env
    return shutil.which(name) or shutil.which(name + ".exe")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _probe(ffprobe_bin: str, path: str) -> dict[str, Any] | None:
    """ffprobe -> {sr, ch, dur} or None if it does not decode.

    Never trust exit 0 alone — this is the whole point of the module: a file
    can exist and an ffmpeg command can exit 0 while writing nothing but
    silence. ffprobe actually reading stream metadata back out is the first
    real proof the bytes are usable audio.
    """
    r = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "stream=sample_rate,channels:format=duration",
            "-of",
            "json",
            path,
        ]
    )
    if r.returncode != 0:
        return None
    try:
        j = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = j.get("streams") or []
    if not streams:
        return None
    st = streams[0]
    fmt = j.get("format") or {}
    return {
        "sr": int(st.get("sample_rate", 0) or 0),
        "ch": int(st.get("channels", 0) or 0),
        "dur": float(fmt.get("duration", 0.0) or 0.0),
    }


def _mean_volume_db(ffmpeg_bin: str, path: str) -> float | None:
    """volumedetect prints 'mean_volume: -23.4 dB' to stderr; None if
    unreadable. This is the not_silence_floor measurement."""
    r = _run([ffmpeg_bin, "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"])
    for line in r.stderr.splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0])
            except (ValueError, IndexError):
                return None
    return None


def verify_capture(
    path: str,
    expected_seconds: float | None,
    final: bool = False,
    cfg: CaptureConfig | None = None,
) -> VerificationReport:
    """Run the full capture contract against one WAV file.

    Checks, in order, matching the source rig near-verbatim: exists_nonempty,
    decodes, sample_rate, channels, duration_in_band (only when
    `expected_seconds` is given), not_silence_floor. `final=True` widens the
    duration tolerance to `cfg.duration_tolerance_final_s` (the finalize-time
    check against measured wall-clock duration) instead of
    `cfg.duration_tolerance_segment_s` (the per-segment, mid-run check).

    Returns a VerificationReport (`{ok, checks}`) rather than raising or
    exiting — this is a library function; Unit 07's CLI decides what exit
    code (0/2/64) a caller sees. A `checks` row that fails a hard contract
    check (exists_nonempty, decodes, sample_rate, channels, duration_in_band,
    or a not_silence_floor FAIL) makes `ok` False. A not_silence_floor WARN
    does NOT fail the report — matching the source rig, where WARN prints but
    does not flip the exit code to 2.
    """
    cfg = cfg or CaptureConfig()
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    exists = os.path.isfile(path) and os.path.getsize(path) > _MIN_NONEMPTY_BYTES
    add(
        "exists_nonempty",
        exists,
        "" if exists else f"file missing or ~empty (< {_MIN_NONEMPTY_BYTES} bytes): {path}",
    )
    if not exists:
        # Matches the source rig: no point probing a file that isn't there.
        return {"ok": False, "checks": checks}

    ffprobe_bin = _resolve_bin("ffprobe", cfg.ffprobe_bin)
    ffmpeg_bin = _resolve_bin("ffmpeg", cfg.ffmpeg_bin)
    if ffprobe_bin is None or ffmpeg_bin is None:
        # This is the source rig's exit-64 case ("bad args / no ffmpeg") —
        # here it is reported as a failed report rather than a raised
        # exception, so callers (tests, the future CLI) can inspect it
        # uniformly. Unit 07's CLI is expected to map "ffmpeg unresolved" to
        # exit 64 specifically, distinct from a normal contract violation
        # (exit 2) — see this module's docstring.
        add(
            "decodes",
            False,
            "could not find ffmpeg/ffprobe (checked cfg.ffmpeg_bin/ffprobe_bin, "
            "FFMPEG/FFPROBE env vars, PATH)",
        )
        return {"ok": False, "checks": checks}

    probed = _probe(ffprobe_bin, path)
    if probed is None:
        add("decodes", False, "ffprobe could not read the file")
        return {"ok": False, "checks": checks}
    add("decodes", True, f"sr={probed['sr']}, ch={probed['ch']}, dur={probed['dur']:.2f}s")

    sr_ok = probed["sr"] == cfg.sample_rate
    add(
        "sample_rate",
        sr_ok,
        f"{probed['sr']} Hz" if sr_ok else f"{probed['sr']} != {cfg.sample_rate} Hz",
    )

    ch_ok = probed["ch"] == cfg.channels
    add(
        "channels",
        ch_ok,
        f"{probed['ch']}" if ch_ok else f"{probed['ch']} != {cfg.channels}",
    )

    if expected_seconds:
        tol = cfg.duration_tolerance_final_s if final else cfg.duration_tolerance_segment_s
        dur_ok = abs(probed["dur"] - expected_seconds) <= tol
        add(
            "duration_in_band",
            dur_ok,
            f"{probed['dur']:.2f}s vs {expected_seconds:.2f}s (+/-{tol:.1f}s)",
        )

    mean_db = _mean_volume_db(ffmpeg_bin, path)
    if mean_db is None:
        # Matches the source rig: an unreadable mean_volume is a WARN, not a
        # FAIL — it does not by itself prove the capture is dead.
        add("not_silence_floor", True, "could not read mean_volume (warn, not fatal)")
    elif mean_db < cfg.silence_fail_db:
        add(
            "not_silence_floor",
            False,
            f"{mean_db:.1f} dB < {cfg.silence_fail_db:.1f} dB -- DEAD CAPTURE "
            f"(mic blocked/unplugged?)",
        )
    elif mean_db < cfg.silence_warn_db:
        add(
            "not_silence_floor",
            True,
            f"{mean_db:.1f} dB is quiet (< {cfg.silence_warn_db:.1f} dB) -- "
            f"check mic gain/placement (warn, not fatal)",
        )
    else:
        add("not_silence_floor", True, f"{mean_db:.1f} dB")

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}
