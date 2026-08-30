from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from codeyf.config import AppConfig
from codeyf.domain import ModelResponse, ToolCall
from codeyf.model import ScriptedModelClient
from codeyf.web import AgentService, CodeYFHTTPServer, CodeYFRequestHandler


def request_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read())


def test_request_handler_silences_client_disconnect_before_request_line(monkeypatch) -> None:
    handler = object.__new__(CodeYFRequestHandler)

    def abort(_handler) -> None:
        raise ConnectionAbortedError(10053, "client aborted")

    monkeypatch.setattr(BaseHTTPRequestHandler, "handle", abort)
    handler.handle()


def test_web_health_session_and_unconfigured_task_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEYF_API_KEY", raising=False)
    workspace = tmp_path / "repo"
    frontend = tmp_path / "frontend"
    workspace.mkdir()
    alternate = tmp_path / "alternate-repo"
    alternate.mkdir()
    (workspace / "hello.py").write_text("print('real file')\n", encoding="utf-8")
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>CodeYF</h1>", encoding="utf-8")
    config = AppConfig()
    config.storage.directory = str(tmp_path / "data")
    service = AgentService(config, workspace)
    server = CodeYFHTTPServer(("127.0.0.1", 0), service, frontend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        health = request_json(base + "/api/health")
        workspaces = request_json(base + "/api/workspaces")
        assert workspaces["default"] == str(workspace)
        assert workspaces["workspaces"][0]["path"] == str(workspace)
        selected = request_json(base + "/api/workspaces/select", "POST", {"path": str(alternate)})
        assert selected["path"] == str(alternate)
        alternate_session = request_json(base + "/api/sessions", "POST", {"workspace": str(alternate)})
        assert alternate_session["workspace"] == str(alternate)
        session = request_json(base + "/api/sessions", "POST", {})
        snapshot = request_json(base + f"/api/sessions/{session['session_id']}")
        assert health["ok"] is True
        assert snapshot["status"] == "idle"
        assert snapshot["events"][0]["type"] == "session.created"
        preview = request_json(base + f"/api/sessions/{session['session_id']}/files?path=hello.py")
        assert preview == {"path": "hello.py", "content": "print('real file')\n", "line_count": 1}
        accepted = request_json(base + f"/api/sessions/{session['session_id']}/tasks", "POST", {"message": "inspect"})
        assert accepted["accepted"] is True
        for _ in range(30):
            snapshot = request_json(base + f"/api/sessions/{session['session_id']}")
            if snapshot["status"] == "failed":
                break
            time.sleep(0.02)
        assert snapshot["error"]["code"] == "MODEL_AUTHENTICATION"
        listed = request_json(base + "/api/sessions")["sessions"][0]
        assert listed["title"] == "inspect"
        try:
            request_json(base + f"/api/sessions/{session['session_id']}/files?path=../secret.txt")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("path traversal should be rejected")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_task_applies_patch_to_disk_and_exposes_real_preview(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    task_workspace = tmp_path / "task-repo"
    frontend = tmp_path / "frontend"
    workspace.mkdir()
    task_workspace.mkdir()
    frontend.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
    (task_workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
    (frontend / "index.html").write_text("<h1>CodeYF</h1>", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: app.py
@@
-value = 1
+value = 2
*** End Patch"""
    model = ScriptedModelClient([
        ModelResponse(tool_calls=(ToolCall("call_patch", "apply_patch", {"patch": patch}),)),
        ModelResponse(content="真实修改完成。", finish_reason="stop"),
    ])
    config = AppConfig()
    config.storage.enabled = False
    service = AgentService(config, workspace, model_factory=lambda: model)
    server = CodeYFHTTPServer(("127.0.0.1", 0), service, frontend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        session = request_json(base + "/api/sessions", "POST", {"workspace": str(task_workspace)})
        assert session["workspace"] == str(task_workspace)
        request_json(base + f"/api/sessions/{session['session_id']}/tasks", "POST", {"message": "修改 app.py"})
        for _ in range(100):
            snapshot = request_json(base + f"/api/sessions/{session['session_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.02)
        assert snapshot["status"] == "completed"
        assert task_workspace.joinpath("app.py").read_text(encoding="utf-8") == "value = 2\n"
        assert workspace.joinpath("app.py").read_text(encoding="utf-8") == "value = 1\n"
        preview = request_json(base + f"/api/sessions/{session['session_id']}/files?path=app.py")
        assert preview["content"] == "value = 2\n"
        finished = next(event for event in snapshot["events"] if event["type"] == "tool.finished")
        assert finished["data"]["result"]["data"]["changes"][0]["path"] == "app.py"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_approval_wakes_worker_without_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    frontend = tmp_path / "frontend"
    workspace.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>CodeYF</h1>", encoding="utf-8")
    model = ScriptedModelClient([
        ModelResponse(
            tool_calls=(
                ToolCall("call_approved", "run_command", {
                    "argv": [sys.executable, "-c", "print('approved locally')"],
                }),
            ),
            finish_reason="tool_calls",
        ),
        ModelResponse(content="本地审批测试完成。", finish_reason="stop"),
    ])
    config = AppConfig()
    config.storage.enabled = False
    config.security.approval = "strict"
    service = AgentService(config, workspace, model_factory=lambda: model)
    server = CodeYFHTTPServer(("127.0.0.1", 0), service, frontend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    started = time.monotonic()
    try:
        session = request_json(base + "/api/sessions", "POST", {})
        request_json(base + f"/api/sessions/{session['session_id']}/tasks", "POST", {"message": "运行离线命令"})
        for _ in range(200):
            snapshot = request_json(base + f"/api/sessions/{session['session_id']}")
            requested = next(
                (event for event in snapshot["events"] if event["type"] == "approval.requested"),
                None,
            )
            if requested:
                break
            time.sleep(0.01)
        assert requested is not None
        approval_id = requested["data"]["approval_id"]
        accepted = request_json(
            base + f"/api/sessions/{session['session_id']}/approvals/{approval_id}",
            "POST",
            {"decision": "approve_once"},
        )
        assert accepted["accepted"] is True
        for _ in range(200):
            snapshot = request_json(base + f"/api/sessions/{session['session_id']}")
            if snapshot["status"] == "completed":
                break
            time.sleep(0.01)
        assert snapshot["status"] == "completed"
        assert time.monotonic() - started < 2
        decided = next(event for event in snapshot["events"] if event["type"] == "approval.decided")
        assert decided["data"]["decision"] == "approve_once"
        event_types = [event["type"] for event in snapshot["events"]]
        approval_index = event_types.index("approval.decided")
        running_index = next(
            index
            for index, event in enumerate(snapshot["events"])
            if event["type"] == "state.changed"
            and event["data"]["to"] == "running"
            and index > approval_index
        )
        started_index = event_types.index("tool.started")
        finished_index = event_types.index("tool.finished")
        assert approval_index < running_index < started_index < finished_index
        finished = next(event for event in snapshot["events"] if event["type"] == "tool.finished")
        assert finished["data"]["result"]["ok"] is True
        assert finished["data"]["result"]["data"]["stdout"].strip() == "approved locally"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_server_rejects_second_instance_on_same_port(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    frontend = tmp_path / "frontend"
    workspace.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>CodeYF</h1>", encoding="utf-8")
    config = AppConfig()
    config.storage.enabled = False
    first = CodeYFHTTPServer(("127.0.0.1", 0), AgentService(config, workspace), frontend)
    try:
        with pytest.raises(OSError):
            CodeYFHTTPServer(
                ("127.0.0.1", first.server_port),
                AgentService(config, workspace),
                frontend,
            )
    finally:
        first.server_close()


def test_projects_own_workspaces_memories_and_multiple_sessions(tmp_path: Path) -> None:
    default_workspace = tmp_path / "default"
    workspace_a = tmp_path / "project-a"
    workspace_b = tmp_path / "project-b"
    frontend = tmp_path / "frontend"
    for directory in (default_workspace, workspace_a, workspace_b, frontend):
        directory.mkdir()
    (frontend / "index.html").write_text("<h1>CodeYF</h1>", encoding="utf-8")
    config = AppConfig()
    config.storage.enabled = False
    service = AgentService(config, default_workspace)
    server = CodeYFHTTPServer(("127.0.0.1", 0), service, frontend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        project_a = request_json(base + "/api/projects", "POST", {
            "name": "Project A",
            "workspace": str(workspace_a),
            "memory": "Use pytest.",
        })
        project_b = request_json(base + "/api/projects", "POST", {
            "name": "Project B",
            "workspace": str(workspace_b),
            "memory": "Use npm test.",
        })
        session_a1 = request_json(base + "/api/sessions", "POST", {
            "project_id": project_a["project_id"],
            "approval_mode": "balanced",
        })
        session_a2 = request_json(base + "/api/sessions", "POST", {
            "project_id": project_a["project_id"],
            "approval_mode": "auto",
        })
        session_b = request_json(base + "/api/sessions", "POST", {
            "project_id": project_b["project_id"],
        })
        updated = request_json(
            base + f"/api/projects/{project_a['project_id']}",
            "POST",
            {"name": "Project A+", "memory": "Use Python 3.13."},
        )
        projects = request_json(base + "/api/projects")["projects"]

        assert session_a1["project_id"] == project_a["project_id"]
        assert session_a2["project_id"] == project_a["project_id"]
        assert session_a1["workspace"] == str(workspace_a)
        assert session_a2["approval_mode"] == "auto"

        updated_mode = request_json(
            base + f"/api/sessions/{session_a1['session_id']}/settings",
            "POST",
            {"approval_mode": "auto"},
        )
        assert updated_mode["approval_mode"] == "auto"
        assert session_b["project_id"] == project_b["project_id"]
        assert session_b["workspace"] == str(workspace_b)
        assert updated["memory"] == "Use Python 3.13."
        summary_a = next(item for item in projects if item["project_id"] == project_a["project_id"])
        assert summary_a["name"] == "Project A+"
        assert summary_a["session_count"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
