//! A minimal, hand-rolled HTTP client for `GET /jobs/{id}` -- the job-status
//! endpoint this TUI monitors. Built directly against the REAL, live source
//! of `tscribe-class-carnyx`'s `server/app.py` + `server/jobs.py` (read, not
//! guessed): per `SPEC.md` §5, tscribe's own Unit 03 ports that `/jobs`
//! contract **verbatim, byte-for-byte** as the new server's surface, so this
//! client is built against the contract tscribe itself has already committed
//! to reproducing -- it should keep working unchanged once Unit 03 lands,
//! pointed at a `tscribe --mode server` instance instead of carnyx.
//!
//! Unlike `lufs-audio/lrex`'s `tui`, which watches a persistent SSE stream,
//! this API has no push/stream endpoint -- `server/jobs.py`'s `Job` is a
//! plain polled resource. So this is request/response on a timer, not a held
//! connection: ask "what's the status now?", get an answer, sleep, repeat.
use serde::Deserialize;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::Duration;

#[derive(Debug, Clone, Deserialize)]
pub struct Check {
    pub name: String,
    pub ok: bool,
    #[serde(default)]
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Report {
    pub ok: bool,
    #[serde(default)]
    pub checks: Vec<Check>,
    // `server/jobs.py::_report_to_dict` sends this as well: the same failing
    // check names as `checks.iter().filter(|c| !c.ok)`, minus each check's
    // `detail`. `app.rs`'s renderer deliberately reads the richer `checks`
    // list instead (name + detail, not just name), so this field is kept
    // for accurate, complete modeling of the real wire shape -- not dead
    // logic, just a field this particular consumer doesn't need to re-derive
    // information `checks` already gives it in full.
    #[serde(default)]
    #[allow(dead_code)]
    pub failures: Vec<String>,
}

/// Mirrors `server/jobs.py`'s `Job.public()` field for field, read directly
/// from that source. Every field is defensively defaulted on parse (not just
/// the ones observed to sometimes be absent) -- the same "don't trust the
/// wire shape to stay exactly as read today" discipline `lrex`'s `tui`
/// applies to its own JSON, and doubly warranted here since this contract is
/// *planned* to be reproduced by a not-yet-written server, not just served
/// by a stable one.
#[derive(Debug, Clone, Deserialize)]
pub struct JobSnapshot {
    pub id: String,
    pub status: String, // "queued" | "running" | "done" | "failed"
    #[serde(default)]
    pub verified: bool,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub created_at: f64,
    #[serde(default)]
    pub updated_at: f64,
    #[serde(default)]
    pub transcript: Option<String>,
    #[serde(default)]
    pub report: Option<Report>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub transcript_file_id: Option<String>,
    #[serde(default)]
    pub upload_error: Option<String>,
    #[serde(default)]
    pub moved: Vec<String>,
    #[serde(default)]
    pub move_errors: Vec<String>,
}

impl JobSnapshot {
    /// `updated_at - created_at`, both server timestamps -- a duration
    /// computed entirely from the server's own clock, so it can't be thrown
    /// off by skew between this machine's clock and the server's (unlike
    /// comparing a server timestamp against a local `SystemTime::now()`).
    pub fn elapsed_s(&self) -> f64 {
        (self.updated_at - self.created_at).max(0.0)
    }

