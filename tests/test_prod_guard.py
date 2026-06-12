"""Tests for the production write guard and confirmation tokens."""

from __future__ import annotations

import pytest

from odoo_mcp.errors import ProdGuardError
from odoo_mcp.security.prod_guard import ProdGuard, compute_payload_digest


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
    with pytest.raises(ProdGuardError, match="Burst limit reached") as excinfo:
        guard.consume_pending(t3, "prod", "create", "res.partner", now=5.0)
    # The error must spell out that dry-runs DON'T count. Agents in the
    # field were defensively shrinking batch sizes because the message
    # didn't say so — wasting throughput the burst budget was meant to
    # allow. Pin the language explicitly.
    msg = str(excinfo.value)
    assert "Dry-runs do NOT count" in msg or "dry-runs do not count" in msg.lower()
    assert "successful commits" in msg.lower()


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


def test_renewal_while_active_keeps_tokens_valid() -> None:
    """Re-unlocking while the window is still active renews it in place:
    the budget resets but the window identity is preserved, so tokens
    issued before the renewal stay consumable. This is the fix for the
    field-observed churn where a burst-limit renewal forced agents to
    re-do dry runs they had already reviewed.
    """
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0, max_commits=1)
    token = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    # Renew mid-window (e.g. after hitting the burst limit).
    guard.unlock("prod", production=True, now=10.0, max_commits=5)
    assert guard.commits_remaining("prod", now=10.0) == 5
    # The pre-renewal token commits fine under the renewed window.
    guard.consume_pending(token, "prod", "create", "res.partner", now=11.0)
    assert guard.commits_remaining("prod", now=12.0) == 4


def test_renewal_after_expiry_still_invalidates_tokens() -> None:
    """An expired window gets a fresh identity on re-unlock — renewal
    in place applies only while the previous window is still active.
    Keeps the H1 property: stale tokens never survive a real expiry.
    """
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    token = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    # Lapse the window early (simulated — keeps the token inside its own
    # 5-min TTL so the *window* check is what fires, not token expiry).
    guard._unlocked["prod"].expires_at = 5.0  # noqa: SLF001 — test stages the expiry
    # Re-unlock after the lapse: unlock() sees an expired state and must
    # mint a fresh identity instead of renewing in place.
    guard.unlock("prod", production=True, now=10.0)
    with pytest.raises(ProdGuardError, match="different unlock window"):
        guard.consume_pending(token, "prod", "create", "res.partner", now=11.0)


def test_burst_error_promises_token_survival() -> None:
    """The burst-limit error tells the agent its tokens survive the
    renewal — pin that language so the advice stays true and stated."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0, max_commits=0)
    token = guard.create_pending("prod", "create", "res.partner", "s", now=0.0)
    with pytest.raises(ProdGuardError, match="stay valid across the renewal"):
        guard.consume_pending(token, "prod", "create", "res.partner", now=1.0)


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


# --- payload digest binding --------------------------------------------------


def test_payload_digest_is_canonical() -> None:
    """Key order in the payload dict must not affect the digest.

    The dispatcher computes the digest from ``args.get(k) for k in ...``,
    which yields a Python dict whose insertion order can in principle
    differ between the dry-run call and the commit call (e.g. a client
    re-serialising JSON differently). The digest must collapse those.
    """
    a = compute_payload_digest({"ids": [1, 2, 3], "values": {"name": "X"}})
    b = compute_payload_digest({"values": {"name": "X"}, "ids": [1, 2, 3]})
    assert a == b


def test_payload_digest_distinguishes_extra_ids() -> None:
    """The exact scope-upgrade attack: same model+op, more ids."""
    narrow = compute_payload_digest({"ids": [1], "values": {"active": False}})
    wide = compute_payload_digest({"ids": [1, 2, 3], "values": {"active": False}})
    assert narrow != wide


def test_payload_digest_distinguishes_swapped_values() -> None:
    base = compute_payload_digest({"ids": [1], "values": {"name": "Alice"}})
    swap = compute_payload_digest({"ids": [1], "values": {"name": "Eve"}})
    assert base != swap


def test_payload_digest_distinguishes_added_partner() -> None:
    base = compute_payload_digest({"record_id": 1, "partner_ids": [10]})
    add = compute_payload_digest({"record_id": 1, "partner_ids": [10, 11]})
    assert base != add


def test_payload_digest_distinguishes_mode_swap() -> None:
    """Archive vs. delete is the dangerous mode swap to catch."""
    arch = compute_payload_digest({"ids": [1, 2], "mode": "archive"})
    delete = compute_payload_digest({"ids": [1, 2], "mode": "delete"})
    assert arch != delete


def test_token_rejects_extra_ids_on_write() -> None:
    """An agent that previewed ``ids=[1]`` cannot commit ``ids=[1..1000]``
    using the same token. This is the AlanOgic C1 attack on write."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    issued_digest = compute_payload_digest({"ids": [1], "values": {"active": False}})
    token = guard.create_pending(
        "prod",
        "write",
        "res.partner",
        "s",
        now=0.0,
        payload_digest=issued_digest,
    )
    wider_digest = compute_payload_digest(
        {"ids": list(range(1, 1001)), "values": {"active": False}}
    )
    with pytest.raises(ProdGuardError, match="different payload"):
        guard.consume_pending(
            token, "prod", "write", "res.partner", now=1.0, payload_digest=wider_digest
        )


