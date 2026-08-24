# Hardware Audio Capture & End-to-End Test Plan

> **Purpose:** Step-by-step verification runbook for testing `tscribe capture`, `tscribe record`, and the downstream pipeline (`transcribe`, `summarize`, `tscribe-tui`) on real hardware with a physical microphone.

---

## 1. Scope & Objective

While unit tests mock audio hardware via synthetic ffmpeg sources (`lavfi`), this runbook exercises the physical hardware path:
1. Physical microphone discovery via PipeWire (`pactl`) on Linux and DirectShow on Windows.
2. Segmented multi-file recording (`tscribe capture start`) with timestamp resets.
3. Graceful termination and header finalization under `SIGINT` / Ctrl-C.
4. Intermediate segment contract verification (`tscribe capture verify`) against silence and format gates.
5. Lossless concatenation and duration verification (`tscribe capture finalize`).
6. End-to-end composition into local and remote transcription (`tscribe transcribe --summarize`).
7. Direct sounddevice recording (`tscribe devices` and `tscribe record`).
8. Real-time TUI job monitoring (`tscribe-tui`) against a running server.

---

## 2. Preconditions & Setup

### Hardware Checklist
- [ ] Computer running Linux (with PipeWire / PulseAudio) or Windows (10/11).
- [ ] Physical microphone attached (e.g., USB microphone like Tula/Blue Yeti/Rode, built-in mic, or line-in).
- [ ] Working speakers or headphones to optionally audit recorded takes.

### Software Checklist
- [ ] Python 3.10+ with `tscribe` installed: `pip install -e ".[all]"` (or `uv sync --all-extras`).
- [ ] `ffmpeg` and `ffprobe` accessible on `PATH` (or configured in `tscribe.yaml`).
- [ ] (Linux) `pactl` utility installed and PipeWire running.
- [ ] Rust toolchain (optional, for `tscribe-tui` monitor): `cargo --version`.

---

## 3. Step-by-Step Test Procedure

### Step 1: Device Discovery

Check that the hardware microphone is recognized by the operating system and audio layer.

**On Linux (PipeWire / Pulse):**
```bash
# List short PipeWire sources
tscribe capture start --list
```
*Expected output:* A list of audio sources with index, name, and driver string.
Example:
```
sources: [
  {
    "name": "alsa_input.usb-Tula_Mic_001-00.mono-fallback",
    "raw": "48\talsa_input.usb-Tula_Mic_001-00.mono-fallback\tPipeWire\ts16le 1ch 48000Hz\tRUNNING"
  }
]
```
Note down a unique substring of the device name (e.g. `Tula` or `usb-Tula_Mic`).

**On Windows (DirectShow):**
```bash
tscribe capture start --list
```
*Expected output:* ffmpeg stderr listing audio devices.
Example: `[dshow @ ...]  "Microphone (Realtek High Definition Audio)"`
Note down the exact device name string inside quotes.

---

### Step 2: Configuration Setup

Create a test `tscribe.yaml` in your test workspace configured for capture:

```yaml
mode: client
backend: local

local:
  model_size: small
  device: cpu        # or 'cuda' if GPU is available
  compute_type: int8 # or 'float16' for cuda

capture:
  # Linux: substring match against pactl source name
  mic_match: "Tula"  # Replace with substring from Step 1
  # Windows: exact name from DirectShow listing
  mic_name: ""       # Replace with exact name on Windows

  sample_rate: 16000
  channels: 1

  # Shortened segment time for testing (default in production is 900s / 15 min)
  segment_seconds: 10

  out_dir: "test_capture_session"

  duration_tolerance_segment_s: 2.0
  duration_tolerance_final_s: 5.0

  silence_warn_db: -50.0
  silence_fail_db: -60.0
```

---

### Step 3: Capture Dry-Run Validation

Verify that `tscribe capture start` builds the correct `ffmpeg` command string without starting recording:

```bash
tscribe capture start --dry-run
```