    pub fn is_terminal(&self) -> bool {
        matches!(self.status.as_str(), "done" | "failed")
    }
}

/// What happened when we tried to check on a job. Every variant is a real,
/// distinct outcome the actual server code can produce (read from
/// `server/app.py`'s route definitions directly): a 401 means the API key is
/// wrong, a 503 means the server has no key configured **at all** (it fails
/// closed rather than running open), a 404 means the job id doesn't exist,
/// and anything else is reported with its real status code rather than
/// folded into one generic failure.
#[derive(Debug, Clone)]
pub enum PollOutcome {
    // Boxed so this variant doesn't blow up the size of every other variant
    // in the enum (`JobSnapshot` carries several Strings/Vecs/an Option --
    // clippy's `large_enum_variant` correctly flags the ~11x size gap
    // between this and e.g. `NotFound` otherwise).
    Snapshot(Box<JobSnapshot>),
    NotFound,
    AuthFailed,
    ServerMisconfigured,
    UnexpectedStatus(u16, String),
    Unreachable(String),
}

const READ_TIMEOUT: Duration = Duration::from_secs(10);
/// While a job is actively queued/running, a human watching benefits from a
/// near-live view.
const ACTIVE_POLL_INTERVAL: Duration = Duration::from_secs(1);
/// Once a job has reached a terminal state, or the server/job/credentials
/// are in some error condition that won't resolve itself between one poll
/// and the next, there is nothing time-sensitive left to observe -- polling
/// every second forever would just be load with no signal. A job's own
/// timeline is minutes to hours (a real four-hour GPU run is the motivating
/// case in `SPEC.md`), so a 1s-vs-5s difference is immaterial to a human but
/// meaningfully kinder to the server.
const SLOW_POLL_INTERVAL: Duration = Duration::from_secs(5);

fn next_interval(outcome: &PollOutcome) -> Duration {
    match outcome {
        PollOutcome::Snapshot(s) if !s.is_terminal() => ACTIVE_POLL_INTERVAL,
        _ => SLOW_POLL_INTERVAL,
    }
}

/// Spawns a background thread that polls `GET /jobs/{job_id}` forever (until
/// the receiver is dropped), forwarding every outcome over the returned
/// channel. Runs on its own thread -- not inline in the render loop -- for
/// the same reason `lrex`'s SSE reader does: a slow or wedged server must
/// never freeze key handling, and `poll_once` can block for up to
/// `READ_TIMEOUT`.
pub fn spawn(host: String, port: u16, api_key: String, job_id: String) -> Receiver<PollOutcome> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || loop {
        let outcome = poll_once(&host, port, &api_key, &job_id);
        let interval = next_interval(&outcome);
        if tx.send(outcome).is_err() {
            return; // receiver gone -- TUI is exiting, stop quietly
        }
        thread::sleep(interval);
    });
    rx
}

/// One blocking `GET /jobs/{job_id}`. No retry/reconnect loop in here (unlike
/// `lrex::tui::stream`'s persistent-stream client) -- the caller is already
/// on its own timer via `spawn` and will call again next tick regardless of
/// outcome.
pub fn poll_once(host: &str, port: u16, api_key: &str, job_id: &str) -> PollOutcome {
    match poll_inner(host, port, api_key, job_id) {
        Ok(outcome) => outcome,
        Err(e) => PollOutcome::Unreachable(e.to_string()),
    }
}

fn poll_inner(host: &str, port: u16, api_key: &str, job_id: &str) -> std::io::Result<PollOutcome> {
    let mut stream = TcpStream::connect((host, port))?;
    stream.set_read_timeout(Some(READ_TIMEOUT))?;
    stream.set_write_timeout(Some(READ_TIMEOUT))?;

    let request = format!(
        "GET /jobs/{job_id} HTTP/1.1\r\nHost: {host}:{port}\r\nX-API-Key: {api_key}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;

    let mut reader = BufReader::new(stream);

    let mut status_line = String::new();
    reader.read_line(&mut status_line)?;
    let status_code = parse_status_code(&status_line).unwrap_or(0);

    let mut content_length: Option<usize> = None;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            break; // connection closed before headers finished
        }
        let trimmed = line.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            break; // blank line -- end of headers
        }
        if let Some(v) = trimmed.to_ascii_lowercase().strip_prefix("content-length:") {
            content_length = v.trim().parse().ok();
        }
    }

    let mut body = Vec::new();
    match content_length {
        Some(n) => {
            body.resize(n, 0);
            reader.read_exact(&mut body)?;
        }
        None => {
            reader.read_to_end(&mut body)?;
        }
    }
    let body_str = String::from_utf8_lossy(&body);

    Ok(match status_code {
        200 => match serde_json::from_str::<JobSnapshot>(&body_str) {
            Ok(snap) => PollOutcome::Snapshot(Box::new(snap)),
            Err(e) => PollOutcome::UnexpectedStatus(
                200,
                format!("200 but body didn't match the expected job shape: {e}"),
            ),
        },
        401 => PollOutcome::AuthFailed,
        404 => PollOutcome::NotFound,
        503 => PollOutcome::ServerMisconfigured,
        other => PollOutcome::UnexpectedStatus(other, body_str.into_owned()),
    })
}

