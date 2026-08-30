# CodeYF 前端设计说明书

文档版本：1.0  
实现原型：[frontend/index.html](../frontend/index.html)

## 1. 设计定位

前端定位为本地 Coding Agent 的任务工作台，对标 Codex、Claude Code Desktop 等产品的信息密度和执行透明度，但不复制第三方品牌、专有图标或视觉资产。

设计重点：

1. 用户始终知道当前任务、执行状态和工作区；
2. 文件读取、补丁、命令和审批形成可审计的时间线；
3. 对话、diff 与终端可同时查看，减少上下文切换；
4. 普通进度保持安静，高风险操作必须打断并清楚说明影响；
5. 前端只展示和提交意图，不直接执行本地文件或命令工具。

## 2. 信息架构

```text
应用外壳
├── 任务侧栏
│   ├── 新建与搜索
│   ├── 按时间分组的任务列表
│   └── 工作区与审批模式
├── 主工作区
│   ├── 任务标题与状态
│   ├── 用户/智能体时间线
│   ├── 工具执行与 diff 卡片
│   └── 上下文感知输入框
└── 详情面板
    ├── 工作区变更
    ├── 终端输出
    └── 上下文预算
```

桌面端使用三栏布局；980 px 以下详情面板变为右侧抽屉；720 px 以下任务栏变为左侧抽屉，主工作区保持完整宽度。

## 3. 视觉系统

- 色彩：暖灰背景、黑白主层级、陶土橙作为运行/品牌强调色；成功为绿色、审批为琥珀色、错误为红色。
- 字体：系统无衬线字体承载界面，系统等宽字体承载路径、命令和代码。
- 密度：任务列表和工具摘要保持紧凑，正文对话留出更大行距。
- 圆角：主输入框 14 px、卡片 10–12 px、按钮 7–9 px。
- 动效：只用于运行状态、弹层和面板切换；遵循 `prefers-reduced-motion`。
- 主题：设计 token 同时支持浅色与深色，不在组件中散落硬编码主题颜色。

## 4. 核心组件

| 组件 | 职责 | 关键状态 |
|---|---|---|
| `TaskSidebar` | 创建、搜索、切换和查看任务摘要 | idle、active、running、failed、completed |
| `WorkspacePicker` | 为新任务选择本机目录，并展示最近工作区 | closed、validating、selected、invalid |
| `TaskHeader` | 标题、路径、当前状态、变更入口 | running、waiting_approval、completed |
| `ConversationTimeline` | 用户消息和智能体执行过程 | streaming、settled、cancelled |
| `ToolActivityCard` | 展示工具名、参数摘要、耗时和结果 | pending、running、success、error、denied |
| `DiffPreview` | 就地审阅局部修改 | created、updated、deleted |
| `Composer` | 发送任务、附加上下文、显示运行模式 | empty、typing、disabled、submitting |
| `DetailsPanel` | 文件 diff、终端、上下文详情 | changes、terminal、context |
| `InlineApprovalCard` | 在对话时间线内展示操作、原因、范围、风险和决定 | requested、approved、denied、cancelled |
| `CommandPalette` | 键盘优先的任务与操作导航 | closed、open、filtering |

## 5. 状态与事件映射

| 后端状态/事件 | 前端行为 |
|---|---|
| `state.changed → running` | 顶部显示运行胶囊，当前任务显示脉冲状态点 |
| `model.responded` 文本增量 | 在当前智能体消息中追加文本，不展示隐藏推理 |
| `tool.requested` | 创建 pending 工具卡片 |
| `tool.started` | 卡片显示运行动画和开始时间 |
| `tool.finished` | 显示耗时、结果摘要；失败时提供可展开错误 |
| `approval.requested` | 状态切为 waiting，在时间线插入权限卡片，不自动批准 |
| `approval.decided` | 原位更新权限卡片状态并禁用操作按钮 |
| `context.compacted` | 上下文面板显示压缩标记和释放量 |
| `task.completed` | 状态变绿，恢复输入，显示修改与验证摘要 |
| `task.failed/cancelled` | 明确显示原因，保留历史并允许后续消息 |

## 6. 审批交互

内联权限卡片必须在确认按钮附近同时展示：

- 工具和规范化命令/路径；
- 请求原因与可能影响；
- 风险等级和命中的规则；
- “拒绝”“仅允许这一次”两个明确选择。

权限卡片不得使用打断式弹窗，也不能因 Esc 或点击空白处消失。审批期间任务输入仍可编辑，但不能提交与当前任务冲突的新动作。

## 7. 后端连接

推荐通道：

- HTTP：创建会话、提交任务、取消、审批决定、读取快照；
- SSE 或 WebSocket：接收按 `seq` 排序的事件和 stdout/stderr 增量；
- REST diff 接口：按文件读取变更和指定版本内容。

前端状态仓库以 `(session_id, seq)` 去重事件。断线重连时携带最后确认的 `seq`，先补齐事件再恢复实时流。收到序号缺口时必须请求恢复，不可猜测工具已经成功。

建议最小 HTTP 接口：

```text
GET  /api/projects
POST /api/projects
GET  /api/projects/{project_id}
POST /api/projects/{project_id}
POST /api/sessions
GET  /api/sessions
GET  /api/sessions/{session_id}
POST /api/sessions/{session_id}/tasks
POST /api/sessions/{session_id}/cancel
POST /api/sessions/{session_id}/settings
POST /api/sessions/{session_id}/approvals/{approval_id}
GET  /api/sessions/{session_id}/events?after={seq}
GET  /api/sessions/{session_id}/changes
GET  /api/sessions/{session_id}/files/{path}
```

工作区采用“项目绑定”语义：项目固化本机规范绝对目录与共享顶层记忆，一个项目可包含多个会话。`POST /api/sessions` 携带 `project_id`，后端从项目继承工作区。权限菜单以内联浮层展示严格、平衡、完全访问三种模式；空闲会话可切换，运行中禁止切换。

## 8. 可访问性与键盘

- 所有图标按钮提供可读 `aria-label`；页签使用 `role=tab` 和 `aria-selected`。
- 模态框使用 `role=dialog`、标题关联和焦点约束；正式实现需补齐焦点循环与关闭后焦点恢复。
- 状态不能只靠颜色表达，同时提供文本或图标。
- 动态消息区域使用克制的 `aria-live=polite`，流式 token 不逐 token 播报。
- 快捷键：`Ctrl/Cmd + K` 搜索，`Ctrl/Cmd + N` 新任务，`Ctrl/Cmd + J` 终端，Esc 关闭临时界面。
- 代码和终端允许横向滚动，不缩小到不可读字号。

## 9. 原型与正式实现边界

当前实现已接入真实项目、多会话、共享顶层记忆、SSE 事件恢复、文件预览和三种权限模式。后续增强项包括：

- 将 DOM 拼接迁移为组件化渲染并加入运行时 schema 校验；
- 实现大型任务列表虚拟化和终端流式缓冲；
- 增加焦点陷阱、断线恢复、错误重试和空状态；
- 对 diff 使用专业解析/渲染，而非直接插入模型文本；
- 建立桌面、平板、移动端视觉回归测试。
