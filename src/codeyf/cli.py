from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import AgentLoop
from .config import ConfigError, load_config
from .domain import AgentSession
from .model import OpenAICompatibleClient
from .persistence import SessionStore
from .security import ConsoleApproval
from .tools import build_default_registry
from .web import run_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codeyf", description="本地优先、无 Agent 框架的编程智能体")
    subcommands = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", type=Path, default=Path.cwd(), help="工作区目录")
    common.add_argument("--config", type=Path, help="配置文件")
    common.add_argument("--model", help="覆盖模型名称")
    common.add_argument("--base-url", help="覆盖 OpenAI 兼容 API 地址")
    common.add_argument("--approval", choices=["strict", "balanced", "auto"], help="审批策略")
    common.add_argument("--max-turns", type=int, help="最大模型轮次")
    common.add_argument("--timeout", type=float, help="任务总时限（秒）")

    run = subcommands.add_parser("run", parents=[common], help="执行一次任务")
    run.add_argument("task", nargs="?", help="任务内容；省略时从 stdin 读取")
    run.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    subcommands.add_parser("chat", parents=[common], help="进入交互会话")

    web = subcommands.add_parser("web", parents=[common], help="启动本地 Web 工作台")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=5173)
    web.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    web.add_argument("--frontend", type=Path, help="前端静态资源目录")
    web.add_argument("--verbose", action="store_true")
    return parser


def _load(args: argparse.Namespace):
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ConfigError("workspace 必须是目录")
    overrides: dict[str, Any] = {
        "model": args.model,
        "base_url": args.base_url,
        "approval": args.approval,
        "max_turns": args.max_turns,
        "timeout": args.timeout,
    }
    return workspace, load_config(workspace, args.config, overrides)


def _build_loop(workspace: Path, config):
    registry, _ = build_default_registry(workspace, config.tools, config.security)
    model = OpenAICompatibleClient(config.model, config.api_key)
    store = SessionStore(Path(config.storage.directory), config.storage.enabled)
    return AgentLoop(config, model, registry, ConsoleApproval(), store), store


def _run_once(args: argparse.Namespace, workspace: Path, config) -> int:
    task = args.task if args.task is not None else sys.stdin.read()
    if not task.strip():
        print("任务不能为空", file=sys.stderr)
        return 2
    loop, _ = _build_loop(workspace, config)
    session = AgentSession(workspace, config.model.name, config.security.approval)
    session.emit("session.created", {"workspace": str(workspace), "model": config.model.name, "approval_mode": config.security.approval})
    result = loop.run(session, task.strip())
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.final_text:
        print(result.final_text)
    elif result.error:
        print(f"任务失败 [{result.error.get('code')}]: {result.error.get('message')}", file=sys.stderr)
    return {"completed": 0, "cancelled": 5}.get(result.status, 3 if result.stop_reason == "model_error" else 1)


def _chat(args: argparse.Namespace, workspace: Path, config) -> int:
    loop, _ = _build_loop(workspace, config)
    session = AgentSession(workspace, config.model.name, config.security.approval)
    session.emit("session.created", {"workspace": str(workspace), "model": config.model.name, "approval_mode": config.security.approval})
    print(f"CodeYF · {config.model.name} · {workspace}")
    print("输入 /help 查看命令，/exit 退出。")
    while True:
        try:
            text = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/exit":
            return 0
        if text == "/help":
            print("/status 查看状态 · /clear 新会话 · /exit 退出")
            continue
        if text == "/status":
            print(json.dumps(session.snapshot(include_messages=False), ensure_ascii=False, indent=2))
            continue
        if text == "/clear":
            session = AgentSession(workspace, config.model.name, config.security.approval)
            print("已开始新会话。")
            continue
        result = loop.run(session, text)
        print(f"\nCodeYF > {result.final_text or result.error}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        workspace, config = _load(args)
    except (ConfigError, OSError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.command == "run":
        return _run_once(args, workspace, config)
    if args.command == "chat":
        return _chat(args, workspace, config)
    frontend = args.frontend
    if frontend is None:
        frontend = Path(__file__).resolve().parents[2] / "frontend"
    try:
        frontend = frontend.resolve(strict=True)
    except OSError:
        print("找不到前端目录；请用 --frontend 指定 frontend 路径", file=sys.stderr)
        return 2
    try:
        run_server(config, workspace, frontend, args.host, args.port, not args.no_open, args.verbose)
    except OSError as exc:
        print(f"无法启动 Web 服务或会话存储：{exc}", file=sys.stderr)
        return 1
    return 0
