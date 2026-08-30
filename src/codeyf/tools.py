from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import SecurityConfig, ToolConfig
from .domain import AgentSession, SessionStatus, ToolCall, ToolError, ToolResult
from .security import (
    ApprovalProvider,
    PathGuard,
    PathSecurityError,
    PolicyDecision,
    SecurityPolicy,
    canonical_call_hash,
)


IGNORED_DIRS = {".git", ".codeyf", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def execute(self, arguments: dict[str, Any], session: AgentSession) -> ToolResult: ...


def _error(code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(False, error=ToolError(code, message, retryable, details or {}))


def _trim_output(text: str, limit: int) -> tuple[str, bool, int]:
    if len(text) <= limit:
        return text, False, 0
    keep = max(1, (limit - 100) // 2)
    omitted = len(text) - keep * 2
    return f"{text[:keep]}\n[... omitted {omitted} chars ...]\n{text[-keep:]}", True, omitted


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=flags,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if process.poll() is None:
        process.kill()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象"
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        return f"缺少必填参数: {', '.join(missing)}"
    if schema.get("additionalProperties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            return f"存在未知参数: {', '.join(sorted(unknown))}"
    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    for name, value in arguments.items():
        definition = properties.get(name, {})
        expected_name = definition.get("type")
        expected = type_map.get(expected_name)
        if expected and (not isinstance(value, expected) or expected_name in {"integer", "number"} and isinstance(value, bool)):
            return f"参数 {name} 必须是 {expected_name}"
        if isinstance(value, (int, float)) and "minimum" in definition and value < definition["minimum"]:
            return f"参数 {name} 小于最小值 {definition['minimum']}"
        if isinstance(value, list) and "minItems" in definition and len(value) < definition["minItems"]:
            return f"参数 {name} 至少包含 {definition['minItems']} 项"
    return None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": tool.name, "description": tool.description, "parameters": tool.input_schema},
            }
            for tool in self._tools.values()
        ]


@dataclass(slots=True)
class ReadFileTool:
    guard: PathGuard
    config: ToolConfig
    name = "read_file"
    description = "读取工作区内 UTF-8 文本文件的指定行范围；较大结果会被截断。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], session: AgentSession) -> ToolResult:
        path_value = arguments.get("path")
        if not isinstance(path_value, str):
            return _error("INVALID_ARGUMENT", "path 必须是字符串")
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line")
        if not isinstance(start, int) or start < 1 or (end is not None and (not isinstance(end, int) or end < start)):
            return _error("INVALID_ARGUMENT", "行范围无效")
        try:
            path = self.guard.resolve(path_value, must_exist=True)
            if not path.is_file():
                return _error("PATH_IS_DIRECTORY", "目标不是文件", details={"path": path_value})
            raw = path.read_bytes()
            if b"\x00" in raw:
                return _error("BINARY_FILE", "不支持读取二进制文件", details={"path": path_value})
            text = raw.decode("utf-8")
        except PathSecurityError as exc:
            return _error("PATH_OUTSIDE_WORKSPACE", str(exc), details={"path": path_value})
        except FileNotFoundError:
            return _error("PATH_NOT_FOUND", "文件不存在", details={"path": path_value})
        except UnicodeDecodeError:
            return _error("BINARY_FILE", "文件不是 UTF-8 文本", details={"path": path_value})
        except OSError as exc:
            return _error("TOOL_IO_ERROR", f"读取文件失败: {exc}")
        lines = text.splitlines()
        selected = lines[start - 1 : end]
        numbered = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start=start))
        content, truncated, omitted = _trim_output(numbered, self.config.max_file_read_chars)
        return ToolResult(True, {
            "path": self.guard.relative(path),
            "content": content,
            "start_line": start,
            "end_line": start + max(0, len(selected) - 1),
            "total_lines": len(lines),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }, meta={"truncated": truncated, "omitted_chars": omitted})


