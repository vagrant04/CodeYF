const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const appShell = $(".app-shell");
const conversation = $("#conversation");
const promptInput = $("#promptInput");
const composer = $("#composer");
const runState = $(".run-state");
const runStateText = $("#runStateText");
const workspaceModal = $("#workspaceModal");
const commandPalette = $("#commandPalette");
const paletteInput = $("#paletteInput");
const modeMenu = $("#modeMenu");

const backendState = {
  available: false,
  configured: false,
  health: null,
  sessionId: null,
  snapshot: null,
  source: null,
  lastSeq: 0,
  activeTask: false,
  toolCards: new Map(),
  approvalCards: new Map(),
  changes: new Map(),
  commands: [],
  usage: null,
  sessions: [],
  workspaces: [],
  selectedWorkspace: null,
  projects: [],
  selectedProject: null,
  editingProjectId: null,
  selectedApprovalMode: null,
  restoringSnapshot: false,
  renderedAssistantEvents: new Set(),
  lastAssistantText: "",
  watchdogTimer: null
};

function icon(id) {
  return `<svg aria-hidden="true"><use href="#${id}"></use></svg>`;
}

const approvalModeLabels = { strict: "严格", balanced: "平衡", auto: "完全访问" };

function renderApprovalMode() {
  const mode = backendState.selectedApprovalMode || "balanced";
  const button = $("#modeButton");
  $("span", button).textContent = `Agent · ${approvalModeLabels[mode] || "平衡"}`;
  button.dataset.mode = mode;
  $("#composerHint").textContent = mode === "auto"
    ? "完全访问已启用：命令将自动运行。请检查 diff；工作区边界和危险命令硬阻止仍然生效。"
    : "CodeYF 可能出错。应用修改前请检查 diff，重要操作会请求确认。";
  $$("[data-approval-mode]", modeMenu).forEach((item) => {
    item.setAttribute("aria-checked", String(item.dataset.approvalMode === mode));
  });
}

function closeModeMenu() {
  modeMenu.hidden = true;
  $("#modeButton").setAttribute("aria-expanded", "false");
}

async function selectApprovalMode(mode) {
  if (!["strict", "balanced", "auto"].includes(mode)) return;
  if (backendState.activeTask) {
    closeModeMenu();
    showToast("任务运行期间不能切换权限模式");
    return;
  }
  try {
    if (backendState.sessionId) {
      await api(`/api/sessions/${backendState.sessionId}/settings`, {
        method: "POST",
        body: JSON.stringify({ approval_mode: mode })
      });
    }
    backendState.selectedApprovalMode = mode;
    renderApprovalMode();
    closeModeMenu();
    showToast(mode === "auto" ? "已启用完全访问：命令将自动运行" : `权限模式已切换为${approvalModeLabels[mode]}`);
  } catch (error) {
    closeModeMenu();
    showToast(error.message || "权限模式切换失败");
  }
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", "\"": "&quot;"
  })[character]);
}

function renderInlineMarkdown(text) {
  const protectedHtml = [];
  const protect = (html) => `\u0000${protectedHtml.push(html) - 1}\u0000`;
  const renderLink = (label, href, prefix = "") => {
    try {
      const url = new URL(href);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("unsupported protocol");
      return protect(`${prefix}<a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    } catch {
      return protect(`<span>${escapeHtml(prefix + label)}</span>`);
    }
  };
  let source = String(text || "");
  source = source.replace(/`([^`\n]+)`/g, (_, code) => protect(`<code>${escapeHtml(code)}</code>`));
  source = source.replace(/!\[([^\]\n]*)\]\(([^\s)]+)\)/g, (_, label, href) => renderLink(label || "图片", href, "图片："));
  source = source.replace(/\[([^\]\n]+)\]\(([^\s)]+)\)/g, (_, label, href) => renderLink(label, href));
  source = escapeHtml(source)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return source.replace(/\u0000(\d+)\u0000/g, (_, index) => protectedHtml[Number(index)] || "");
}

function renderMarkdown(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let paragraph = [];
  let listType = null;
  let inCode = false;
  let codeLanguage = "";
  let codeLines = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (!listType) return;
    html.push(`</${listType}>`);
    listType = null;
  };
  const openList = (type) => {
    if (listType === type) return;
    closeList();
    html.push(`<${type}>`);
    listType = type;
  };
  const tableCells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex];
    const fence = line.match(/^\s*```\s*([\w.+-]*)\s*$/);
    if (fence) {
      flushParagraph();
      closeList();
      if (inCode) {
        const languageClass = codeLanguage ? ` class="language-${escapeHtml(codeLanguage)}"` : "";
        html.push(`<pre><code${languageClass}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        inCode = false;
        codeLanguage = "";
        codeLines = [];
      } else {
        inCode = true;
        codeLanguage = fence[1] || "";
      }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }
    if (!line.trim()) { flushParagraph(); closeList(); continue; }
    const tableSeparator = lines[lineIndex + 1] || "";
    if (line.includes("|") && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(tableSeparator)) {
      flushParagraph(); closeList();
      html.push(`<div class="markdown-table-wrap"><table><thead><tr>${tableCells(line).map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>`);
      lineIndex += 2;
      while (lineIndex < lines.length && lines[lineIndex].includes("|") && lines[lineIndex].trim()) {
        html.push(`<tr>${tableCells(lines[lineIndex]).map((cell) => `<td>${renderInlineMarkdown(cell)}</td>`).join("")}</tr>`);
        lineIndex += 1;
      }
      lineIndex -= 1;
      html.push("</tbody></table></div>");
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph(); closeList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const unordered = line.match(/^\s*[-+*]\s+(.+)$/);
    if (unordered) {
      flushParagraph(); openList("ul");
      const task = unordered[1].match(/^\[([ xX])\]\s+(.+)$/);
      html.push(task
        ? `<li class="markdown-task"><input type="checkbox" disabled ${task[1].toLowerCase() === "x" ? "checked" : ""}>${renderInlineMarkdown(task[2])}</li>`
        : `<li>${renderInlineMarkdown(unordered[1])}</li>`);
      continue;
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) { flushParagraph(); openList("ol"); html.push(`<li>${renderInlineMarkdown(ordered[1])}</li>`); continue; }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) { flushParagraph(); closeList(); html.push(`<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`); continue; }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { flushParagraph(); closeList(); html.push("<hr>"); continue; }
    closeList();
    paragraph.push(line.trim());
  }
  if (inCode) html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  flushParagraph();
  closeList();
  return html.join("");
}

