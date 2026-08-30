from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import codeyf.tools as tools_module
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


def test_apply_patch_accepts_minimax_unified_diff_for_new_file(tmp_path: Path) -> None:
    patch = """*** Begin Patch
--- /dev/null
+++ config.h
@@ -0,0 +1,3 @@
+#pragma once
+#define APP_NAME "CodeYF"
+
*** End Patch"""

    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))

    assert result.ok, result.to_dict()
    assert (tmp_path / "config.h").read_text(encoding="utf-8") == (
        '#pragma once\n#define APP_NAME "CodeYF"\n\n'
    )
    assert result.data["changes"] == [
        {"path": "config.h", "action": "add", "added": 3, "removed": 0}
    ]


def test_apply_patch_unified_diff_updates_and_deletes_files(tmp_path: Path) -> None:
    (tmp_path / "main.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "obsolete.txt").write_text("remove me\n", encoding="utf-8")
    patch = """diff --git a/main.txt b/main.txt
--- a/main.txt
+++ b/main.txt
@@ -1,2 +1,2 @@
 alpha
-beta
+gamma
diff --git a/obsolete.txt b/obsolete.txt
--- a/obsolete.txt
+++ /dev/null
@@ -1 +0,0 @@
-remove me"""

    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))

    assert result.ok, result.to_dict()
    assert (tmp_path / "main.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert not (tmp_path / "obsolete.txt").exists()


def test_apply_patch_multifile_prepare_failure_leaves_every_file_unchanged(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("actual\n", encoding="utf-8")
    patch = """*** Begin Patch
--- /dev/null
+++ created.txt
@@ -0,0 +1 @@
+new
--- existing.txt
+++ existing.txt
@@ -1 +1 @@
-expected
+changed
*** End Patch"""

    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))

    assert not result.ok
    assert result.error.code == "PATCH_CONTEXT_MISMATCH"
    assert not (tmp_path / "created.txt").exists()
    assert existing.read_text(encoding="utf-8") == "actual\n"


def test_apply_patch_multifile_write_failure_rolls_back_prior_write(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: first.txt
@@
-old first
+new first
*** Update File: second.txt
@@
-old second
+new second
*** End Patch"""
    real_atomic_write = tools_module._atomic_write_bytes

    def fail_second_write(path: Path, data: bytes) -> None:
        if path.name == "second.txt" and data == b"new second\n":
            raise OSError("simulated second-file failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(tools_module, "_atomic_write_bytes", fail_second_write)

    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))

    assert not result.ok
    assert result.error.code == "TOOL_IO_ERROR"
    assert result.error.details["rollback_complete"] is True
    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "old second\n"


def test_apply_patch_rejects_unified_diff_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped.txt"
    patch = """*** Begin Patch
--- /dev/null
+++ ../escaped.txt
@@ -0,0 +1 @@
+escape
*** End Patch"""

    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))

    assert not result.ok
    assert result.error.code == "PATH_OUTSIDE_WORKSPACE"
    assert not outside.exists()


def test_apply_patch_rejects_absolute_unified_diff_path(tmp_path: Path) -> None:
    patch = """*** Begin Patch
--- /dev/null
+++ C:/outside.txt
@@ -0,0 +1 @@
+escape
*** End Patch"""

    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))

    assert not result.ok
    assert result.error.code == "PATH_OUTSIDE_WORKSPACE"


def test_invalid_patch_error_explains_detected_and_expected_formats(tmp_path: Path) -> None:
    patch = "*** Begin Patch\nthis is not a patch\n*** End Patch"

    result = ApplyPatchTool(PathGuard(tmp_path)).execute({"patch": patch}, session(tmp_path))

    assert not result.ok
    assert result.error.code == "PATCH_PARSE_ERROR"
    assert result.error.details["detected_format"] == "wrapped_unknown"
    assert result.error.details["expected_formats"] == ["codeyf_native", "unified_diff"]
    assert "Add File: config.h" in result.error.details["correction_example"]


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


def test_missing_command_is_rejected_before_approval(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(RunCommandTool(PathGuard(tmp_path), ToolConfig(), SecurityConfig()))
    active_session = session(tmp_path)
    dispatcher = ToolDispatcher(registry, SecurityPolicy("balanced"), AutoDenyApproval())

    result = dispatcher.execute(
        ToolCall("call_missing", "run_command", {"argv": ["codeyf-compiler-that-does-not-exist"]}),
        active_session,
    )

    assert not result.ok
    assert result.error.code == "COMMAND_NOT_FOUND"
    assert result.error.details["executable"] == "codeyf-compiler-that-does-not-exist"
    assert not any(event.type == "approval.requested" for event in active_session.events)


def test_policy_denies_windows_shell_file_write_bypass() -> None:
    policy = SecurityPolicy("auto")

    cmd_result = policy.assess(
        "run_command",
        {"argv": ["cmd", "/c", "type", "nul", ">", "created.txt"]},
    )
    powershell_result = policy.assess(
        "run_command",
        {"argv": ["powershell", "-Command", "Set-Content created.txt value"]},
    )

    assert cmd_result.decision.value == "deny"
    assert cmd_result.rule_ids == ("CMD_FILE_WRITE_BYPASS",)
    assert powershell_result.decision.value == "deny"
    assert powershell_result.rule_ids == ("CMD_FILE_WRITE_BYPASS",)


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
