from __future__ import annotations

from pathlib import Path


def test_approval_ui_waits_for_backend_decision_and_has_snapshot_recovery() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
    submit_section = source.split("async function submitApproval", 1)[1].split(
        "async function reconcileApprovalState", 1
    )[0]
    decided_section = source.split('event.type === "approval.decided"', 1)[1].split(
        'event.type === "task.completed"', 1
    )[0]

    assert 'updateApprovalCard(card, apiDecision)' not in submit_section
    assert 'updateApprovalCard(card, "submitted")' in submit_section
    assert "reconcileApprovalState" in submit_section
    assert "data.decision" in decided_section
    assert 'await api("/api/sessions/" + sessionId)' in source
    assert '["tool.started", "tool.finished"]' in source


def test_live_model_text_watchdog_scroll_and_user_markdown_contracts() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")

    assert 'event.type === "model.responded"' in source
    assert '"model-" + (data.model_call_id || event.seq)' in source
    assert "scheduleSessionWatch" in source
    assert "recoverActiveSession" in source
    assert 'renderSnapshot(snapshot, { preserveScroll: true })' in source
    assert 'behavior: "smooth"' not in source
    assert '<div class="user-prompt markdown-body">${renderMarkdown(text)}</div>' in source


def test_full_access_mode_has_inline_menu_and_updates_current_idle_session() -> None:
    root = Path(__file__).parents[1]
    source = (root / "frontend" / "app.js").read_text(encoding="utf-8")
    markup = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    assert 'data-approval-mode="auto"' in markup
    assert "完全访问" in markup
    assert 'role="menu"' in markup
    assert '`/api/sessions/${backendState.sessionId}/settings`' in source
    assert 'if (backendState.activeTask)' in source


def test_compacted_snapshot_uses_durable_transcript_without_clearing_history() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
    render = source.split("function renderSnapshot", 1)[1].split("async function loadSession", 1)[0]

    assert "snapshot.transcript" in render
    assert "const hasConversation = hasTranscript" in render
    assert 'if (!hasConversation) renderEmpty();' in render
    assert 'if (!(snapshot.messages || []).some((message) => message.role === "user")) renderEmpty();' not in render
    assert "snapshot.title || firstUser" in render


def test_html_preview_uses_sandboxed_iframe_and_code_toggle() -> None:
    root = Path(__file__).parents[1]
    source = (root / "frontend" / "app.js").read_text(encoding="utf-8")
    markup = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    assert 'id="htmlPreview"' in markup
    assert 'sandbox="allow-scripts"' in markup
    assert "allow-same-origin" not in markup
    assert 'data-editor-view="preview"' in markup
    assert 'data-editor-view="code"' in markup
    assert "/html-preview?path=" in source
    assert 'setEditorView(html ? "preview" : "code", path)' in source
