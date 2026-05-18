"""Tests for the odoo_default_get tool.

``odoo_default_get`` is the read-only companion to ``odoo_create``: it
wraps Odoo's ``default_get`` so a caller can preview the values Odoo
would auto-fill on a new record. It must go through the same model
allowlist + field-policy + redaction pipeline as every other read.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from odoo_mcp.dispatcher import Dispatcher
from odoo_mcp.tools import build_tools

_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "name": {"type": "char"},
    "company_id": {"type": "many2one"},
    "currency_id": {"type": "many2one"},
    "vat": {"type": "char"},
    "access_token": {"type": "char"},
}


class _FakeClient:
    """Minimal stand-in for OdooClient covering the default_get path."""

    def __init__(
        self,
        defaults: dict[str, Any] | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._defaults = defaults if defaults is not None else {"company_id": 1, "currency_id": 2}
        # Entries returned regardless of the requested field list — used to
        # simulate Odoo volunteering a field, so the response-side redaction
        # layer can be exercised.
        self._extra = extra or {}
        self.default_get_calls: list[tuple[str, list[str]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _FIELDS

    def default_get(self, model: str, fields: list[str]) -> dict[str, Any]:
        self.default_get_calls.append((model, list(fields)))
        # Real Odoo only returns entries for requested fields that have a
        # default — mirror that here, plus any forced `extra` entries.
        out = {k: v for k, v in self._defaults.items() if k in fields}
        out.update(self._extra)
        return out


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_default_get", args))
    assert len(contents) == 1
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def test_default_get_registered() -> None:
    names = [t.name for t in build_tools()]
    assert "odoo_default_get" in names


def test_default_get_returns_defaults_and_missing(
    make_app: Callable[..., Any],
) -> None:
    fake = _FakeClient()
    app = make_app(client=fake)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "sale.order",
            "fields": ["company_id", "currency_id", "name"],
        },
    )
    assert payload["ok"] is True
    assert payload["model"] == "sale.order"
    assert payload["defaults"] == {"company_id": 1, "currency_id": 2}
    # `name` was requested but Odoo returned no default for it.
    assert payload["fields_without_default"] == ["name"]


def test_default_get_forwards_exact_field_list(
    make_app: Callable[..., Any],
) -> None:
    fake = _FakeClient()
    app = make_app(client=fake)
    _call(
        Dispatcher(app),
        {"instance": "dev", "model": "sale.order", "fields": ["company_id"]},
    )
    assert fake.default_get_calls == [("sale.order", ["company_id"])]


def test_default_get_rejects_unknown_field(make_app: Callable[..., Any]) -> None:
    fake = _FakeClient()
    app = make_app(client=fake)
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "sale.order", "fields": ["does_not_exist"]},
    )
    assert payload["ok"] is False
    assert "does_not_exist" in payload["error"]
    # Field validation runs before Odoo is contacted.
    assert fake.default_get_calls == []


def test_default_get_rejects_denied_model(make_app: Callable[..., Any]) -> None:
    fake = _FakeClient()
    app = make_app(client=fake)
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.users", "fields": ["name"]},
    )
    assert payload["ok"] is False
    assert "denylist" in payload["error"].lower()
    assert fake.default_get_calls == []


def test_default_get_sensitive_field_requires_optin(
    make_app: Callable[..., Any],
) -> None:
    fake = _FakeClient(defaults={"vat": "BE0123"})
    app = make_app(client=fake)
    # `vat` on res.partner is default-hidden — refused without opt-in.
    refused = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "res.partner", "fields": ["vat"]},
    )
    assert refused["ok"] is False
    assert "sensitive" in refused["error"].lower()
    # With the explicit opt-in it comes through.
    allowed = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "fields": ["vat"],
            "allow_sensitive_fields": ["vat"],
        },
    )
    assert allowed["ok"] is True
    assert allowed["defaults"] == {"vat": "BE0123"}


def test_default_get_redacts_always_redacted_in_response(
    make_app: Callable[..., Any],
) -> None:
    # Defense in depth: even if Odoo returns a default sitting on an
    # always-redacted field, the redaction pipeline drops it. `access_token`
    # is never requestable, so simulate Odoo volunteering it.
    fake = _FakeClient(defaults={"name": "Draft"}, extra={"access_token": "secret"})
    app = make_app(client=fake)
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "sale.order", "fields": ["name"]},
    )
    assert payload["ok"] is True
    assert "access_token" not in payload["defaults"]


def test_default_get_requires_non_empty_fields(
    make_app: Callable[..., Any],
) -> None:
    fake = _FakeClient()
    app = make_app(client=fake)
    payload = _call(
        Dispatcher(app),
        {"instance": "dev", "model": "sale.order", "fields": []},
    )
    assert payload["ok"] is False
    assert fake.default_get_calls == []
