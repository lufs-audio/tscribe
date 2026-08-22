"""tscribe.core.summarizer — LLM summarization via a LiteLLM-compatible endpoint.

Ported from tscribe-transcription-tool's Summarizer. Summarization always
requires a `remote:` section in tscribe.yaml — the LLM call goes over the
network regardless of what `backend` is set to for transcription (a
`backend: local` config can still carry a `remote:` section used only here;
RuntimeConfig's validation in Unit 01 only *requires* `remote` when
`backend: remote`, it never forbids setting it otherwise).
"""

from __future__ import annotations

import os

import requests

from tscribe.config import RuntimeConfig

DEFAULT_SYSTEM_PROMPT = (
    "You are a summarization assistant. "
    "Given a raw transcript, produce a clean markdown summary with "
    "key points, action items, and notable concepts."
)


class Summarizer:
    def __init__(self, cfg: RuntimeConfig) -> None:
        self.cfg = cfg

    def summarize(self, text: str, system_prompt: str | None = None) -> str:
        remote = self.cfg.remote
        if remote is None:
            raise RuntimeError(
                "summarize() requires a 'remote:' section in tscribe.yaml "
                "(endpoint + api_key_env) — the summarization LLM call always "
                "goes over the network, even when backend: local is set for "
                "transcription."
            )

        prompt = system_prompt or self.cfg.summarization.prompt or DEFAULT_SYSTEM_PROMPT
        api_key = os.environ.get(remote.api_key_env, "")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.cfg.summarization.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
            "max_tokens": 4096,
        }

        response = requests.post(
            f"{remote.endpoint.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
            timeout=180,
        )

        if response.status_code == 401:
            raise RuntimeError(
                f"liteLLM API authentication failed. Check the secret in env var "
                f"'{remote.api_key_env}'."
            )
        if response.status_code == 404:
            raise RuntimeError(f"liteLLM model '{self.cfg.summarization.model}' not found.")

        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"].strip()


def summarize(transcript: str, cfg: RuntimeConfig, obsidian_format: bool = False) -> str:
    """Matches SPEC.md Unit 02's interface contract."""
    summary = Summarizer(cfg).summarize(transcript)
    if obsidian_format:
        frontmatter = "---\ntags: [voice-memo, ai-processed]\n---\n"
        return frontmatter + "\n" + summary
    return summary
