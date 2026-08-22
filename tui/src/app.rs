//! The live job-status TUI -- a `ratatui` client of the `/jobs/{id}` polling
//! endpoint (see `client.rs`). Deliberately a MONITOR, not a control
//! surface: there is no code path here that creates, cancels, or retries a
//! job. Start one with `curl` or the `tscribe` CLI; this only watches.
use crate::client::{self, JobSnapshot, PollOutcome};
use crate::theme::{Theme, ThemeKind};
use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::{Frame, Terminal};
use std::io::{self, Stdout};
use std::sync::mpsc::{Receiver, TryRecvError};
use std::time::{Duration, Instant};

struct AppState {
    theme: Theme,
    tool_name: String,
    host: String,
    port: u16,
    job_id: String,
    latest: Option<PollOutcome>,
    /// When THIS TUI first observed the job as `running`. NOT when the job
    /// truly started running (the real server doesn't expose that as a live
    /// value -- see `running_elapsed_s`'s doc comment for why
    /// `JobSnapshot::elapsed_s()` can't be used for this).
    running_since: Option<Instant>,
    log: String,
}

impl AppState {
    fn new(host: String, port: u16, job_id: String, theme: Theme) -> Self {
        Self {
            theme,
            tool_name: crate::invoked_name(),
            host,
            port,
            job_id,
            latest: None,
            running_since: None,
            log: "connecting…".to_string(),
        }
    }

    fn apply(&mut self, outcome: PollOutcome) {
        if let PollOutcome::Snapshot(s) = &outcome {
            if s.status == "running" && self.running_since.is_none() {
                self.running_since = Some(Instant::now());
            }
        }
        self.log = describe(&outcome, &self.job_id, self.running_elapsed_s());
        self.latest = Some(outcome);
    }

    /// Wall-clock time since THIS TUI first observed the job as `running` --
    /// deliberately NOT derived from `JobSnapshot::elapsed_s()`
    /// (`updated_at - created_at`). The real server (`jobs.py`) only calls
    /// `touch()` -- which is what advances `updated_at` -- once entering
    /// `running` and once more at `done`/`failed`; it is never touched as a
    /// heartbeat during the potentially hours-long transcription run in
    /// between. So `elapsed_s()` stays essentially frozen at "a few seconds"
    /// for the entire run this tool exists to watch, which is a real bug,
    /// not a nitpick, for a tool whose motivating case is a four-hour job.
    /// This local measurement is honest about its own limit too: for a job
    /// that was already running before this TUI attached, it understates
    /// the job's true total runtime -- an inherent consequence of the
    /// server not exposing a live elapsed value, not something this client
    /// can paper over. `describe`/`draw_snapshot` say "since first observed"
    /// rather than implying otherwise.
    fn running_elapsed_s(&self) -> f64 {
        self.running_since
            .map_or(0.0, |t| t.elapsed().as_secs_f64())
    }

    /// The masthead's right-hand status word + its color. Always paired with
    /// distinguishing TEXT (see `describe`/`draw_snapshot`), never a signal
    /// carried by color alone -- under the `mono` theme, `done`/`running`/
    /// `failed` all collapse to the same ink color by construction
    /// (`Theme::mono`), so the word itself is what actually distinguishes
    /// the states there.
    fn masthead_status_word(&self) -> (String, Color) {
        match &self.latest {
            None => ("connecting".to_string(), self.theme.dim),
            Some(PollOutcome::Snapshot(s)) => match s.status.as_str() {
                "queued" => ("queued".to_string(), self.theme.dim),
                "running" => ("running".to_string(), self.theme.running),
                "done" if s.verified => ("done ✓".to_string(), self.theme.done),
                "done" => ("done, unverified".to_string(), self.theme.failed),
                "failed" => ("failed ✗".to_string(), self.theme.failed),
                other => (other.to_string(), self.theme.dim),
            },
            Some(PollOutcome::NotFound) => ("not found".to_string(), self.theme.failed),
            Some(PollOutcome::AuthFailed) => ("auth failed".to_string(), self.theme.failed),
            Some(PollOutcome::ServerMisconfigured) => {
                ("server misconfigured".to_string(), self.theme.failed)
            }
            Some(PollOutcome::UnexpectedStatus(code, _)) => {
                (format!("http {code}"), self.theme.failed)
            }
            Some(PollOutcome::Unreachable(_)) => ("unreachable".to_string(), self.theme.failed),
        }
    }
}

