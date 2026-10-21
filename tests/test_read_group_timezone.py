"""``odoo_read_group`` says which timezone datetime groups are bucketed in.

Odoo groups a ``datetime`` field by day / week / month in ``context['tz']``,
and in UTC when the context has no ``tz`` (``_read_group_groupby`` on 16.0
through 18.0, ``fields_temporal.property_to_sql`` / ``_read_group_groupby`` on
19.0 through 20.0). XML-RPC starts every call from an empty context, and the
client only ever sent ``{"lang": "en_US"}``, so ``create_date:month`` was a UTC
month while the user's own pivot view groups in their timezone. Records
created between 00:00 and 02:00 Brussels time on the 1st were counted in the
previous month, and nothing in the response said so.

The fix is a vetted ``tz`` opt-in: validated against the IANA zone list (Odoo
silently falls back to UTC on a name it does not know), sent as
``context['tz']`` for that one call, and reported back as ``timezone``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_token_budgets import _build_app, _call, _FakeClient

from odoo_mcp import dispatcher as dispatcher_mod
from odoo_mcp.client import _FROZEN_CONTEXT, OdooClient
from odoo_mcp.dispatcher import Dispatcher
from odoo_mcp.tools import build_tools

_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "create_date": {"type": "datetime"},
    "date_deadline": {"type": "date"},
    "stage_id": {"type": "many2one", "relation": "crm.stage"},
    "expected_revenue": {"type": "monetary"},
}


class _RecordingClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__(
            fields=_FIELDS,
            groups=[{"create_date:month": "October 2026", "__count": 3}],
        )
        self.read_group_calls: list[dict[str, Any]] = []

    def read_group(  # type: ignore[override]
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        *,
        limit: int,
        offset: int,
        orderby: str | None,
        lazy: bool,
        tz: str | None = None,
    ) -> list[dict[str, Any]]:
        self.read_group_calls.append({"groupby": groupby, "tz": tz})
        return [dict(g) for g in self._groups]


def _run(tmp_path: Path, **extra: Any) -> tuple[dict[str, Any], _RecordingClient]:
    fake = _RecordingClient()
    app = _build_app(tmp_path, fake)
    args: dict[str, Any] = {
        "instance": "dev",
        "model": "crm.lead",
        "fields": ["expected_revenue:sum"],
        "groupby": ["create_date:month"],
    }
    args.update(extra)
    payload, _ = _call(Dispatcher(app), "odoo_read_group", args)
    return payload, fake


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_datetime_groupby_without_tz_reports_utc(tmp_path: Path) -> None:
    payload, fake = _run(tmp_path)
    assert payload["ok"] is True
    assert payload["timezone"] == "UTC"
    assert "create_date:month grouped in UTC" in payload["notes"][0]
    assert fake.read_group_calls == [{"groupby": ["create_date:month"], "tz": None}]


def test_tz_is_sent_to_odoo_and_reported(tmp_path: Path) -> None:
    payload, fake = _run(tmp_path, tz="Europe/Brussels")
    assert payload["ok"] is True
    assert payload["timezone"] == "Europe/Brussels"
    assert "notes" not in payload
    assert fake.read_group_calls[0]["tz"] == "Europe/Brussels"


def test_date_and_relational_groupby_say_nothing(tmp_path: Path) -> None:
    # A date has no time of day, so its buckets are the same in every zone.
    payload, _ = _run(tmp_path, groupby=["date_deadline:month", "stage_id"])
    assert payload["ok"] is True
    assert "timezone" not in payload
    assert "notes" not in payload


def test_unknown_tz_is_refused_before_odoo(tmp_path: Path) -> None:
    # Odoo would silently group in UTC on this typo.
    payload, fake = _run(tmp_path, tz="Europe/Brusels")
    assert payload["ok"] is False
    assert "Europe/Brusels" in payload["error"]
    assert fake.read_group_calls == []


@pytest.mark.parametrize("bad", [42, "", "../../etc/passwd", "europe/brussels"])
def test_malformed_tz_is_refused(tmp_path: Path, bad: Any) -> None:
    payload, fake = _run(tmp_path, tz=bad)
    assert payload["ok"] is False
    assert fake.read_group_calls == []


def test_host_without_timezone_database_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher_mod, "_TIMEZONES", frozenset())
    payload, _ = _run(tmp_path, tz="Europe/Brussels")
    assert payload["ok"] is False
    assert "tzdata" in payload["error"]


def test_audit_records_the_timezone(tmp_path: Path) -> None:
    _run(tmp_path, tz="Europe/Brussels")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["details"]["tz"] == "Europe/Brussels"
    assert entry["details"]["args"]["tz"] == "Europe/Brussels"


def test_schema_declares_tz() -> None:
    tool = next(t for t in build_tools() if t.name == "odoo_read_group")
    assert tool.inputSchema["properties"]["tz"]["type"] == "string"
    assert "UTC" in (tool.description or "")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class _RecordingProxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute_kw(self, db: str, uid: int, key: str, model: str, method: str,
                   args: list[Any], kwargs: dict[str, Any]) -> Any:  # fmt: skip
        self.calls.append((method, kwargs))
        return []


def _client(tmp_path: Path) -> tuple[OdooClient, _RecordingProxy]:
    app = _build_app(tmp_path, _FakeClient())
    real = OdooClient(app.instances["dev"].config, credentials=_creds())
    real._uid = 7
    proxy = _RecordingProxy()
    real._object = proxy  # type: ignore[assignment]
    return real, proxy


def _creds() -> Any:
    from odoo_mcp.credentials import Credentials

    return Credentials(instance_name="dev", username="u", _api_key="k" * 10)


def _read_group(client: OdooClient, tz: str | None) -> None:
    client.read_group(
        "crm.lead", [], ["id:count"], ["create_date:day"],
        limit=None, offset=0, orderby=None, lazy=True, tz=tz,
    )  # fmt: skip


def test_client_sends_tz_in_context(tmp_path: Path) -> None:
    client, proxy = _client(tmp_path)
    _read_group(client, "Europe/Brussels")
    method, kwargs = proxy.calls[0]
    assert method == "read_group"
    assert kwargs["context"] == {**_FROZEN_CONTEXT, "tz": "Europe/Brussels"}


def test_client_without_tz_sends_only_the_frozen_context(tmp_path: Path) -> None:
    client, proxy = _client(tmp_path)
    _read_group(client, None)
    assert proxy.calls[0][1]["context"] == _FROZEN_CONTEXT


def test_execute_refuses_context_keys_that_are_not_opt_ins(tmp_path: Path) -> None:
    client, proxy = _client(tmp_path)
    with pytest.raises(ValueError, match="active_test"):
        client._execute("crm.lead", "search_read", [[]], {}, extra_context={"active_test": False})
    with pytest.raises(ValueError, match="lang"):
        client._execute("crm.lead", "search_read", [[]], {}, extra_context={"lang": "nl_BE"})
    assert proxy.calls == []