fn parse_status_code(status_line: &str) -> Option<u16> {
    // "HTTP/1.1 200 OK\r\n" -> 200
    status_line.split_whitespace().nth(1)?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_status_code_from_a_real_status_line() {
        assert_eq!(parse_status_code("HTTP/1.1 200 OK\r\n"), Some(200));
        assert_eq!(parse_status_code("HTTP/1.1 404 Not Found\r\n"), Some(404));
        assert_eq!(parse_status_code("garbage"), None);
    }

    #[test]
    fn job_snapshot_parses_the_real_public_shape() {
        // Field-for-field from `server/jobs.py`'s `Job.public()`, including a
        // populated report -- confirmed against the real source, not guessed.
        let raw = serde_json::json!({
            "id": "abc123",
            "status": "done",
            "verified": true,
            "source": "drive:xyz",
            "created_at": 1000.0,
            "updated_at": 1042.5,
            "transcript": "hello world",
            "report": {
                "ok": true,
                "checks": [{"name": "coverage", "ok": true, "detail": null}],
                "failures": []
            },
            "error": null,
            "transcript_file_id": "drive-file-1",
            "upload_error": null,
            "moved": ["asset-1"],
            "move_errors": []
        });
        let snap: JobSnapshot = serde_json::from_value(raw).unwrap();
        assert_eq!(snap.status, "done");
        assert!(snap.verified);
        assert!((snap.elapsed_s() - 42.5).abs() < 1e-9);
        assert!(snap.is_terminal());
        let report = snap.report.unwrap();
        assert!(report.ok);
        assert_eq!(report.checks.len(), 1);
    }

    #[test]
    fn job_snapshot_handles_a_failed_job_that_still_carries_a_transcript() {
        // jobs.py is explicit that a failed job keeps its transcript + report
        // as evidence -- `verified` is what actually gates usability, not
        // "is a transcript present."
        let raw = serde_json::json!({
            "id": "def456",
            "status": "failed",
            "verified": false,
            "source": "upload",
            "created_at": 500.0,
            "updated_at": 530.0,
            "transcript": "partial evidence transcript",
            "report": {"ok": false, "checks": [], "failures": ["coverage"]},
            "error": "ContractViolation: coverage below threshold",
            "transcript_file_id": null,
            "upload_error": null,
            "moved": [],
            "move_errors": []
        });
        let snap: JobSnapshot = serde_json::from_value(raw).unwrap();
        assert!(!snap.verified);
        assert!(snap.transcript.is_some());
        assert!(snap.is_terminal());
        assert_eq!(snap.report.unwrap().failures, vec!["coverage".to_string()]);
    }

    #[test]
    fn job_snapshot_queued_job_has_no_report_or_transcript_yet() {
        let raw = serde_json::json!({
            "id": "ghi789",
            "status": "queued",
            "verified": false,
            "source": "drive:xyz",
            "created_at": 900.0,
            "updated_at": 900.0,
            "transcript": null,
            "report": null,
            "error": null,
            "transcript_file_id": null,
            "upload_error": null,
            "moved": [],
            "move_errors": []
        });
        let snap: JobSnapshot = serde_json::from_value(raw).unwrap();
        assert!(!snap.is_terminal());
        assert!(snap.report.is_none());
        assert_eq!(snap.elapsed_s(), 0.0);
    }

    #[test]
    fn elapsed_s_never_goes_negative_even_on_a_bogus_clock() {
        let raw = serde_json::json!({
            "id": "x", "status": "running", "verified": false, "source": "x",
            "created_at": 1000.0, "updated_at": 999.0,
            "transcript": null, "report": null, "error": null,
            "transcript_file_id": null, "upload_error": null,
            "moved": [], "move_errors": []
        });
        let snap: JobSnapshot = serde_json::from_value(raw).unwrap();
        assert_eq!(snap.elapsed_s(), 0.0);
    }

    #[test]
    fn missing_optional_fields_do_not_panic_or_fail_to_parse() {
        // The minimum a caller could reasonably send and still call it a
        // job: just id + status. Every other field must default rather than
        // reject the payload outright.
        let raw = serde_json::json!({"id": "bare", "status": "queued"});
        let snap: JobSnapshot = serde_json::from_value(raw).unwrap();
        assert_eq!(snap.id, "bare");
        assert!(!snap.verified);
        assert!(snap.moved.is_empty());
    }

    #[test]
    fn next_interval_is_fast_only_while_actively_in_progress() {
        let queued = PollOutcome::Snapshot(Box::new(
            serde_json::from_value(serde_json::json!({"id": "a", "status": "queued"})).unwrap(),
        ));
        let running = PollOutcome::Snapshot(Box::new(
            serde_json::from_value(serde_json::json!({"id": "a", "status": "running"})).unwrap(),
        ));
        let done = PollOutcome::Snapshot(Box::new(
            serde_json::from_value(serde_json::json!({"id": "a", "status": "done"})).unwrap(),
        ));
        assert_eq!(next_interval(&queued), ACTIVE_POLL_INTERVAL);
        assert_eq!(next_interval(&running), ACTIVE_POLL_INTERVAL);
        assert_eq!(next_interval(&done), SLOW_POLL_INTERVAL);
        assert_eq!(next_interval(&PollOutcome::NotFound), SLOW_POLL_INTERVAL);
        assert_eq!(next_interval(&PollOutcome::AuthFailed), SLOW_POLL_INTERVAL);
        assert_eq!(
            next_interval(&PollOutcome::Unreachable("x".into())),
            SLOW_POLL_INTERVAL
        );
    }
}
