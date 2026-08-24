"""Tests for tscribe.core.summarizer — LiteLLM/OpenAI-compatible summarization.

Covers:
- Configuration validation (requires remote: section)
- Request payload construction (model, messages, headers, auth)
- System prompt precedence (method arg > config.summarization.prompt > default)
- Obsidian markdown formatting wrapper
- Error handling (401 invalid key, 404 model not found, upstream 5xx errors)
"""

from __future__ import annotations

import pytest
import requests

from tscribe.config import (
    Backend,
    Mode,
    RemoteConfig,
    RuntimeConfig,
    SummarizationConfig,
)
from tscribe.core.summarizer import DEFAULT_SYSTEM_PROMPT, Summarizer, summarize


class MockResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None) -> None:
        self.status_code = status_code
        self._json_data = json_data or {"choices": [{"message": {"content": "Summary of points."}}]}

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _valid_cfg(
    prompt: str | None = None,
    model: str = "gpt-4o-mini",
) -> RuntimeConfig:
    return RuntimeConfig(
        mode=Mode.CLIENT,
        backend=Backend.LOCAL,
        remote=RemoteConfig(
            endpoint="https://api.example.com/v1",
            api_key_env="TEST_LLM_KEY",
        ),
        summarization=SummarizationConfig(
            model=model,
            prompt=prompt,
        ),
    )


def test_summarize_requires_remote_config():
    cfg = RuntimeConfig(mode=Mode.CLIENT, backend=Backend.LOCAL, remote=None)
    with pytest.raises(RuntimeError, match="requires a 'remote:' section"):
        summarize("transcript text", cfg)


def test_summarize_success(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-test-key")
    captured = {}

    def mock_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return MockResponse(
            status_code=200,
            json_data={"choices": [{"message": {"content": "  Key takeaway from lecture.  "}}]},
        )

    monkeypatch.setattr(requests, "post", mock_post)

    cfg = _valid_cfg()
    res = summarize("This is the raw lecture transcript.", cfg)

    assert res == "Key takeaway from lecture."
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-test-key"
    assert captured["json"]["model"] == "gpt-4o-mini"
    assert captured["json"]["messages"][0]["role"] == "system"
    assert captured["json"]["messages"][0]["content"] == DEFAULT_SYSTEM_PROMPT
    assert captured["json"]["messages"][1]["role"] == "user"
    assert captured["json"]["messages"][1]["content"] == "This is the raw lecture transcript."


def test_summarize_custom_prompt_and_obsidian(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-test-key")

    def mock_post(url, headers=None, json=None, timeout=None):
        return MockResponse(
            status_code=200,
            json_data={"choices": [{"message": {"content": "- Note 1\n- Note 2"}}]},
        )

    monkeypatch.setattr(requests, "post", mock_post)

    cfg = _valid_cfg(prompt="Custom prompt from config.")
    res = summarize("transcript text", cfg, obsidian_format=True)

    assert res.startswith("---\ntags: [voice-memo, ai-processed]\n---\n\n")
    assert "- Note 1\n- Note 2" in res


def test_summarizer_explicit_prompt_arg(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-test-key")
    captured = {}

    def mock_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return MockResponse(200)

    monkeypatch.setattr(requests, "post", mock_post)

    cfg = _valid_cfg(prompt="Config prompt")
    summarizer = Summarizer(cfg)
    summarizer.summarize("some text", system_prompt="Explicit prompt arg")

    assert captured["json"]["messages"][0]["content"] == "Explicit prompt arg"


def test_summarize_401_auth_error(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "invalid-key")

    def mock_post(url, headers=None, json=None, timeout=None):
        return MockResponse(status_code=401)

    monkeypatch.setattr(requests, "post", mock_post)

    cfg = _valid_cfg()
    with pytest.raises(RuntimeError, match="authentication failed.*TEST_LLM_KEY"):
        summarize("transcript text", cfg)


def test_summarize_404_model_not_found(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "valid-key")

    def mock_post(url, headers=None, json=None, timeout=None):
        return MockResponse(status_code=404)

    monkeypatch.setattr(requests, "post", mock_post)

    cfg = _valid_cfg(model="nonexistent-model")
    with pytest.raises(RuntimeError, match="model 'nonexistent-model' not found"):
        summarize("transcript text", cfg)


def test_summarize_upstream_http_error(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "valid-key")

    def mock_post(url, headers=None, json=None, timeout=None):
        return MockResponse(status_code=502)

    monkeypatch.setattr(requests, "post", mock_post)

    cfg = _valid_cfg()
    with pytest.raises(requests.HTTPError, match="HTTP 502"):
        summarize("transcript text", cfg)
