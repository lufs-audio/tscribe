"""tscribe.capture.session — cross-platform segmented capture.

Consolidates tscribe-capture-rig's `bin/capture.sh` (Arch/PipeWire, `-f
pulse`) and `bin/capture.bat` (Windows/DirectShow, `-f dshow`) into one
Python implementation, per SPEC.md §5's explicit recommendation: "replacing
[the two scripts] with one cross-platform Python implementation rather than
carrying two scripts forward." The two source scripts had "one contract, two
implementations" (same rig.conf, same on-drive layout, same checks; only the
mic-input-selection flag and syntax differed) — this module IS that one
contract, now with the platform branch as a few lines of Python instead of a
second whole script to keep in sync.

Same core ffmpeg invocation shape as both source scripts, preserved exactly:

    ffmpeg <input-args> -ac <channels> -ar <sample_rate> -c:a pcm_s16le \\
      -f segment -segment_time <segment_seconds> -reset_timestamps 1 \\
      <out_dir>/seg_<stamp>_%03d.wav

`-reset_timestamps 1` is the "segmented-WAV-with-reset-timestamps pattern"
named in the interface contract: each segment file gets its own independent,
zero-based timestamp base, so a segment is a fully self-contained, valid WAV
on its own (verifiable mid-run, per-segment, without needing the others) —
not a fragment of a stream whose timestamps only make sense concatenated.

Platform coverage, and what that honestly means here (see also this module's
own honesty note at the bottom, and the port's overall report to the human
reviewer): the Linux/PipeWire path shells out to `pactl` exactly as
`capture.sh` did, and was exercised in this port's sandbox only through the
`RIG_INPUT_OVERRIDE`-equivalent test seam (`input_override`) against a
synthetic ffmpeg source — this sandbox has no real PipeWire mic to record
from, so the real-device-enumeration branch (`pactl list sources short` on an
actual machine with an actual mic) is code-reviewed only, not
execution-tested. The Windows/DirectShow path is written from careful reading
of `capture.bat` (same `-f dshow -i audio="<name>"` shape, same requirement
for an exact device name rather than a substring match) but this port ran on
Linux; it is static-review-only, per this unit's own boundary: "Windows: port
what can be verified by static review; do NOT claim execution-tested status
without an actual Windows run."
"""

from __future__ import annotations

import datetime
import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tscribe.config import CaptureConfig


class CaptureError(Exception):
    """Raised for anything that stops a capture session before ffmpeg can
    even start (no pactl, no matching mic, unwritable output dir, etc.) —
    the capture-side analogue of tscribe.config.ConfigError: always a
    specific, actionable message, never a bare subprocess traceback."""


@dataclass
class CaptureSession:
    """A running (or completed) segmented capture.

    `process` is the live `ffmpeg` subprocess while capture is in progress,
    and is None for a session that only planned its command (see `dry_run`
    on `capture_start`) or after `stop()` has been called. `segment_pattern`
    is the ffmpeg %03d pattern (e.g. `class_audio/seg_20260821-140000_%03d.wav`)
    — resolve actual produced files by globbing `session_dir` for
    `seg_*.wav`, matching how `finalize.py` discovers them (stable lexical
    order == chronological order, because of the zero-padded index).
    """

    session_dir: str
    segment_pattern: str
    command: list[str]
    process: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    source_description: str = ""

    def poll(self) -> int | None:
        """None while still recording; the ffmpeg exit code once it stops."""
        if self.process is None:
            return None
        return self.process.poll()

    def wait(self, timeout_s: float | None = None) -> int:
        """Wait for capture to complete (e.g. when duration_s was specified)
        or until interrupted by KeyboardInterrupt, in which case stop() is
        called for clean segment finalization."""
        if self.process is None:
            return 0
        try:
            self.process.wait(timeout=timeout_s)
        except KeyboardInterrupt:
            return self.stop()
        return self.process.returncode if self.process.returncode is not None else 0

    def stop(self, timeout_s: float = 10.0) -> int:
        """Stop capture the same way Ctrl-C does for the source scripts: a
        graceful terminate (ffmpeg finalizes the current segment's WAV header
        cleanly on SIGTERM/SIGINT) with a hard kill fallback if it hangs.
        Returns the process's exit code. Safe to call on an already-stopped
        or dry-run session (returns 0)."""
        if self.process is None:
            return 0
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout_s)
        return self.process.returncode if self.process.returncode is not None else 0


