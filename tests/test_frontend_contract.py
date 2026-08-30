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