fn short_id(id: &str) -> String {
    let count = id.chars().count();
    if count > 10 {
        let head: String = id.chars().take(8).collect();
        format!("{head}…")
    } else {
        id.to_string()
    }
}

fn describe(outcome: &PollOutcome, job_id: &str, running_elapsed_s: f64) -> String {
    match outcome {
        PollOutcome::Snapshot(s) => match s.status.as_str() {
            "queued" => "queued — waiting to start".to_string(),
            "running" => format!("running · {running_elapsed_s:.0}s since first observed"),
            "done" if s.verified => format!("done · verified · {:.0}s", s.elapsed_s()),
            "done" => format!("done · NOT verified · {:.0}s", s.elapsed_s()),
            "failed" => format!(
                "failed{}",
                s.error
                    .as_deref()
                    .map(|e| format!(": {e}"))
                    .unwrap_or_default()
            ),
            other => format!("status: {other}"),
        },
        PollOutcome::NotFound => format!("no job '{job_id}' found"),
        PollOutcome::AuthFailed => "authentication failed — check the API key".to_string(),
        PollOutcome::ServerMisconfigured => {
            "server has no API key configured (fails closed)".to_string()
        }
        PollOutcome::UnexpectedStatus(code, _) => format!("unexpected HTTP {code}"),
        PollOutcome::Unreachable(msg) => format!("cannot reach the server — {msg}"),
    }
}

fn truncate_preview(s: &str, max_chars: usize) -> String {
    let flattened: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    let count = flattened.chars().count();
    if count <= max_chars {
        flattened
    } else {
        let head: String = flattened.chars().take(max_chars).collect();
        format!("{head}…")
    }
}

struct TerminalGuard {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TerminalGuard {
    fn new() -> io::Result<Self> {
        enable_raw_mode()?;
        let mut stdout = io::stdout();
        execute!(stdout, EnterAlternateScreen)?;
        let backend = CrosstermBackend::new(stdout);
        let terminal = Terminal::new(backend)?;
        Ok(Self { terminal })
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(io::stdout(), LeaveAlternateScreen);
    }
}

pub fn run(
    host: String,
    port: u16,
    job_id: String,
    api_key: String,
    theme_kind: ThemeKind,
) -> anyhow::Result<()> {
    let guard = TerminalGuard::new()?;
    let rx = client::spawn(host.clone(), port, api_key, job_id.clone());
    let mut state = AppState::new(host, port, job_id, Theme::from_kind(theme_kind));
    event_loop(guard, &mut state, rx)
}

fn event_loop(
    mut guard: TerminalGuard,
    state: &mut AppState,
    rx: Receiver<PollOutcome>,
) -> anyhow::Result<()> {
    loop {
        guard.terminal.draw(|frame| draw(frame, state))?;

        loop {
            match rx.try_recv() {
                Ok(outcome) => state.apply(outcome),
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => break,
            }
        }

        if event::poll(Duration::from_millis(120))? {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press
                    && matches!(key.code, KeyCode::Char('q') | KeyCode::Esc)
                {
                    drop(guard);
                    return Ok(());
                }
            }
        }
    }
}

fn draw(frame: &mut Frame, state: &AppState) {
    let theme = state.theme;
    let area = frame.area();
    frame.render_widget(
        ratatui::widgets::Block::default().style(Style::default().bg(theme.ground).fg(theme.ink)),
        area,
    );

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(3),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .split(area);

    draw_masthead(frame, chunks[0], state);
    draw_body(frame, chunks[1], state);
    draw_cli_line(frame, chunks[2], state);
    draw_footer(frame, chunks[3], state);
}