*Expected output:*
- Status 0 / exit code 0.
- Command displayed matching:
  `ffmpeg -f pulse -i <source> -ac 1 -ar 16000 -c:a pcm_s16le -f segment -segment_time 10 -reset_timestamps 1 test_capture_session/seg_<stamp>_%03d.wav` (on Linux) or `-f dshow -i audio="<mic_name>"` (on Windows).

---

### Step 4: Multi-Segment Live Recording

Run a timed capture session that produces multiple segments while speaking naturally into the microphone.

```bash
# Record for 25 seconds (with 10-second segments, produces seg_..._000.wav, seg_..._001.wav, seg_..._002.wav)
tscribe capture start --duration 25
```

**During this time:** Speak aloud into the microphone (e.g., read a sentence or count from 1 to 20).

*Verification Checklist:*
- [ ] Directory `test_capture_session/` is created.
- [ ] Three segment files are created: `seg_<timestamp>_000.wav`, `seg_<timestamp>_001.wav`, `seg_<timestamp>_002.wav`.
- [ ] File sizes are non-zero and roughly equal (~320 KB for 10s of 16kHz 16-bit mono).

---

### Step 5: Manual Interrupt & Graceful Header Finalization

Verify that interrupting a live recording session (e.g. with `Ctrl-C` / `SIGINT`) flushes the current segment cleanly with a valid WAV header.

```bash
# Start an indefinite capture session
tscribe capture start
```
1. Speak into the microphone for 5-8 seconds.
2. Press `Ctrl-C` to stop the session.

*Verification Checklist:*
- [ ] Process exits cleanly with code 0 (or 7 if declined/aborted before start).
- [ ] The partial segment WAV file decodes without errors (`ffprobe test_capture_session/seg_*.wav`).

---

### Step 6: Intermediate Segment Verification

Verify each segment against the capture contract gates.

```bash
for seg in test_capture_session/seg_*.wav; do
    echo "Verifying $seg..."
    tscribe --json capture verify "$seg" --expected-seconds 10
done
```

