"""Tests for the event-registration email gate on odoo_create / odoo_write.

An event.registration that becomes 'open' runs its event's "after each
registration" schedulers from inside create/write (``_update_mail_schedulers``,
16.0 to 20.0, as superuser), so the attendee gets the event's confirmation
mail during the call. These tests pin that this needs the same two
external-comms opt-ins as ``odoo_send_message``, before the dry run, and that
payloads Odoo sends nothing for stay allowed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from odoo_mcp._odoo_reference import ODOO_STANDARD_FIELDS
from odoo_mcp.dispatcher import _EVENT_SCHEDULER_MODEL, _REGISTRATION_MODEL, Dispatcher

_MAILING_EVENT = 3  # has an "after each registration" scheduler
_QUIET_EVENT = 4  # has none

_META: dict[str, dict[str, dict[str, Any]]] = {
    "event.registration": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "email": {"type": "char"},
        "state": {"type": "selection"},
        "event_id": {"type": "many2one", "relation": "event.event"},
    },
    "event.event": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "registration_ids": {"type": "one2many", "relation": "event.registration"},
    },
}


class _FakeClient:
    def __init__(self, *, reg_state: str, reg_event: int) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.reg_state = reg_state
        self.reg_event = reg_event
        self.counted: list[tuple[str, list[Any]]] = []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.written: list[tuple[str, list[int], dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _META[model]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        assert model == _REGISTRATION_MODEL
        assert set(fields) <= set(ODOO_STANDARD_FIELDS[model]), fields  # (#62)
        row = {"state": self.reg_state, "event_id": [self.reg_event, "E"]}
        return [{"id": i, **{f: row[f] for f in fields}} for i in ids]

    def search_count(self, model: str, domain: list[Any]) -> int:
        assert model == _EVENT_SCHEDULER_MODEL
        for name, _op, _value in domain:
            assert name in ODOO_STANDARD_FIELDS[model], name  # (#62)
        self.counted.append((model, domain))
        event_ids = next(value for name, _op, value in domain if name == "event_id")
        return 1 if _MAILING_EVENT in event_ids else 0

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
        reg_state: str = "draft",
        reg_event: int = _MAILING_EVENT,
    ) -> tuple[Dispatcher, _FakeClient]:
        fake = _FakeClient(reg_state=reg_state, reg_event=reg_event)
        app = make_app(client=fake, external_comms_enabled=external_comms_enabled)
        return Dispatcher(app), fake

    return _build


@pytest.fixture(autouse=True)
def _no_comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, {"instance": "dev", **args}))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _create(values: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    return {"model": _REGISTRATION_MODEL, "values": values, "dry_run": dry_run}


def _confirm(*, dry_run: bool = False) -> dict[str, Any]:
    return {
        "model": _REGISTRATION_MODEL,
        "ids": [5],
        "values": {"state": "open"},
        "dry_run": dry_run,
    }


# --- refused without the opt-ins ---------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("state", [None, "open"])
def test_create_on_mailing_event_refused(build: Any, dry_run: bool, state: str | None) -> None:
    # 17.0+ defaults a new registration to 'open', so no state counts too.
    disp, fake = build()
    values: dict[str, Any] = {"event_id": _MAILING_EVENT, "name": "A", "email": "a@x.test"}
    if state:
        values["state"] = state
    payload = _call(disp, "odoo_create", _create(values, dry_run=dry_run))
    assert payload["ok"] is False
    assert payload["error_code"] == "prod_guard"
    assert "after each registration" in payload["error"]
    assert "ODOO_MCP_ENABLE_EXTERNAL_COMMS" in payload["error"]
    assert fake.created == []


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("current", ["draft", "cancel"])
def test_confirm_on_mailing_event_refused(build: Any, dry_run: bool, current: str) -> None:
    disp, fake = build(reg_state=current)
    payload = _call(disp, "odoo_write", _confirm(dry_run=dry_run))
    assert payload["ok"] is False
    assert f"{_REGISTRATION_MODEL} [5]" in payload["error"]
    assert fake.written == []


def test_nested_registration_without_event_id_refused(build: Any) -> None:
    # (0, 0, vals) under event.event: the event is not in vals, so assume it mails.
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_write",
        {
            "model": "event.event",
            "ids": [_QUIET_EVENT],
            "values": {"registration_ids": [(0, 0, {"name": "A"})]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert f"new {_REGISTRATION_MODEL}" in payload["error"]
    assert fake.written == []


def test_nested_confirm_refused(build: Any) -> None:
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_write",
        {
            "model": "event.event",
            "ids": [_MAILING_EVENT],
            "values": {"registration_ids": [(1, 5, {"state": "open"})]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert f"{_REGISTRATION_MODEL} [5]" in payload["error"]
    assert fake.written == []


# --- payloads Odoo sends nothing for stay allowed -----------------------------


def test_create_on_quiet_event_allowed(build: Any) -> None:
    disp, fake = build()
    payload = _call(disp, "odoo_create", _create({"event_id": _QUIET_EVENT, "name": "A"}))
    assert payload["committed"] is True
    assert "odoo_will_email" not in payload
    assert len(fake.created) == 1


@pytest.mark.parametrize("state", ["draft", "done", "cancel"])
def test_create_not_open_allowed(build: Any, state: str) -> None:
    # Odoo only runs the schedulers for registrations in state 'open'.
    disp, fake = build()
    values = {"event_id": _MAILING_EVENT, "name": "A", "state": state}
    payload = _call(disp, "odoo_create", _create(values))
    assert payload["committed"] is True
    assert fake.counted == []


def test_confirm_already_open_allowed(build: Any) -> None:
    disp, fake = build(reg_state="open")
    payload = _call(disp, "odoo_write", _confirm())
    assert payload["committed"] is True
    assert fake.counted == []


def test_write_to_done_allowed(build: Any) -> None:
    # 'done' passes Odoo's confirming check, but the schedulers filter on 'open'.
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_write",
        {"model": _REGISTRATION_MODEL, "ids": [5], "values": {"state": "done"}, "dry_run": False},
    )
    assert payload["committed"] is True


def test_confirm_on_quiet_event_allowed(build: Any) -> None:
    disp, fake = build(reg_event=_QUIET_EVENT)
    payload = _call(disp, "odoo_write", _confirm())
    assert payload["committed"] is True
    assert "odoo_will_email" not in payload


# --- allowed with both opt-ins, and disclosed ---------------------------------


def test_opt_ins_allow_and_disclose(build: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")
    disp, fake = build(external_comms_enabled=True)
    values = {"event_id": _MAILING_EVENT, "name": "A"}
    preview = _call(disp, "odoo_create", _create(values, dry_run=True))
    assert any("after each registration" in e for e in preview["odoo_will_email"])
    result = _call(disp, "odoo_create", _create(values))
    assert result["committed"] is True
    assert result["odoo_will_email"] == preview["odoo_will_email"]