function showToast(message) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerHTML = `${icon("i-check")}<span>${escapeHtml(message)}</span>`;
  $("#toastRegion").appendChild(toast);
  window.setTimeout(() => toast.remove(), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error?.message || `HTTP ${response.status}`);
    error.code = payload.error?.code;
    throw error;
  }
  return payload;
}

function safeErrorMessage(error) {
  if (error?.code === "MODEL_AUTHENTICATION") {
    return "API Key 无效或已失效。请设置新的 CODEYF_API_KEY，并重启 CodeYF 后端。";
  }
  return error?.message || "未知错误";
}

function statusLabel(status) {
  return ({
    idle: "空闲", running: "正在运行", waiting_approval: "等待确认",
    completed: "已完成", failed: "失败", cancelled: "已取消"
  })[status] || status || "空闲";
}

function statusClass(status) {
  if (status === "running" || status === "waiting_approval") return "running";
  if (status === "completed") return "done";
  if (status === "failed" || status === "cancelled") return "failed";
  return "idle";
}

function relativeTime(timestamp) {
  if (!timestamp) return "";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function shortTitle(text) {
  const normalized = String(text || "新任务").replace(/\s+/g, " ").trim();
  return normalized.length > 42 ? `${normalized.slice(0, 42)}…` : normalized;
}

function setRunState(label, completed = false) {
  runStateText.textContent = label;
  runState.classList.toggle("completed", completed);
}

function setComposerEnabled(enabled, placeholder) {
  promptInput.disabled = !enabled;
  $("#sendButton").disabled = !enabled;
  if (placeholder) promptInput.placeholder = placeholder;
}

function renderEmpty(title = "把真实编程任务交给 CodeYF", detail = "这里只显示后端实际执行的模型调用、工具结果和文件变更，不再展示演示数据。") {
  $(".conversation-inner").innerHTML = `
    <div class="empty-state" id="emptyState">
      <span class="agent-logo">${icon("i-spark")}</span>
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(detail)}</p>
    </div>`;
}

function appendUserMessage(text, meta = "刚刚") {
  $("#emptyState")?.remove();
  const block = document.createElement("div");
  block.className = "task-brief appended-message";
  block.innerHTML = `
    <div class="user-avatar">YF</div>
    <div class="brief-content">
      <div class="message-meta"><strong>你</strong><span>${escapeHtml(meta)}</span></div>
      <div class="user-prompt markdown-body">${renderMarkdown(text)}</div>
    </div>`;
  $(".conversation-inner").appendChild(block);
  scrollToBottom();
}

function appendAgentText(text, meta = "刚刚", cssClass = "", eventKey = null) {
  if (!text) return;
  if (eventKey && backendState.renderedAssistantEvents.has(eventKey)) return;
  if (eventKey) backendState.renderedAssistantEvents.add(eventKey);
  backendState.lastAssistantText = String(text).trim();
  $("#emptyState")?.remove();
  const block = document.createElement("article");
  block.className = `agent-turn backend-turn ${cssClass}`.trim();
  block.innerHTML = `
    <div class="agent-rail"><span class="agent-logo">${icon("i-spark")}</span><span class="rail-line"></span></div>
    <div class="turn-content">
      <div class="message-meta"><strong>CodeYF</strong><span>${escapeHtml(meta)}</span></div>
      <div class="agent-summary markdown-body">${renderMarkdown(text)}</div>
    </div>`;
  $(".conversation-inner").appendChild(block);
  scrollToBottom();
}

function ensureToolTurn() {
  let turn = $(".backend-live-turn:last-of-type", $(".conversation-inner"));
  if (turn) return turn;
  turn = document.createElement("article");
  turn.className = "agent-turn backend-live-turn";
  turn.innerHTML = `
    <div class="agent-rail"><span class="agent-logo">${icon("i-spark")}</span><span class="rail-line"></span></div>
    <div class="turn-content"><div class="message-meta"><strong>CodeYF</strong><span>真实工具调用</span></div></div>`;
  $(".conversation-inner").appendChild(turn);
  return turn;
}

function ensureToolCard(callId, name, args = {}) {
  if (backendState.toolCards.has(callId)) return backendState.toolCards.get(callId);
  $("#emptyState")?.remove();
  const card = document.createElement("details");
  card.className = "activity-card backend-tool-card";
  card.open = true;
  card.dataset.toolName = name;
  card.innerHTML = `
    <summary>
      <span class="activity-icon spinning"><span class="spinner"></span></span>
      <span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(JSON.stringify(args).slice(0, 220))}</small></span>
      <span class="activity-duration live">等待中</span>
      <svg class="summary-chevron"><use href="#i-chevron"></use></svg>
    </summary>`;
  $(".turn-content", ensureToolTurn()).appendChild(card);
  backendState.toolCards.set(callId, card);
  scrollToBottom();
  return card;
}

function summarizeToolResult(name, data) {
  if (!data) return "完成";
  if (name === "read_file") return `${data.path} · ${data.start_line}–${data.end_line} / ${data.total_lines} 行`;
  if (name === "list_files") return `发现 ${data.count} 个文件`;
  if (name === "search_text") return `找到 ${data.count} 处匹配`;
  if (name === "apply_patch") return (data.changes || []).map((item) => `${item.action} ${item.path} (+${item.added} -${item.removed})`).join("\n");
  if (name === "run_command") return `${(data.argv || []).join(" ")}\nexit ${data.exit_code}\n${data.stdout || data.stderr || ""}`.trim();
  return JSON.stringify(data, null, 2).slice(0, 1800);
}

function recordToolResult(name, result) {
  if (!result?.ok) return;
  if (name === "apply_patch") {
    for (const change of result.data?.changes || []) backendState.changes.set(change.path, change);
    renderChanges();
  }
  if (name === "run_command" && result.data) {
    backendState.commands.push(result.data);
    renderTerminal();
  }
}

function finishToolCard(callId, data) {
  const card = ensureToolCard(callId, data.name || "tool");
  const result = data.result || {};
  const iconWrap = $(".activity-icon", card);
  iconWrap.className = `activity-icon ${data.ok ? "success" : ""}`;
  iconWrap.innerHTML = icon(data.ok ? "i-check" : "i-x");
  const duration = $(".activity-duration", card);
  duration.textContent = `${data.duration_ms || result.meta?.duration_ms || 0}ms`;
  duration.classList.remove("live");
  $("summary strong", card).textContent = data.ok ? `${data.name} 完成` : `${data.name} 失败`;
  $(".backend-result", card)?.remove();
  const detail = document.createElement("div");
  detail.className = "activity-body backend-result";
  detail.innerHTML = `<pre>${escapeHtml(result.ok ? summarizeToolResult(data.name, result.data) : `${result.error?.code || "ERROR"}: ${result.error?.message || "工具失败"}`)}</pre>`;
  card.appendChild(detail);
  recordToolResult(data.name, result);
}

function resetDetails() {
  backendState.toolCards.clear();
  backendState.approvalCards.clear();
  backendState.changes.clear();
  backendState.commands = [];
  backendState.usage = null;
  backendState.renderedAssistantEvents.clear();
  backendState.lastAssistantText = "";
  renderChanges();
  renderTerminal();
  renderContext();
}

function fileBadge(path) {
  const suffix = path.includes(".") ? path.split(".").pop().slice(0, 4).toUpperCase() : "FILE";
  return suffix || "FILE";
}

function isHtmlFile(path) {
  return /\.html?$/i.test(String(path || ""));
}

function setEditorView(mode, path = $("#changeTree .tree-file.selected")?.dataset.file) {
  const html = isHtmlFile(path);
  const selectedMode = html && mode === "preview" ? "preview" : "code";
  const editor = $("#codeEditor");
  const preview = $("#htmlPreview");
  editor.hidden = selectedMode === "preview";
  preview.hidden = selectedMode !== "preview";
  $$("[data-editor-view]", $("#editorViewSwitch")).forEach((button) => {
    button.classList.toggle("active", button.dataset.editorView === selectedMode);
  });
  if (selectedMode === "preview" && backendState.sessionId && path) {
    preview.src = `/api/sessions/${backendState.sessionId}/html-preview?path=${encodeURIComponent(path)}&v=${Date.now()}`;
  } else if (!html) {
    preview.removeAttribute("src");
  }
}

function renderChanges() {
  const changes = [...backendState.changes.values()];
  const added = changes.reduce((sum, item) => sum + (item.added || 0), 0);
  const removed = changes.reduce((sum, item) => sum + (item.removed || 0), 0);
  $("#changeCount").textContent = String(changes.length);
  $("#openDiff span").textContent = `${changes.length} 个变更`;
  $("#changeSummary").textContent = `+${added} −${removed}`;
  const tree = $("#changeTree");
  if (!changes.length) {
    tree.innerHTML = '<div class="panel-empty">当前会话尚未修改文件</div>';
    $("#editorFileName").textContent = "未选择文件";
    $("#editorBadge").textContent = "FILE";
    $("#codeEditor").innerHTML = '<div class="panel-empty">应用补丁后，可在此查看磁盘上的真实文件内容。</div>';
    $("#codeEditor").hidden = false;
    $("#htmlPreview").hidden = true;
    $("#htmlPreview").removeAttribute("src");
    $("#editorViewSwitch").hidden = true;
    return;
  }
  tree.innerHTML = changes.map((change, index) => `
    <button class="tree-file ${index === 0 ? "selected" : ""}" data-file="${escapeHtml(change.path)}">
      <span class="file-badge python">${escapeHtml(fileBadge(change.path))}</span>
      <span>${escapeHtml(change.path)}</span>
      <span class="file-stat"><b>+${change.added || 0}</b><em>−${change.removed || 0}</em></span>
    </button>`).join("");
  $$(".tree-file", tree).forEach((button) => button.addEventListener("click", () => loadFilePreview(button.dataset.file)));
  loadFilePreview(changes[0].path);
}

async function loadFilePreview(path) {
  if (!path || !backendState.sessionId) return;
  $$(".tree-file", $("#changeTree")).forEach((button) => button.classList.toggle("selected", button.dataset.file === path));
  $("#editorFileName").textContent = path;
  $("#editorBadge").textContent = fileBadge(path);
  const html = isHtmlFile(path);
  $("#editorViewSwitch").hidden = !html;
  const editor = $("#codeEditor");
  editor.innerHTML = '<div class="panel-empty">正在读取磁盘文件…</div>';
  try {
    const file = await api(`/api/sessions/${backendState.sessionId}/files?path=${encodeURIComponent(path)}`);
    const lines = file.content.split(/\r?\n/);
    if (lines.at(-1) === "") lines.pop();
    editor.innerHTML = lines.map((line, index) => `<div class="code-line"><span>${index + 1}</span><code>${escapeHtml(line)}</code></div>`).join("") || '<div class="panel-empty">文件为空</div>';
    setEditorView(html ? "preview" : "code", path);
  } catch (error) {
    setEditorView("code", path);
    editor.innerHTML = `<div class="panel-empty">${escapeHtml(error.code === "PATH_NOT_FOUND" ? "文件已被真实删除" : safeErrorMessage(error))}</div>`;
  }
}

function renderTerminal() {
  const terminal = $(".full-terminal");
  if (!backendState.commands.length) {
    terminal.textContent = "当前会话尚未执行命令。";
    return;
  }
  terminal.textContent = backendState.commands.map((data) => {
    const command = (data.argv || []).join(" ") || data.command || "command";
    return `$ ${command}\n${data.stdout || ""}${data.stderr || ""}\n[exit ${data.exit_code}]`;
  }).join("\n\n");
}

function renderContext() {
  const snapshot = backendState.snapshot || {};
  const total = backendState.usage?.total_tokens || 0;
  const windowSize = backendState.health?.context_window_tokens || 0;
  const percent = windowSize ? Math.min(100, Math.round(total / windowSize * 100)) : 0;
  $("#contextPercent").textContent = `${percent}%`;
  $("#contextUsage").textContent = total && windowSize ? `${total.toLocaleString()} / ${windowSize.toLocaleString()} tokens` : "等待模型返回 usage";
  $("#turnCount").textContent = String(snapshot.turn_count || 0);
  $("#toolCallCount").textContent = String(snapshot.tool_call_count || 0);
  $("#eventCount").textContent = String(snapshot.events?.length || backendState.lastSeq || 0);
  $("#contextModel").textContent = snapshot.model || backendState.health?.model || "未连接";
  $("#contextStatus").textContent = statusLabel(snapshot.status);
  $("#contextMeterText").textContent = `${snapshot.turn_count || 0} 轮 · ${snapshot.tool_call_count || 0} 次工具`;
  $("#contextMeterFill").style.width = `${Math.max(percent, snapshot.turn_count ? 3 : 0)}%`;
}

function renderSessions() {
  const nav = $("#taskNav");
  if (!backendState.projects.length) {
    nav.innerHTML = '<div class="nav-empty">暂无项目</div>';
    $("#paletteSessions").innerHTML = '<div class="nav-empty">暂无会话</div>';
    return;
  }
  nav.innerHTML = backendState.projects.map((project) => {
    const projectSessions = backendState.sessions.filter((session) => session.project_id === project.project_id);
    const items = projectSessions.map((session) => `
      <button class="task-item ${session.session_id === backendState.sessionId ? "active" : ""}" data-session="${session.session_id}">
        <span class="task-status ${statusClass(session.status)}"></span>
        <span class="task-copy"><strong>${escapeHtml(shortTitle(session.title))}</strong><small>${escapeHtml(statusLabel(session.status))} · ${session.tool_call_count || 0} 次工具</small></span>
        <span class="task-time">${escapeHtml(relativeTime(session.updated_at))}</span>
      </button>`).join("");
    return `<div class="nav-section project-section ${project.project_id === backendState.selectedProject?.project_id ? "selected" : ""}">
      <button class="nav-heading project-heading" data-project-select="${project.project_id}">
        <span>${escapeHtml(project.name)}</span><small>${projectSessions.length}</small>
      </button>
      ${items || '<div class="nav-empty compact">暂无会话</div>'}
    </div>`;
  }).join("");
  $$("[data-session]", nav).forEach((button) => button.addEventListener("click", () => loadSession(button.dataset.session)));
  $$("[data-project-select]", nav).forEach((button) => button.addEventListener("click", () => selectProject(button.dataset.projectSelect)));
  $("#paletteSessions").innerHTML = backendState.sessions.slice(0, 8).map((session) => `
    <button data-session="${session.session_id}">${icon("i-clock")}<span><strong>${escapeHtml(shortTitle(session.title))}</strong><small>${escapeHtml(statusLabel(session.status))}</small></span></button>`).join("") || '<div class="nav-empty">暂无会话</div>';
  $$("[data-session]", $("#paletteSessions")).forEach((button) => button.addEventListener("click", () => { loadSession(button.dataset.session); closePalette(); }));
}

function updateProjectCard(project, persist = true) {
  if (!project?.project_id) return;
  backendState.selectedProject = project;
  backendState.selectedWorkspace = { path: project.workspace, name: project.name };
  $("#workspaceName").textContent = project.name;
  $("#workspacePath").textContent = project.workspace;
  $("#breadcrumbPath").textContent = project.name;
  if (persist) localStorage.setItem("codeyf-project", project.project_id);
  renderWorkspaceOptions();
  renderSessions();
}

function renderWorkspaceOptions() {
  const list = $("#workspaceList");
  if (!backendState.projects.length) {
    list.innerHTML = '<div class="panel-empty">暂无项目，可在下方创建</div>';
    return;
  }
  list.innerHTML = backendState.projects.map((project) => `
    <button class="workspace-option ${project.project_id === backendState.selectedProject?.project_id ? "active" : ""}" data-project="${project.project_id}">
      ${icon("i-folder")}
      <span><strong>${escapeHtml(project.name)}</strong><small>${escapeHtml(project.workspace)} · ${project.session_count || 0} 个会话</small></span>
      <em>${project.project_id === backendState.selectedProject?.project_id ? "当前" : project.is_default ? "默认" : ""}</em>
    </button>`).join("");
  $$("[data-project]", list).forEach((button) => button.addEventListener("click", () => selectProject(button.dataset.project)));
}

async function loadProjects() {
  const payload = await api("/api/projects");
  backendState.projects = payload.projects || [];
  const stored = localStorage.getItem("codeyf-project");
  const selected = backendState.projects.find((project) => project.project_id === stored)
    || backendState.projects.find((project) => project.project_id === payload.default_project_id)
    || backendState.projects[0];
  if (selected) updateProjectCard(selected, false);
}

function openWorkspaceModal() {
  if (!backendState.available) {
    showToast("后端未连接，暂时无法验证本机目录");
    return;
  }
  if (backendState.activeTask) {
    showToast("当前任务运行中，结束或取消后再切换工作区");
    return;
  }
  renderWorkspaceOptions();
  beginEditProject(backendState.selectedProject);
  $("#workspaceError").textContent = "";
  workspaceModal.classList.add("open");
  workspaceModal.setAttribute("aria-hidden", "false");
  window.setTimeout(() => $("#workspaceInput").focus(), 30);
}

function closeWorkspaceModal() {
  workspaceModal.classList.remove("open");
  workspaceModal.setAttribute("aria-hidden", "true");
}

function beginEditProject(project = null) {
  backendState.editingProjectId = project?.project_id || null;
  $("#projectFormTitle").textContent = project ? "编辑当前项目" : "创建新项目";
  $("#projectNameInput").value = project?.name || "";
  $("#workspaceInput").value = project?.workspace || "";
  $("#workspaceInput").disabled = Boolean(project);
  $("#projectMemoryInput").value = project?.memory || "";
  $("#projectFormSubmit").textContent = project ? "保存项目" : "创建项目";
}

async function selectProject(projectId, startFresh = true) {
  const project = backendState.projects.find((item) => item.project_id === projectId);
  if (!project) return;
  updateProjectCard(project, true);
  closeWorkspaceModal();
  if (startFresh) startNewTask(false);
}

async function saveProjectForm() {
  const name = $("#projectNameInput").value.trim();
  const workspace = $("#workspaceInput").value.trim();
  const memory = $("#projectMemoryInput").value;
  if (!name || !workspace) {
    $("#workspaceError").textContent = "项目名称和工作区不能为空";
    return;
  }
  try {
    const editing = backendState.editingProjectId;
    const project = await api(editing ? "/api/projects/" + editing : "/api/projects", {
      method: "POST",
      body: JSON.stringify(editing ? { name, memory } : { name, workspace, memory })
    });
    await loadProjects();
    const selected = backendState.projects.find((item) => item.project_id === project.project_id);
    if (selected) updateProjectCard(selected, true);
    closeWorkspaceModal();
    startNewTask(false);
    showToast(editing ? "项目设置已保存" : "项目已创建");
  } catch (error) {
    $("#workspaceError").textContent = safeErrorMessage(error);
  }
}

async function refreshSessions() {
  if (!backendState.available) return;
  const payload = await api("/api/sessions");
  backendState.sessions = payload.sessions || [];
  renderSessions();
}

function renderSnapshot(snapshot, { preserveScroll = false } = {}) {
  const previousScrollTop = conversation.scrollTop;
  backendState.restoringSnapshot = true;
  backendState.snapshot = snapshot;
  backendState.lastSeq = Math.max(0, (snapshot.next_event_seq || 1) - 1);
  $(".conversation-inner").innerHTML = "";
  resetDetails();
  const callNames = new Map();
  let assistantTextCount = 0;
  const transcript = Array.isArray(snapshot.transcript) ? snapshot.transcript : [];
  const hasTranscript = transcript.length > 0;
  if (hasTranscript) {
    for (const [entryIndex, entry] of transcript.entries()) {
      if (entry.role === "user" && entry.content) appendUserMessage(entry.content, entry.recovered ? "历史 · 摘要恢复" : "历史");
      if (entry.role === "assistant" && entry.content) {
        appendAgentText(entry.content, "历史", "", "transcript-" + entryIndex);
        assistantTextCount += 1;
      }
    }
  }
  for (const [messageIndex, message] of (snapshot.messages || []).entries()) {
    if (!hasTranscript && message.role === "user") appendUserMessage(message.content, "历史");
    if (message.role === "assistant") {
      for (const call of message.tool_calls || []) {
        const name = call.function?.name || "tool";
        let args = {};
        try { args = JSON.parse(call.function?.arguments || "{}"); } catch { args = {}; }
        callNames.set(call.id, name);
        ensureToolCard(call.id, name, args);
      }
      if (!hasTranscript && message.content) {
        appendAgentText(message.content, "历史", "", "history-" + messageIndex);
        assistantTextCount += 1;
      }
    }
    if (message.role === "tool") {
      let result;
      try { result = JSON.parse(message.content); } catch { result = { ok: false, error: { code: "INVALID_RESULT", message: "工具结果无法解析" } }; }
      const name = message.name || callNames.get(message.tool_call_id) || "tool";
      finishToolCard(message.tool_call_id, { name, ok: Boolean(result.ok), result, duration_ms: result.meta?.duration_ms || 0 });
    }
  }
  const approvalDecisions = new Map(
    (snapshot.events || [])
      .filter((event) => event.type === "approval.decided")
      .map((event) => [event.data?.approval_id, event.data?.decision])
  );
  for (const event of snapshot.events || []) {
    if (event.type === "model.responded" && event.data?.usage) backendState.usage = event.data.usage;
    if (event.type === "tool.requested") {
      ensureToolCard(event.data?.tool_call_id, event.data?.name, event.data?.arguments);
    }
    if (event.type === "tool.started") {
      const card = ensureToolCard(event.data?.tool_call_id, event.data?.name);
      $(".activity-duration", card).textContent = "运行中";
    }
    if (event.type === "tool.finished") {
      finishToolCard(event.data?.tool_call_id, event.data);
    }
    if (event.type === "approval.requested") showApprovalRequest(event.data, approvalDecisions.get(event.data?.approval_id));
  }
  if (!assistantTextCount && snapshot.final_text) appendAgentText(snapshot.final_text, "历史", "", "snapshot-final");
  const hasConversation = hasTranscript
    || (snapshot.messages || []).some((message) => message.role !== "system")
    || Boolean(snapshot.final_text)
    || (snapshot.events || []).some((event) => ["tool.requested", "tool.finished", "task.completed"].includes(event.type));
  if (!hasConversation) renderEmpty();
  const firstUser = transcript.find((entry) => entry.role === "user")?.content
    || (snapshot.messages || []).find((message) => message.role === "user")?.content;
  $("#threadTitle").textContent = shortTitle(snapshot.title || firstUser || "新任务");
  const project = backendState.projects.find((item) => item.project_id === snapshot.project_id);
  if (project) updateProjectCard(project, true);
  setRunState(statusLabel(snapshot.status), snapshot.status === "completed");
  if (snapshot.error) {
    appendAgentText(`任务失败：${safeErrorMessage(snapshot.error)}`, "失败", "error-turn");
  }
  renderContext();
  backendState.restoringSnapshot = false;
  window.requestAnimationFrame(() => {
    if (preserveScroll) conversation.scrollTop = previousScrollTop;
    else conversation.scrollTop = conversation.scrollHeight;
  });
}

async function loadSession(sessionId) {
  if (!sessionId) return;
  if (backendState.source) backendState.source.close();
  const snapshot = await api(`/api/sessions/${sessionId}`);
  backendState.sessionId = sessionId;
  backendState.activeTask = ["running", "waiting_approval"].includes(snapshot.status);
  backendState.selectedApprovalMode = snapshot.approval_mode || backendState.selectedApprovalMode;
  renderApprovalMode();
  const project = backendState.projects.find((item) => item.project_id === snapshot.project_id);
  if (project) updateProjectCard(project, true);
  renderSnapshot(snapshot);
  renderSessions();
  appShell.classList.remove("sidebar-open");
  if (backendState.activeTask) connectEventStream();
}

async function detectBackend() {
  try {
    const health = await api("/api/health");
    backendState.available = true;
    backendState.configured = health.configured;
    backendState.health = health;
    backendState.selectedApprovalMode = ["strict", "balanced", "auto"].includes(health.approval)
      ? health.approval
      : "balanced";
    renderApprovalMode();
    $("#profileStatus").textContent = `${health.approval} · ${health.model}`;
    $(".online-dot").title = health.configured ? "Agent 后端已连接" : "后端已连接，API Key 未配置";
    setComposerEnabled(true, health.configured ? "描述真实编程任务…" : "可以先输入任务；配置 CODEYF_API_KEY 后再运行");
    await loadProjects();
    await refreshSessions();
    if (backendState.sessions.length) await loadSession(backendState.sessions[0].session_id);
    else startNewTask(false);
    if (!health.configured) showToast("后端已连接，但尚未配置 API Key");
  } catch (error) {
    backendState.available = false;
    $("#profileStatus").textContent = "后端未连接";
    $(".online-dot").classList.add("offline");
    setComposerEnabled(true, "可以先输入任务；启动 codeyf web 后再运行");
    renderEmpty("后端未连接", "不会生成模拟任务。请先启动 CodeYF Web 后端，然后刷新页面。");
    setRunState("离线", false);
  }
}

async function ensureSession() {
  if (backendState.sessionId) return backendState.sessionId;
  const session = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({
      project_id: backendState.selectedProject?.project_id || null,
      approval_mode: backendState.selectedApprovalMode || null
    })
  });
  backendState.sessionId = session.session_id;
  backendState.snapshot = session;
  backendState.lastSeq = 0;
  await refreshSessions();
  return session.session_id;
}

