from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .agent import AgentLoop
from .config import AppConfig
from .domain import AgentSession, SessionStatus
from .model import ModelClient, OpenAICompatibleClient
from .persistence import SessionStore
from .security import ApprovalBroker, PathGuard, PathSecurityError
from .tools import build_default_registry


class AgentService:
    def __init__(
        self,
        config: AppConfig,
        default_workspace: Path,
        model_factory: Callable[[], ModelClient] | None = None,
    ) -> None:
        self.config = config
        self.default_workspace = default_workspace.resolve(strict=True)
        self.store = SessionStore(Path(config.storage.directory), config.storage.enabled)
        self.approvals = ApprovalBroker()
        self.model_factory = model_factory
        self.sessions: dict[str, AgentSession] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.RLock()

    def resolve_workspace(self, workspace: str | None = None) -> Path:
        target = Path(workspace).expanduser().resolve(strict=True) if workspace else self.default_workspace
        if not target.is_dir():
            raise ValueError("workspace 必须是目录")
        return target

    @staticmethod
    def workspace_summary(target: Path, default_workspace: Path) -> dict[str, Any]:
        return {
            "path": str(target),
            "name": target.name or target.anchor,
            "is_default": target == default_workspace,
        }

    def list_workspaces(self) -> list[dict[str, Any]]:
        paths: dict[str, Path] = {str(self.default_workspace).casefold(): self.default_workspace}
        for item in self.store.list():
            try:
                target = Path(item["workspace"]).resolve(strict=True)
            except (OSError, KeyError):
                continue
            if target.is_dir():
                paths.setdefault(str(target).casefold(), target)
        with self.lock:
            for session in self.sessions.values():
                paths.setdefault(str(session.workspace).casefold(), session.workspace)
        return [
            self.workspace_summary(target, self.default_workspace)
            for target in sorted(paths.values(), key=lambda path: (path != self.default_workspace, str(path).casefold()))
        ]

    def create_session(self, workspace: str | None = None) -> AgentSession:
        target = self.resolve_workspace(workspace)
        session = AgentSession(target, self.config.model.name, self.config.security.approval)
        session.emit("session.created", {
            "workspace": str(target),
            "model": self.config.model.name,
            "approval_mode": self.config.security.approval,
            "configured": bool(self.config.api_key),
        })
        with self.lock:
            self.sessions[session.id] = session
        self.store.save(session)
        return session

    def get(self, session_id: str) -> AgentSession | None:
        with self.lock:
            session = self.sessions.get(session_id)
        if session:
            return session
        try:
            session = self.store.load(session_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        with self.lock:
            self.sessions[session_id] = session
        return session

    def list(self) -> list[dict[str, Any]]:
        persisted = {item["session_id"]: item for item in self.store.list()}
        with self.lock:
            for session in self.sessions.values():
                snapshot = session.snapshot(include_messages=False)
                first_user = next(
                    (item.get("content", "") for item in session.messages if item.get("role") == "user"),
                    "",
                )
                persisted[session.id] = {
                    "session_id": session.id,
                    "workspace": str(session.workspace),
                    "model": session.model,
                    "status": session.status.value,
                    "final_text": session.final_text,
                    "title": first_user.strip()[:80] or "新任务",
                    "tool_call_count": session.tool_call_count,
                    "error": session.error,
                    "updated_at": session.updated_at,
                    "turn_count": snapshot["turn_count"],
                }
        return sorted(persisted.values(), key=lambda item: item.get("updated_at", 0), reverse=True)

    def start_task(self, session: AgentSession, message: str) -> None:
        with session.lock:
            if session.status in {SessionStatus.RUNNING, SessionStatus.WAITING_APPROVAL}:
                raise RuntimeError("session is busy")
        registry, _ = build_default_registry(session.workspace, self.config.tools, self.config.security)
        model = self.model_factory() if self.model_factory else OpenAICompatibleClient(self.config.model, self.config.api_key)
        loop = AgentLoop(self.config, model, registry, self.approvals, self.store)

        def worker() -> None:
            loop.run(session, message)

        thread = threading.Thread(target=worker, name=f"codeyf-{session.id[:8]}", daemon=True)
        with self.lock:
            self.threads[session.id] = thread
        thread.start()

    def cancel(self, session: AgentSession) -> None:
        session.cancel_event.set()
        session.emit("task.cancel.requested", {"reason": "user_request"})
        self.store.save(session)


class CodeYFRequestHandler(BaseHTTPRequestHandler):
    server_version = "CodeYF/0.1"

    @property
    def app(self) -> "CodeYFHTTPServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        if self.app.verbose:
            super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/health":
            self._json({
                "ok": True,
                "version": "0.1.0",
                "model": self.app.service.config.model.name,
                "configured": bool(self.app.service.config.api_key),
                "approval": self.app.service.config.security.approval,
                "context_window_tokens": self.app.service.config.model.context_window_tokens,
            })
            return
        if path == "/api/sessions":
            self._json({"sessions": self.app.service.list()})
            return
        if path == "/api/workspaces":
            self._json({
                "default": str(self.app.service.default_workspace),
                "workspaces": self.app.service.list_workspaces(),
            })
            return
        parts = [urllib.parse.unquote(item) for item in path.split("/") if item]
        if len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
            session = self.app.service.get(parts[2])
            if not session:
                self._error(HTTPStatus.NOT_FOUND, "SESSION_NOT_FOUND", "会话不存在")
                return
            if len(parts) == 3:
                snapshot = session.snapshot(include_messages=True)
                snapshot["events"] = [event.to_dict() for event in session.events]
                self._json(snapshot)
                return
            if parts[3:] == ["events"]:
                after = int(query.get("after", ["0"])[0])
                events = [event.to_dict() for event in session.events if event.seq > after]
                self._json({"events": events, "next_seq": session.next_event_seq})
                return
            if parts[3:] == ["events", "stream"]:
                after = int(query.get("after", ["0"])[0])
                self._event_stream(session, after)
                return
            if parts[3:] == ["files"]:
                requested = query.get("path", [""])[0]
                if not requested:
                    self._error(HTTPStatus.BAD_REQUEST, "INVALID_ARGUMENT", "path 必须是非空字符串")
                    return
                try:
                    target = PathGuard(session.workspace).resolve(requested)
                    if not target.is_file():
                        self._error(HTTPStatus.NOT_FOUND, "PATH_NOT_FOUND", "文件不存在")
                        return
                    if target.stat().st_size > 512_000:
                        self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "FILE_TOO_LARGE", "文件超过 500 KB，无法预览")
                        return
                    content = target.read_text(encoding="utf-8")
                except PathSecurityError as exc:
                    self._error(HTTPStatus.FORBIDDEN, "PATH_OUTSIDE_WORKSPACE", str(exc))
                    return
                except UnicodeDecodeError:
                    self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "BINARY_FILE", "不能预览非 UTF-8 文件")
                    return
                except OSError as exc:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "FILE_READ_ERROR", str(exc))
                    return
                self._json({"path": requested, "content": content, "line_count": len(content.splitlines())})
                return
        if path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在")
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json()
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", str(exc))
            return
        if path == "/api/sessions":
            try:
                session = self.app.service.create_session(body.get("workspace"))
            except (OSError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_WORKSPACE", str(exc))
                return
            self._json(session.snapshot(include_messages=False), status=HTTPStatus.CREATED)
            return
        if path == "/api/workspaces/select":
            workspace = body.get("path")
            if not isinstance(workspace, str) or not workspace.strip():
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_WORKSPACE", "path 必须是非空字符串")
                return
            try:
                target = self.app.service.resolve_workspace(workspace.strip())
            except (OSError, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_WORKSPACE", str(exc))
                return
            self._json(self.app.service.workspace_summary(target, self.app.service.default_workspace))
            return
        parts = [urllib.parse.unquote(item) for item in path.split("/") if item]
        if len(parts) < 4 or parts[:2] != ["api", "sessions"]:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在")
            return
        session = self.app.service.get(parts[2])
        if not session:
            self._error(HTTPStatus.NOT_FOUND, "SESSION_NOT_FOUND", "会话不存在")
            return
        action = parts[3]
        if action == "tasks":
            message = body.get("message")
            if not isinstance(message, str) or not message.strip():
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_ARGUMENT", "message 必须是非空字符串")
                return
            try:
                self.app.service.start_task(session, message.strip())
            except RuntimeError:
                self._error(HTTPStatus.CONFLICT, "SESSION_BUSY", "会话已有任务在运行")
                return
            self._json({"accepted": True, "session_id": session.id}, status=HTTPStatus.ACCEPTED)
            return
        if action == "cancel":
            self.app.service.cancel(session)
            self._json({"accepted": True})
            return
        if action == "approvals" and len(parts) == 5:
            decision = body.get("decision")
            if not isinstance(decision, str) or not self.app.service.approvals.resolve(parts[4], decision):
                self._error(HTTPStatus.CONFLICT, "APPROVAL_NOT_PENDING", "审批不存在、已结束或决定无效")
                return
            self._json({"accepted": True})
            return
        self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "接口不存在")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求体超过 1 MB")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON 请求体必须是对象")
        return value

    def _json(self, value: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json({"error": {"code": code, "message": message}}, status)

    def _event_stream(self, session: AgentSession, after: int) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        deadline = time.monotonic() + 30
        cursor = after
        try:
            while time.monotonic() < deadline:
                events = [event for event in session.events if event.seq > cursor]
                for event in events:
                    payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n".encode("utf-8"))
                    cursor = event.seq
                if events:
                    self.wfile.flush()
                if session.status in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED} and cursor >= session.next_event_seq - 1:
                    break
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_static(self, url_path: str) -> None:
        relative = urllib.parse.unquote(url_path).lstrip("/") or "index.html"
        candidate = (self.app.frontend_dir / relative).resolve()
        try:
            candidate.relative_to(self.app.frontend_dir)
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "PATH_OUTSIDE_FRONTEND", "非法静态资源路径")
            return
        if not candidate.is_file():
            candidate = self.app.frontend_dir / "index.html"
        try:
            body = candidate.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "资源不存在")
            return
        content_type, _ = mimetypes.guess_type(candidate.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", (content_type or "application/octet-stream") + ("; charset=utf-8" if candidate.suffix in {".html", ".css", ".js"} else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)


class CodeYFHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: AgentService, frontend_dir: Path, verbose: bool = False) -> None:
        super().__init__(address, CodeYFRequestHandler)
        self.service = service
        self.frontend_dir = frontend_dir.resolve(strict=True)
        self.verbose = verbose


def run_server(
    config: AppConfig,
    workspace: Path,
    frontend_dir: Path,
    host: str = "127.0.0.1",
    port: int = 5173,
    open_browser: bool = True,
    verbose: bool = False,
) -> None:
    service = AgentService(config, workspace)
    server = CodeYFHTTPServer((host, port), service, frontend_dir, verbose)
    url = f"http://{host}:{server.server_port}/"
    print(f"CodeYF Web 已启动：{url}")
    if not config.api_key:
        print(f"提示：尚未设置 {config.model.api_key_env}，界面可打开，但真实任务会返回配置错误。")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在关闭 CodeYF Web…")
    finally:
        server.server_close()