fn draw_masthead(frame: &mut Frame, area: Rect, state: &AppState) {
    let (status_word, status_color) = state.masthead_status_word();
    let left = Span::styled(
        state.tool_name.clone(),
        Style::default().add_modifier(Modifier::BOLD),
    );
    let right = format!(
        "{}:{} · job {} · {status_word}",
        state.host,
        state.port,
        short_id(&state.job_id)
    );
    let left_len = state.tool_name.chars().count();
    let gap = (area.width as usize).saturating_sub(left_len + right.chars().count() + 2);
    let line = Line::from(vec![
        left,
        Span::raw(" ".repeat(gap.max(1))),
        Span::styled(right, Style::default().fg(status_color)),
    ]);
    frame.render_widget(Paragraph::new(line), area);
}

/// The footer's cli-equivalent label. The API key is deliberately redacted
/// here -- this is a display-only string (the real request in `client.rs`
/// uses the real key over the socket, never through this function), and a
/// terminal screen is not a safe place to echo a live secret back (screen
/// recordings, shoulder-surfing, and scrollback capture can all read it).
fn cli_line_command(host: &str, port: u16, job_id: &str) -> String {
    format!("curl -H 'X-API-Key: ***' http://{host}:{port}/jobs/{job_id}")
}

fn draw_cli_line(frame: &mut Frame, area: Rect, state: &AppState) {
    let theme = state.theme;
    let cmd = cli_line_command(&state.host, state.port, &state.job_id);
    let line = Line::from(vec![
        Span::styled("$ ", Style::default().fg(theme.accent)),
        Span::styled(cmd, Style::default().fg(theme.dim)),
    ]);
    frame.render_widget(Paragraph::new(line), area);
}

fn draw_footer(frame: &mut Frame, area: Rect, state: &AppState) {
    let theme = state.theme;
    let keys = "q quit";
    let left_len = state.log.chars().count();
    let gap = (area.width as usize).saturating_sub(left_len + keys.len() + 2);
    let line = Line::from(vec![
        Span::styled(
            state.log.clone(),
            Style::default()
                .fg(theme.ink)
                .add_modifier(Modifier::ITALIC),
        ),
        Span::raw(" ".repeat(gap.max(1))),
        Span::styled(keys, Style::default().fg(theme.dim)),
    ]);
    frame.render_widget(Paragraph::new(line), area);
}

