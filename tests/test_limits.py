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
