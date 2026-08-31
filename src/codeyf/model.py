from __future__ import annotations

import json
import random
import re
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
                name = function["name"]
                arguments = self._parse_tool_arguments(function.get("arguments", {}), tool_name=name)
                calls.append(ToolCall(item.get("id") or new_tool_call_id(), name, arguments))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                tool_name = str((item.get("function") or {}).get("name") or "unknown") if isinstance(item, dict) else "unknown"
                raise ModelProtocolError(
                    f"模型返回了格式错误的工具调用（{tool_name}.arguments 不是可恢复的 JSON 对象）"
                ) from exc

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

    @classmethod
    def _parse_tool_arguments(cls, value: Any, tool_name: str = "") -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            raise TypeError("arguments must be a JSON object or string")
        source = value.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", source, flags=re.I | re.S)
        if fenced:
            source = fenced.group(1).strip()
        if not source:
            return {}
        candidates = [source]
        without_trailing_commas = cls._remove_trailing_commas(source)
        if without_trailing_commas != source:
            candidates.append(without_trailing_commas)
        last_error: Exception | None = None
        for candidate in candidates:
            for strict in (True, False):
                try:
                    parsed = json.loads(candidate, strict=strict)
                except (json.JSONDecodeError, TypeError) as exc:
                    last_error = exc
                    continue
                if not isinstance(parsed, dict):
                    raise TypeError("arguments must decode to an object")
                return parsed
        if tool_name == "apply_patch":
            recovered_patch = cls._recover_complete_patch_argument(source)
            if recovered_patch is not None:
                return {"patch": recovered_patch}
        raise ValueError("arguments are not recoverable JSON") from last_error

    @classmethod
    def _recover_complete_patch_argument(cls, source: str) -> str | None:
        """Recover one narrowly defined MiniMax failure without guessing truncated data.

        Large HTML patches occasionally contain literal quotes or invalid JSON escapes in
        the outer ``{"patch":"..."}`` wrapper. Recovery is safe only when that wrapper has
        no other fields and the native patch has both explicit boundary markers. The patch
        tool still performs its normal format, path and atomicity validation afterwards.
        """
        prefix = re.match(r'^\s*\{\s*"patch"\s*:\s*"', source, flags=re.S)
        suffix = re.search(r'"\s*,?\s*\}\s*$', source, flags=re.S)
        if not prefix or not suffix or suffix.start() < prefix.end():
            return None
        body = source[prefix.end():suffix.start()]
        patch = cls._decode_relaxed_json_string(body)
        stripped = patch.strip()
        if not stripped.startswith("*** Begin Patch") or not stripped.endswith("*** End Patch"):
            return None
        return patch

    @staticmethod
    def _decode_relaxed_json_string(value: str) -> str:
        output: list[str] = []
        escapes = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        index = 0
        while index < len(value):
            char = value[index]
            if char != "\\" or index + 1 >= len(value):
                output.append(char)
                index += 1
                continue
            escaped = value[index + 1]
            if escaped == "u" and index + 5 < len(value):
                digits = value[index + 2:index + 6]
                if re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    output.append(chr(int(digits, 16)))
                    index += 6
                    continue
            if escaped in escapes:
                output.append(escapes[escaped])
                index += 2
                continue
            # Invalid JSON escapes are common in embedded JavaScript regexes. Preserve
            # them verbatim because removing the slash would silently change the file.
            output.extend(("\\", escaped))
            index += 2
        return "".join(output)

    @staticmethod
    def _remove_trailing_commas(source: str) -> str:
        output: list[str] = []
        in_string = False
        escaped = False
        index = 0
        while index < len(source):
            char = source[index]
            if in_string:
                output.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                index += 1
                continue
            if char == '"':
                in_string = True
                output.append(char)
                index += 1
                continue
            if char == ",":
                lookahead = index + 1
                while lookahead < len(source) and source[lookahead].isspace():
                    lookahead += 1
                if lookahead < len(source) and source[lookahead] in "}]":
                    index += 1
                    continue
            output.append(char)
            index += 1
        return "".join(output)

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
