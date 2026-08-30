from __future__ import annotations

from pathlib import Path

import pytest

from codeyf.security import PathGuard, PathSecurityError, PolicyDecision, SecurityPolicy


def test_path_guard_allows_workspace_and_blocks_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "file.txt").write_text("ok", encoding="utf-8")
    guard = PathGuard(workspace)

    assert guard.resolve("file.txt", must_exist=True) == (workspace / "file.txt").resolve()
    with pytest.raises(PathSecurityError):
        guard.resolve("../secret.txt")


def test_balanced_policy_allows_safe_command_and_asks_for_delete() -> None:
    policy = SecurityPolicy("balanced")
    safe = policy.assess("run_command", {"argv": ["python", "-m", "pytest", "-q"]})
    dangerous = policy.assess("run_command", {"argv": ["rm", "-rf", "build"]})

    assert safe.decision == PolicyDecision.ALLOW
    assert dangerous.decision == PolicyDecision.ASK


def test_policy_hard_denies_system_power_command() -> None:
    policy = SecurityPolicy("auto")
    result = policy.assess("run_command", {"argv": ["shutdown", "-h", "now"]})
    assert result.decision == PolicyDecision.DENY

