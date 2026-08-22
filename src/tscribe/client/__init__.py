"""tscribe.client — mode: client. Local, remote-server, and remote-inference.

Restores the standalone personal CLI that tscribe-transcription-tool provided
(record/transcribe/summarize/devices), plus a thin HTTP client against
Unit 03's server contract when pointed at a remote tscribe instance. See
SPEC.md §10 Open Question #1 for why there are two "remote" client shapes.
"""

from tscribe.config import Backend, RemoteKind, RuntimeConfig

from .base import AudioSource, ClientBase, JobHandle, JobResult, TscribeClient
from .local import LocalClient
from .remote_inference import RemoteInferenceClient
from .remote_server import RemoteServerClient

__all__ = [
    "AudioSource",
    "ClientBase",
    "JobHandle",
    "JobResult",
    "TscribeClient",
    "LocalClient",
    "RemoteInferenceClient",
    "RemoteServerClient",
    "make_client",
]


def make_client(cfg: RuntimeConfig) -> ClientBase:
    """Select the right client implementation for `cfg`, per SPEC.md's
    Mode/Backend/RemoteKind nouns. This is the one place that branching logic
    lives — Unit 07's CLI calls this rather than re-deriving the mapping."""
    if cfg.backend == Backend.LOCAL:
        return LocalClient(cfg)
    # cfg.backend == Backend.REMOTE past this point (RuntimeConfig.__post_init__
    # already guarantees cfg.remote is not None whenever backend is REMOTE).
    if cfg.remote.kind == RemoteKind.TSCRIBE_SERVER:
        return RemoteServerClient(cfg)
    return RemoteInferenceClient(cfg)