@dataclass(slots=True)
class ListFilesTool:
    guard: PathGuard
    config: ToolConfig
    name = "list_files"
    description = "按 glob 列出工作区文件。默认忽略依赖、构建产物和版本控制目录。"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "include_hidden": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 5000},
        },
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], session: AgentSession) -> ToolResult:
        pattern = arguments.get("pattern", "**/*")
        include_hidden = arguments.get("include_hidden", False)
        limit = arguments.get("max_results", self.config.max_list_files)
        if not isinstance(pattern, str) or not isinstance(include_hidden, bool) or not isinstance(limit, int):
            return _error("INVALID_ARGUMENT", "list_files 参数类型无效")
        limit = min(max(1, limit), 5000)
        results: list[dict[str, Any]] = []
        try:
            for root, dirs, files in os.walk(self.guard.workspace, followlinks=False):
                dirs[:] = [
                    name for name in dirs
                    if name not in IGNORED_DIRS and (include_hidden or not name.startswith("."))
                ]
                for name in files:
                    if not include_hidden and name.startswith("."):
                        continue
                    path = Path(root) / name
                    rel = self.guard.relative(path)
                    if fnmatch.fnmatch(rel, pattern) or (pattern == "**/*"):
                        results.append({"path": rel, "size": path.stat().st_size})
                        if len(results) >= limit:
                            break
                if len(results) >= limit:
                    break
        except OSError as exc:
            return _error("TOOL_IO_ERROR", f"列出文件失败: {exc}")
        results.sort(key=lambda item: item["path"])
        return ToolResult(True, {"files": results, "count": len(results)}, meta={"truncated": len(results) >= limit})


@dataclass(slots=True)
class SearchTextTool:
    guard: PathGuard
    config: ToolConfig
    name = "search_text"
    description = "在工作区文本文件中搜索文字或正则表达式，可限制目录和 glob。"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "regex": {"type": "boolean"},
            "case_sensitive": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], session: AgentSession) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            return _error("INVALID_ARGUMENT", "query 必须是非空字符串")
        base_value = arguments.get("path", ".")
        glob_value = arguments.get("glob")
        is_regex = arguments.get("regex", False)
        case_sensitive = arguments.get("case_sensitive", True)
        limit = min(arguments.get("max_results", self.config.max_search_matches), 1000)
        if not isinstance(base_value, str) or not isinstance(is_regex, bool) or not isinstance(case_sensitive, bool) or not isinstance(limit, int):
            return _error("INVALID_ARGUMENT", "search_text 参数类型无效")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if is_regex else re.escape(query), flags)
        except re.error as exc:
            return _error("INVALID_REGEX", f"正则表达式无效: {exc}")
        try:
            base = self.guard.resolve(base_value, must_exist=True)
        except PathSecurityError as exc:
            return _error("PATH_OUTSIDE_WORKSPACE", str(exc))
        except FileNotFoundError:
            return _error("PATH_NOT_FOUND", "搜索路径不存在")
        candidates = [base] if base.is_file() else base.rglob("*")
        matches: list[dict[str, Any]] = []
        try:
            for path in candidates:
                if not path.is_file() or any(part in IGNORED_DIRS for part in path.relative_to(self.guard.workspace).parts):
                    continue
                rel = self.guard.relative(path)
                if glob_value and not fnmatch.fnmatch(rel, str(glob_value)):
                    continue
                if path.stat().st_size > 2_000_000:
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for line_number, line in enumerate(content.splitlines(), 1):
                    found = pattern.search(line)
                    if found:
                        preview, truncated, _ = _trim_output(line, 500)
                        matches.append({
                            "path": rel,
                            "line": line_number,
                            "column": found.start() + 1,
                            "text": preview,
                            "text_truncated": truncated,
                        })
                        if len(matches) >= limit:
                            return ToolResult(True, {"matches": matches, "count": len(matches), "engine": "python"}, meta={"truncated": True})
        except OSError as exc:
            return _error("TOOL_IO_ERROR", f"搜索失败: {exc}")
        return ToolResult(True, {"matches": matches, "count": len(matches), "engine": "python"}, meta={"truncated": False})


@dataclass(slots=True)
class _PatchOperation:
    action: str
    path: str
    body: list[str]


