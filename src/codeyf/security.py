from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class PathSecurityError(ValueError):
    pass


class PathGuard:
    """Resolves untrusted model paths without allowing workspace escape."""

    def __init__(self, workspace: Path) -> None:
        if not workspace.exists() or not workspace.is_dir():
            raise PathSecurityError(f"工作区不存在或不是目录: {workspace}")
        self.workspace = workspace.resolve(strict=True)

    def resolve(self, candidate: str, *, must_exist: bool = False) -> Path:
        if not isinstance(candidate, str) or not candidate.strip():
            raise PathSecurityError("路径不能为空")
        if "\x00" in candidate:
            raise PathSecurityError("路径包含非法 NUL 字符")
        path = Path(candidate)
        if path.is_absolute():
            target = path
        else:
            target = self.workspace / path
        target = self._resolve_allow_missing(target)
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:
            raise PathSecurityError("路径不在工作区内") from exc
        if must_exist and not target.exists():
            raise FileNotFoundError(candidate)
        return target

    def relative(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix() or "."

    @staticmethod
    def _resolve_allow_missing(path: Path) -> Path:
        missing: list[str] = []
        current = path
        while not current.exists():
            parent = current.parent
            if parent == current:
                break
            missing.append(current.name)
            current = parent
        resolved = current.resolve(strict=True)
        for part in reversed(missing):
            resolved = resolved / part
        return resolved


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    decision: PolicyDecision
    risk: str
    rule_ids: tuple[str, ...]
    summary: str


class SecurityPolicy:
    SAFE_COMMANDS = {
        "rg", "rg.exe", "grep", "git", "git.exe", "python", "python.exe", "python3", "py", "pytest", "node", "node.exe", "npm", "pnpm",
        "yarn", "bun", "cargo", "go", "dotnet", "java", "javac", "mvn", "gradle",
    }
    HARD_DENY = (
        (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "CMD_SYSTEM_POWER"),
        (re.compile(r"\b(mkfs|diskpart|format\.com|bcdedit)\b", re.I), "CMD_DISK_CONTROL"),
        (re.compile(r"\b(reg\s+delete|deluser|userdel)\b", re.I), "CMD_SYSTEM_MUTATION"),
        (re.compile(r"\b(cat|type|copy)\b.*(\.ssh|credentials|id_rsa|\.env)(\b|[/\\])", re.I), "CMD_CREDENTIAL_READ"),
        (
            re.compile(
                r"\b(cmd(?:\.exe)?\s+/[cs]|powershell(?:\.exe)?|pwsh(?:\.exe)?)\b"
                r".*(?:>|out-file|set-content|add-content|new-item)",
                re.I,
            ),
            "CMD_FILE_WRITE_BYPASS",
        ),
    )
    ASK_PATTERNS = (
        (re.compile(r"\b(rm|rmdir|del|remove-item)\b", re.I), "CMD_DELETE"),
        (re.compile(r"\bgit\s+(push|reset|clean)\b", re.I), "CMD_GIT_REMOTE_OR_DESTRUCTIVE"),
        (re.compile(r"\b(pip|npm|pnpm|yarn|bun|cargo)\s+(install|add|update)\b", re.I), "CMD_INSTALL"),
        (re.compile(r"\b(curl|wget|invoke-webrequest|irm|iwr)\b", re.I), "CMD_NETWORK"),
        (re.compile(r"[>|;&`]|\$\(", re.I), "CMD_SHELL_COMPOSITION"),
    )

    def __init__(self, mode: str = "balanced", *, allow_shell: bool = False, allow_network: bool = False) -> None:
        self.mode = mode
        self.allow_shell = allow_shell
        self.allow_network = allow_network

    def assess(self, tool_name: str, arguments: dict[str, Any]) -> RiskAssessment:
        if tool_name in {"read_file", "list_files", "search_text"}:
            return RiskAssessment(PolicyDecision.ALLOW, "low", (), "只读工作区操作")
        if tool_name == "apply_patch":
            patch = str(arguments.get("patch", ""))
            deleting = "*** Delete File:" in patch or bool(
                re.search(r"(?m)^\+\+\+\s+/dev/null(?:\s|$)", patch)
            )
            if self.mode == "strict" or deleting:
                return RiskAssessment(PolicyDecision.ASK, "medium", ("FILE_WRITE",), "修改工作区文件")
            return RiskAssessment(PolicyDecision.ALLOW, "medium", ("FILE_WRITE",), "修改工作区文件")
        if tool_name != "run_command":
            return RiskAssessment(PolicyDecision.DENY, "high", ("UNKNOWN_TOOL",), "未知工具")

        shell = bool(arguments.get("shell"))
        argv = arguments.get("argv") or []
        display = str(arguments.get("command") or " ".join(map(str, argv)))
        for pattern, rule in self.HARD_DENY:
            if pattern.search(display):
                return RiskAssessment(PolicyDecision.DENY, "critical", (rule,), "命令命中硬性禁止规则")
        rules = [rule for pattern, rule in self.ASK_PATTERNS if pattern.search(display)]
        if shell and not self.allow_shell:
            return RiskAssessment(PolicyDecision.DENY, "high", ("CMD_SHELL_DISABLED",), "配置禁止通过 shell 执行命令")
        if "CMD_NETWORK" in rules and self.allow_network:
            rules.remove("CMD_NETWORK")

        if rules:
            decision = PolicyDecision.ALLOW if self.mode == "auto" else PolicyDecision.ASK
            return RiskAssessment(decision, "high", tuple(dict.fromkeys(rules)), f"执行高风险命令: {display[:160]}")
        if self.mode == "strict":
            return RiskAssessment(PolicyDecision.ASK, "medium", ("CMD_STRICT_MODE",), f"执行命令: {display[:160]}")
        executable = Path(str(argv[0])).name.lower() if argv else "shell"
        if executable not in self.SAFE_COMMANDS and self.mode == "balanced":
            return RiskAssessment(PolicyDecision.ASK, "medium", ("CMD_UNKNOWN_EXECUTABLE",), f"执行未分类命令: {display[:160]}")
        return RiskAssessment(PolicyDecision.ALLOW, "low", (), f"执行命令: {display[:160]}")


class ApprovalProvider(Protocol):
    def prepare(self, request: dict[str, Any]) -> None: ...

    def decide(self, request: dict[str, Any], timeout: float | None = None) -> str: ...


class AutoDenyApproval:
    def prepare(self, request: dict[str, Any]) -> None:
        return None

    def decide(self, request: dict[str, Any], timeout: float | None = None) -> str:
        return "deny"


class ConsoleApproval:
    def prepare(self, request: dict[str, Any]) -> None:
        return None

    def decide(self, request: dict[str, Any], timeout: float | None = None) -> str:
        print("\n需要确认：", request["summary"])
        print("工具：", request["tool_name"])
        print("风险：", request["risk"], ", ".join(request["rule_ids"]))
        answer = input("仅允许这一次？[y/N/cancel] ").strip().lower()
        if answer in {"y", "yes"}:
            return "approve_once"
        if answer in {"cancel", "c"}:
            return "cancel_task"
        return "deny"


@dataclass(slots=True)
class _PendingApproval:
    request: dict[str, Any]
    condition: threading.Condition = field(default_factory=threading.Condition)
    decision: str | None = None


class ApprovalBroker:
    """Thread-safe bridge between a running agent and the web approval endpoint."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.RLock()

    def prepare(self, request: dict[str, Any]) -> None:
        """Register before publishing approval.requested so fast clicks cannot be lost."""
        approval_id = request["approval_id"]
        with self._lock:
            self._pending.setdefault(approval_id, _PendingApproval(request))

    def decide(self, request: dict[str, Any], timeout: float | None = 300.0) -> str:
        approval_id = request["approval_id"]
        with self._lock:
            pending = self._pending.setdefault(approval_id, _PendingApproval(request))
        with pending.condition:
            pending.condition.wait_for(lambda: pending.decision is not None, timeout=timeout)
        with self._lock:
            if self._pending.get(approval_id) is pending:
                self._pending.pop(approval_id, None)
        return pending.decision or "deny"

    def resolve(self, approval_id: str, decision: str, session_id: str | None = None) -> bool:
        if decision not in {"approve_once", "deny", "cancel_task"}:
            return False
        with self._lock:
            pending = self._pending.get(approval_id)
        if pending is None:
            return False
        if session_id is not None and pending.request.get("session_id") != session_id:
            return False
        with pending.condition:
            pending.decision = decision
            pending.condition.notify_all()
        return True

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(value.request) for value in self._pending.values()]


def canonical_call_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}\n{encoded}".encode("utf-8")).hexdigest()


class Redactor:
    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = [value for value in secrets or [] if len(value) >= 6]

    def redact(self, value: str) -> str:
        result = value
        for secret in self.secrets:
            result = result.replace(secret, "***REDACTED***")
        result = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+", r"\1***REDACTED***", result)
        return result