function connectEventStream() {
  if (!backendState.sessionId) return;
  if (backendState.source) backendState.source.close();
  const source = new EventSource(`/api/sessions/${backendState.sessionId}/events/stream?after=${backendState.lastSeq}`);
  backendState.source = source;
  const eventNames = [
    "state.changed", "model.responded", "model.failed", "tool.requested", "tool.started", "tool.finished",
    "approval.requested", "approval.decided", "task.completed", "task.failed", "task.cancelled", "context.compacted"
  ];
  eventNames.forEach((name) => source.addEventListener(name, (event) => {
    const payload = JSON.parse(event.data);
    backendState.lastSeq = Math.max(backendState.lastSeq, payload.seq || 0);
    handleBackendEvent(payload);
    scheduleSessionWatch(backendState.sessionId);
  }));
  source.onerror = () => {
    source.close();
    if (backendState.activeTask) recoverActiveSession(backendState.sessionId);
  };
  scheduleSessionWatch(backendState.sessionId);
}

function scheduleSessionWatch(sessionId) {
  window.clearTimeout(backendState.watchdogTimer);
  if (!sessionId || !backendState.activeTask) return;
  backendState.watchdogTimer = window.setTimeout(() => recoverActiveSession(sessionId), 1500);
}

async function recoverActiveSession(sessionId) {
  if (!sessionId || backendState.sessionId !== sessionId || !backendState.activeTask) return;
  try {
    const snapshot = await api("/api/sessions/" + sessionId);
    if (backendState.sessionId !== sessionId) return;
    const terminal = ["completed", "failed", "cancelled"].includes(snapshot.status);
    const missedEvents = (snapshot.next_event_seq || 1) - 1 > backendState.lastSeq;
    if (terminal || missedEvents) {
      backendState.source?.close();
      backendState.activeTask = ["running", "waiting_approval"].includes(snapshot.status);
      renderSnapshot(snapshot, { preserveScroll: true });
      if (backendState.activeTask) connectEventStream();
      return;
    }
  } catch {
    // Keep the current UI and retry; switching sessions is no longer required.
  }
  if (backendState.sessionId === sessionId && backendState.activeTask) {
    scheduleSessionWatch(sessionId);
  }
}