def _parse_legacy_patch(patch: str) -> list[_PatchOperation]:
    lines = patch.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "*** Begin Patch" or "*** End Patch" not in lines:
        raise ValueError("补丁必须由 *** Begin Patch 和 *** End Patch 包围")
    operations: list[_PatchOperation] = []
    current: _PatchOperation | None = None
    for line in lines[1:]:
        if line == "*** End Patch":
            if current:
                operations.append(current)
            return operations
        match = re.match(r"\*\*\* (Update|Add|Delete) File: (.+)$", line)
        if match:
            if current:
                operations.append(current)
            current = _PatchOperation(match.group(1).lower(), match.group(2).strip(), [])
        elif current is not None:
            current.body.append(line)
        elif line.strip():
            raise ValueError("文件声明前存在补丁内容")
    raise ValueError("补丁缺少结束标记")


PATCH_MARKER = "*" * 3
PATCH_BEGIN = PATCH_MARKER + " Begin Patch"
PATCH_END = PATCH_MARKER + " End Patch"
PATCH_CORRECTION_EXAMPLE = "\n".join((
    PATCH_BEGIN,
    PATCH_MARKER + " Add File: config.h",
    "+#pragma once",
    "+#define APP_NAME CodeYF",
    PATCH_END,
))


def _patch_lines(patch: str) -> list[str]:
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[0] == PATCH_BEGIN:
        if lines[-1] != PATCH_END:
            raise ValueError("CodeYF 补丁开始标记必须有对应的结束标记")
        return lines[1:-1]
    return lines


def _detect_patch_format(patch: str) -> str:
    normalized = patch.replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"(?m)^\*\*\* (?:Add|Update|Delete) File:", normalized):
        return "codeyf_native"
    if re.search(r"(?m)^diff --git ", normalized) or (
        re.search(r"(?m)^--- ", normalized) and re.search(r"(?m)^\+\+\+ ", normalized)
    ):
        return "unified_diff"
    if normalized.lstrip().startswith(PATCH_BEGIN):
        return "wrapped_unknown"
    return "unknown"


def _parse_native_patch(lines: list[str]) -> list[_PatchOperation]:
    operations: list[_PatchOperation] = []
    current: _PatchOperation | None = None
    for line in lines:
        match = re.match(r"\*\*\* (Update|Add|Delete) File: (.+)$", line)
        if match:
            if current:
                operations.append(current)
            current = _PatchOperation(match.group(1).lower(), match.group(2).strip(), [])
        elif current is not None:
            current.body.append(line)
        elif line.strip():
            raise ValueError("文件声明前存在补丁内容")
    if current:
        operations.append(current)
    if not operations:
        raise ValueError("补丁不包含文件操作")
    return operations


def _unified_path(header: str, marker: str) -> str | None:
    raw = header[len(marker):].strip()
    if not raw:
        raise ValueError(f"{marker.strip()} 文件头缺少路径")
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"') or raw.endswith('"'):
        raise ValueError("暂不支持带引号的 unified diff 路径；请改用 CodeYF 原生格式")
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    if not raw:
        raise ValueError("unified diff 文件路径不能为空")
    return raw


def _normalize_unified_add(body: list[str]) -> list[str]:
    result: list[str] = []
    saw_hunk = False
    for line in body:
        if line.startswith("@@"):
            saw_hunk = True
            continue
        if line == r"\ No newline at end of file":
            continue
        if not saw_hunk:
            if not line.strip():
                continue
            raise ValueError("unified diff 文件内容前缺少 @@ hunk")
        if line.startswith("+"):
            result.append(line)
        elif line.startswith((" ", "-")):
            raise ValueError("从 /dev/null 新增文件的 hunk 只能包含 + 内容行")
        elif line.strip():
            raise ValueError("unified diff hunk 行必须以空格、+ 或 - 开头")
    if not saw_hunk:
        raise ValueError("unified diff 至少需要一个 @@ hunk")
    return result


def _parse_unified_patch(lines: list[str]) -> list[_PatchOperation]:
    operations: list[_PatchOperation] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old_path = _unified_path(line, "--- ")
            new_path = _unified_path(lines[index + 1], "+++ ")
            index += 2
            body: list[str] = []
            while index < len(lines):
                if lines[index].startswith("diff --git "):
                    break
                if (
                    lines[index].startswith("--- ")
                    and index + 1 < len(lines)
                    and lines[index + 1].startswith("+++ ")
                ):
                    break
                body.append(lines[index])
                index += 1
            if old_path is None and new_path is None:
                raise ValueError("unified diff 的新旧路径不能同时为 /dev/null")
            if old_path is None:
                action = "add"
                target = new_path
                body = _normalize_unified_add(body)
            elif new_path is None:
                action = "delete"
                target = old_path
            else:
                if old_path != new_path:
                    raise ValueError("不支持通过 unified diff 重命名文件；请拆分为删除和新增")
                action = "update"
                target = new_path
            if not any(item.startswith("@@") for item in body) and action != "add":
                raise ValueError("unified diff 至少需要一个 @@ hunk")
            operations.append(_PatchOperation(action, target or "", body))
            continue
        if line.startswith(("diff --git ", "index ", "new file mode ", "deleted file mode ")):
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        raise ValueError(f"无法识别 unified diff 行: {line[:120]}")
    if not operations:
        raise ValueError("unified diff 不包含 ---/+++ 文件头")
    return operations


