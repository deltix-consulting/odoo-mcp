"""Tests for rate limiting and record-count clamping."""

from __future__ import annotations

import pytest

from odoo_mcp.errors import LimitExceededError
from odoo_mcp.security.limits import RateLimiter, clamp_limit


def test_clamp_limit_defaults() -> None:
    assert clamp_limit(None, default=50, hard_cap=500) == 50


def test_clamp_limit_caps_above_hard_cap() -> None:
    assert clamp_limit(10_000, default=50, hard_cap=500) == 500


def test_clamp_limit_rejects_zero_and_negative() -> None:
    with pytest.raises(LimitExceededError):
        clamp_limit(0, default=50, hard_cap=500)
    with pytest.raises(LimitExceededError):
        clamp_limit(-1, default=50, hard_cap=500)


def test_clamp_limit_rejects_non_int() -> None:
    with pytest.raises(LimitExceededError):
        clamp_limit("100", default=50, hard_cap=500)  # type: ignore[arg-type]
    with pytest.raises(LimitExceededError):
        clamp_limit(True, default=50, hard_cap=500)


def test_rate_limiter_consumes_tokens() -> None:
    rl = RateLimiter()
    rl.configure("dev", rate_per_minute=60)
    # Start with a full bucket; 60 consecutive calls should succeed.
    for _ in range(60):
        rl.take("dev", now=0.0)
    # The 61st exceeds the bucket.
    with pytest.raises(LimitExceededError, match="Rate limit"):
        rl.take("dev", now=0.0)


def test_rate_limiter_refills_over_time() -> None:
    rl = RateLimiter()
    rl.configure("dev", rate_per_minute=60)
    # Drain.
    for _ in range(60):
        rl.take("dev", now=0.0)
    with pytest.raises(LimitExceededError):
        rl.take("dev", now=0.0)
    # 1 second later, 1 token has refilled (60/min = 1/sec).
    rl.take("dev", now=1.0)


def test_rate_limiter_per_instance_isolation() -> None:
    rl = RateLimiter()
    rl.configure("dev", rate_per_minute=5)
    rl.configure("prod", rate_per_minute=5)
    for _ in range(5):
        rl.take("dev", now=0.0)
    # dev is now empty.
    with pytest.raises(LimitExceededError):
        rl.take("dev", now=0.0)
    # prod still full.
    for _ in range(5):
        rl.take("prod", now=0.0)


def test_rate_limiter_unknown_instance_rejected() -> None:
    rl = RateLimiter()
    with pytest.raises(LimitExceededError, match="No rate limiter"):
        rl.take("ghost")


def test_peek_does_not_consume() -> None:
    rl = RateLimiter()
    rl.configure("dev", rate_per_minute=60)
    # Peek multiple times — bucket stays full.
    assert rl.peek("dev", now=0.0) == 60.0
    assert rl.peek("dev", now=0.0) == 60.0
    # Consume one, peek reflects it.
    rl.take("dev", now=0.0)
    assert rl.peek("dev", now=0.0) == 59.0


def test_peek_reflects_refill_without_consuming() -> None:
    rl = RateLimiter()
    rl.configure("dev", rate_per_minute=60)
    for _ in range(60):
        rl.take("dev", now=0.0)
    # 10s later, 10 tokens should have refilled (60/min = 1/s).
    assert rl.peek("dev", now=10.0) == pytest.approx(10.0)
    # A second peek at the same timestamp returns the same number
    # (no consumption).
    assert rl.peek("dev", now=10.0) == pytest.approx(10.0)


def test_peek_unknown_instance() -> None:
    rl = RateLimiter()
    with pytest.raises(LimitExceededError, match="No rate limiter"):
        rl.peek("ghost")


def test_offset_rejects_string_value() -> None:
    """`offset='10'` is no longer silently coerced to int(10).

    Prior to v0.8.0 the dispatcher ran ``int(args.get('offset') or 0)``
    which accepted strings and floats. The new ``_require_int_or_default``
    helper enforces real int.
    """
    from odoo_mcp.dispatcher import _offset
    from odoo_mcp.errors import OdooMcpError

    with pytest.raises(OdooMcpError, match="offset must be an integer"):
        _offset({"offset": "10"})


def test_offset_rejects_float_value() -> None:
    from odoo_mcp.dispatcher import _offset
    from odoo_mcp.errors import OdooMcpError

    with pytest.raises(OdooMcpError, match="offset must be an integer"):
        _offset({"offset": 10.5})


def test_offset_rejects_bool_value() -> None:
    """Bool subclasses int — explicitly rejected so True doesn't sneak in as 1."""
    from odoo_mcp.dispatcher import _offset
    from odoo_mcp.errors import OdooMcpError

    with pytest.raises(OdooMcpError, match="offset must be an integer"):
        _offset({"offset": True})


def test_offset_accepts_none_or_omitted() -> None:
    from odoo_mcp.dispatcher import _offset

    assert _offset({}) == 0
    assert _offset({"offset": None}) == 0
    assert _offset({"offset": 5}) == 5


def test_offset_rejects_negative_int() -> None:
    from odoo_mcp.dispatcher import _offset
    from odoo_mcp.errors import OdooMcpError

    with pytest.raises(OdooMcpError, match="offset must be >= 0"):
        _offset({"offset": -1})
