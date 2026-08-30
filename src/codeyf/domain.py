from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class SessionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    response_id: str | None = None
    usage: dict[str, int] | None = None


@dataclass(slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "ok": self.ok,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
            "meta": self.meta,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class Event:
    session_id: str
    seq: int
    type: str
    data: dict[str, Any]
    correlation_id: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "type": self.type,
            "correlation_id": self.correlation_id,
            "data": self.data,
        }


@dataclass(slots=True)
class Project:
    name: str
    workspace: Path
    memory: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": self.id,
            "name": self.name,
            "workspace": str(self.workspace),
            "memory": self.memory,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class AgentSession:
    workspace: Path
    model: str
    approval_mode: str
    project_id: str | None = None
    title: str = "新任务"
    transcript: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: SessionStatus = SessionStatus.IDLE
    messages: list[dict[str, Any]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    turn_count: int = 0
    tool_call_count: int = 0
    next_event_seq: int = 1
    stop_reason: str | None = None
    final_text: str | None = None
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def emit(self, event_type: str, data: dict[str, Any] | None = None, correlation_id: str | None = None) -> Event:
        with self.lock:
            event = Event(self.id, self.next_event_seq, event_type, data or {}, correlation_id)
            self.next_event_seq += 1
            self.events.append(event)
            self.updated_at = time.time()
            return event

    def transition(self, status: SessionStatus, reason: str = "") -> None:
        previous = self.status
        self.status = status
        self.emit("state.changed", {"from": previous.value, "to": status.value, "reason": reason})

    def snapshot(self, include_messages: bool = True) -> dict[str, Any]:
        with self.lock:
            return {
                "schema_version": 1,
                "session_id": self.id,
                "project_id": self.project_id,
                "title": self.title,
                "transcript": list(self.transcript),
                "workspace": str(self.workspace),
                "model": self.model,
                "approval_mode": self.approval_mode,
                "status": self.status.value,
                "messages": list(self.messages) if include_messages else [],
                "turn_count": self.turn_count,
                "tool_call_count": self.tool_call_count,
                "next_event_seq": self.next_event_seq,
                "stop_reason": self.stop_reason,
                "final_text": self.final_text,
                "error": self.error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }


MessageRole = Literal["system", "user", "assistant", "tool"]


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"
