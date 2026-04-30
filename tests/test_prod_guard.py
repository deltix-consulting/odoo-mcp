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


def test_token_rejected_after_unlock_expires_and_renews() -> None:
    """H1: a confirmation token issued under unlock window A must NOT be
    consumable under a later, different unlock window B even if the
    token's own 5-minute TTL has not yet elapsed.

    Staging note: the unlock TTL is 15min and the token TTL is 5min,
    so to expose the bug we need a token still within its own TTL but
    issued under a distinct, no-longer-active unlock. We force the
    first unlock to drop by reaching into ``_unlocked`` directly —
    simulating a real expiry without having to fast-forward past the
    token's 5-min TTL too. Pre-fix this consume would silently
    succeed; post-fix it raises with the H1 error.
    """
    guard = ProdGuard()
    # Window A
    guard.unlock("prod", production=True, now=0.0)
    token = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    # Window A expires (simulated — keeps token within its 5-min TTL).
    guard._unlocked.pop("prod")  # noqa: SLF001 — test stages the H1 race
    # Window B: fresh unlock with a distinct unlocked_at.
    guard.unlock("prod", production=True, now=10.0)
    # Token from A must be rejected under B even though both the token
    # TTL (5min) and the unlock TTL (15min) are still in the future.
    with pytest.raises(ProdGuardError, match="different unlock window"):
        guard.consume_pending(token, "prod", "create", "res.partner", now=10.0)


def test_token_accepted_when_unlock_was_only_touched() -> None:
    """A touch() within the same unlock must not invalidate tokens —
    only re-acquiring the unlock does. This guards the H1 fix from
    over-rejecting and breaking normal multi-write flows.
    """
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    token = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    # Activity a few minutes in extends the unlock window.
    guard.touch("prod", now=2 * 60)
    # Token should still be consumable (within the 5-min token TTL,
    # under the same unlock identity that touch did NOT change).
    guard.consume_pending(token, "prod", "create", "res.partner", now=2 * 60 + 1)


def test_default_is_ten() -> None:
    from odoo_mcp.security.prod_guard import DEFAULT_MAX_COMMITS_PER_UNLOCK

    assert DEFAULT_MAX_COMMITS_PER_UNLOCK == 10
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    assert guard.commits_remaining("prod", now=1.0) == 10


def test_unknown_token_error_does_not_echo_token_value() -> None:
    """Confirmation tokens must not appear verbatim in the error message.

    Tokens are credential-shaped and the dispatcher records error messages
    in the audit log (30-day retention). Echoing a supplied (or
    accidentally-mistyped) token is a leak path.
    """
    from odoo_mcp.errors import ProdGuardError

    guard = ProdGuard()
    bad_token = "conf_abcdef_ThisShouldNotAppearInTheError_0123456"
    try:
        guard.consume_pending(bad_token, "prod", "create", "res.partner", now=1.0)
    except ProdGuardError as exc:
        msg = str(exc)
        assert bad_token not in msg, f"token leaked into error message: {msg!r}"
        # We still want a useful message.
        assert "confirmation token" in msg.lower()
    else:
        raise AssertionError("expected ProdGuardError")


def test_expired_token_error_does_not_echo_token_value() -> None:
    from odoo_mcp.errors import ProdGuardError

    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    token = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    # Past the 5-minute pending-token TTL.
    try:
        guard.consume_pending(token, "prod", "create", "res.partner", now=10 * 60)
    except ProdGuardError as exc:
        assert token not in str(exc)
        assert "expired" in str(exc).lower()
    else:
        raise AssertionError("expected ProdGuardError")