def _parse_patch(patch: str) -> list[_PatchOperation]:
    lines = _patch_lines(patch)
    detected = _detect_patch_format(patch)
    if detected == "codeyf_native":
        return _parse_native_patch(lines)
    if detected == "unified_diff":
        return _parse_unified_patch(lines)
    raise ValueError("未识别到 CodeYF 文件声明或 unified diff 的 ---/+++ 文件头")


def _patch_parse_error(patch: str, error: Exception) -> ToolResult:
    detected = _detect_patch_format(patch)
    message = (
        f"{error}；实际识别格式: {detected}。"
        "请使用 CodeYF 原生的 Add/Update/Delete File 格式，"
        "或包含 ---、+++、@@ 的 unified diff。"
    )
    return _error(
        "PATCH_PARSE_ERROR",
        message,
        retryable=True,
        details={
            "expected_formats": ["codeyf_native", "unified_diff"],
            "detected_format": detected,
            "correction_example": PATCH_CORRECTION_EXAMPLE,
        },
    )


def _apply_update(original: str, body: list[str]) -> str:
    source = original.splitlines()
    trailing_newline = original.endswith("\n")
    hunks: list[list[str]] = []
    current: list[str] = []
    for line in body:
        if line == r"\ No newline at end of file":
            continue
        if line.startswith("@@"):
            if current:
                hunks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        hunks.append(current)
    if not hunks:
        raise ValueError("更新补丁至少需要一个 @@ hunk")
    cursor = 0
    for hunk in hunks:
        old = [line[1:] for line in hunk if line.startswith((" ", "-"))]
        new = [line[1:] for line in hunk if line.startswith((" ", "+"))]
        if any(line and line[0] not in " +-" for line in hunk):
            raise ValueError("hunk 行必须以空格、+ 或 - 开头")
        found = -1
        for index in range(cursor, len(source) - len(old) + 1):
            if source[index : index + len(old)] == old:
                found = index
                break
        if found < 0:
            raise LookupError("补丁上下文与文件不匹配")
        source[found : found + len(old)] = new
        cursor = found + len(new)
    result = "\n".join(source)
    return result + ("\n" if trailing_newline or result else "")


