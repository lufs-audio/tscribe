mod app;
mod client;
mod theme;

use clap::Parser;
use theme::ThemeKind;

/// Terminal monitor for a tscribe transcription job. Polls `GET /jobs/{id}`
/// on a running server -- read-only: it cannot create, cancel, or otherwise
/// control a job. Start one with `curl` or the `tscribe` CLI, then point
/// this at the returned job id to watch it.
///
/// Targets tscribe's own committed `/jobs` contract (`SPEC.md` §5: Unit 03
/// ports `tscribe-class-carnyx`'s server "verbatim, byte-for-byte... NOT
/// redesigned"), so it should work unchanged against either the legacy
/// carnyx server today or a real `tscribe --mode server` once Unit 03 lands
/// -- same host/port/job-id shape either way.
#[derive(Parser, Debug)]
#[command(version, about, long_about = None)]
struct Cli {
    /// Host the server is bound to.
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    /// Port the server is bound to. No single default is correct across
    /// deployments (it's whatever uvicorn/tscribe was started with), so this
    /// is required rather than silently guessing one.
    #[arg(long)]
    port: u16,

    /// The job id to watch (the `id` returned by `POST /jobs` or
    /// `/jobs/upload`). This TUI only watches a job that already exists.
    #[arg(long)]
    job_id: String,

    /// API key sent as `X-API-Key`. Prefer $TSCRIBE_API_KEY over this flag
    /// when you can -- an env var doesn't show up in `ps`/shell history the
    /// way a CLI argument can.
    #[arg(long)]
    api_key: Option<String>,

    /// Color theme: lufs, catppuccin, or mono (the accessibility-focused
    /// standard -- no hue anywhere, state conveyed via text/shape only).
    /// Same three themes as `lufs-audio/bplate`'s wizard and
    /// `lufs-audio/lrex`'s `tui`, per the cross-tool TUI style guide.
    #[arg(long, value_enum, default_value = "lufs")]
    theme: ThemeKind,
}

/// argv[0]'s basename -- so `--help`/usage output says whatever this binary
/// was actually invoked as, matching the convention `lufs-audio/lrex`
/// established with its own `invoked_name()` (relevant here mainly if this
/// binary is ever symlinked/renamed; there is only one build target today).
/// Falls back to "tscribe-tui" if argv[0] is somehow unavailable.
pub fn invoked_name() -> String {
    std::env::args_os()
        .next()
        .and_then(|p| {
            std::path::Path::new(&p)
                .file_name()
                .map(|f| f.to_string_lossy().into_owned())
        })
        .unwrap_or_else(|| "tscribe-tui".to_string())
}

fn main() -> std::process::ExitCode {
    let cli = Cli::parse();

    let api_key = match cli
        .api_key
        .or_else(|| std::env::var("TSCRIBE_API_KEY").ok())
    {
        Some(k) if !k.is_empty() => k,
        _ => {
            eprintln!(
                "tscribe-tui: no API key given -- pass --api-key or set $TSCRIBE_API_KEY \
                 (every /jobs route requires one; see tscribe-class-carnyx's \
                 server/app.py::require_api_key, ported verbatim per SPEC.md Unit 03)"
            );
            return std::process::ExitCode::from(2);
        }
    };

    match app::run(cli.host, cli.port, cli.job_id, api_key, cli.theme) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("tscribe-tui: {e}");
            std::process::ExitCode::from(1)
        }
    }
}
