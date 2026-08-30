from __future__ import annotations

import hashlib
import json
import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when configuration cannot be validated."""


@dataclass(slots=True)
class ModelConfig:
    name: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "CODEYF_API_KEY"
    request_timeout_seconds: float = 120.0
    max_retries: int = 3
    temperature: float = 0.1
    max_output_tokens: int = 4096
    context_window_tokens: int = 32768


@dataclass(slots=True)
class AgentConfig:
    max_turns: int = 30
    max_tool_calls: int = 100
    task_timeout_seconds: float = 1800.0
    empty_response_limit: int = 2
    repeat_failure_limit: int = 3
    compaction_threshold: float = 0.85


@dataclass(slots=True)
class ToolConfig:
    command_timeout_seconds: float = 120.0
    max_output_chars: int = 50_000
    max_file_read_chars: int = 40_000
    max_search_matches: int = 200
    max_list_files: int = 1_000


@dataclass(slots=True)
class SecurityConfig:
    approval: str = "balanced"
    allow_shell: bool = False
    allow_outbound_network_commands: bool = False
    inherit_environment: list[str] = field(
        default_factory=lambda: ["PATH", "PATHEXT", "SYSTEMROOT", "TMP", "TEMP", "LANG"]
    )


@dataclass(slots=True)
class StorageConfig:
    enabled: bool = True
    directory: str = ""


@dataclass(slots=True)
class AppConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.model.api_key_env, "")

    def fingerprint(self) -> str:
        safe = asdict(self)
        encoded = json.dumps(safe, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


_SECTION_TYPES = {
    "model": ModelConfig,
    "agent": AgentConfig,
    "tools": ToolConfig,
    "security": SecurityConfig,
    "storage": StorageConfig,
}


def _user_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "codeyf" / "config.toml"


def default_storage_path() -> Path:
    override = os.environ.get("CODEYF_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / "codeyf").resolve()


def _merge_config(config: AppConfig, raw: Mapping[str, Any]) -> None:
    unknown_sections = set(raw) - {"schema_version", *_SECTION_TYPES}
    if unknown_sections:
        raise ConfigError(f"未知配置节: {', '.join(sorted(unknown_sections))}")
    for name, cls in _SECTION_TYPES.items():
        values = raw.get(name)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ConfigError(f"配置节 [{name}] 必须是表")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(values) - allowed
        if unknown:
            raise ConfigError(f"[{name}] 中存在未知字段: {', '.join(sorted(unknown))}")
        current = getattr(config, name)
        for key, value in values.items():
            setattr(current, key, value)


def _load_toml(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"配置文件不存在: {path}")
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc


def _apply_env(config: AppConfig) -> None:
    mapping: dict[str, tuple[object, str, Any]] = {
        "CODEYF_MODEL": (config.model, "name", str),
        "CODEYF_BASE_URL": (config.model, "base_url", str),
        "CODEYF_APPROVAL": (config.security, "approval", str),
        "CODEYF_MAX_TURNS": (config.agent, "max_turns", int),
        "CODEYF_CONTEXT_WINDOW_TOKENS": (config.model, "context_window_tokens", int),
        "CODEYF_MAX_OUTPUT_TOKENS": (config.model, "max_output_tokens", int),
    }
    for variable, (target, field_name, caster) in mapping.items():
        value = os.environ.get(variable)
        if value is not None:
            try:
                setattr(target, field_name, caster(value))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"环境变量 {variable} 的值无效") from exc


def validate_config(config: AppConfig) -> None:
    parsed = urlparse(config.model.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("model.base_url 必须是有效的 http(s) URL")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigError("非本机模型服务必须使用 HTTPS")
    if config.security.approval not in {"strict", "balanced", "auto"}:
        raise ConfigError("security.approval 必须是 strict、balanced 或 auto")
    if not 1 <= config.agent.max_turns <= 200:
        raise ConfigError("agent.max_turns 必须在 1..200 之间")
    if not 1 <= config.agent.max_tool_calls <= 1000:
        raise ConfigError("agent.max_tool_calls 必须在 1..1000 之间")
    if not 0 < config.agent.compaction_threshold < 1:
        raise ConfigError("agent.compaction_threshold 必须在 0..1 之间")
    if config.model.max_output_tokens >= config.model.context_window_tokens:
        raise ConfigError("max_output_tokens 必须小于 context_window_tokens")


def load_config(workspace: Path, explicit_path: Path | None = None, overrides: Mapping[str, Any] | None = None) -> AppConfig:
    config = AppConfig()
    _merge_config(config, _load_toml(_user_config_path()))
    _merge_config(config, _load_toml(workspace / ".codeyf.toml"))
    if explicit_path:
        _merge_config(config, _load_toml(explicit_path, required=True))
    _apply_env(config)
    if overrides:
        if overrides.get("model"):
            config.model.name = str(overrides["model"])
        if overrides.get("base_url"):
            config.model.base_url = str(overrides["base_url"])
        if overrides.get("approval"):
            config.security.approval = str(overrides["approval"])
        if overrides.get("max_turns") is not None:
            config.agent.max_turns = int(overrides["max_turns"])
        if overrides.get("timeout") is not None:
            config.agent.task_timeout_seconds = float(overrides["timeout"])
    if not config.storage.directory:
        config.storage.directory = str(default_storage_path())
    validate_config(config)
    return config