@dataclass(slots=True)
class ApplyPatchTool:
    guard: PathGuard
    name = "apply_patch"
    description = (
        "以原子方式创建、更新或删除工作区文本文件。patch 可使用 CodeYF 原生格式"
        "（Add/Update/Delete File）或常见 unified diff（---/+++ 与 @@）；"
        "路径必须相对工作区，禁止绝对路径和 ..。不要用 run_command 或 shell 重定向代替此工具写文件。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": (
                    "CodeYF 原生补丁或 unified diff。新增文件可使用 "
                    "'--- /dev/null'、'+++ path' 和 '@@ -0,0 +1,N @@'。"
                ),
            }
        },
        "required": ["patch"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], session: AgentSession) -> ToolResult:
        patch = arguments.get("patch")
        if not isinstance(patch, str) or not patch:
            return _error("INVALID_ARGUMENT", "patch 必须是非空字符串")
        try:
            operations = _parse_patch(patch)
        except ValueError as exc:
            return _patch_parse_error(patch, exc)
        staged: list[tuple[_PatchOperation, Path, str | None, bytes | None]] = []
        targets: set[str] = set()
        try:
            for operation in operations:
                raw_path = Path(operation.path)
                if raw_path.is_absolute() or ".." in raw_path.parts:
                    raise PathSecurityError("补丁路径必须是工作区内不含 .. 的相对路径")
                path = self.guard.resolve(operation.path)
                target_key = os.path.normcase(str(path))
                if target_key in targets:
                    raise ValueError(f"同一补丁不能多次操作同一路径: {operation.path}")
                targets.add(target_key)
                before = path.read_bytes() if path.exists() else None
                if operation.action == "add":
                    if path.exists():
                        raise FileExistsError(operation.path)
                    content = "\n".join(line[1:] if line.startswith("+") else line for line in operation.body)
                    if operation.body and operation.body[-1] == "+":
                        content += "\n"
                    if content and not content.endswith("\n"):
                        content += "\n"
                    staged.append((operation, path, content, before))
                elif operation.action == "delete":
                    if not path.is_file():
                        raise FileNotFoundError(operation.path)
                    staged.append((operation, path, None, before))
                else:
                    if not path.is_file():
                        raise FileNotFoundError(operation.path)
                    original = before.decode("utf-8") if before is not None else ""
                    updated = _apply_update(original, operation.body)
                    staged.append((operation, path, updated, before))
        except PathSecurityError as exc:
            return _error("PATH_OUTSIDE_WORKSPACE", str(exc))
        except FileNotFoundError as exc:
            return _error("PATH_NOT_FOUND", f"补丁目标不存在: {exc}", retryable=True)
        except FileExistsError as exc:
            return _error("FILE_EXISTS", f"新增目标已存在: {exc}", retryable=True)
        except UnicodeDecodeError:
            return _error("BINARY_FILE", "不能用补丁修改非 UTF-8 文件")
        except LookupError as exc:
            return _error("PATCH_CONTEXT_MISMATCH", str(exc), retryable=True)
        except ValueError as exc:
            return _patch_parse_error(patch, exc)
        except OSError as exc:
            return _error("TOOL_IO_ERROR", f"准备补丁失败: {exc}")

        changes: list[dict[str, Any]] = []
        applied: list[tuple[Path, bytes | None]] = []
        try:
            for operation, path, _content, before in staged:
                if before is None:
                    if path.exists():
                        return _error("FILE_CHANGED", f"文件在补丁应用前发生变化: {operation.path}", retryable=True)
                elif not path.exists() or path.read_bytes() != before:
                    return _error("FILE_CHANGED", f"文件在补丁应用前发生变化: {operation.path}", retryable=True)
            for operation, path, content, before in staged:
                if operation.action == "delete":
                    path.unlink()
                    applied.append((path, before))
                    added = 0
                    removed = len((before or b"").decode("utf-8").splitlines())
                else:
                    encoded = (content or "").encode("utf-8")
                    _atomic_write_bytes(path, encoded)
                    applied.append((path, before))
                    before_lines = (before or b"").decode("utf-8").splitlines()
                    after_lines = (content or "").splitlines()
                    added = max(0, len(after_lines) - len(before_lines))
                    removed = max(0, len(before_lines) - len(after_lines))
                    if operation.action == "update":
                        plus = sum(1 for line in operation.body if line.startswith("+"))
                        minus = sum(1 for line in operation.body if line.startswith("-"))
                        added, removed = plus, minus
                changes.append({"path": operation.path, "action": operation.action, "added": added, "removed": removed})
        except OSError as exc:
            rollback_errors: list[str] = []
            for applied_path, original in reversed(applied):
                try:
                    if original is None:
                        if applied_path.exists():
                            applied_path.unlink()
                    else:
                        _atomic_write_bytes(applied_path, original)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{applied_path.name}: {rollback_exc}")
            message = f"写入补丁失败，已回滚已应用文件: {exc}"
            if rollback_errors:
                message += f"；回滚不完整: {'; '.join(rollback_errors)}"
            return _error("TOOL_IO_ERROR", message, details={"rollback_complete": not rollback_errors})
        return ToolResult(True, {"changes": changes})