function approvalDecisionLabel(decision) {
  return ({ approve_once: "已允许一次", deny: "已拒绝", cancel_task: "已取消任务", submitting: "正在提交…", submitted: "已提交，等待执行", syncing: "正在校准状态…" })[decision] || "等待你的决定";
}

function updateApprovalCard(card, decision) {
  if (!card) return;
  const status = $(".approval-status", card);
  status.textContent = approvalDecisionLabel(decision);
  status.className = `approval-status ${decision || "pending"}`;
  $$("button", card).forEach((button) => { button.disabled = Boolean(decision); });
}

function showApprovalRequest(request, decision = null) {
  if (!request?.approval_id) return;
  let card = backendState.approvalCards.get(request.approval_id);
  if (!card) {
    const displayArguments = request.display_arguments || {};
    const command = displayArguments.command
      || (displayArguments.argv || []).join(" ")
      || JSON.stringify(displayArguments, null, 2);
    const block = document.createElement("article");
    block.className = "agent-turn approval-turn";
    block.innerHTML = `
      <div class="agent-rail"><span class="approval-rail-icon">${icon("i-shield")}</span><span class="rail-line"></span></div>
      <div class="turn-content">
        <div class="message-meta"><strong>CodeYF 请求权限</strong><span class="approval-status pending">等待你的决定</span></div>
        <section class="inline-approval" role="group" aria-label="工具执行权限请求">
          <div class="inline-approval-head">
            <div><span class="eyebrow">需要你的确认</span><h3>允许执行 ${escapeHtml(request.tool_name || "工具")}？</h3></div>
            <span class="risk-badge"><i></i>${escapeHtml(request.risk || "unknown")}</span>
          </div>
          <p>${escapeHtml(request.summary || "此操作需要用户确认。")}</p>
          <pre><code>${escapeHtml(command)}</code></pre>
          <small>${escapeHtml((request.rule_ids || []).join(" · ") || "未提供规则编号")}</small>
          <div class="inline-approval-actions">
            <button class="ghost-button" data-approval-decision="deny">拒绝</button>
            <button class="primary-button" data-approval-decision="approve">仅允许这一次</button>
          </div>
        </section>
      </div>`;
    card = block;
    block.dataset.approvalId = request.approval_id;
    $$("[data-approval-decision]", block).forEach((button) => button.addEventListener("click", () => {
      submitApproval(request.approval_id, button.dataset.approvalDecision);
    }));
    $(".conversation-inner").appendChild(block);
    backendState.approvalCards.set(request.approval_id, block);
  }
  updateApprovalCard(card, decision);
  scrollToBottom();
}

