"""Tests for the loyalty-card email gate on odoo_create / odoo_write.

A loyalty.card mails its customer from inside create/write when its program
has a communication plan (``loyalty.mail``), 16.0 to 20.0: the "At Creation"
template on create (the coupon or gift card code), and the "When Reaching"
template when a points increase crosses a milestone (``loyalty.card.write``
up to 19.0, ``loyalty.history.create`` from 20.0). These tests pin that this
needs the same two external-comms opt-ins as ``odoo_send_message``, before
the dry run, and that payloads Odoo sends nothing for stay allowed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from odoo_mcp._odoo_reference import ODOO_STANDARD_FIELDS
from odoo_mcp.dispatcher import (
    _LOYALTY_CARD_MODEL,
    _LOYALTY_HISTORY_MODEL,
    _LOYALTY_MAIL_MODEL,
    Dispatcher,
)

_MAILING_PROGRAM = 3  # "At Creation" plan + a milestone at 100 points
_QUIET_PROGRAM = 4  # no communication plan
_MILESTONE = 100.0

_CARD_META: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "code": {"type": "char"},
    "points": {"type": "float"},
    "partner_id": {"type": "many2one", "relation": "res.partner"},
    "program_id": {"type": "many2one", "relation": "loyalty.program"},
}
_HISTORY_META_19: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "card_id": {"type": "many2one", "relation": "loyalty.card"},
    "issued": {"type": "float"},
    "used": {"type": "float"},
}
_HISTORY_META_20 = {
    **_HISTORY_META_19,
    "linked_loyalty_history_id": {"type": "many2one", "relation": "loyalty.history"},
}


class _FakeClient:
    def __init__(self, *, card_points: float, card_partner: int | None, odoo20: bool) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.card_points = card_points
        self.card_partner = card_partner
        self.meta: dict[str, dict[str, dict[str, Any]]] = {
            _LOYALTY_CARD_MODEL: _CARD_META,
            _LOYALTY_HISTORY_MODEL: _HISTORY_META_20 if odoo20 else _HISTORY_META_19,
            "loyalty.program": {
                "id": {"type": "integer"},
                "name": {"type": "char"},
                "coupon_ids": {"type": "one2many", "relation": _LOYALTY_CARD_MODEL},
            },
        }
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.written: list[tuple[str, list[int], dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self.meta[model]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        assert model == _LOYALTY_CARD_MODEL
        assert set(fields) <= set(ODOO_STANDARD_FIELDS[model]), fields  # (#62)
        row = {
            "points": self.card_points,
            "partner_id": [self.card_partner, "P"] if self.card_partner else False,
            "program_id": [_MAILING_PROGRAM, "Gift"],
        }
        return [{"id": i, **{f: row[f] for f in fields}} for i in ids]

    def _check_domain(self, model: str, domain: list[Any]) -> dict[str, Any]:
        assert model == _LOYALTY_MAIL_MODEL
        for name, _op, _value in domain:
            assert name in ODOO_STANDARD_FIELDS[model], name  # (#62)
        return {name: value for name, _op, value in domain}

    def search_count(self, model: str, domain: list[Any]) -> int:
        leaves = self._check_domain(model, domain)
        return 1 if leaves["program_id"] == _MAILING_PROGRAM else 0

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
    ) -> list[dict[str, Any]]:
        leaves = self._check_domain(model, domain)
        assert set(fields) <= set(ODOO_STANDARD_FIELDS[model]), fields
        assert leaves["trigger"] == "points_reach"
        if _MAILING_PROGRAM not in leaves["program_id"]:
            return []
        return [{"id": 9, "program_id": [_MAILING_PROGRAM, "Gift"], "points": _MILESTONE}]

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.created.append((model, values))
        return 42

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.written.append((model, ids, values))
        return True


@pytest.fixture
def build(
    make_app: Callable[..., Any],
) -> Callable[..., tuple[Dispatcher, _FakeClient]]:
    def _build(
        *,
        external_comms_enabled: bool = False,
        card_points: float = 50.0,
        card_partner: int | None = 7,
        odoo20: bool = False,
    ) -> tuple[Dispatcher, _FakeClient]:
        fake = _FakeClient(card_points=card_points, card_partner=card_partner, odoo20=odoo20)
        app = make_app(client=fake, external_comms_enabled=external_comms_enabled)
        return Dispatcher(app), fake

    return _build


@pytest.fixture(autouse=True)
def _no_comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, {"instance": "dev", **args}))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _create(model: str, values: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    return {"model": model, "values": values, "dry_run": dry_run}


def _top_up(values: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    return {"model": _LOYALTY_CARD_MODEL, "ids": [5], "values": values, "dry_run": dry_run}


def _assert_refused(payload: dict[str, Any], phrase: str) -> None:
    assert payload["ok"] is False
    assert payload["error_code"] == "prod_guard"
    assert phrase in payload["error"]
    assert "ODOO_MCP_ENABLE_EXTERNAL_COMMS" in payload["error"]


# --- refused without the opt-ins ---------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_create_card_for_customer_refused(build: Any, dry_run: bool) -> None:
    disp, fake = build()
    values = {"program_id": _MAILING_PROGRAM, "partner_id": 7}
    payload = _call(disp, "odoo_create", _create(_LOYALTY_CARD_MODEL, values, dry_run=dry_run))
    _assert_refused(payload, "loyalty emails to the customer")
    assert fake.created == []


def test_nested_card_under_program_fails_closed(build: Any) -> None:
    # A (0, 0, vals) under loyalty.program has no program_id of its own yet.
    disp, fake = build()
    values = {"name": "Gift", "coupon_ids": [[0, 0, {"partner_id": 7}]]}
    payload = _call(disp, "odoo_create", _create("loyalty.program", values))
    _assert_refused(payload, "loyalty emails to the customer")
    assert fake.created == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_points_past_milestone_refused(build: Any, dry_run: bool) -> None:
    disp, fake = build(card_points=50.0)
    payload = _call(disp, "odoo_write", _top_up({"points": 150.0}, dry_run=dry_run))
    _assert_refused(payload, "'When Reaching' email")
    assert fake.written == []


def test_history_line_past_milestone_refused_on_20(build: Any) -> None:
    disp, fake = build(card_points=50.0, odoo20=True)
    values = {"card_id": 5, "issued": 60.0}
    payload = _call(disp, "odoo_create", _create(_LOYALTY_HISTORY_MODEL, values))
    _assert_refused(payload, "'When Reaching' email")
    assert fake.created == []


# --- allowed: Odoo sends nothing ---------------------------------------------


def test_create_card_without_customer_allowed(build: Any) -> None:
    disp, fake = build()
    payload = _call(disp, "odoo_create", _create(_LOYALTY_CARD_MODEL, {"program_id": 3}))
    assert payload["ok"] is True, payload
    assert "odoo_will_email" not in payload
    assert fake.created


def test_create_card_on_quiet_program_allowed(build: Any) -> None:
    disp, fake = build()
    values = {"program_id": _QUIET_PROGRAM, "partner_id": 7, "points": 500.0}
    payload = _call(disp, "odoo_create", _create(_LOYALTY_CARD_MODEL, values))
    assert payload["ok"] is True, payload
    assert fake.created


@pytest.mark.parametrize("new_points", [80.0, 20.0, 50.0])
def test_points_below_milestone_or_down_allowed(build: Any, new_points: float) -> None:
    disp, fake = build(card_points=50.0)
    payload = _call(disp, "odoo_write", _top_up({"points": new_points}))
    assert payload["ok"] is True, payload
    assert fake.written


def test_points_already_past_milestone_allowed(build: Any) -> None:
    disp, fake = build(card_points=120.0)
    payload = _call(disp, "odoo_write", _top_up({"points": 300.0}))
    assert payload["ok"] is True, payload


def test_points_on_card_without_customer_allowed(build: Any) -> None:
    disp, fake = build(card_partner=None)
    payload = _call(disp, "odoo_write", _top_up({"points": 150.0}))
    assert payload["ok"] is True, payload


def test_clearing_customer_in_same_write_allowed(build: Any) -> None:
    disp, fake = build(card_points=50.0)
    payload = _call(disp, "odoo_write", _top_up({"points": 150.0, "partner_id": False}))
    assert payload["ok"] is True, payload


def test_write_without_points_allowed(build: Any) -> None:
    disp, fake = build()
    payload = _call(disp, "odoo_write", _top_up({"code": "X"}))
    assert payload["ok"] is True, payload


def test_history_line_before_20_allowed(build: Any) -> None:
    # Up to 19.0 loyalty.history.create sends nothing.
    disp, fake = build(card_points=50.0, odoo20=False)
    values = {"card_id": 5, "issued": 60.0}
    payload = _call(disp, "odoo_create", _create(_LOYALTY_HISTORY_MODEL, values))
    assert payload["ok"] is True, payload


def test_history_used_line_allowed_on_20(build: Any) -> None:
    disp, fake = build(card_points=150.0, odoo20=True)
    values = {"card_id": 5, "used": 60.0}
    payload = _call(disp, "odoo_create", _create(_LOYALTY_HISTORY_MODEL, values))
    assert payload["ok"] is True, payload


# --- with the opt-ins: allowed and disclosed ---------------------------------


def test_opted_in_create_discloses(build: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")
    disp, fake = build(external_comms_enabled=True)
    values = {"program_id": _MAILING_PROGRAM, "partner_id": 7}
    payload = _call(disp, "odoo_create", _create(_LOYALTY_CARD_MODEL, values))
    assert payload["ok"] is True, payload
    assert any("loyalty emails" in line for line in payload["odoo_will_email"])
    assert fake.created


def test_opted_in_top_up_preview_discloses(build: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")
    disp, fake = build(external_comms_enabled=True, card_points=50.0)
    payload = _call(disp, "odoo_write", _top_up({"points": 150.0}, dry_run=True))
    assert payload["ok"] is True, payload
    assert any("loyalty.card [5]" in line for line in payload["odoo_will_email"])
    assert fake.written == []
