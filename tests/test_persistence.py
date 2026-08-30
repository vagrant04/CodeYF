from __future__ import annotations

from pathlib import Path

from codeyf.domain import AgentSession, Project
from codeyf.persistence import SessionStore


def test_session_store_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session = AgentSession(workspace, "test-model", "balanced")
    session.messages.append({"role": "user", "content": "hello"})
    session.title = "hello"
    session.transcript = [{"role": "user", "content": "hello"}]
    session.emit("session.created", {"model": "test-model"})
    store = SessionStore(tmp_path / "data")
    store.save(session)

    restored = store.load(session.id)
    assert restored.id == session.id
    assert restored.messages == session.messages
    assert restored.title == "hello"
    assert restored.transcript == session.transcript
    assert restored.events[0].type == "session.created"


def test_legacy_compacted_session_recovers_title_and_visible_transcript(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = SessionStore(tmp_path / "data")
    session = AgentSession(workspace, "test-model", "balanced")
    session.messages = [
        {"role": "system", "content": "system"},
        {"role": "system", "content": "历史压缩摘要：\nuser: 创建番茄钟页面"},
        {"role": "assistant", "content": "任务完成。"},
    ]
    store.save(session)
    snapshot_path = store.root / "sessions" / session.id / "snapshot.json"
    data = __import__("json").loads(snapshot_path.read_text(encoding="utf-8"))
    data.pop("title", None)
    data.pop("transcript", None)
    snapshot_path.write_text(__import__("json").dumps(data, ensure_ascii=False), encoding="utf-8")

    restored = store.load(session.id)

    assert restored.title == "创建番茄钟页面"
    assert restored.transcript[0]["role"] == "user"
    assert restored.transcript[0]["recovered"] is True
    assert restored.transcript[-1] == {"role": "assistant", "content": "任务完成。"}
    migrated = __import__("json").loads(snapshot_path.read_text(encoding="utf-8"))
    assert migrated["title"] == "创建番茄钟页面"
    assert migrated["transcript"][0]["recovered"] is True


def test_project_store_round_trip_includes_workspace_and_shared_memory(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = SessionStore(tmp_path / "data")
    project = Project("Demo", workspace, "Always run pytest.")

    store.save_project(project)
    restored = store.load_project(project.id)

    assert restored.name == "Demo"
    assert restored.workspace == workspace.resolve()
    assert restored.memory == "Always run pytest."
    assert [item.id for item in store.list_projects()] == [project.id]
