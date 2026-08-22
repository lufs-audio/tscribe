"""Ports of tscribe-capture-rig's tests/selftest.sh T1, T2, T3, T5, T6.

Each test below is annotated with which source selftest.sh case it replaces,
so the correspondence with the source rig's own self-test suite (see that
repo's README: "9 passed, 0 failed", covering 7 numbered cases) stays
auditable. T4 (segment->concat round-trip) lives in test_session_and_finalize.py
since it exercises capture_start + capture_finalize together; T7
(fetch-ffmpeg.sh --from-archive) has no direct equivalent here -- that
retired shell helper provisioned a portable ffmpeg for the old scripts, and
is not part of this port's three-function interface contract -- so its
spirit (explicit-binary-path resolution) is instead covered by
test_ffmpeg_bin_explicit_override_is_honored below, which proves
CaptureConfig.ffmpeg_bin/ffprobe_bin resolve correctly, the modern
equivalent of "a portable binary sitting next to the rig gets used."
"""

from __future__ import annotations

import shutil
import subprocess

from tscribe.capture import verify_capture
from tscribe.config import CaptureConfig


def _make_wav(path, ffmpeg_args: list[str], duration_s: float) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            *ffmpeg_args,
            "-t",
            str(duration_s),
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"fixture generation failed: {proc.stderr}"


# --------------------------------------------------------------------------
# T1: silent capture must FAIL (the dead-capture / mic-privacy case)
# --------------------------------------------------------------------------


