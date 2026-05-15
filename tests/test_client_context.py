"""The Odoo call context carries the instance's configured locale.

``OdooClient._execute`` is the single chokepoint that calls ``execute_kw``.
It must inject a ``context`` built from the instance's operator-configured
``language`` — and nothing the caller supplied.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from odoo_mcp.client import OdooClient
from odoo_mcp.credentials import Credentials


def _client_capturing_kwargs(
    make_instance_config: Callable[..., Any],
    **inst_overrides: Any,
) -> tuple[OdooClient, dict[str, Any]]:
    """Build an authenticated-looking client whose execute_kw is a spy."""
    cfg = make_instance_config(**inst_overrides)
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    client._uid = 1  # type: ignore[attr-defined]
    captured: dict[str, Any] = {}

    def _fake_execute_kw(
        db: str,
        uid: int,
        key: str,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> int:
        captured["kwargs"] = kwargs
        return 0

    client._object.execute_kw = _fake_execute_kw  # type: ignore[method-assign]
    return client, captured


def test_context_defaults_to_en_us(make_instance_config: Callable[..., Any]) -> None:
    client, captured = _client_capturing_kwargs(make_instance_config)
    client.search_count("res.partner", [])
    assert captured["kwargs"]["context"] == {"lang": "en_US"}


def test_context_uses_configured_language(
    make_instance_config: Callable[..., Any],
) -> None:
    client, captured = _client_capturing_kwargs(make_instance_config, language="nl_BE")
    client.search_count("res.partner", [])
    assert captured["kwargs"]["context"] == {"lang": "nl_BE"}


def test_context_is_a_fresh_copy_per_call(
    make_instance_config: Callable[..., Any],
) -> None:
    """Mutating the context Odoo received must not corrupt later calls."""
    client, captured = _client_capturing_kwargs(make_instance_config, language="fr_FR")
    client.search_count("res.partner", [])
    captured["kwargs"]["context"]["lang"] = "tampered"
    client.search_count("res.partner", [])
    assert captured["kwargs"]["context"] == {"lang": "fr_FR"}