@dataclass(slots=True)
class RunCommandTool:
    guard: PathGuard
    tool_config: ToolConfig
    security_config: SecurityConfig
    name = "run_command"
    description = "在当前宿主操作系统的工作区子目录运行已安装命令，并返回 stdout、stderr、退出码和超时状态。优先使用 argv，避免 shell；不要假定 Linux 命令存在。"
    input_schema = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "command": {"type": "string"},
            "shell": {"type": "boolean"},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 3600},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "additionalProperties": False,
    }

    def preflight(self, arguments: dict[str, Any], session: AgentSession) -> ToolResult | None:
        """Reject missing argv executables before policy/approval is evaluated."""
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            return None
        executable = argv[0].strip()
        if not executable:
            return None
        cwd_value = arguments.get("cwd", ".")
        try:
            cwd = self.guard.resolve(cwd_value, must_exist=True)
        except (PathSecurityError, FileNotFoundError):
            return None
        environment_path = os.environ.get("PATH") if "PATH" in self.security_config.inherit_environment else None
        has_path_part = Path(executable).is_absolute() or any(separator in executable for separator in ("/", "\\"))
        if has_path_part:
            candidate = Path(executable)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            found = candidate.is_file()
        else:
            found = shutil.which(executable, path=environment_path) is not None
        if found:
            return None
        host = platform.system() or os.name
        return _error(
            "COMMAND_NOT_FOUND",
            f"当前 {host} 环境找不到命令: {executable}。请只使用运行环境列出的可用命令；不要改用其他操作系统的命令。",
            details={"executable": executable, "platform": host},
        )

    def execute(self, arguments: dict[str, Any], session: AgentSession) -> ToolResult:
        argv = arguments.get("argv")
        command = arguments.get("command")
        shell = arguments.get("shell", False)
        if (argv is None) == (command is None):
            return _error("INVALID_ARGUMENT", "必须且只能提供 argv 或 command")
        if argv is not None and (not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv)):
            return _error("INVALID_ARGUMENT", "argv 必须是非空字符串数组")
        if command is not None and (not isinstance(command, str) or not shell):
            return _error("INVALID_ARGUMENT", "command 仅在 shell=true 时可用")
        if shell and not self.security_config.allow_shell:
            return _error("POLICY_DENIED", "配置禁止 shell=true")
        cwd_value = arguments.get("cwd", ".")
        try:
            cwd = self.guard.resolve(cwd_value, must_exist=True)
        except PathSecurityError as exc:
            return _error("PATH_OUTSIDE_WORKSPACE", str(exc))
        except FileNotFoundError:
            return _error("PATH_NOT_FOUND", "命令工作目录不存在")
        if not cwd.is_dir():
            return _error("INVALID_ARGUMENT", "命令 cwd 必须是目录")
        timeout = float(arguments.get("timeout_seconds", self.tool_config.command_timeout_seconds))
        environment = {key: os.environ[key] for key in self.security_config.inherit_environment if key in os.environ}
        extra_env = arguments.get("env", {})
        if not isinstance(extra_env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra_env.items()):
            return _error("INVALID_ARGUMENT", "env 必须是字符串键值对象")
        environment.update(extra_env)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        start = time.monotonic()
        try:
            process = subprocess.Popen(
                command if command is not None else argv,
                cwd=cwd,
                env=environment,
                shell=shell,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            deadline = time.monotonic() + timeout
            timed_out = False
            cancelled = False
            while True:
                if session.cancel_event.is_set():
                    cancelled = True
                    _terminate_process_tree(process)
                    stdout, stderr = process.communicate(timeout=5)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate_process_tree(process)
                    stdout, stderr = process.communicate(timeout=5)
                    break
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except FileNotFoundError:
            return _error("COMMAND_NOT_FOUND", f"找不到命令: {(argv or ['shell'])[0]}")
        except OSError as exc:
            return _error("TOOL_IO_ERROR", f"无法启动命令: {exc}")
        duration = int((time.monotonic() - start) * 1000)
        stdout, stdout_cut, stdout_omitted = _trim_output(stdout, self.tool_config.max_output_chars)
        stderr, stderr_cut, stderr_omitted = _trim_output(stderr, self.tool_config.max_output_chars)
        data = {
            "argv": argv if argv is not None else [command],
            "cwd": self.guard.relative(cwd),
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
        }
        meta = {"duration_ms": duration, "truncated": stdout_cut or stderr_cut, "omitted_chars": stdout_omitted + stderr_omitted}
        if cancelled:
            return ToolResult(False, data, ToolError("CANCELLED", "命令被用户取消"), meta)
        if timed_out:
            return ToolResult(False, data, ToolError("COMMAND_TIMEOUT", f"命令超过 {timeout:g} 秒"), meta)
        if process.returncode != 0:
            return ToolResult(False, data, ToolError("COMMAND_FAILED", f"命令退出码为 {process.returncode}", True), meta)
        return ToolResult(True, data, meta=meta)


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, policy: SecurityPolicy, approvals: ApprovalProvider) -> None:
        self.registry = registry
        self.policy = policy
        self.approvals = approvals

    def execute(self, call: ToolCall, session: AgentSession) -> ToolResult:
        started = time.monotonic()
        tool = self.registry.get(call.name)
        if tool is None:
            return self._finish(call, session, _error("UNKNOWN_TOOL", f"未知工具: {call.name}"), started, "not_required")
        validation_error = _validate_arguments(tool.input_schema, call.arguments)
        if validation_error:
            result = _error("INVALID_ARGUMENT", validation_error, retryable=True)
            return self._finish(call, session, result, started, "not_required")
        preflight = getattr(tool, "preflight", None)
        if callable(preflight):
            preflight_result = preflight(call.arguments, session)
            if preflight_result is not None:
                return self._finish(call, session, preflight_result, started, "not_required")
        assessment = self.policy.assess(call.name, call.arguments)
        if assessment.decision == PolicyDecision.DENY:
            result = _error("POLICY_DENIED", assessment.summary, details={"rule_ids": list(assessment.rule_ids)})
            return self._finish(call, session, result, started, "denied_by_policy")
        approval_state = "not_required"
        if assessment.decision == PolicyDecision.ASK:
            approval_id = f"apr_{call.id}"
            request = {
                "approval_id": approval_id,
                "session_id": session.id,
                "tool_call_id": call.id,
                "tool_name": call.name,
                "risk": assessment.risk,
                "rule_ids": list(assessment.rule_ids),
                "summary": assessment.summary,
                "display_arguments": call.arguments,
                "argument_hash": canonical_call_hash(call.name, call.arguments),
            }
            session.transition(SessionStatus.WAITING_APPROVAL, "approval_required")
            self.approvals.prepare(request)
            session.emit("approval.requested", request, call.id)
            decision = self.approvals.decide(request)
            session.emit("approval.decided", {"approval_id": approval_id, "decision": decision}, call.id)
            session.transition(SessionStatus.RUNNING, f"approval_{decision}")
            if decision == "cancel_task":
                session.cancel_event.set()
                return self._finish(call, session, _error("CANCELLED", "用户取消了任务"), started, "cancelled")
            if decision != "approve_once":
                return self._finish(call, session, _error("APPROVAL_DENIED", "用户拒绝了此操作"), started, "denied")
            approval_state = "approved_once"
        session.emit("tool.started", {"tool_call_id": call.id, "name": call.name}, call.id)
        try:
            result = tool.execute(call.arguments, session)
        except Exception as exc:  # defensive boundary around every plugin/tool
            result = _error("INTERNAL_ERROR", f"工具执行发生未预期错误: {type(exc).__name__}")
        return self._finish(call, session, result, started, approval_state)

    @staticmethod
    def _finish(
        call: ToolCall,
        session: AgentSession,
        result: ToolResult,
        started: float,
        approval_state: str,
    ) -> ToolResult:
        result.meta.setdefault("duration_ms", int((time.monotonic() - started) * 1000))
        result.meta.setdefault("approval", approval_state)
        session.emit("tool.finished", {
            "tool_call_id": call.id,
            "name": call.name,
            "ok": result.ok,
            "error_code": result.error.code if result.error else None,
            "duration_ms": result.meta.get("duration_ms", 0),
            "truncated": result.meta.get("truncated", False),
            "result": result.to_dict(),
        }, call.id)
        return result


def build_default_registry(workspace: Path, tool_config: ToolConfig, security_config: SecurityConfig) -> tuple[ToolRegistry, PathGuard]:
    guard = PathGuard(workspace)
    registry = ToolRegistry()
    registry.register(ReadFileTool(guard, tool_config))
    registry.register(ListFilesTool(guard, tool_config))
    registry.register(SearchTextTool(guard, tool_config))
    registry.register(ApplyPatchTool(guard))
    registry.register(RunCommandTool(guard, tool_config, security_config))
    return registry, guard
