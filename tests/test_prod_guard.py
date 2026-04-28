"""Tests for the production write guard and confirmation tokens."""

from __future__ import annotations

import pytest

from odoo_mcp.errors import ProdGuardError
from odoo_mcp.security.prod_guard import ProdGuard


def test_check_write_no_op_on_non_prod() -> None:
    guard = ProdGuard()
    # Does not raise.
    guard.check_write("dev", production=False)


def test_check_write_blocks_prod_by_default() -> None:
    guard = ProdGuard()
    with pytest.raises(ProdGuardError, match="blocked"):
        guard.check_write("prod", production=True)


def test_unlock_allows_writes() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    assert guard.is_unlocked("prod", now=100.0)
    guard.check_write("prod", production=True, now=100.0)


def test_unlock_refuses_non_production_instance() -> None:
    guard = ProdGuard()
    with pytest.raises(ProdGuardError, match="not flagged as production"):
        guard.unlock("dev", production=False)


def test_unlock_auto_expires() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    # 15 minutes + 1 second later
    assert not guard.is_unlocked("prod", now=15 * 60 + 1)
    with pytest.raises(ProdGuardError):
        guard.check_write("prod", production=True, now=15 * 60 + 1)


def test_effective_dry_run_defaults_true_on_prod() -> None:
    guard = ProdGuard()
    assert guard.effective_dry_run(None, production=True) is True
    assert guard.effective_dry_run(None, production=False) is False
    assert guard.effective_dry_run(False, production=True) is False
    assert guard.effective_dry_run(True, production=False) is True


def test_confirmation_token_roundtrip() -> None:
    guard = ProdGuard()
    token = guard.create_pending("prod", "create", "res.partner", "summary", now=0.0)
    assert token.startswith("conf_")
    guard.consume_pending(token, "prod", "create", "res.partner", now=10.0)


def test_confirmation_token_single_use() -> None:
    guard = ProdGuard()
    token = guard.create_pending("prod", "create", "res.partner", "summary", now=0.0)
    guard.consume_pending(token, "prod", "create", "res.partner", now=10.0)
    with pytest.raises(ProdGuardError, match="unknown or already used"):
        guard.consume_pending(token, "prod", "create", "res.partner", now=20.0)


def test_confirmation_token_mismatched_scope_rejected() -> None:
    guard = ProdGuard()
    token = guard.create_pending("prod", "create", "res.partner", "summary", now=0.0)
    with pytest.raises(ProdGuardError, match="does not match"):
        guard.consume_pending(token, "prod", "write", "res.partner", now=10.0)


def test_confirmation_token_expires() -> None:
    guard = ProdGuard()
    token = guard.create_pending("prod", "create", "res.partner", "summary", now=0.0)
    # 5 minute TTL.
    with pytest.raises(ProdGuardError, match="expired"):
        guard.consume_pending(token, "prod", "create", "res.partner", now=5 * 60 + 1)


def test_touch_extends_unlock_window() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    # At t=10min the unlock would expire at t=15min.
    guard.touch("prod", now=10 * 60)
    # At t=20min we should still be unlocked because touch reset the window.
    assert guard.is_unlocked("prod", now=20 * 60)


# --- burst limit -------------------------------------------------------------


def test_burst_limit_enforced() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0, max_commits=2)
    # First commit
    t1 = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    guard.consume_pending(t1, "prod", "create", "res.partner", now=1.0)
    # Second commit
    t2 = guard.create_pending("prod", "create", "res.partner", "s", now=2.0)
    guard.consume_pending(t2, "prod", "create", "res.partner", now=3.0)
    # Third commit -> burst limit
    t3 = guard.create_pending("prod", "create", "res.partner", "s", now=4.0)
    with pytest.raises(ProdGuardError, match="Burst limit reached"):
        guard.consume_pending(t3, "prod", "create", "res.partner", now=5.0)


def test_dry_runs_dont_count_toward_burst() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0, max_commits=2)
    # Many dry-runs: create_pending is fine, no consume happens.
    for _ in range(10):
        guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    # Counter still intact: 2 commits available.
    assert guard.commits_remaining("prod", now=1.0) == 2


def test_relock_resets_counter() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0, max_commits=1)
    t1 = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    guard.consume_pending(t1, "prod", "create", "res.partner", now=1.0)
    assert guard.commits_remaining("prod", now=2.0) == 0
    # New unlock past the previous TTL
    guard.unlock("prod", production=True, now=20 * 60, max_commits=1)
    assert guard.commits_remaining("prod", now=20 * 60 + 1) == 1


def test_default_is_ten() -> None:
    from odoo_mcp.security.prod_guard import DEFAULT_MAX_COMMITS_PER_UNLOCK

    assert DEFAULT_MAX_COMMITS_PER_UNLOCK == 10
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    assert guard.commits_remaining("prod", now=1.0) == 10