function handleBackendEvent(event) {
  const data = event.data || {};
  if (backendState.snapshot) {
    backendState.snapshot.events = backendState.snapshot.events || [];
    backendState.snapshot.events.push(event);
  }
  if (event.type === "state.changed") {
    if (backendState.snapshot) backendState.snapshot.status = data.to;
    setRunState(statusLabel(data.to), data.to === "completed");
  } else if (event.type === "model.responded") {
    if (data.usage) backendState.usage = data.usage;
    if (data.content) appendAgentText(
      data.content,
      "模型返回",
      "",
      "model-" + (data.model_call_id || event.seq)
    );
  } else if (event.type === "tool.requested") {
    ensureToolCard(data.tool_call_id, data.name, data.arguments);
  } else if (event.type === "tool.started") {
    const card = ensureToolCard(data.tool_call_id, data.name);
    $(".activity-duration", card).textContent = "运行中";
  } else if (event.type === "tool.finished") {
    finishToolCard(data.tool_call_id, data);
    if (backendState.snapshot) backendState.snapshot.tool_call_count = (backendState.snapshot.tool_call_count || 0) + 1;
  } else if (event.type === "approval.requested") {
    showApprovalRequest(data);
  } else if (event.type === "approval.decided") {
    updateApprovalCard(backendState.approvalCards.get(data.approval_id), data.decision);
  } else if (event.type === "task.completed") {
    backendState.activeTask = false;
    backendState.source?.close();
    if (data.final_text && String(data.final_text).trim() !== backendState.lastAssistantText) {
      appendAgentText(data.final_text, "已完成", "", "task-final-" + event.seq);
    }
    setRunState("已完成", true);
    refreshSessions();
    showToast("真实任务已完成");
  } else if (event.type === "model.failed") {
    showToast(`模型调用失败：${safeErrorMessage(data.error)}`);
  } else if (event.type === "task.failed") {
    backendState.activeTask = false;
    const error = data.error || {};
    appendAgentText(`任务失败：${safeErrorMessage(error)}`, "失败", "error-turn");
    setRunState("失败", false);
    refreshSessions();
  } else if (event.type === "task.cancelled") {
    backendState.activeTask = false;
    appendAgentText("任务已取消。", "已取消");
    setRunState("已取消", false);
    refreshSessions();
  } else if (event.type === "context.compacted") {
    showToast("已自动压缩较早上下文");
  }
  renderContext();
}

