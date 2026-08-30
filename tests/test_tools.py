from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from codeyf.config import SecurityConfig, ToolConfig
from codeyf.domain import AgentSession
from codeyf.security import PathGuard
from codeyf.tools import ApplyPatchTool, ReadFileTool, RunCommandTool, SearchTextTool
from codeyf.tools import ToolDispatcher, ToolRegistry
from codeyf.domain import ToolCall
from codeyf.security import AutoDenyApproval, SecurityPolicy


def session(workspace: Path) -> AgentSession:
    return AgentSession(workspace, "test-model", "balanced")


def test_read_file_returns_numbered_lines_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "hello.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = ReadFileTool(PathGuard(tmp_path), ToolConfig()).execute(
        {"path": "hello.py", "start_line": 2, "end_line": 3}, session(tmp_path)
    )
    assert result.ok
    assert result.data["content"] == "2: two\n3: three"
    assert len(result.data["sha256"]) == 64


def test_search_text_supports_regex(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    result = SearchTextTool(PathGuard(tmp_path), ToolConfig()).execute(
        {"query": r"b.ta", "regex": True}, session(tmp_path)
    )
    assert result.ok
    assert result.data["matches"][0]["line"] == 2


def test_apply_patch_updates_and_creates_files(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def add(a, b):\n    pass\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: main.py
@@
 def add(a, b):
-    pass
+    return a + b
*** Add File: test_main.py
+from main import add
+assert add(1, 2) == 3
*** End Patch"""
    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))
    assert result.ok, result.to_dict()
    assert "return a + b" in (tmp_path / "main.py").read_text(encoding="utf-8")
    assert (tmp_path / "test_main.py").exists()
    assert len(result.data["changes"]) == 2


def test_apply_patch_is_noop_when_context_mismatches(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("actual\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: main.py
@@
-expected
+changed
*** End Patch"""
    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))
    assert not result.ok
    assert result.error.code == "PATCH_CONTEXT_MISMATCH"
    assert path.read_text(encoding="utf-8") == "actual\n"


def test_run_command_captures_exit_code_and_output(tmp_path: Path) -> None:
    tool = RunCommandTool(PathGuard(tmp_path), ToolConfig(), SecurityConfig())
    result = tool.execute(
        {"argv": [sys.executable, "-c", "print('hello')"]}, session(tmp_path)
    )
    assert result.ok
    assert result.data["exit_code"] == 0
    assert result.data["stdout"].strip() == "hello"


def test_dispatcher_rejects_unknown_argument_before_execution(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool(PathGuard(tmp_path), ToolConfig()))
    dispatcher = ToolDispatcher(registry, SecurityPolicy("balanced"), AutoDenyApproval())
    result = dispatcher.execute(
        ToolCall("call_bad", "read_file", {"path": "x", "surprise": True}), session(tmp_path)
    )
    assert not result.ok
    assert result.error.code == "INVALID_ARGUMENT"


def test_run_command_honors_session_cancellation(tmp_path: Path) -> None:
    active_session = session(tmp_path)
    tool = RunCommandTool(PathGuard(tmp_path), ToolConfig(command_timeout_seconds=10), SecurityConfig())
    output = {}

    def run() -> None:
        output["result"] = tool.execute(
            {"argv": [sys.executable, "-c", "import time; time.sleep(5)"]}, active_session
        )

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.15)
    active_session.cancel_event.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert output["result"].error.code == "CANCELLED"
