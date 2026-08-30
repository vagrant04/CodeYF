# CodeYF

CodeYF 是一个从零实现的本地编程智能体（coding agent）。它直接调用 OpenAI 兼容模型接口，让模型通过本项目定义的工具读取/修改工作区并执行命令。项目没有使用 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架，也不依赖服务端托管的代码执行或文件工具。

## 已实现

- OpenAI 兼容 `/chat/completions` + 原生 tool calling；
- 显式 agent loop、历史维护、停止条件、重复失败检测和上下文裁剪；
- `read_file`、`list_files`、`search_text`、`apply_patch`、`run_command`；
- 工作区路径沙箱、命令风险规则、strict/balanced/auto 审批；
- JSONL 事件日志和可恢复会话快照；
- 单次 CLI、交互式 CLI；
- 本地 Web 工作台、REST API 和 SSE 事件流；
- Codex 风格的任务级工作区：新任务可选择本机目录，每个任务的文件与命令操作固定隔离在自己的工作区；
- 无 API key 或后端离线时明确禁用任务提交，绝不伪造执行结果；
- 不依赖公网的脚本化假模型测试。

## 快速开始

要求 Python 3.11 或更高版本。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install -e .
```

配置模型：

```bash
# PowerShell
$env:CODEYF_API_KEY="your-key"
$env:CODEYF_MODEL="deepseek-v4-flash"
$env:CODEYF_BASE_URL="https://api.deepseek.com"

# bash/zsh
export CODEYF_API_KEY="your-key"
export CODEYF_MODEL="deepseek-v4-flash"
export CODEYF_BASE_URL="https://api.deepseek.com"
```

运行一次任务：

```bash
codeyf run "阅读项目并补充 README 中缺失的运行说明" --workspace .
```

进入交互模式：

```bash
codeyf chat --workspace .
```

启动 Web 工作台：

```bash
codeyf web --workspace .
```

默认访问 `http://127.0.0.1:5173`。不希望自动打开浏览器时添加 `--no-open`。

Web 工作台左下角显示当前新任务将使用的工作区。点击该区域可以从最近目录中选择，或输入一个本机已存在目录的绝对路径。工作区在任务创建时固化；打开历史任务会恢复并显示它自己的工作区，不会把修改落到 CodeYF 源码目录（除非明确选择了该目录）。

## 配置

配置优先级为 CLI > `CODEYF_*` 环境变量 > 指定 TOML > 工作区 `.codeyf.toml` > 用户配置。完整字段见 [接口文档](docs/03-interfaces.md)。最小配置示例：

```toml
[model]
name = "deepseek-v4-flash"
base_url = "https://api.deepseek.com"
api_key_env = "CODEYF_API_KEY"

[security]
approval = "balanced"
allow_shell = false
```

## 安全边界

模型输出、仓库内容和命令输出均视为不可信数据。所有文件路径必须通过真实路径校验；工作区外路径会被拒绝。高风险命令由宿主策略独立识别并审批，提示词不能绕过该策略。

这仍是应用层防护，不是操作系统级沙箱。运行未知或不可信仓库时，建议在容器、虚拟机或低权限账户中使用。默认 Web 服务只绑定 `127.0.0.1`，不要直接暴露到公网。

## 测试

```bash
python -m pip install -e ".[dev]"
pytest
```

测试使用 `ScriptedModelClient`，核心端到端循环无需真实 API key 或公网。

## 项目结构

```text
src/codeyf/      Agent 内核、模型、工具、安全、CLI 与 Web API
frontend/        无构建依赖的 Web 工作台
tests/           单元和端到端测试
docs/            需求、设计、接口与前端设计文档
```

## 参考与独立实现说明

设计过程中参考了 Learn Claude Code 对单循环 harness 的教学拆解、OpenCode 的权限分层以及 DeepSeek Harness 的可扩展架构理念。CodeYF 的代码、协议映射、工具、持久化、安全策略和 Web 层均在本仓库独立实现，没有封装或导入这些产品。