async function submitRealPrompt(text) {
  appendUserMessage(text);
  promptInput.value = "";
  autoResizeInput();
  $("#threadTitle").textContent = shortTitle(text);
  setRunState("正在启动", false);
  backendState.activeTask = true;
  try {
    const sessionId = await ensureSession();
    await api(`/api/sessions/${sessionId}/tasks`, { method: "POST", body: JSON.stringify({ message: text }) });
    connectEventStream();
    await refreshSessions();
  } catch (error) {
    backendState.activeTask = false;
    setRunState("启动失败", false);
    appendAgentText(`无法启动任务：${safeErrorMessage(error)}`, "错误", "error-turn");
  }
}

function submitPrompt(text) {
  const clean = text.trim();
  if (!clean || backendState.activeTask) return;
  if (!backendState.available) {
    showToast("后端未连接，不会运行模拟任务");
    return;
  }
  if (!backendState.configured) {
    showToast("请先配置有效的 CODEYF_API_KEY 并重启后端");
    return;
  }
  if (!backendState.selectedProject) {
    showToast("请先创建或选择项目");
    return;
  }
  submitRealPrompt(clean);
}

function startNewTask(focus = true) {
  closePalette();
  appShell.classList.remove("sidebar-open");
  backendState.source?.close();
  backendState.sessionId = null;
  backendState.snapshot = null;
  backendState.lastSeq = 0;
  backendState.activeTask = false;
  window.clearTimeout(backendState.watchdogTimer);
  resetDetails();
  renderEmpty();
  $("#threadTitle").textContent = "新任务";
  $("#breadcrumbPath").textContent = backendState.selectedProject?.name || "请选择项目";
  setRunState("空闲", false);
  promptInput.value = "";
  promptInput.placeholder = backendState.configured ? "描述一个新的真实编程任务…" : "请先配置 CODEYF_API_KEY 并重启后端";
  renderSessions();
  if (focus) promptInput.focus();
}

