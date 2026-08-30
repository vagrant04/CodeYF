from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, Sequence
from urllib.parse import urlparse

from .config import ModelConfig
from .domain import ModelResponse, ToolCall, new_tool_call_id


class ModelError(RuntimeError):
    code = "MODEL_ERROR"
    retryable = False


class ModelAuthenticationError(ModelError):
    code = "MODEL_AUTHENTICATION"


class ModelRateLimited(ModelError):
    code = "MODEL_RATE_LIMITED"
    retryable = True


class ModelUnavailable(ModelError):
    code = "MODEL_UNAVAILABLE"
    retryable = True


class ModelInvalidRequest(ModelError):
    code = "MODEL_INVALID_REQUEST"


class ModelProtocolError(ModelError):
    code = "MODEL_PROTOCOL"


class ModelContextExceeded(ModelError):
    code = "CONTEXT_EXHAUSTED"


class ModelClient(Protocol):
    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> ModelResponse: ...


@dataclass(slots=True)
class OpenAICompatibleClient:
    config: ModelConfig
    api_key: str

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> ModelResponse:
        if not self.api_key:
            raise ModelAuthenticationError(f"缺少 API key；请设置 {self.config.api_key_env}")
        payload: dict[str, Any] = {
            "model": self.config.name,
            "messages": list(messages),
            "temperature": self.config.temperature,
        }
        if urlparse(self.config.base_url).hostname == "api.openai.com":
            payload["max_completion_tokens"] = self.config.max_output_tokens
        else:
            payload["max_tokens"] = self.config.max_output_tokens
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        last_error: ModelError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                return self._request(payload)
            except ModelError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.config.max_retries:
                    raise
                delay = min(1.0 * (2**attempt) + random.random() * 0.3, 20.0)
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _request(self, payload: dict[str, Any]) -> ModelResponse:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "CodeYF/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            message = self._safe_http_error(exc)
            if exc.code in {401, 403}:
                raise ModelAuthenticationError(message) from exc
            if exc.code == 429:
                raise ModelRateLimited(message) from exc
            if exc.code in {408, 500, 502, 503, 504}:
                raise ModelUnavailable(message) from exc
            lowered = message.lower()
            if exc.code in {400, 413, 422} and "context" in lowered and any(word in lowered for word in ("length", "window", "token", "maximum")):
                raise ModelContextExceeded(message) from exc
            raise ModelInvalidRequest(message) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelUnavailable(f"模型服务连接失败: {exc.reason if hasattr(exc, 'reason') else exc}") from exc
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            message = choice["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError("模型服务返回了无法解析的响应") from exc

        calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            try:
                function = item["function"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise TypeError("arguments must be object")
                calls.append(ToolCall(item.get("id") or new_tool_call_id(), function["name"], arguments))
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ModelProtocolError("模型返回了格式错误的工具调用") from exc

        usage = data.get("usage")
        normalized_usage = None
        if isinstance(usage, dict):
            normalized_usage = {
                "input_tokens": int(usage.get("prompt_tokens", usage.get("input_tokens", 0))),
                "output_tokens": int(usage.get("completion_tokens", usage.get("output_tokens", 0))),
                "total_tokens": int(usage.get("total_tokens", 0)),
            }
        return ModelResponse(
            content=message.get("content"),
            tool_calls=tuple(calls),
            finish_reason=choice.get("finish_reason"),
            response_id=data.get("id"),
            usage=normalized_usage,
        )

    @staticmethod
    def _safe_http_error(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read(4096).decode("utf-8", "replace")
            payload = json.loads(raw)
            return str(payload.get("error", {}).get("message") or f"HTTP {exc.code}")
        except Exception:
            return f"模型服务返回 HTTP {exc.code}"


class ScriptedModelClient:
    """Deterministic test double that returns pre-built responses."""

    def __init__(self, responses: Sequence[ModelResponse | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def complete(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> ModelResponse:
        self.requests.append((list(messages), list(tools)))
        if not self.responses:
            raise AssertionError("ScriptedModelClient has no response left")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
