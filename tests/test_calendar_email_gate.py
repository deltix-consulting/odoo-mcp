"""Tests for the calendar.event email gate on odoo_create / odoo_write.

Odoo emails a calendar.event's attendees from inside create/write itself
(addons/calendar/models/calendar_event.py, 16.0 to 20.0; ``force_send=True``
from 18.0 on): an invitation when attendees are added, and a "date changed"
mail when ``start`` moves. Without a gate, a generic create/write sends
outbound email that ``odoo_send_message`` keeps behind two opt-ins. These
tests pin that the same two opt-ins now apply, before the dry run, and that
both the preview and the commit say what Odoo will email.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from odoo_mcp._odoo_reference import ODOO_STANDARD_FIELDS
from odoo_mcp.dispatcher import (
    _CALENDAR_ATTENDEE_FIELDS,
    _CALENDAR_EVENT_MODEL,
    _CALENDAR_RESCHEDULE_FIELD,
    Dispatcher,
)

_ME = 3  # res.partner of the session user (uid 1)
_CUSTOMER = 7

_META: dict[str, dict[str, dict[str, Any]]] = {
    "calendar.event": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "start": {"type": "datetime"},
        "stop": {"type": "datetime"},
        "partner_ids": {"type": "many2many", "relation": "res.partner"},
        "attendee_ids": {"type": "one2many", "relation": "calendar.attendee"},
    },
    "calendar.attendee": {
        "id": {"type": "integer"},
        "partner_id": {"type": "many2one", "relation": "res.partner"},
    },
    "crm.lead": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "calendar_event_ids": {"type": "one2many", "relation": "calendar.event"},
    },
    "res.partner": {"id": {"type": "integer"}, "name": {"type": "char"}},
}


class _FakeClient:
    def __init__(self, event_partners: dict[int, list[int]] | None = None) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.event_partners = event_partners or {}
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.written: list[tuple[str, list[int], dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _META[model]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        if model == "res.users":
            assert ids == [self.uid] and fields == ["partner_id"]
            return [{"id": self.uid, "partner_id": [_ME, "Me"]}]
        assert model == "calendar.event" and fields == ["partner_ids"]
        return [{"id": i, "partner_ids": self.event_partners.get(i, [])} for i in ids]

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
        *, external_comms_enabled: bool = False, event_partners: dict[int, list[int]] | None = None
    ) -> tuple[Dispatcher, _FakeClient]:
        fake = _FakeClient(event_partners)
        app = make_app(client=fake, external_comms_enabled=external_comms_enabled)
        return Dispatcher(app), fake

    return _build


@pytest.fixture
def comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")


@pytest.fixture(autouse=True)
def _no_comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, {"instance": "dev", **args}))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _create_event(values: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    return {"model": "calendar.event", "values": {"name": "Demo", **values}, "dry_run": dry_run}


# --- refused without the opt-ins ---------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_create_with_attendee_partner_refused(build: Any, dry_run: bool) -> None:
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_create",
        _create_event({"partner_ids": [(6, 0, [_ME, _CUSTOMER])]}, dry_run=dry_run),
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "prod_guard"
    assert "invitation" in payload["error"]
    assert "ODOO_MCP_ENABLE_EXTERNAL_COMMS" in payload["error"]
    assert fake.created == []


def test_create_with_attendee_ids_command_refused(build: Any) -> None:
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_create",
        _create_event({"attendee_ids": [(0, 0, {"partner_id": _CUSTOMER})]}),
    )
    assert payload["ok"] is False
    assert "attendee_ids" in payload["error"]
    assert fake.created == []


def test_write_adding_attendee_refused(build: Any) -> None:
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_write",
        {
            "model": "calendar.event",
            "ids": [5],
            "values": {"partner_ids": [(4, _CUSTOMER)]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert fake.written == []


def test_reschedule_with_other_attendees_refused(build: Any) -> None:
    disp, fake = build(event_partners={5: [_ME, _CUSTOMER]})
    payload = _call(
        disp,
        "odoo_write",
        {
            "model": "calendar.event",
            "ids": [5],
            "values": {"start": "2030-01-02 09:00:00"},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert "date changed" in payload["error"]
    assert fake.written == []


def test_nested_event_create_refused(build: Any) -> None:
    # crm.lead -> calendar_event_ids (0, 0, vals) is a calendar.event create.
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_create",
        {
            "model": "crm.lead",
            "values": {
                "name": "Lead",
                "calendar_event_ids": [(0, 0, {"name": "x", "partner_ids": [(4, _CUSTOMER)]})],
            },
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert "new calendar.event" in payload["error"]
    assert fake.created == []


def test_env_alone_is_not_enough(build: Any, comms_env: None) -> None:
    disp, fake = build(external_comms_enabled=False)
    payload = _call(disp, "odoo_create", _create_event({"partner_ids": [(4, _CUSTOMER)]}))
    assert payload["ok"] is False
    assert "external_comms_enabled" in payload["error"]
    assert fake.created == []


def test_instance_flag_alone_is_not_enough(build: Any) -> None:
    disp, fake = build(external_comms_enabled=True)
    payload = _call(disp, "odoo_create", _create_event({"partner_ids": [(4, _CUSTOMER)]}))
    assert payload["ok"] is False
    assert fake.created == []


# --- payloads Odoo sends nothing for stay allowed -----------------------------


@pytest.mark.parametrize(
    "values",
    [
        {},  # Odoo's default attendee is the author, who is never mailed
        {"partner_ids": [(6, 0, [_ME])]},
        {"partner_ids": [(4, _ME)]},
    ],
)
def test_create_without_other_attendees_allowed(build: Any, values: dict[str, Any]) -> None:
    disp, fake = build()
    payload = _call(disp, "odoo_create", _create_event(values))
    assert payload["committed"] is True
    assert "odoo_will_email" not in payload
    assert len(fake.created) == 1


def test_reschedule_solo_event_allowed(build: Any) -> None:
    disp, fake = build(event_partners={5: [_ME]})
    payload = _call(
        disp,
        "odoo_write",
        {
            "model": "calendar.event",
            "ids": [5],
            "values": {"start": "2030-01-02 09:00:00"},
            "dry_run": False,
        },
    )
    assert payload["committed"] is True
    assert "odoo_will_email" not in payload


def test_removing_attendee_allowed(build: Any) -> None:
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_write",
        {
            "model": "calendar.event",
            "ids": [5],
            "values": {"partner_ids": [(3, _CUSTOMER)], "name": "Renamed"},
            "dry_run": False,
        },
    )
    assert payload["committed"] is True
    assert len(fake.written) == 1


# --- with both opt-ins: allowed, and disclosed in preview AND commit ---------


def test_opted_in_preview_and_commit_both_disclose(build: Any, comms_env: None) -> None:
    disp, fake = build(external_comms_enabled=True)
    args = _create_event({"partner_ids": [(4, _CUSTOMER)]})
    preview = _call(disp, "odoo_create", {**args, "dry_run": True})
    assert preview["preview"] is True
    assert fake.created == []
    commit = _call(disp, "odoo_create", args)
    assert commit["committed"] is True
    assert preview["odoo_will_email"] == commit["odoo_will_email"]
    assert "invitation" in commit["odoo_will_email"][0]


# --- every name the gate sends or keys on exists in Odoo ---------------------


def test_gate_names_exist_in_odoo_reference() -> None:
    event_fields = ODOO_STANDARD_FIELDS[_CALENDAR_EVENT_MODEL]
    for name in (*_CALENDAR_ATTENDEE_FIELDS, _CALENDAR_RESCHEDULE_FIELD):
        assert name in event_fields, name
    assert "partner_id" in ODOO_STANDARD_FIELDS["res.users"]
