"""The production admin refusal must hold on every call, not only the first.

``OdooClient.authenticate`` stores the uid before the admin check runs (the
``has_group`` probe goes through ``_execute``, which needs it), and
``ensure_authenticated`` skips authenticate() whenever a uid is cached. The
client lives for the whole MCP session, so a uid left behind by a refused
sign-in let the next tool call (an agent's ordinary retry) run with the very
admin key that was just refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_mcp.client import OdooClient
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import OdooAuthError


def _cfg(*, production: bool = True, refuse_admin: bool = True) -> InstanceConfig:
    return InstanceConfig(
        name="prod" if production else "dev",
        url="https://prod.example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_TEST",
        production=production,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=60,
        allow_self_signed=False,
        allowed_models=frozenset({"res.partner"}),
        refuse_admin_on_production=refuse_admin,
    )


class _Counter:
    def __init__(self) -> None:
        self.authenticate = 0
        self.has_group = 0


def _client(
    cfg: InstanceConfig, *, uid: int, has_group: bool | Exception
) -> tuple[OdooClient, _Counter]:
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    calls = _Counter()

    class _StubCommon:
        def authenticate(self, *_args: Any, **_kw: Any) -> int:
            calls.authenticate += 1
            return uid

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        if model == "res.users" and method == "has_group":
            calls.has_group += 1
            if isinstance(has_group, Exception):
                raise has_group
            return has_group
        raise AssertionError(f"unexpected execute_kw call: {model}.{method}")

    client._common = _StubCommon()  # type: ignore[assignment]
    client._execute = _execute  # type: ignore[assignment]
    return client, calls


@pytest.mark.parametrize(("uid", "has_group"), [(1, False), (7, True)])
def test_refusal_holds_on_the_retry(uid: int, has_group: bool) -> None:
    """Superuser and group_system alike: the second call is refused too."""
    client, calls = _client(_cfg(), uid=uid, has_group=has_group)
    for _ in range(3):
        with pytest.raises(OdooAuthError, match="Refusing to use admin credentials"):
            client.ensure_authenticated()
    # Each retry signs in again instead of reusing a refused session.
    assert calls.authenticate == 3


def test_refused_client_keeps_no_uid() -> None:
    client, _ = _client(_cfg(), uid=1, has_group=False)
    with pytest.raises(OdooAuthError):
        client.authenticate()
    with pytest.raises(OdooAuthError, match="has not been authenticated"):
        _ = client.uid


def test_tool_calls_after_a_refusal_never_reach_odoo() -> None:
    """The bug's real shape: a data call after the refused one went through."""
    client, _ = _client(_cfg(), uid=1, has_group=False)
    seen: list[str] = []

    def _execute_kw(*args: Any) -> list[dict[str, Any]]:
        seen.append(f"{args[3]}.{args[4]}")
        return []

    client._object.execute_kw = _execute_kw  # type: ignore[method-assign]
    for _ in range(2):
        with pytest.raises(OdooAuthError):
            client.ensure_authenticated()
    assert seen == []


def test_admin_on_dev_still_signs_in_once() -> None:
    """Non-production keeps the warn-only behaviour and caches the uid."""
    client, calls = _client(_cfg(production=False), uid=1, has_group=False)
    client.ensure_authenticated()
    client.ensure_authenticated()
    assert client.uid == 1
    assert client.is_admin is True
    assert calls.authenticate == 1


def test_opted_out_production_still_signs_in_once() -> None:
    client, calls = _client(_cfg(refuse_admin=False), uid=7, has_group=True)
    client.ensure_authenticated()
    client.ensure_authenticated()
    assert client.uid == 7
    assert calls.authenticate == 1


def test_regular_user_on_production_signs_in_once() -> None:
    client, calls = _client(_cfg(), uid=7, has_group=False)
    client.ensure_authenticated()
    client.ensure_authenticated()
    assert client.uid == 7
    assert client.is_admin is False
    assert calls.authenticate == 1
    assert calls.has_group == 1


def test_unexpected_error_in_the_check_also_forgets_the_uid() -> None:
    """Anything escaping the post-login checks leaves no half-signed-in client."""
    client, calls = _client(_cfg(), uid=7, has_group=RuntimeError("boom"))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="boom"):
            client.ensure_authenticated()
    assert calls.authenticate == 2
    with pytest.raises(OdooAuthError, match="has not been authenticated"):
        _ = client.uid