function scrollToBottom() {
  if (backendState.restoringSnapshot) return;
  conversation.scrollTo({ top: conversation.scrollHeight, behavior: "auto" });
}

function autoResizeInput() {
  promptInput.style.height = "auto";
  promptInput.style.height = `${Math.min(promptInput.scrollHeight, 150)}px`;
}

function openDetail(panel = "changes") {
  appShell.classList.remove("details-hidden");
  appShell.classList.add("details-open-mobile");
  selectDetailTab(panel);
}

function closeDetail() {
  if (window.matchMedia("(max-width: 980px)").matches) appShell.classList.remove("details-open-mobile");
  else appShell.classList.add("details-hidden");
}

function selectDetailTab(name) {
  $$(".details-tabs [data-panel]").forEach((tab) => {
    const active = tab.dataset.panel === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  $$(".detail-content").forEach((content) => content.classList.toggle("active", content.dataset.content === name));
}

function openPalette() {
  commandPalette.classList.add("open");
  commandPalette.setAttribute("aria-hidden", "false");
  paletteInput.value = "";
  window.setTimeout(() => paletteInput.focus(), 30);
}

function closePalette() {
  commandPalette.classList.remove("open");
  commandPalette.setAttribute("aria-hidden", "true");
}

function filterPalette(value) {
  const query = value.trim().toLowerCase();
  $$(".palette-results button").forEach((button) => { button.hidden = Boolean(query && !button.textContent.toLowerCase().includes(query)); });
  $$(".task-item").forEach((button) => { button.hidden = Boolean(query && !button.textContent.toLowerCase().includes(query)); });
}

async function submitApproval(approvalId, decision) {
  if (!approvalId || !backendState.sessionId) return;
  const card = backendState.approvalCards.get(approvalId);
  updateApprovalCard(card, "submitting");
  const apiDecision = decision === "approve" ? "approve_once" : "deny";
  try {
    await api(`/api/sessions/${backendState.sessionId}/approvals/${approvalId}`, {
      method: "POST", body: JSON.stringify({ decision: apiDecision })
    });
    // The POST only acknowledges delivery. Show approval only after the worker
    // emits approval.decided through the event stream.
    updateApprovalCard(card, "submitted");
    window.setTimeout(() => reconcileApprovalState(backendState.sessionId, approvalId), 350);
  } catch (error) {
    updateApprovalCard(card, null);
    showToast(`审批提交失败：${safeErrorMessage(error)}`);
  }
}

async function reconcileApprovalState(sessionId, approvalId, attempt = 0) {
  if (!sessionId || backendState.sessionId !== sessionId || !backendState.activeTask) return;
  try {
    const snapshot = await api("/api/sessions/" + sessionId);
    if (backendState.sessionId !== sessionId) return;
    const events = snapshot.events || [];
    const request = events.find(
      (event) => event.type === "approval.requested" && event.data?.approval_id === approvalId
    );
    const toolCallId = request?.data?.tool_call_id;
    const confirmed = events.some(
      (event) => event.type === "approval.decided" && event.data?.approval_id === approvalId
    );
    const toolProgressed = Boolean(toolCallId) && events.some(
      (event) => ["tool.started", "tool.finished"].includes(event.type)
        && event.data?.tool_call_id === toolCallId
    );
    if (confirmed || toolProgressed || snapshot.status !== "waiting_approval") {
      backendState.source?.close();
      backendState.activeTask = ["running", "waiting_approval"].includes(snapshot.status);
      renderSnapshot(snapshot, { preserveScroll: true });
      if (backendState.activeTask) connectEventStream();
      return;
    }
    updateApprovalCard(backendState.approvalCards.get(approvalId), "syncing");
  } catch {
    // A transient snapshot failure is retried below while this task remains active.
  }
  if (backendState.sessionId === sessionId && backendState.activeTask) {
    const delay = attempt < 4 ? 500 * (attempt + 1) : 3000;
    window.setTimeout(() => reconcileApprovalState(sessionId, approvalId, attempt + 1), delay);
  }
}

composer.addEventListener("submit", (event) => { event.preventDefault(); submitPrompt(promptInput.value); });
promptInput.addEventListener("input", autoResizeInput);
promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitPrompt(promptInput.value); }
});

