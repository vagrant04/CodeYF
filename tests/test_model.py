from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from codeyf.config import ModelConfig
from codeyf.model import ModelProtocolError, OpenAICompatibleClient


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.body


def test_openai_adapter_parses_text_and_tool_calls(monkeypatch) -> None:
    payload = {
        "id": "response_1",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": "我先读取文件。",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"},
                }],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeResponse(payload))
    client = OpenAICompatibleClient(ModelConfig(base_url="http://localhost:9999/v1"), "test-key")
    response = client.complete([{"role": "user", "content": "read"}], [])
    assert response.content == "我先读取文件。"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.usage["total_tokens"] == 18


def test_openai_adapter_rejects_malformed_tool_arguments(monkeypatch) -> None:
    payload = {
        "choices": [{
            "message": {"content": None, "tool_calls": [{"id": "x", "function": {"name": "read_file", "arguments": "{"}}]},
            "finish_reason": "tool_calls",
        }]
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeResponse(payload))
    client = OpenAICompatibleClient(ModelConfig(base_url="http://localhost:9999/v1"), "test-key")
    with pytest.raises(ModelProtocolError):
        client.complete([{"role": "user", "content": "read"}], [])


def test_official_openai_uses_max_completion_tokens(monkeypatch) -> None:
    captured: dict = {}

    def respond(request, **kwargs):
        captured.update(json.loads(request.data))
        return FakeResponse({
            "id": "response_2",
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        })

    monkeypatch.setattr("urllib.request.urlopen", respond)
    config = ModelConfig(name="chat-latest", base_url="https://api.openai.com/v1", max_output_tokens=2048)
    OpenAICompatibleClient(config, "test-key").complete([{"role": "user", "content": "hello"}], [])

    assert captured["max_completion_tokens"] == 2048
    assert "max_tokens" not in captured
