"""tscribe.cli.output — the --json envelope and exit-code contract (Unit 07).

Every subcommand's result flows through here before main() decides what to
print and what the process exits with. Two envelope shapes only, matching
docs/units/07-cli-surface-and-exit-codes.md exactly:

    {"status": "success", "data": {...}}
    {"status": "error", "code": <int>, "message": "..."}

The `code` in an error envelope IS the process exit code (see ExitCode) —
not an HTTP status, not an app-specific number that needs a lookup table.
`--json` output is never allowed to say something different from what the
process actually did with sys.exit.
"""

from __future__ import annotations

import json
import sys
from enum import IntEnum
from typing import Any, TypedDict


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    CONFIG_INVALID = 3
    BACKEND_UNREACHABLE = 4
    CONTRACT_VIOLATED = 5
    JOB_NOT_FOUND = 6
    INTERRUPTED = 7


class CliResponse(TypedDict, total=False):
    status: str  # "success" | "error"
    data: dict
    code: int
    message: str


def success(data: dict[str, Any] | None = None) -> CliResponse:
    return {"status": "success", "data": data if data is not None else {}}


def error(message: str, code: ExitCode) -> CliResponse:
    return {"status": "error", "code": int(code), "message": message}


def _default_human(data: dict[str, Any]) -> str:
    """Minimal, dependency-free human rendering for a success envelope that
    didn't get a purpose-built `human=` string. Good enough for machine-ish
    dict payloads (devices, bootstrap summaries); commands with a genuinely
    prose-shaped result (transcripts) should always pass `human=` explicitly.
    """
    if not data:
        return "ok"
    lines = []
    for key, value in data.items():
        if isinstance(value, (list, dict)):
            lines.append(f"{key}: {json.dumps(value, indent=2)}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def emit(response: CliResponse, *, json_mode: bool, human: str | None = None) -> None:
    """Print `response` per `json_mode`. Success goes to stdout either way;
    a human-mode error goes to stderr (matching normal CLI convention) while
    a --json error still goes to stdout, since --json callers parse stdout
    for the envelope regardless of status and `status: error` in the JSON
    itself is the signal, not which stream it arrived on."""
    if json_mode:
        print(json.dumps(response))
        return
    if response.get("status") == "success":
        print(human if human is not None else _default_human(response.get("data") or {}))
    else:
        print(f"error: {response.get('message')}", file=sys.stderr)
