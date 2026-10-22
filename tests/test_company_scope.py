"""``odoo_read_group`` and ``odoo_search_count`` say when they add up companies.

Odoo evaluates record rules against ``env.companies``. When the context has no
``allowed_company_ids`` that is EVERY company the user belongs to
(``Environment.companies`` on 16.0 through 20.0; Odoo's own comment there
assumes "all normal RPCs will have a proper allowed_company_ids"). The web
client sends the companies ticked in its switcher; odoo-mcp sends none. So for
a user in two companies, "revenue this month" from ``odoo_read_group`` was the
sum of both companies, and nothing in the response said so. A row from
``odoo_search_read`` shows its ``company_id``; a count or a total does not.

The fix is a disclosure: the user's companies are read once per client
(``res.users.company_ids`` is in ``SELF_READABLE_FIELDS``), and the two
aggregate tools add ``company_ids`` plus a note when the total may span
several of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_token_budgets import _build_app, _call, _FakeClient

from odoo_mcp.client import OdooClient
from odoo_mcp.dispatcher import Dispatcher
from odoo_mcp.errors import OdooRemoteError

_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "amount_total": {"type": "monetary"},
    "state": {"type": "selection"},
    "company_id": {"type": "many2one", "relation": "res.company"},
}


class _CompanyClient(_FakeClient):
    def __init__(
        self,
        company_ids: list[int] | None,
        *,
        fields: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            fields=fields if fields is not None else _FIELDS,
            groups=[{"state": "sale", "amount_total": 1500.0, "__count": 3}],
        )
        self._company_ids = company_ids
        self.company_lookups = 0

    def user_company_ids(self) -> list[int] | None:
        self.company_lookups += 1
        return self._company_ids

    def search_count(self, model: str, domain: list[Any]) -> int:
        return 7


def _group(tmp_path: Path, fake: _CompanyClient, **extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "instance": "dev",
        "model": "sale.order",
        "fields": ["amount_total:sum"],
        "groupby": ["state"],
    }
    args.update(extra)
    payload, _ = _call(Dispatcher(_build_app(tmp_path, fake)), "odoo_read_group", args)
    assert payload["ok"] is True, payload
    return payload


def _count(tmp_path: Path, fake: _CompanyClient, **extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"instance": "dev", "model": "sale.order"}
    args.update(extra)
    payload, _ = _call(Dispatcher(_build_app(tmp_path, fake)), "odoo_search_count", args)
    assert payload["ok"] is True, payload
    return payload


# ---------------------------------------------------------------------------
# Dispatcher: when the note is added
# ---------------------------------------------------------------------------


def test_read_group_over_two_companies_says_so(tmp_path: Path) -> None:
    payload = _group(tmp_path, _CompanyClient([1, 3]))
    assert payload["company_ids"] == [1, 3]
    assert "Covers all 2 companies" in payload["notes"][0]
    assert "company_id" in payload["notes"][0]


def test_search_count_over_two_companies_says_so(tmp_path: Path) -> None:
    payload = _count(tmp_path, _CompanyClient([1, 3]))
    assert payload["count"] == 7
    assert payload["company_ids"] == [1, 3]
    assert "Covers all 2 companies" in payload["notes"][0]


def test_single_company_user_gets_no_note(tmp_path: Path) -> None:
    payload = _group(tmp_path, _CompanyClient([1]))
    assert "company_ids" not in payload
    assert "notes" not in payload


def test_unknown_companies_get_no_note(tmp_path: Path) -> None:
    """A failed lookup is not a reason to fail, or to guess."""
    payload = _count(tmp_path, _CompanyClient(None))
    assert "company_ids" not in payload
    assert "notes" not in payload


def test_model_without_company_field_skips_the_lookup(tmp_path: Path) -> None:
    fields = {k: v for k, v in _FIELDS.items() if k != "company_id"}
    fake = _CompanyClient([1, 3], fields=fields)
    payload = _group(tmp_path, fake)
    assert "notes" not in payload
    assert fake.company_lookups == 0


def test_company_id_to_another_model_is_not_a_company(tmp_path: Path) -> None:
    fields = dict(_FIELDS, company_id={"type": "many2one", "relation": "x.branch"})
    fake = _CompanyClient([1, 3], fields=fields)
    assert "notes" not in _group(tmp_path, fake)
    assert fake.company_lookups == 0


def test_grouping_by_company_needs_no_note(tmp_path: Path) -> None:
    fake = _CompanyClient([1, 3])
    payload = _group(tmp_path, fake, groupby=["company_id", "state"])
    assert "notes" not in payload
    assert fake.company_lookups == 0


@pytest.mark.parametrize(
    "domain",
    [
        [["company_id", "=", 1]],
        [["state", "=", "sale"], ["company_id", "in", [1]]],
        ["|", ["company_id", "=", False], ["company_id", "=", 3]],
    ],
)
def test_filtering_on_company_needs_no_note(tmp_path: Path, domain: list[Any]) -> None:
    fake = _CompanyClient([1, 3])
    assert "notes" not in _count(tmp_path, fake, domain=domain)
    assert fake.company_lookups == 0


def test_other_filters_still_get_the_note(tmp_path: Path) -> None:
    payload = _group(tmp_path, _CompanyClient([1, 3]), domain=[["state", "=", "sale"]])
    assert payload["company_ids"] == [1, 3]


# ---------------------------------------------------------------------------
# Client: the lookup
# ---------------------------------------------------------------------------


class _ReadClient(OdooClient):
    """An OdooClient whose ``read`` is scripted, authenticated as uid 7."""

    def __init__(self, reply: Any) -> None:  # noqa: D107 — test double
        self._uid = 7
        self._company_ids = None
        self._reply = reply
        self.reads: list[tuple[str, list[int], list[str]]] = []

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        self.reads.append((model, ids, fields))
        if isinstance(self._reply, Exception):
            raise self._reply
        reply: list[dict[str, Any]] = self._reply
        return reply


def test_client_reads_own_companies_once_and_sorts_them() -> None:
    client = _ReadClient([{"id": 7, "company_ids": [3, 1]}])
    assert client.user_company_ids() == [1, 3]
    assert client.user_company_ids() == [1, 3]
    assert client.reads == [("res.users", [7], ["company_ids"])]


def test_client_lookup_failure_returns_none_and_retries() -> None:
    client = _ReadClient(OdooRemoteError("AccessError"))
    assert client.user_company_ids() is None
    assert client.user_company_ids() is None
    assert len(client.reads) == 2


@pytest.mark.parametrize(
    "reply",
    [[], [{"id": 7}], [{"id": 7, "company_ids": False}], [{"id": 7, "company_ids": [1, True]}]],
)
def test_client_odd_reply_returns_none(reply: Any) -> None:
    assert _ReadClient(reply).user_company_ids() is None
