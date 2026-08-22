# tscribe

> tscribe - Scaffolded with `bplate` on 2026-08-21.

## Setup & Running

```bash
# Setup virtual environment and sync dependencies
uv sync

# Run the CLI
uv run tscribe --help

# Run tests
uv run pytest
```

## Terminal monitor (`tui/`)

A separate Rust crate, `tui/`, provides `tscribe-tui` — a read-only terminal
monitor for a job in progress (`GET /jobs/{id}`: status, elapsed time, and
the verification report). It isn't part of this Python package or its `uv`
build; see `tui/README.md` for why, and how to build/run it. Follows the
cross-tool TUI style guide locked for LUFS's terminal tools
(`lufs-audio/bplate` → `references/studies/tui-style-directions/`), the same
one `lufs-audio/lrex`'s own `tui` follows — one interaction shape, one set of
three themes, shared across the family. Per `SPEC.md` §7, tscribe's own spec
explicitly deferred TUI design to that guide rather than designing one here.

---

**Note (Ciani, 2026-08-21):** this README is still the bplate scaffold boilerplate. See `SPEC.md` for what tscribe actually is and `docs/units/` for the implementation contracts — this file gets real content as Unit 08 lands.