*Checks performed on each segment:*
1. `exists_nonempty`: File exists and size > 0.
2. `decodes`: ffprobe decodes format and duration successfully.
3. `sample_rate`: Exactly 16000 Hz.
4. `channels`: Exactly 1 (mono).
5. `duration_in_band`: Within tolerance (`segment_seconds ± 2.0s`).
6. `not_silence_floor`: Mean volume is above `-60.0 dBFS` (proving the mic wasn't muted or disconnected).

*Expected result:* All checks report `"ok": true`.

#### Intentional Silence Floor Gate Test (Negative Test):
1. Mute the physical microphone (or cover it completely in a silent room).
2. Record a 10s clip: `tscribe capture start --duration 10`.
3. Run: `tscribe capture verify test_capture_session/seg_<muted>_000.wav`.
4. *Expected result:* Fails with exit code 5 (`CONTRACT_VIOLATED`), identifying `not_silence_floor` failure, while preserving the audio file for audit.

---

### Step 7: Finalize & Concatenation Check

Concatenate all segments in the session directory into a single continuous take and verify the result:

```bash
tscribe capture finalize test_capture_session --out test_capture_session/final_lecture.wav
```

*Expected output:*
- Status 0 / exit code 0.
- Output file `test_capture_session/final_lecture.wav` created.
- Lossless concatenation verification:
  - Preserves duration across the sum of segments (`±5.0s`).
  - Passes all 6 capture contract checks on the combined file.

---

### Step 8: End-to-End Transcription & Summarization

Feed the finalized real-mic audio directly into `tscribe transcribe`:

```bash
# Transcribe and summarize
tscribe transcribe --summarize test_capture_session/final_lecture.wav
```

*Verification Checklist:*
- [ ] Audio passes the 18 transcription core verification checks (silence snapping, non-empty speech, WPM ceilings, non-degenerate reassembly).
- [ ] Output displays timestamps (`## [00:00:00]`) and the spoken words matching what was spoken in Step 4.
- [ ] LLM summary appears with markdown headings, key concepts, and action items.

---

### Step 9: Standalone Device Recording (`tscribe record`)

Verify sounddevice direct recording independently of ffmpeg segmentation:

```bash
# List sounddevice devices
tscribe devices

# Record 5 seconds to a standalone WAV
tscribe record --seconds 5 --out test_take.wav

# Transcribe the take
tscribe transcribe test_take.wav
```

*Verification Checklist:*
- [ ] Device indices match system ALSA / Pulse / PipeWire inputs.
- [ ] `test_take.wav` records cleanly and transcribes accurately.

---

### Step 10: TUI Monitor (`tscribe-tui`) Live Run

Test the terminal monitor against an active job on the local server or `tscribe-class-carnyx`:

```bash
# 1. Start or target a tscribe server on port 6390 with API key
export TSCRIBE_API_KEY="c3156010b3d7c58d2d17377763162164c5a9ae41cfe047f2"

# 2. Submit the finalized audio as a job via curl or CLI
JOB_ID=$(curl -s -H "X-API-Key: $TSCRIBE_API_KEY" -F "file=@test_capture_session/final_lecture.wav" http://localhost:6390/jobs/upload | jq -r .id)
echo "Submitted job: $JOB_ID"

# 3. Launch the TUI monitor to watch the job in real time
cargo run --manifest-path tui/Cargo.toml -- --port 6390 --job-id "$JOB_ID" --theme lufs
```

*Verification Checklist:*
- [ ] Masthead shows server `127.0.0.1:6390`, shortened job ID, and live status.
- [ ] Status transitions: `QUEUED` -> `RUNNING (since first observed)` -> `DONE ✓ VERIFIED`.
- [ ] Live elapsed counter advances every second.
- [ ] Once complete, verification check summary (`18/18 checks passed`) and transcript preview are rendered.
- [ ] Pressing `q` or `Esc` cleanly exits the TUI and restores the terminal alternate screen.

---

## 4. Exit Code & Contract Verification Reference

| Scenario | Expected Exit Code | Envelope `status` |
|---|---|---|
| Successful recording / verify / finalize | `0` | `"success"` |
| Device not found / no mic matching pattern | `2` (`USAGE`) | `"error"`, code 2 |
| Invalid `tscribe.yaml` capture settings | `3` (`CONFIG_INVALID`) | `"error"`, code 3 |
| Silence floor violation (dead mic) | `5` (`CONTRACT_VIOLATED`) | `"error"`, code 5 |
| User interrupt during prompt | `7` (`INTERRUPTED`) | `"error"`, code 7 |

---

## 5. Pass/Fail Sign-off Matrix

When executing this test run on a physical box, fill out the following sign-off table:

| Test Item | Pass/Fail | Notes / Device Used |
|---|---|---|
| **Step 1: Device Discovery** (`capture start --list`) | [ ] PASS | Device: |
| **Step 3: Capture Dry-Run** (`capture start --dry-run`) | [ ] PASS | |
| **Step 4: Multi-segment Capture** (3+ segments recorded) | [ ] PASS | Segments count: |
| **Step 5: Ctrl-C Clean Interruption** | [ ] PASS | Partial file decodes: |
| **Step 6: Segment Verification** (`capture verify`) | [ ] PASS | Silence floor dBFS: |
| **Step 6: Silence Floor Failure Gate** (Negative test) | [ ] PASS | Exit code 5 confirmed: |
| **Step 7: Finalize & Concat** (`capture finalize`) | [ ] PASS | Final duration: |
| **Step 8: Transcribe Composition** (`transcribe`) | [ ] PASS | Word accuracy: |
| **Step 9: Sounddevice Record** (`record`) | [ ] PASS | |
| **Step 10: TUI Monitor** (`tscribe-tui`) | [ ] PASS | Theme tested: |
