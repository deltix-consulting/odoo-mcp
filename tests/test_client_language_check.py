"""Sign-in refuses a configured ``language`` the Odoo database has not activated.

``config.py`` validates only the code's shape. Odoo 16/17 silently fall back
to English for an inactive code and 18+ raise ``Invalid language code`` on
every call, so ``OdooClient.authenticate`` checks ``res.lang.get_installed``
once and refuses loudly, without leaving a usable cached uid behind.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from odoo_mcp.client import OdooClient
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import ConfigError, OdooRemoteError, OdooTransportError

_INSTALLED = [["nl_BE", "Dutch (BE) / Nederlands (BE)"], ["en_US", "English (US)"]]


def _client(
    make_instance_config: Callable[..., Any],
    *,
    language: str,
    get_installed: Callable[[], Any],
) -> tuple[OdooClient, list[str]]:
    cfg = make_instance_config(language=language)
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    calls: list[str] = []

    class _StubCommon:
        def authenticate(self, *_a: Any, **_k: Any) -> int:
            calls.append("authenticate")
            return 7

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        calls.append(f"{model}.{method}")
        if (model, method) == ("res.users", "has_group"):
            return False
        if (model, method) == ("res.lang", "get_installed"):
            return get_installed()
        if (model, method) == ("res.partner", "search_count"):
            return 3
        raise AssertionError(f"unexpected call {model}.{method}")

    client._common = _StubCommon()  # type: ignore[assignment]
    client._execute = _execute  # type: ignore[assignment]
    return client, calls


def test_en_us_costs_no_round_trip(make_instance_config: Callable[..., Any]) -> None:
    client, calls = _client(
        make_instance_config,
        language="en_US",
        get_installed=lambda: pytest.fail("en_US must not be checked"),
    )
    assert client.authenticate() == 7
    assert "res.lang.get_installed" not in calls


def test_installed_language_passes(make_instance_config: Callable[..., Any]) -> None:
    client, calls = _client(
        make_instance_config, language="nl_BE", get_installed=lambda: _INSTALLED
    )
    assert client.authenticate() == 7
    assert "res.lang.get_installed" in calls


def test_tuple_rows_are_accepted(make_instance_config: Callable[..., Any]) -> None:
    rows = [("nl_BE", "Dutch"), ("en_US", "English")]
    client, _ = _client(make_instance_config, language="nl_BE", get_installed=lambda: rows)
    assert client.authenticate() == 7


def test_inactive_language_refused_with_active_list(
    make_instance_config: Callable[..., Any],
) -> None:
    client, _ = _client(make_instance_config, language="nl_NL", get_installed=lambda: _INSTALLED)
    with pytest.raises(ConfigError) as info:
        client.authenticate()
    msg = info.value.user_message
    assert "'nl_NL'" in msg
    assert "en_US, nl_BE" in msg
    assert "Settings > Languages" in msg


def test_refusal_does_not_leave_a_cached_uid(make_instance_config: Callable[..., Any]) -> None:
    """The second call must re-run sign-in and refuse again, not run silently."""
    client, calls = _client(
        make_instance_config, language="nl_NL", get_installed=lambda: _INSTALLED
    )
    with pytest.raises(ConfigError):
        client.ensure_authenticated()
    assert client._uid is None  # type: ignore[attr-defined]
    with pytest.raises(ConfigError):
        client.ensure_authenticated()
    assert calls.count("authenticate") == 2


def test_odoo_18_invalid_language_fault_refuses(
    make_instance_config: Callable[..., Any],
) -> None:
    """18+ may raise on the probe itself because the context carries the bad code."""

    def _raise() -> Any:
        raise OdooRemoteError(
            "Odoo fault on res.lang.get_installed: Invalid language code: xx_XX",
            server_text=True,
        )

    client, _ = _client(make_instance_config, language="xx_XX", get_installed=_raise)
    with pytest.raises(ConfigError) as info:
        client.authenticate()
    assert "Active languages: unknown" in info.value.user_message
    assert client._uid is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "exc",
    [
        OdooRemoteError("Odoo fault on res.lang.get_installed: AccessError", server_text=True),
        OdooTransportError("Timeout calling res.lang.get_installed"),
    ],
)
def test_probe_failure_for_another_reason_does_not_block_sign_in(
    make_instance_config: Callable[..., Any], exc: Exception
) -> None:
    def _raise() -> Any:
        raise exc

    client, _ = _client(make_instance_config, language="nl_BE", get_installed=_raise)
    assert client.authenticate() == 7


def test_probe_goes_over_execute_kw_with_no_args(
    make_instance_config: Callable[..., Any],
) -> None:
    """Wire shape: ``execute_kw(db, uid, key, 'res.lang', 'get_installed', [], {context})``."""
    cfg = make_instance_config(language="nl_BE")
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    seen: list[tuple[str, str, list[Any], dict[str, Any]]] = []

    class _StubCommon:
        def authenticate(self, *_a: Any, **_k: Any) -> int:
            return 7

    def _execute_kw(
        db: str,
        uid: int,
        key: str,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        seen.append((model, method, args, kwargs))
        if method == "has_group":
            return False
        return _INSTALLED

    client._common = _StubCommon()  # type: ignore[assignment]
    client._object.execute_kw = _execute_kw  # type: ignore[method-assign]
    client.authenticate()
    assert ("res.lang", "get_installed", [], {"context": {"lang": "nl_BE"}}) in seen