def test_token_rejects_swapped_values_on_create() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    preview = compute_payload_digest({"values": {"name": "Alice", "email": "a@x"}})
    token = guard.create_pending(
        "prod",
        "create",
        "res.partner",
        "s",
        now=0.0,
        payload_digest=preview,
    )
    commit = compute_payload_digest({"values": {"name": "Eve", "email": "eve@evil"}})
    with pytest.raises(ProdGuardError, match="different payload"):
        guard.consume_pending(
            token, "prod", "create", "res.partner", now=1.0, payload_digest=commit
        )


def test_token_rejects_mode_swap_archive_to_delete() -> None:
    """The same token must not let archive(1,2) become delete(1,2)."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    preview = compute_payload_digest({"ids": [1, 2], "mode": "archive"})
    token = guard.create_pending(
        "prod",
        "archive",
        "res.partner",
        "s",
        now=0.0,
        payload_digest=preview,
    )
    commit = compute_payload_digest({"ids": [1, 2], "mode": "delete"})
    with pytest.raises(ProdGuardError, match="different payload"):
        guard.consume_pending(
            token, "prod", "archive", "res.partner", now=1.0, payload_digest=commit
        )


def test_token_rejects_added_partner_on_send_message() -> None:
    """An agent that previewed sending to one partner cannot fan out
    to extra recipients using the issued token."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    preview = compute_payload_digest(
        {
            "record_id": 1,
            "body": "Hello",
            "subject": None,
            "partner_ids": [10],
            "message_type": "comment",
        }
    )
    token = guard.create_pending(
        "prod",
        "send_message",
        "res.partner",
        "s",
        now=0.0,
        payload_digest=preview,
    )
    commit = compute_payload_digest(
        {
            "record_id": 1,
            "body": "Hello",
            "subject": None,
            "partner_ids": [10, 11, 12],
            "message_type": "comment",
        }
    )
    with pytest.raises(ProdGuardError, match="different payload"):
        guard.consume_pending(
            token, "prod", "send_message", "res.partner", now=1.0, payload_digest=commit
        )


def test_token_rejects_changed_action_on_document_action() -> None:
    """Swapping `confirm` for `cancel` (or vice versa) on the same
    records must invalidate the token."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    preview = compute_payload_digest({"record_ids": [1, 2], "action": "confirm"})
    token = guard.create_pending(
        "prod",
        "document_action",
        "sale.order",
        "s",
        now=0.0,
        payload_digest=preview,
    )
    commit = compute_payload_digest({"record_ids": [1, 2], "action": "cancel"})
    with pytest.raises(ProdGuardError, match="different payload"):
        guard.consume_pending(
            token, "prod", "document_action", "sale.order", now=1.0, payload_digest=commit
        )


def test_token_accepts_identical_payload() -> None:
    """Happy path: re-call with the exact same payload commits cleanly."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    digest = compute_payload_digest({"ids": [1, 2], "values": {"active": False}})
    token = guard.create_pending(
        "prod", "write", "res.partner", "s", now=0.0, payload_digest=digest
    )
    # Same payload digest recomputed from same inputs.
    guard.consume_pending(token, "prod", "write", "res.partner", now=1.0, payload_digest=digest)


def test_payload_digest_error_does_not_echo_token_value() -> None:
    """The payload-mismatch error must not leak the token (audit retention)."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    preview = compute_payload_digest({"ids": [1], "values": {"x": 1}})
    token = guard.create_pending(
        "prod", "write", "res.partner", "s", now=0.0, payload_digest=preview
    )
    commit = compute_payload_digest({"ids": [1, 2], "values": {"x": 1}})
    try:
        guard.consume_pending(token, "prod", "write", "res.partner", now=1.0, payload_digest=commit)
    except ProdGuardError as exc:
        assert token not in str(exc)
        assert "different payload" in str(exc).lower()
    else:
        raise AssertionError("expected ProdGuardError")


def test_consume_skips_digest_check_when_token_has_none() -> None:
    """No-binding path: legacy unit-test callers of consume_pending that
    don't supply payload_digest at issue time keep working (the dispatcher
    always supplies one in production)."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=0.0)
    token = guard.create_pending("prod", "write", "res.partner", "s", now=0.0)
    # No payload binding at issue → consume can pass any digest, including None.
    guard.consume_pending(token, "prod", "write", "res.partner", now=1.0)