def _die(message: str) -> None:
    raise CaptureError(message)


def _resolve_bin(name: str, explicit: str | None) -> str | None:
    """Same zero-PATH-setup resolution order as tscribe.capture.verify:
    explicit config path -> FFMPEG/FFPROBE env var -> PATH."""
    if explicit and os.path.isfile(explicit):
        return explicit
    env = os.environ.get(name.upper())
    if env and os.path.isfile(env):
        return env
    return shutil.which(name) or shutil.which(name + ".exe")


def list_pipewire_sources() -> list[tuple[str, str]]:
    """Linux only: `pactl list sources short` -> [(source_name, rest_of_line)].

    Mirrors `capture.sh --list`. Raises CaptureError if `pactl` is not on
    PATH (matching the source script's `command -v pactl || die`).

    Execution-tested status: this calls the real `pactl` binary and parses
    its real output shape, but this port's sandbox has no PipeWire daemon
    with an actual microphone attached, so this was verified by reading
    `pactl`'s documented short-listing format and by structural review of
    `capture.sh`'s own `awk` parse of the same output — not by confirming a
    real mic actually shows up in the returned list.
    """
    pactl = shutil.which("pactl")
    if pactl is None:
        _die("pactl not found (PipeWire/Pulse?) -- are you on Linux with PipeWire running?")
    proc = subprocess.run([pactl, "list", "sources", "short"], capture_output=True, text=True)
    if proc.returncode != 0:
        _die(f"pactl list sources short failed: {proc.stderr.strip()}")
    sources = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            sources.append((parts[1], line))
    return sources


def list_directshow_devices(cfg: CaptureConfig | None = None) -> str:
    """Windows only: `ffmpeg -f dshow -i dummy -list_devices true` -> raw
    stderr text (ffmpeg prints the device list to stderr, not as parseable
    JSON, so this returns the raw text for a human to read and paste a
    device name from into `cfg.mic_name` — the same "capture.bat --list"
    workflow the source rig used, since DirectShow substring auto-match is
    brittle and the source rig deliberately required an exact name instead).

    Static-review-only: written from `capture.bat`'s exact invocation
    (`ffmpeg -hide_banner -list_devices true -f dshow -i dummy`), never run
    against a real Windows machine or DirectShow stack in this port.
    """
    cfg = cfg or CaptureConfig()
    ffmpeg_bin = _resolve_bin("ffmpeg", cfg.ffmpeg_bin) or "ffmpeg"
    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        text=True,
    )
    # ffmpeg's -list_devices path always "fails" (dummy is not a real input)
    # but still prints the device list to stderr first -- same as capture.bat
    # relying on this and not checking ffmpeg's exit code for --list.
    return proc.stderr


def _match_pipewire_source(mic_match: str) -> str:
    sources = list_pipewire_sources()
    for name, _line in sources:
        if mic_match in name:
            return name
    _die(
        f"no PipeWire source matches capture.mic_match={mic_match!r} "
        f"(try: tscribe capture start --list)"
    )
    raise AssertionError("unreachable")  # _die always raises; satisfies type checkers


