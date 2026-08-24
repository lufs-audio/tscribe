"""Test/DI seam for the server's transcriber.

Ported from tscribe-class-carnyx's server/config.py, narrowed to just the
factory hook — the actual settings (api key, audio limits, timeouts) now live
in tscribe.config.ServerConfig (Unit 01/03), since they belong in tscribe.yaml
like everything else rather than a second, server-only config surface.

The heavy transcriber import is done lazily inside the default factory so the
app and tests can import without pulling in the whole ML stack. Tests
override `transcriber_factory` with a mock — same pattern the source repo used.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tscribe.config import RuntimeConfig


def default_transcriber_factory(cfg: RuntimeConfig) -> Any:
    """Build tscribe's real Transcriber. Imported lazily so the FastAPI
    process needn't load faster-whisper unless it actually runs."""
    from tscribe.core.transcriber import Transcriber  # heavy import, deferred

    return Transcriber(cfg)


# Overridable hook (tests swap in a mock). Must return an object with
# transcribe(path) -> (text, info).
transcriber_factory: Callable[[RuntimeConfig], Any] = default_transcriber_factory