fn draw_body(frame: &mut Frame, area: Rect, state: &AppState) {
    let theme = state.theme;
    let mut lines: Vec<Line<'static>> = Vec::new();

    match &state.latest {
        None => {
            lines.push(Line::styled(
                format!("connecting to {}:{}…", state.host, state.port),
                Style::default().fg(theme.dim),
            ));
        }
        Some(PollOutcome::Unreachable(msg)) => {
            lines.push(Line::styled(
                format!("cannot reach {}:{}", state.host, state.port),
                Style::default()
                    .fg(theme.failed)
                    .add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::raw(""));
            lines.push(Line::styled(
                "is a tscribe (or, today, tscribe-class-carnyx) server running there?",
                Style::default().fg(theme.dim),
            ));
            lines.push(Line::styled(
                truncate_preview(msg, 100),
                Style::default().fg(theme.dim2),
            ));
        }
        Some(PollOutcome::AuthFailed) => {
            lines.push(Line::styled(
                "authentication failed",
                Style::default()
                    .fg(theme.failed)
                    .add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::raw(""));
            lines.push(Line::styled(
                "the API key this TUI sent doesn't match the server's.",
                Style::default().fg(theme.dim),
            ));
            lines.push(Line::styled(
                "check --api-key or $TSCRIBE_API_KEY.",
                Style::default().fg(theme.dim),
            ));
        }
        Some(PollOutcome::ServerMisconfigured) => {
            lines.push(Line::styled(
                "server has no API key configured",
                Style::default()
                    .fg(theme.failed)
                    .add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::raw(""));
            lines.push(Line::styled(
                "it fails closed rather than accepting unauthenticated requests --",
                Style::default().fg(theme.dim),
            ));
            lines.push(Line::styled(
                "this isn't about your key; nothing can authenticate right now.",
                Style::default().fg(theme.dim),
            ));
        }
        Some(PollOutcome::NotFound) => {
            lines.push(Line::styled(
                format!("no job '{}' at {}:{}", state.job_id, state.host, state.port),
                Style::default()
                    .fg(theme.failed)
                    .add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::raw(""));
            lines.push(Line::styled(
                "check the id -- this TUI only watches a job someone already",
                Style::default().fg(theme.dim),
            ));
            lines.push(Line::styled(
                "started (via curl or the tscribe CLI); it can't create one.",
                Style::default().fg(theme.dim),
            ));
        }
        Some(PollOutcome::UnexpectedStatus(code, body)) => {
            lines.push(Line::styled(
                format!("unexpected HTTP {code}"),
                Style::default()
                    .fg(theme.failed)
                    .add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::raw(""));
            lines.push(Line::styled(
                truncate_preview(body, 200),
                Style::default().fg(theme.dim),
            ));
        }
        Some(PollOutcome::Snapshot(snap)) => draw_snapshot(
            &mut lines,
            snap,
            &theme,
            &state.job_id,
            state.running_elapsed_s(),
        ),
    }

    frame.render_widget(Paragraph::new(lines), area);
}

fn draw_snapshot(
    lines: &mut Vec<Line<'static>>,
    snap: &JobSnapshot,
    theme: &Theme,
    expected_id: &str,
    running_elapsed_s: f64,
) {
    // Trust, but verify: the server is asked for a specific job id, so its
    // response claiming to be about a DIFFERENT id (a proxy/cache bug, a
    // keying bug on the server, anything) is not "close enough" data to
    // render as if it were the requested job -- it's a distinct failure
    // mode in its own right, exactly the class of thing this whole
    // ecosystem's "verified, not merely exited 0" doctrine exists to catch.
    if snap.id != expected_id {
        lines.push(Line::styled(
            format!(
                "⚠ server returned job '{}', not the requested '{expected_id}'",
                snap.id
            ),
            Style::default()
                .fg(theme.failed)
                .add_modifier(Modifier::BOLD),
        ));
        lines.push(Line::styled(
            "refusing to display this as if it were the requested job.",
            Style::default().fg(theme.dim),
        ));
        return;
    }

    // `running`'s elapsed figure is measured locally (see
    // `AppState::running_elapsed_s`'s doc comment for why: the real server
    // never updates `updated_at` during the run itself, only at its start
    // and end) and says so explicitly, rather than silently passing off a
    // frozen or understated number as the job's true total runtime.
    let (word, color, elapsed_display, elapsed_suffix) = match snap.status.as_str() {
        "queued" => ("QUEUED".to_string(), theme.dim, snap.elapsed_s(), ""),
        "running" => (
            "RUNNING".to_string(),
            theme.running,
            running_elapsed_s,
            " (since first observed)",
        ),
        "done" if snap.verified => (
            "DONE ✓ VERIFIED".to_string(),
            theme.done,
            snap.elapsed_s(),
            "",
        ),
        "done" => (
            "DONE, NOT VERIFIED".to_string(),
            theme.failed,
            snap.elapsed_s(),
            "",
        ),
        "failed" => ("FAILED ✗".to_string(), theme.failed, snap.elapsed_s(), ""),
        other => (other.to_uppercase(), theme.dim, snap.elapsed_s(), ""),
    };
    lines.push(Line::styled(
        format!("{word} · {elapsed_display:.0}s{elapsed_suffix}"),
        Style::default().fg(color).add_modifier(Modifier::BOLD),
    ));
    lines.push(Line::raw(""));
    lines.push(Line::styled(
        format!("source: {}", snap.source),
        Style::default().fg(theme.dim),
    ));

    if !snap.is_terminal() {
        lines.push(Line::styled(
            "no finer-grained progress than this is available yet --",
            Style::default().fg(theme.dim2),
        ));
        lines.push(Line::styled(
            "the job API reports status, not a percent-complete.",
            Style::default().fg(theme.dim2),
        ));
        return;
    }

    if let Some(report) = &snap.report {
        let passed = report.checks.iter().filter(|c| c.ok).count();
        lines.push(Line::styled(
            format!(
                "report: {passed}/{} checks passed{}",
                report.checks.len(),
                if report.ok { "" } else { " -- NOT ok" }
            ),
            Style::default().fg(if report.ok { theme.dim } else { theme.failed }),
        ));
        // Walk `checks[]` itself (name + ok + detail) rather than the
        // separate `failures[]` list `server/jobs.py::_report_to_dict`
        // also sends: `failures` is just the same failing names again with
        // no `detail`, so reading `checks` directly surfaces strictly more
        // of what the server actually reported -- which check, and why.
        for c in report.checks.iter().filter(|c| !c.ok) {
            let detail = c
                .detail
                .as_deref()
                .map(|d| format!(" — {d}"))
                .unwrap_or_default();
            lines.push(Line::styled(
                format!("  ✗ {}{detail}", c.name),
                Style::default().fg(theme.failed),
            ));
        }
    }

    if let Some(err) = &snap.error {
        lines.push(Line::styled(
            format!("error: {err}"),
            Style::default().fg(theme.failed),
        ));
    }

    if let Some(t) = &snap.transcript {
        lines.push(Line::raw(""));
        lines.push(Line::styled(
            format!("transcript preview: {}", truncate_preview(t, 160)),
            Style::default().fg(theme.dim),
        ));
    }

    if let Some(fid) = &snap.transcript_file_id {
        lines.push(Line::styled(
            format!("written back to Drive: {fid}"),
            Style::default().fg(theme.dim),
        ));
    }
    if let Some(ue) = &snap.upload_error {
        lines.push(Line::styled(
            format!("Drive write-back failed (transcript is still safe above): {ue}"),
            Style::default().fg(theme.failed),
        ));
    }
    if !snap.moved.is_empty() {
        lines.push(Line::styled(
            format!(
                "{} source asset(s) grouped into the destination folder",
                snap.moved.len()
            ),
            Style::default().fg(theme.dim),
        ));
    }
    for e in &snap.move_errors {
        lines.push(Line::styled(
            format!("  move error: {e}"),
            Style::default().fg(theme.failed),
        ));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(status: &str, verified: bool) -> JobSnapshot {
        serde_json::from_value(serde_json::json!({
            "id": "abcdef0123456789",
            "status": status,
            "verified": verified,
            "source": "drive:xyz",
            "created_at": 1000.0,
            "updated_at": 1010.0,
        }))
        .unwrap()
    }

    /// Flattens rendered lines to plain text for substring assertions --
    /// tests care about what a reader would see, not `ratatui`'s internal
    /// `Line`/`Span` structure.
    fn render_text(lines: &[Line<'static>]) -> String {
        lines
            .iter()
            .map(|l| {
                l.spans
                    .iter()
                    .map(|s| s.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn short_id_passes_through_short_ids_unchanged() {
        assert_eq!(short_id("abc123"), "abc123");
    }

    #[test]
    fn short_id_truncates_long_ids_with_an_ellipsis() {
        let long = "abcdef0123456789abcdef0123456789";
        let short = short_id(long);
        assert!(short.ends_with('…'));
        assert!(short.chars().count() < long.chars().count());
    }

    #[test]
    fn truncate_preview_leaves_short_text_alone() {
        assert_eq!(truncate_preview("hello world", 100), "hello world");
    }

    #[test]
    fn truncate_preview_collapses_whitespace_and_truncates_long_text() {
        let text = "hello\n\nworld   this   has\nlots of   whitespace and is quite long indeed";
        let out = truncate_preview(text, 20);
        assert!(out.ends_with('…'));
        assert!(!out.contains('\n'));
        assert!(out.chars().count() <= 21); // 20 chars + the ellipsis char
    }

    #[test]
    fn describe_distinguishes_verified_from_unverified_done_in_text() {
        // The whole point of the never-color-alone rule: these two states
        // must read differently as WORDS, not just as two colors that
        // happen to collapse to the same ink under the mono theme.
        let verified = describe(
            &PollOutcome::Snapshot(Box::new(snapshot("done", true))),
            "job",
            0.0,
        );
        let unverified = describe(
            &PollOutcome::Snapshot(Box::new(snapshot("done", false))),
            "job",
            0.0,
        );
        assert_ne!(verified, unverified);
        assert!(verified.contains("verified"));
        assert!(unverified.contains("NOT verified"));
    }

    #[test]
    fn describe_reports_each_real_error_outcome_distinctly() {
        let not_found = describe(&PollOutcome::NotFound, "abc", 0.0);
        let auth = describe(&PollOutcome::AuthFailed, "abc", 0.0);
        let misconfigured = describe(&PollOutcome::ServerMisconfigured, "abc", 0.0);
        let unreachable = describe(&PollOutcome::Unreachable("refused".into()), "abc", 0.0);
        assert!(not_found.contains("abc"));
        assert!(auth.to_lowercase().contains("auth"));
        assert!(misconfigured.to_lowercase().contains("api key"));
        assert!(unreachable.contains("refused"));
        // All four must be distinguishable from one another in text -- an
        // agent or a human grepping the log line shouldn't have to guess
        // which real condition produced it.
        let all = [not_found, auth, misconfigured, unreachable];
        for i in 0..all.len() {
            for j in (i + 1)..all.len() {
                assert_ne!(all[i], all[j]);
            }
        }
    }

    #[test]
    fn masthead_status_word_is_connecting_before_any_response() {
        let state = AppState::new("h".into(), 1, "job".into(), Theme::lufs());
        let (word, _) = state.masthead_status_word();
        assert_eq!(word, "connecting");
    }

    #[test]
    fn masthead_status_word_distinguishes_every_terminal_outcome_by_text() {
        let mut state = AppState::new("h".into(), 1, "job".into(), Theme::mono());
        let mut words = Vec::new();
        for outcome in [
            PollOutcome::Snapshot(Box::new(snapshot("queued", false))),
            PollOutcome::Snapshot(Box::new(snapshot("running", false))),
            PollOutcome::Snapshot(Box::new(snapshot("done", true))),
            PollOutcome::Snapshot(Box::new(snapshot("done", false))),
            PollOutcome::Snapshot(Box::new(snapshot("failed", false))),
            PollOutcome::NotFound,
            PollOutcome::AuthFailed,
            PollOutcome::ServerMisconfigured,
            PollOutcome::UnexpectedStatus(500, "x".into()),
            PollOutcome::Unreachable("x".into()),
        ] {
            state.apply(outcome);
            words.push(state.masthead_status_word().0);
        }
        // Under the mono theme every one of these collapses to the same ink
        // color (by `Theme::mono`'s own construction) -- so if the words
        // weren't all distinct, mono users would have no way to tell these
        // ten real, different situations apart at all.
        let mut unique = words.clone();
        unique.sort();
        unique.dedup();
        assert_eq!(
            unique.len(),
            words.len(),
            "expected all status words to be distinct, got: {words:?}"
        );
    }

    #[test]
    fn apply_updates_log_from_the_outcome() {
        let mut state = AppState::new("h".into(), 1, "myjob".into(), Theme::lufs());
        assert_eq!(state.log, "connecting…");
        state.apply(PollOutcome::NotFound);
        assert!(state.log.contains("myjob"));
    }

    #[test]
    fn draw_snapshot_refuses_to_render_a_mismatched_job_id() {
        let snap = snapshot("done", true); // id is "abcdef0123456789"
        let theme = Theme::lufs();
        let mut lines = Vec::new();
        draw_snapshot(&mut lines, &snap, &theme, "some-other-job-id", 0.0);
        let text = render_text(&lines);
        assert!(text.contains("not the requested"));
        // Must NOT proceed to render the (mismatched) job's own status/report
        // as if it were trustworthy data about the requested job.
        assert!(!text.contains("VERIFIED"));
    }

    #[test]
    fn draw_snapshot_renders_normally_when_the_id_matches() {
        let snap = snapshot("done", true);
        let theme = Theme::lufs();
        let mut lines = Vec::new();
        draw_snapshot(&mut lines, &snap, &theme, "abcdef0123456789", 0.0);
        let text = render_text(&lines);
        assert!(text.contains("VERIFIED"));
        assert!(!text.contains("not the requested"));
    }

    #[test]
    fn draw_snapshot_shows_the_locally_tracked_running_elapsed_not_the_frozen_server_value() {
        // Regression test for a real bug an independent audit caught: the
        // server's own `updated_at - created_at` (`JobSnapshot::elapsed_s`)
        // barely moves during `running` (`jobs.py` only touches it at the
        // start and end of the run, never during), so passing THAT into the
        // running branch would show a number frozen near zero for an entire
        // multi-hour job. The locally-tracked value must be what's shown.
        let mut snap = snapshot("running", false);
        // Server-side elapsed_s() would be tiny here (created_at ~= updated_at
        // at the moment `running` began) -- if this leaked into the display
        // instead of the local value, the test below would catch it.
        snap.updated_at = snap.created_at + 0.5;
        let theme = Theme::lufs();
        let mut lines = Vec::new();
        draw_snapshot(&mut lines, &snap, &theme, "abcdef0123456789", 4321.0);
        let text = render_text(&lines);
        assert!(
            text.contains("4321s"),
            "expected the locally-tracked running elapsed to appear, got: {text}"
        );
        assert!(text.contains("since first observed"));
        assert!(!text.contains(&format!("{:.0}s", snap.elapsed_s())));
    }

    #[test]
    fn app_state_tracks_running_since_from_first_observation_and_it_grows_with_real_time() {
        let mut state = AppState::new("h".into(), 1, "job".into(), Theme::lufs());
        assert_eq!(state.running_elapsed_s(), 0.0);

        state.apply(PollOutcome::Snapshot(Box::new(snapshot("queued", false))));
        assert_eq!(
            state.running_elapsed_s(),
            0.0,
            "must not start the clock before the job is actually running"
        );

        state.apply(PollOutcome::Snapshot(Box::new(snapshot("running", false))));
        let first = state.running_elapsed_s();
        std::thread::sleep(Duration::from_millis(50));
        // A second `running` poll must NOT reset the clock -- it should keep
        // counting from the first observation, not restart every poll.
        state.apply(PollOutcome::Snapshot(Box::new(snapshot("running", false))));
        let second = state.running_elapsed_s();
        assert!(
            second > first,
            "expected running_elapsed_s to grow with real time: first={first}, second={second}"
        );
    }

    #[test]
    fn draw_snapshot_surfaces_per_check_detail_not_just_the_name() {
        let mut snap = snapshot("failed", false);
        snap.report = Some(client::Report {
            ok: false,
            checks: vec![
                client::Check {
                    name: "coverage".to_string(),
                    ok: false,
                    detail: Some("only covered 62% of source audio".to_string()),
                },
                client::Check {
                    name: "wpm_band".to_string(),
                    ok: true,
                    detail: None,
                },
            ],
            failures: vec!["coverage".to_string()],
        });
        let theme = Theme::lufs();
        let mut lines = Vec::new();
        draw_snapshot(&mut lines, &snap, &theme, "abcdef0123456789", 0.0);
        let text = render_text(&lines);
        assert!(text.contains("coverage"));
        assert!(text.contains("only covered 62% of source audio"));
        // The passing check should not show up in the failure listing.
        assert!(!text.contains("wpm_band"));
    }

    #[test]
    fn cli_line_command_never_takes_or_echoes_the_real_api_key() {
        // The function signature itself is the first guarantee: it has no
        // `api_key` parameter, so there is nothing for it to leak. This
        // exercises the exact function `draw_cli_line` calls to build the
        // footer label, not a hand-copied stand-in string.
        let cmd = cli_line_command("127.0.0.1", 8777, "abc123");
        assert_eq!(
            cmd,
            "curl -H 'X-API-Key: ***' http://127.0.0.1:8777/jobs/abc123"
        );
        assert!(cmd.contains("***"));
        assert!(!cmd.to_lowercase().contains("sk-"));
    }
}
