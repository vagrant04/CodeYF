from __future__ import annotations

from pathlib import Path

from codeyf.domain import AgentSession
from codeyf.persistence import SessionStore


def test_session_store_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session = AgentSession(workspace, "test-model", "balanced")
    session.messages.append({"role": "user", "content": "hello"})
    session.emit("session.created", {"model": "test-model"})
    store = SessionStore(tmp_path / "data")
    store.save(session)

    restored = store.load(session.id)
    assert restored.id == session.id
    assert restored.messages == session.messages
    assert restored.events[0].type == "session.created"

