"""RemoteInferenceClient — Unit 02's pipeline in-process, whisper call only
over the network.

Used when `backend: remote` and `remote.kind: inference_endpoint`: unlike
RemoteServerClient, this client does not talk to a tscribe server or any
`/jobs` contract. It runs tscribe.core.class_pipeline.transcribe_class in
this process exactly the way LocalClient does — split, transcribe, reassemble,
verify — and relies on the fact that Unit 02's Transcriber already knows how
to route the actual whisper call at `cfg.remote.endpoint` (a bare
LiteLLM/OpenAI-compatible `/audio/transcriptions` endpoint, with automatic
fallback to local faster-whisper on failure; see
tscribe.core.transcriber.Transcriber.transcribe). This is "carnyx today,"
minus the HTTP job layer RemoteServerClient adds for a real tscribe server.

Nothing about the whisper call itself is reimplemented here — that logic is
owned entirely by Unit 02 and must not be duplicated (see SPEC.md's
Boundaries note for this unit). This class exists only to (a) validate that
`cfg` actually describes a remote inference endpoint before doing any work,
and (b) reuse the exact in-process submit/poll/evidence-retention machinery
LocalClient uses, via the shared _InProcessClient base in local.py.
"""

from __future__ import annotations

from tscribe.config import RemoteKind, RuntimeConfig

from .local import _InProcessClient


class RemoteInferenceClient(_InProcessClient):
    """backend: remote, endpoint is a bare inference endpoint — Unit 02 runs
    in-process here; only the whisper call inside it goes over the network."""

    def __init__(self, cfg: RuntimeConfig) -> None:
        remote = cfg.remote
        if remote is None:
            raise ValueError(
                "RemoteInferenceClient requires a 'remote:' section in tscribe.yaml "
                "(endpoint + api_key_env) — it is the endpoint Unit 02's Transcriber "
                "calls for the whisper request."
            )
        if remote.kind != RemoteKind.INFERENCE_ENDPOINT:
            # Not fatal by itself -- Transcriber only reads cfg.remote.endpoint and
            # doesn't care about `kind` -- but constructing this class with
            # kind: tscribe_server means make_client()'s dispatch and the caller's
            # intent have drifted apart, which is exactly the ambiguity RemoteKind
            # was added to close (see config.py's RemoteKind docstring). Surface it
            # here rather than silently transcribing against the wrong assumption.
            raise ValueError(
                f"RemoteInferenceClient expects remote.kind == "
                f"{RemoteKind.INFERENCE_ENDPOINT!r}, got {remote.kind!r}. "
                f"Use RemoteServerClient for remote.kind == {RemoteKind.TSCRIBE_SERVER!r}."
            )
        super().__init__(cfg)