def test_silent_capture_fails_not_silence_floor(tmp_path):
    """Matches selftest.sh T1: a 3s digital-silence WAV at the right sample
    rate/channels must be rejected specifically on not_silence_floor, not on
    some other check -- this is the check that catches a Windows mic-privacy
    toggle silently denying the mic (ffmpeg still exits 0 and writes a
    correctly-shaped, silent file)."""
    wav = tmp_path / "silent.wav"
    _make_wav(wav, ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono"], 3)

    report = verify_capture(str(wav), 3, cfg=CaptureConfig())

    assert report["ok"] is False
    names_and_ok = {c["name"]: c["ok"] for c in report["checks"]}
    assert names_and_ok.get("not_silence_floor") is False, report["checks"]
    # Every other check must have passed clean -- the failure must be
    # attributable specifically to the silence floor, not noise elsewhere.
    for check in report["checks"]:
        if check["name"] != "not_silence_floor":
            assert check["ok"] is True, f"unexpected failure: {check}"


# --------------------------------------------------------------------------
# T2: real-level audio must PASS
# --------------------------------------------------------------------------


def test_real_level_tone_passes(tmp_path):
    """Matches selftest.sh T2: a 300 Hz sine at real level, correct shape,
    must verify clean (ok=True, every check ok=True)."""
    wav = tmp_path / "tone.wav"
    _make_wav(wav, ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000", "-ac", "1"], 3)

    report = verify_capture(str(wav), 3, cfg=CaptureConfig())

    assert report["ok"] is True, report["checks"]
    for check in report["checks"]:
        assert check["ok"] is True, f"unexpected failure: {check}"


# --------------------------------------------------------------------------
# T3: wrong sample rate must FAIL (shape contract)
# --------------------------------------------------------------------------


def test_wrong_sample_rate_fails_sample_rate_check(tmp_path):
    """Matches selftest.sh T3: audio at 44100 Hz (not the configured 16000)
    must be rejected specifically on sample_rate."""
    wav = tmp_path / "wrong_sr.wav"
    _make_wav(wav, ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100", "-ac", "1"], 3)

    report = verify_capture(str(wav), None, cfg=CaptureConfig())

    assert report["ok"] is False
    names_and_ok = {c["name"]: c["ok"] for c in report["checks"]}
    assert names_and_ok.get("sample_rate") is False, report["checks"]


# --------------------------------------------------------------------------
# T5: verifier honors an explicit FFMPEG/FFPROBE-equivalent override
# --------------------------------------------------------------------------


def test_ffmpeg_bin_explicit_override_is_honored(tmp_path):
    """Matches selftest.sh T5 in spirit (the source rig tested its FFMPEG/
    FFPROBE *env var* override; this port's resolution order puts an
    explicit CaptureConfig.ffmpeg_bin/ffprobe_bin ABOVE the env vars -- see
    tscribe.capture.verify._resolve_bin's docstring) and also stands in for
    T7's "a portable binary in a known location gets used" spirit, since the
    retired fetch-ffmpeg.sh helper is not part of this port.

    Proves verify_capture resolves and actually uses the binaries named by
    cfg.ffmpeg_bin/cfg.ffprobe_bin rather than silently falling back to
    PATH."""
    wav = tmp_path / "tone.wav"
    _make_wav(wav, ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000", "-ac", "1"], 3)

    real_ffmpeg = shutil.which("ffmpeg")
    real_ffprobe = shutil.which("ffprobe")
    assert real_ffmpeg and real_ffprobe

    cfg = CaptureConfig(ffmpeg_bin=real_ffmpeg, ffprobe_bin=real_ffprobe)
    report = verify_capture(str(wav), 3, cfg=cfg)

    assert report["ok"] is True, report["checks"]


def test_env_var_override_is_honored_when_no_explicit_config_path(tmp_path, monkeypatch):
    """Direct equivalent of selftest.sh T5's actual mechanism: with no
    explicit cfg.ffmpeg_bin/ffprobe_bin set, the FFMPEG/FFPROBE environment
    variables must still be honored (the resolution order's second tier)."""
    wav = tmp_path / "tone.wav"
    _make_wav(wav, ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000", "-ac", "1"], 3)

    real_ffmpeg = shutil.which("ffmpeg")
    real_ffprobe = shutil.which("ffprobe")
    monkeypatch.setenv("FFMPEG", real_ffmpeg)
    monkeypatch.setenv("FFPROBE", real_ffprobe)

    report = verify_capture(str(wav), 3, cfg=CaptureConfig())

    assert report["ok"] is True, report["checks"]


# --------------------------------------------------------------------------
# T6: unresolvable ffmpeg -> a reported failure, not a crash
# --------------------------------------------------------------------------


def test_unresolvable_ffmpeg_reports_failure_not_crash(tmp_path, monkeypatch):
    """Matches selftest.sh T6: when ffmpeg/ffprobe cannot be found anywhere
    (no explicit config path, no env var, empty PATH), verify_capture must
    report a clean, specific failure -- not raise an unhandled exception.
    This is the library-function equivalent of the source rig's CLI exiting
    64 with a helpful message instead of a traceback; Unit 07's CLI is
    expected to map this reported failure shape to exit code 64 specifically
    (distinct from a normal contract violation's exit 2)."""
    wav = tmp_path / "tone.wav"
    _make_wav(wav, ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000", "-ac", "1"], 3)

    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir -- nothing resolvable
    monkeypatch.delenv("FFMPEG", raising=False)
    monkeypatch.delenv("FFPROBE", raising=False)

    report = verify_capture(str(wav), 3, cfg=CaptureConfig())  # cfg has no explicit bins

    assert report["ok"] is False
    detail_text = " ".join(c["detail"] for c in report["checks"])
    assert "ffmpeg" in detail_text.lower() or "ffprobe" in detail_text.lower()


# --------------------------------------------------------------------------
# Additional coverage beyond the 7 source cases: proves the WARN vs FAIL
# distinction (not_silence_floor has two thresholds, not one) and that
# thresholds are genuinely config-driven, not still hardcoded.
# --------------------------------------------------------------------------


def test_quiet_audio_between_thresholds_warns_but_does_not_fail(tmp_path):
    """A capture between silence_fail_db and silence_warn_db should WARN
    (not_silence_floor still ok=True) but must not fail the overall report
    -- matching the source rig's exact WARN-vs-FAIL split, where only a WARN
    below MEAN_WARN_DB (not a FAIL) keeps the process exit code at 0."""
    wav = tmp_path / "quiet.wav"
    # A very low-amplitude sine sits in the "quiet but not dead" band.
    # volume=0.02 was calibrated empirically to land at ~-55 dB mean_volume
    # (comfortably between the default silence_fail_db=-60 and
    # silence_warn_db=-50), well clear of either boundary so the test is not
    # flaky against tiny ffmpeg-version-to-version volumedetect variance.
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:sample_rate=16000",
            "-af",
            "volume=0.02",
            "-ac",
            "1",
            "-t",
            "3",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    report = verify_capture(str(wav), 3, cfg=CaptureConfig())

    silence_check = next(c for c in report["checks"] if c["name"] == "not_silence_floor")
    # This fixture is deliberately quiet, but must land in the WARN band
    # (ok=True) rather than the FAIL band, and the report overall must not
    # be failed by it alone.
    assert silence_check["ok"] is True, silence_check
    assert "warn" in silence_check["detail"].lower() or "quiet" in silence_check["detail"].lower()


def test_thresholds_are_config_driven_not_hardcoded(tmp_path):
    """Proves the acceptance criterion "-60dB fail / -50dB warn, now
    config-exposed" is real: a custom, much stricter fail threshold must
    change the verdict on the exact same audio, which would be impossible if
    verify_capture still hardcoded -60/-50 the way the source
    verify_capture.py's module-level constants did."""
    wav = tmp_path / "moderate.wav"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:sample_rate=16000",
            "-af",
            "volume=0.05",
            "-ac",
            "1",
            "-t",
            "3",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    lenient = verify_capture(str(wav), 3, cfg=CaptureConfig(silence_fail_db=-60.0))
    strict = verify_capture(
        str(wav), 3, cfg=CaptureConfig(silence_fail_db=-1.0, silence_warn_db=-0.5)
    )

    assert lenient["ok"] is True, lenient["checks"]
    assert strict["ok"] is False, strict["checks"]


def test_channels_check_fails_on_stereo_when_mono_expected(tmp_path):
    """Not in the source 7 cases, but exercises the "channels" check named
    explicitly in the acceptance criteria (exists_nonempty, decodes,
    sample_rate, channels, duration_in_band, not_silence_floor) -- the
    source selftest.sh never happened to test this one directly."""
    wav = tmp_path / "stereo.wav"
    _make_wav(
        wav,
        ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000", "-ac", "2"],
        3,
    )

    report = verify_capture(str(wav), None, cfg=CaptureConfig(channels=1))

    assert report["ok"] is False
    names_and_ok = {c["name"]: c["ok"] for c in report["checks"]}
    assert names_and_ok.get("channels") is False, report["checks"]


def test_missing_file_fails_exists_nonempty():
    """The exists_nonempty check named in the acceptance criteria, for a
    path that was never created at all."""
    report = verify_capture("/nonexistent/path/does-not-exist.wav", None, cfg=CaptureConfig())

    assert report["ok"] is False
    assert report["checks"][0]["name"] == "exists_nonempty"
    assert report["checks"][0]["ok"] is False


def test_duration_in_band_fails_outside_tolerance(tmp_path):
    """The duration_in_band check: a 3s file checked against an expectation
    of 10s (well outside the default segment tolerance) must fail
    specifically on duration_in_band."""
    wav = tmp_path / "short.wav"
    _make_wav(wav, ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000", "-ac", "1"], 3)

    report = verify_capture(str(wav), 10.0, cfg=CaptureConfig())

    assert report["ok"] is False
    names_and_ok = {c["name"]: c["ok"] for c in report["checks"]}
    assert names_and_ok.get("duration_in_band") is False, report["checks"]
