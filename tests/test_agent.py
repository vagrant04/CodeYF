from __future__ import annotations

from pathlib import Path

import codeyf.agent as agent_module
from codeyf.agent import AgentLoop, ContextManager
from codeyf.config import AppConfig
from codeyf.domain import AgentSession, ModelResponse, ToolCall
from codeyf.model import ModelContextExceeded, ScriptedModelClient
from codeyf.security import AutoDenyApproval
from codeyf.tools import build_default_registry


def test_agent_executes_tool_then_returns_final_text(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    model = ScriptedModelClient([
        ModelResponse(tool_calls=(ToolCall("call_1", "read_file", {"path": "hello.txt"}),), finish_reason="tool_calls"),
        ModelResponse(content="读取完成，没有需要修改的内容。", finish_reason="stop"),
    ])
    config = AppConfig()
    config.storage.enabled = False
    registry, _ = build_default_registry(tmp_path, config.tools, config.security)
    loop = AgentLoop(config, model, registry, AutoDenyApproval())
    session = AgentSession(tmp_path, "test-model", "balanced")

    result = loop.run(session, "读取 hello.txt")

    assert result.status == "completed"
    assert result.tool_calls == 1
    assert result.turns == 2
    assert session.messages[-2]["role"] == "tool"
    assert session.messages[-2]["tool_call_id"] == "call_1"
    assert any(event.type == "tool.finished" for event in session.events)


def test_agent_apply_patch_changes_the_real_workspace_file(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: app.py
@@
-value = 1
+value = 2
*** End Patch"""
    model = ScriptedModelClient([
        ModelResponse(
            tool_calls=(ToolCall("call_patch", "apply_patch", {"patch": patch}),),
            finish_reason="tool_calls",
        ),
        ModelResponse(content="已把 value 修改为 2。", finish_reason="stop"),
    ])
    config = AppConfig()
    config.storage.enabled = False
    registry, _ = build_default_registry(tmp_path, config.tools, config.security)

    result = AgentLoop(config, model, registry, AutoDenyApproval()).run(
        AgentSession(tmp_path, "test-model", "balanced"),
        "把 app.py 中的 value 改为 2",
    )

    assert result.status == "completed"
    assert result.tool_calls == 1
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_agent_stops_repeated_failed_call(tmp_path: Path) -> None:
    call = ToolCall("call_1", "read_file", {"path": "missing.txt"})
    model = ScriptedModelClient([
        ModelResponse(tool_calls=(call,)),
        ModelResponse(tool_calls=(ToolCall("call_2", call.name, call.arguments),)),
        ModelResponse(tool_calls=(ToolCall("call_3", call.name, call.arguments),)),
    ])
    config = AppConfig()
    config.agent.repeat_failure_limit = 3
    registry, _ = build_default_registry(tmp_path, config.tools, config.security)
    result = AgentLoop(config, model, registry, AutoDenyApproval()).run(
        AgentSession(tmp_path, "test-model", "balanced"), "读取不存在的文件"
    )
    assert result.status == "failed"
    assert result.stop_reason == "repeated_tool_failure"


def test_context_compaction_preserves_tool_call_result_pair(tmp_path: Path) -> None:
    config = AppConfig()
    config.model.context_window_tokens = 2500
    config.model.max_output_tokens = 100
    session = AgentSession(tmp_path, "test-model", "balanced")
    session.messages = [{"role": "system", "content": "system"}]
    for index in range(7):
        session.messages.extend([
            {"role": "user", "content": f"task {index} " + "x" * 120},
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{index}", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{index}", "name": "read_file", "content": "y" * 180},
        ])
    manager = ContextManager(config)
    compacted = manager.build(session, [])
    for index, message in enumerate(compacted):
        if message.get("role") == "tool":
            assert index > 0
            assert compacted[index - 1].get("role") in {"assistant", "tool"}
            assistant = next(item for item in reversed(compacted[:index]) if item.get("role") == "assistant")
            assert any(call["id"] == message["tool_call_id"] for call in assistant.get("tool_calls", []))


def test_agent_reactively_compacts_once_after_provider_overflow(tmp_path: Path) -> None:
    model = ScriptedModelClient([
        ModelResponse(content="先建立较长历史。"),
    ])
    config = AppConfig()
    registry, _ = build_default_registry(tmp_path, config.tools, config.security)
    session = AgentSession(tmp_path, "test-model", "balanced")
    # Provide enough closed history for reactive compaction, then raise overflow once.
    session.messages = [{"role": "system", "content": "system"}]
    for index in range(4):
        session.messages.extend([
            {"role": "user", "content": f"old {index}"},
            {"role": "assistant", "content": f"answer {index}"},
        ])
    model.responses = [ModelContextExceeded("maximum context length"), ModelResponse(content="恢复成功")]
    result = AgentLoop(config, model, registry, AutoDenyApproval()).run(session, "继续")
    assert result.status == "completed"
    assert result.final_text == "恢复成功"
    assert any(event.type == "context.compacted" and event.data["strategy"] == "reactive_overflow" for event in session.events)


def test_system_prompt_reports_windows_and_missing_compilers(monkeypatch) -> None:
    monkeypatch.setattr(agent_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(agent_module.platform, "release", lambda: "11")
    monkeypatch.setattr(
        agent_module.shutil,
        "which",
        lambda command: "C:/Python/python.exe" if command == "python" else None,
    )

    prompt = agent_module.build_system_prompt()

    assert "操作系统：Windows 11" in prompt
    assert "可用候选命令：python" in prompt
    assert "未安装的常见 C/C++ 编译器：cl, clang, clang++, gcc, g++, cc" in prompt
    assert "不得使用 bash/Linux 专用命令" in prompt


def test_agent_stops_repeating_same_invalid_patch(tmp_path: Path) -> None:
    invalid_patch = "*** Begin Patch\nnot a valid patch\n*** End Patch"
    model = ScriptedModelClient([
        ModelResponse(tool_calls=(ToolCall(f"bad_{index}", "apply_patch", {"patch": invalid_patch}),))
        for index in range(3)
    ])
    config = AppConfig()
    config.storage.enabled = False
    config.agent.repeat_failure_limit = 3
    registry, _ = build_default_registry(tmp_path, config.tools, config.security)
    active_session = AgentSession(tmp_path, "test-model", "balanced")

    result = AgentLoop(config, model, registry, AutoDenyApproval()).run(
        active_session,
        "创建配置文件",
    )

    assert result.status == "failed"
    assert result.stop_reason == "repeated_tool_failure"
    finished = [event for event in active_session.events if event.type == "tool.finished"]
    assert len(finished) == 3
    assert all(event.data["error_code"] == "PATCH_PARSE_ERROR" for event in finished)
    assert not any(tmp_path.iterdir())
