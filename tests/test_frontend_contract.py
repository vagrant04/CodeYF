from __future__ import annotations

from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def test_permissions_are_inline_and_not_modal() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    script = (FRONTEND / "app.js").read_text(encoding="utf-8")
    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert 'id="approvalModal"' not in html
    assert "showApprovalRequest" in script
    assert "inline-approval" in script
    assert ".inline-approval" in styles


def test_agent_answers_use_safe_markdown_renderer() -> None:
    script = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "function renderMarkdown" in script
    assert 'class="agent-summary markdown-body"' in script
    assert '<p class="agent-summary">${escapeHtml(text)}</p>' not in script
    assert "escapeHtml(codeLines.join" in script