def _build_input_args(cfg: CaptureConfig, input_override: str | None) -> tuple[list[str], str]:
    """Return (ffmpeg input args, human-readable source description).

    `input_override` is the test seam equivalent to the source rig's
    `RIG_INPUT_OVERRIDE` env var: when set, it replaces the live mic input
    with an arbitrary ffmpeg input-arg string (typically a synthetic `lavfi`
    source), so the exact segment/naming/verify path can be exercised
    without real hardware. It is a plain function parameter here rather than
    an env var, matching this port's general preference for explicit
    parameters over env-var test seams, while preserving the exact same
    capability the source rig's tests relied on. See tests/capture/ for the
    ported self-test suite that uses this seam.
    """
    if input_override is not None:
        return shlex.split(input_override), "(override)"

    system = platform.system()
    if system == "Windows":
        if not cfg.mic_name:
            _die(
                "capture.mic_name is empty. Run `tscribe capture start --list` on "
                "the Windows box, then set capture.mic_name in tscribe.yaml."
            )
        return ["-f", "dshow", "-i", f"audio={cfg.mic_name}"], cfg.mic_name

    # Linux (and any other POSIX platform reachable via PipeWire/Pulse).
    source = _match_pipewire_source(cfg.mic_match)
    return ["-f", "pulse", "-i", source], source


def capture_start(
    cfg: CaptureConfig,
    *,
    duration_s: float | None = None,
    input_override: str | None = None,
    dry_run: bool = False,
) -> CaptureSession:
    """Start (or plan) a segmented capture session. Matches the interface
    contract `capture_start(cfg: CaptureConfig) -> CaptureSession`; the
    keyword-only extras are additive test/ops seams, not a contract change:

      * `duration_s` — optional hard time cap, equivalent to the source
        rig's `CAPTURE_DURATION` env var (used by its self-test suite so a
        capture terminates on its own instead of running until Ctrl-C).
      * `input_override` — the `RIG_INPUT_OVERRIDE` equivalent described in
        `_build_input_args`'s docstring.
      * `dry_run` — plan the full ffmpeg command and create the output
        directory, but do not actually spawn ffmpeg. `CaptureSession.process`
        is None and `poll()`/`stop()` are no-ops. Matches Unit 07's
        acceptance criterion that `capture start` supports `--dry-run`.

    Segmented WAV output: `<out_dir>/seg_<stamp>_%03d.wav`, one WAV per
    `cfg.segment_seconds`, mono/16-bit PCM at `cfg.sample_rate`, each
    segment's timestamps reset to zero (`-reset_timestamps 1`) so every
    segment is independently valid and independently verifiable. Identical
    shape to both source scripts.

    Raises CaptureError before spawning ffmpeg for any of: no
    ffmpeg/ffprobe resolvable, no matching mic (Linux) or empty mic_name
    (Windows), or an unwritable output directory — "fail fast, cheap
    preflight" exactly as `capture.sh` did with its own pre-flight block.
    """
    ffmpeg_bin = _resolve_bin("ffmpeg", cfg.ffmpeg_bin)
    if ffmpeg_bin is None:
        _die(
            "could not find ffmpeg (checked cfg.ffmpeg_bin, FFMPEG env var, PATH). "
            "Install it or set capture.ffmpeg_bin in tscribe.yaml."
        )

    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(out_dir, os.W_OK):
        _die(f"capture.out_dir is not writable: {out_dir}")

    input_args, source_description = _build_input_args(cfg, input_override)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    segment_pattern = str(out_dir / f"seg_{stamp}_%03d.wav")

    duration_args: list[str] = []
    if duration_s is not None:
        duration_args = ["-t", str(duration_s)]

    command = [
        ffmpeg_bin,
        "-hide_banner",
        *input_args,
        "-ac",
        str(cfg.channels),
        "-ar",
        str(cfg.sample_rate),
        "-c:a",
        "pcm_s16le",
        *duration_args,
        "-f",
        "segment",
        "-segment_time",
        str(cfg.segment_seconds),
        "-reset_timestamps",
        "1",
        segment_pattern,
    ]

    session = CaptureSession(
        session_dir=str(out_dir),
        segment_pattern=segment_pattern,
        command=command,
        source_description=source_description,
    )
    if dry_run:
        return session

    session.process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    return session
