# CodeYF 前端原型

这是一个无需前端构建工具即可运行的 Coding Agent 工作台，信息架构参考现代编程智能体桌面端，但没有复制第三方品牌、图标或专有资源。

## 运行

前端只显示真实 Agent 状态，不包含静态演示或模拟完成逻辑。先配置模型并启动后端：

```bash
codeyf web --workspace .
```

页面会自动探测 `/api/health`。探测成功后，发送消息会创建真实会话、提交任务并消费 SSE 事件。后端离线、API Key 未配置或认证失败时，页面会明确提示并停止任务，不会生成假的工具卡、diff 或测试结果。

## 已实现交互

- 从后端读取真实任务列表、历史消息与会话状态；
- 新任务创建前可选择本机工作区；每个任务固定绑定自己的工作区，历史任务互不串目录；
- 对话输入、真实模型运行和工具执行卡片；
- 文件变更、终端、上下文三个详情面板；
- `apply_patch` 后读取磁盘当前内容并提供文件切换；
- 仅由 `approval.requested` 事件触发的对话内联权限卡片；
- 智能体最终回答使用安全 Markdown 渲染，不展示原始 Markdown 标记，也不执行模型返回的 HTML；
- `Ctrl/Cmd + K` 命令面板；
- `Ctrl/Cmd + N` 新建任务；
- 浅色/深色主题及移动端适配；
- 基础键盘焦点、ARIA 和 reduced-motion 支持。

## 后端接入边界

`app.js` 只消费真实后端接口与事件：

| 前端状态 | 对应后端事件/接口 |
|---|---|
| 会话列表 | `GET /api/sessions` |
| 最近工作区 | `GET /api/workspaces` |
| 工作区校验 | `POST /api/workspaces/select` |
| 会话详情 | `GET /api/sessions/:id` |
| 任务创建 | `POST /api/sessions`（携带 `workspace`）后调用 `POST /api/sessions/:id/tasks` |
| 助手文本流 | `model.responded` 或单独的 text delta 事件 |
| 工具执行卡片 | `tool.requested`、`tool.started`、`tool.finished` |
| 内联权限卡片 | `approval.requested`，提交 `ApprovalDecision` |
| 顶部状态 | `state.changed` |
| 变更列表 | `apply_patch` 的真实 `tool.finished` 结果 |
| 修改后文件预览 | `GET /api/sessions/:id/files?path=...` |
| 终端输出 | 命令 stdout/stderr 的流式事件 |
| 上下文用量 | `model.responded.usage` 与上下文预算接口 |

建议使用 WebSocket 或 Server-Sent Events 传递只读事件流，普通 HTTP 提交任务、取消和审批决定。前端不得自行执行文件或命令工具，所有副作用继续由后端安全层处理。

工作区选择只接受本机已存在的目录。选中的路径在创建会话时写入会话快照；之后该任务的文件读取、补丁、文件预览和命令执行都由后端以这个目录构造独立的工具注册表，不能被前端临时改到其他目录。
