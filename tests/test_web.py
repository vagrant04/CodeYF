from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

from codeyf.config import AppConfig
from codeyf.domain import ModelResponse, ToolCall
from codeyf.model import ScriptedModelClient
from codeyf.web import AgentService, CodeYFHTTPServer


def request_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read())


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