$("#rightPanelToggle").addEventListener("click", () => {
  if (window.matchMedia("(max-width: 980px)").matches) openDetail("changes");
  else appShell.classList.toggle("details-hidden");
});
$("#detailsClose").addEventListener("click", closeDetail);
$("#openDiff").addEventListener("click", () => openDetail("changes"));
$("#mobileMenu").addEventListener("click", () => appShell.classList.add("sidebar-open"));
$(".sidebar-collapse").addEventListener("click", () => appShell.classList.remove("sidebar-open"));
$$(".details-tabs [data-panel]").forEach((tab) => tab.addEventListener("click", () => selectDetailTab(tab.dataset.panel)));

$("#themeToggle").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme === "dark";
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  localStorage.setItem("codeyf-theme", dark ? "light" : "dark");
});

$("#searchButton").addEventListener("click", openPalette);
$("#newTaskButton").addEventListener("click", () => startNewTask(true));
$(".workspace-card").addEventListener("click", openWorkspaceModal);
$("#workspaceClose").addEventListener("click", closeWorkspaceModal);
workspaceModal.addEventListener("mousedown", (event) => { if (event.target === workspaceModal) closeWorkspaceModal(); });
$("#workspaceForm").addEventListener("submit", (event) => { event.preventDefault(); saveProjectForm(); });
$("#newProjectButton").addEventListener("click", () => beginEditProject(null));
paletteInput.addEventListener("input", (event) => filterPalette(event.target.value));
commandPalette.addEventListener("mousedown", (event) => { if (event.target === commandPalette) closePalette(); });
$$(".palette-results [data-action]").forEach((button) => button.addEventListener("click", () => {
  if (button.dataset.action === "new") startNewTask(true);
  if (button.dataset.action === "terminal") openDetail("terminal");
  if (button.dataset.action === "changes") openDetail("changes");
  closePalette();
}));

$("#modeButton").addEventListener("click", (event) => {
  event.stopPropagation();
  modeMenu.hidden = !modeMenu.hidden;
  $("#modeButton").setAttribute("aria-expanded", String(!modeMenu.hidden));
});
$$("[data-approval-mode]", modeMenu).forEach((button) => button.addEventListener("click", () => selectApprovalMode(button.dataset.approvalMode)));
document.addEventListener("click", (event) => { if (!event.target.closest(".mode-control")) closeModeMenu(); });
$("#refreshPreview").addEventListener("click", () => {
  const selected = $("#changeTree .tree-file.selected")?.dataset.file;
  if (selected) loadFilePreview(selected);
  else showToast("当前会话没有真实文件变更");
});
$$("[data-editor-view]", $("#editorViewSwitch")).forEach((button) => button.addEventListener("click", () => {
  setEditorView(button.dataset.editorView);
}));
$("#reloadSession").addEventListener("click", () => backendState.sessionId && loadSession(backendState.sessionId));

document.addEventListener("keydown", (event) => {
  const modifier = event.metaKey || event.ctrlKey;
  if (modifier && event.key.toLowerCase() === "k") { event.preventDefault(); openPalette(); }
  if (modifier && event.key.toLowerCase() === "n") { event.preventDefault(); startNewTask(true); }
  if (modifier && event.key.toLowerCase() === "j") { event.preventDefault(); openDetail("terminal"); }
  if (event.key === "Escape") {
    closePalette();
    closeWorkspaceModal();
    appShell.classList.remove("sidebar-open", "details-open-mobile");
  }
});

const storedTheme = localStorage.getItem("codeyf-theme");
if (storedTheme) document.documentElement.dataset.theme = storedTheme;

detectBackend();
