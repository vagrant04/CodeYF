from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .domain import AgentSession, Event, SessionStatus


class SessionStore:
    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def save(self, session: AgentSession) -> None:
        if not self.enabled:
            return
        directory = self.root / "sessions" / session.id
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = session.snapshot(include_messages=True)
        self._atomic_json(directory / "snapshot.json", snapshot)
        events_path = directory / "events.jsonl"
        existing = 0
        if events_path.exists():
            try:
                with events_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            existing = max(existing, int(json.loads(line)["seq"]))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                existing = 0
        pending = [event for event in session.events if event.seq > existing]
        if pending:
            with events_path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in pending:
                    handle.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def load(self, session_id: str) -> AgentSession:
        path = self.root / "sessions" / session_id / "snapshot.json"
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("schema_version") != 1:
            raise ValueError("不支持的会话 schema_version")
        workspace = Path(data["workspace"]).resolve(strict=True)
        session = AgentSession(
            workspace=workspace,
            model=data["model"],
            approval_mode=data["approval_mode"],
            id=data["session_id"],
            status=SessionStatus(data["status"]),
            messages=data.get("messages", []),
            turn_count=int(data.get("turn_count", 0)),
            tool_call_count=int(data.get("tool_call_count", 0)),
            next_event_seq=int(data.get("next_event_seq", 1)),
            stop_reason=data.get("stop_reason"),
            final_text=data.get("final_text"),
            error=data.get("error"),
            created_at=float(data.get("created_at", 0)),
            updated_at=float(data.get("updated_at", 0)),
        )
        events_path = path.parent / "events.jsonl"
        if events_path.exists():
            with events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    session.events.append(Event(
                        session_id=session.id,
                        seq=int(item["seq"]),
                        type=item["type"],
                        data=item.get("data", {}),
                        correlation_id=item.get("correlation_id"),
                        timestamp=float(item["timestamp"]),
                    ))
        return session

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return []
        result: list[dict[str, Any]] = []
        for path in sessions_dir.glob("*/snapshot.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                messages = data.get("messages", [])
                first_user = next(
                    (item.get("content", "") for item in messages if item.get("role") == "user"),
                    "",
                )
                result.append({
                    "session_id": data["session_id"],
                    "workspace": data["workspace"],
                    "model": data["model"],
                    "status": data["status"],
                    "final_text": data.get("final_text"),
                    "title": first_user.strip()[:80] or "新任务",
                    "turn_count": int(data.get("turn_count", 0)),
                    "tool_call_count": int(data.get("tool_call_count", 0)),
                    "error": data.get("error"),
                    "updated_at": data.get("updated_at", 0),
                })
            except (OSError, KeyError, json.JSONDecodeError):
                continue
        result.sort(key=lambda item: item["updated_at"], reverse=True)
        return result[:limit]

    @staticmethod
    def _atomic_json(path: Path, data: dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
