"""``odoo_create`` / ``odoo_write`` refuse a ``company_id`` the user is not in.

Odoo's ``write`` checks record rules on the records before the update and
never after it, and does not check the many2one target. Writing the id of a
company outside the user's ``company_ids`` therefore succeeds, and the record
falls outside the multi-company rule: the user can no longer read or fix it.
``create`` raises an AccessError on most models, but only at commit, after a
dry run that said nothing.

The refusal runs before the dry run, so preview and commit agree. ``False``,
the user's own companies, models without a ``company_id`` to ``res.company``
and a failed company lookup are all left alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_token_budgets import _build_app, _call, _FakeClient

from odoo_mcp.dispatcher import Dispatcher

_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "name": {"type": "char"},
    "company_id": {"type": "many2one", "relation": "res.company"},
}


class _WriteClient(_FakeClient):
    def __init__(
        self,
        company_ids: list[int] | None,
        *,
        fields: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(fields=fields if fields is not None else _FIELDS)
        self._company_ids = company_ids
        self.company_lookups = 0
        self.created: list[dict[str, Any]] = []
        self.written: list[tuple[list[int], dict[str, Any]]] = []

    def user_company_ids(self) -> list[int] | None:
        self.company_lookups += 1
        return self._company_ids

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.created.append(values)
        return 42

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.written.append((ids, values))
        return True


def _run(
    tmp_path: Path, fake: _WriteClient, tool: str, values: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    args: dict[str, Any] = {"instance": "dev", "model": "sale.order", "values": values}
    if tool == "odoo_write":
        args["ids"] = [7]
    args.update(extra)
    payload, _ = _call(Dispatcher(_build_app(tmp_path, fake)), tool, args)
    return payload


# ---------------------------------------------------------------------------
# Refused before anything reaches Odoo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["odoo_create", "odoo_write"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_foreign_company_is_refused(tmp_path: Path, tool: str, dry_run: bool) -> None:
    fake = _WriteClient([1, 3])
    payload = _run(tmp_path, fake, tool, {"name": "SO", "company_id": 2}, dry_run=dry_run)
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    message = payload["error"]
    assert "company_id 2" in message
    assert "[1, 3]" in message
    assert fake.created == []
    assert fake.written == []


# ---------------------------------------------------------------------------
# Let through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["odoo_create", "odoo_write"])
def test_own_company_passes(tmp_path: Path, tool: str) -> None:
    fake = _WriteClient([1, 3])
    payload = _run(tmp_path, fake, tool, {"company_id": 3})
    assert payload["ok"] is True, payload
    assert payload["committed"] is True
    assert fake.company_lookups == 1


@pytest.mark.parametrize("tool", ["odoo_create", "odoo_write"])
def test_shared_record_skips_the_lookup(tmp_path: Path, tool: str) -> None:
    """``False`` is a record shared by all companies: Odoo's own design."""
    fake = _WriteClient([1, 3])
    payload = _run(tmp_path, fake, tool, {"company_id": False})
    assert payload["ok"] is True, payload
    assert fake.company_lookups == 0


def test_values_without_company_id_skip_the_lookup(tmp_path: Path) -> None:
    fake = _WriteClient([1, 3])
    payload = _run(tmp_path, fake, "odoo_write", {"name": "SO"})
    assert payload["ok"] is True, payload
    assert fake.company_lookups == 0


def test_create_without_company_id_is_not_refused(tmp_path: Path) -> None:
    payload = _run(tmp_path, _WriteClient([1, 3]), "odoo_create", {"name": "SO"})
    assert payload["ok"] is True, payload


def test_unknown_companies_leave_it_to_odoo(tmp_path: Path) -> None:
    fake = _WriteClient(None)
    payload = _run(tmp_path, fake, "odoo_write", {"company_id": 2})
    assert payload["ok"] is True, payload
    assert fake.written == [([7], {"company_id": 2})]


def test_company_id_to_another_model_is_not_a_company(tmp_path: Path) -> None:
    fields = dict(_FIELDS, company_id={"type": "many2one", "relation": "x.branch"})
    fake = _WriteClient([1, 3], fields=fields)
    payload = _run(tmp_path, fake, "odoo_write", {"company_id": 2})
    assert payload["ok"] is True, payload
    assert fake.company_lookups == 0
