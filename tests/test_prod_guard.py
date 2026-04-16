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
