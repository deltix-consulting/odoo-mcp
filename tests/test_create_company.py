"""``odoo_create`` says which company a new record lands in.

Most ``company_id`` fields default to ``env.company``. Without
``allowed_company_ids`` in the context that is the user's default company
(``Environment.company``, 16.0 through master), and odoo-mcp sends none, while
Odoo's own forms use the company selected in the switcher. For a user in two
companies, a create that left ``company_id`` out could land in a company the
caller never named, and neither the dry-run nor the result said which.

The dry-run now asks Odoo's ``default_get`` for the default ``create`` would
apply, the commit reads the new record back, and both report ``company_id`` plus
a note. No note for single-company users, when ``values`` sets the company, when
the default is empty, or when a lookup fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_token_budgets import _build_app, _call, _FakeClient

from odoo_mcp.dispatcher import Dispatcher
from odoo_mcp.errors import OdooRemoteError

_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "name": {"type": "char"},
    "company_id": {"type": "many2one", "relation": "res.company"},
}


class _CreateClient(_FakeClient):
    def __init__(
        self,
        company_ids: list[int] | None,
        *,
        default: Any = 3,
        created_company: Any = None,
        fields: dict[str, dict[str, Any]] | None = None,
        fail_lookup: bool = False,
    ) -> None:
        super().__init__(fields=fields if fields is not None else _FIELDS)
        self._company_ids = company_ids
        self._default = default
        self._created_company = created_company if created_company is not None else [3, "B"]
        self._fail_lookup = fail_lookup
        self.company_lookups = 0
        self.default_get_calls: list[tuple[str, list[str]]] = []
        self.created: list[dict[str, Any]] = []

    def user_company_ids(self) -> list[int] | None:
        self.company_lookups += 1
        return self._company_ids

    def default_get(self, model: str, fields: list[str]) -> dict[str, Any]:
        self.default_get_calls.append((model, list(fields)))
        if self._fail_lookup:
            raise OdooRemoteError("AccessError")
        return {} if self._default is None else {"company_id": self._default}

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.created.append(values)
        return 42

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        if self._fail_lookup:
            raise OdooRemoteError("AccessError")
        return [{"id": ids[0], "company_id": self._created_company}]


def _create(tmp_path: Path, fake: _CreateClient, **extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "instance": "dev",
        "model": "sale.order",
        "values": {"name": "SO"},
    }
    args.update(extra)
    payload, _ = _call(Dispatcher(_build_app(tmp_path, fake)), "odoo_create", args)
    assert payload["ok"] is True, payload
    return payload


# ---------------------------------------------------------------------------
# Dry-run: Odoo's default_get decides
# ---------------------------------------------------------------------------


def test_preview_names_the_default_company(tmp_path: Path) -> None:
    fake = _CreateClient([1, 3])
    payload = _create(tmp_path, fake, dry_run=True)
    assert payload["preview"] is True
    assert payload["company_id"] == 3
    assert payload["company_ids"] == [1, 3]
    assert "company 3" in payload["notes"][0]
    assert "company switcher" in payload["notes"][0]
    assert fake.default_get_calls == [("sale.order", ["company_id"])]
    assert fake.created == []


def test_preview_accepts_a_pair_from_default_get(tmp_path: Path) -> None:
    payload = _create(tmp_path, _CreateClient([1, 3], default=[1, "A"]), dry_run=True)
    assert payload["company_id"] == 1


def test_preview_without_a_default_company_says_nothing(tmp_path: Path) -> None:
    """An empty default is a record shared by all companies: Odoo's own design."""
    for default in (None, False):
        payload = _create(tmp_path, _CreateClient([1, 3], default=default), dry_run=True)
        assert "company_id" not in payload
        assert "notes" not in payload


# ---------------------------------------------------------------------------
# Commit: the new record is read back
# ---------------------------------------------------------------------------


def test_commit_names_the_company_it_got(tmp_path: Path) -> None:
    fake = _CreateClient([1, 3], created_company=[3, "B"])
    payload = _create(tmp_path, fake)
    assert payload["committed"] is True
    assert payload["id"] == 42
    assert payload["company_id"] == 3
    assert "Odoo set it to company 3" in payload["notes"][0]
    assert fake.default_get_calls == []


def test_commit_shared_record_says_nothing(tmp_path: Path) -> None:
    payload = _create(tmp_path, _CreateClient([1, 3], created_company=False))
    assert payload["committed"] is True
    assert "notes" not in payload


def test_failed_readback_never_fails_a_committed_create(tmp_path: Path) -> None:
    fake = _CreateClient([1, 3], fail_lookup=True)
    payload = _create(tmp_path, fake)
    assert payload["committed"] is True
    assert payload["id"] == 42
    assert "notes" not in payload


def test_failed_default_get_never_fails_a_preview(tmp_path: Path) -> None:
    payload = _create(tmp_path, _CreateClient([1, 3], fail_lookup=True), dry_run=True)
    assert payload["preview"] is True
    assert "notes" not in payload


# ---------------------------------------------------------------------------
# When no lookup is made at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("company", [1, False])
def test_values_with_company_id_skip_the_lookup(tmp_path: Path, company: Any) -> None:
    fake = _CreateClient([1, 3])
    payload = _create(tmp_path, fake, values={"name": "SO", "company_id": company}, dry_run=True)
    assert "notes" not in payload
    assert fake.company_lookups == 0
    assert fake.default_get_calls == []


def test_single_company_user_gets_no_note(tmp_path: Path) -> None:
    fake = _CreateClient([1])
    payload = _create(tmp_path, fake, dry_run=True)
    assert "company_id" not in payload
    assert "notes" not in payload
    assert fake.default_get_calls == []


def test_unknown_companies_get_no_note(tmp_path: Path) -> None:
    fake = _CreateClient(None)
    payload = _create(tmp_path, fake)
    assert "notes" not in payload


def test_model_without_company_field_skips_the_lookup(tmp_path: Path) -> None:
    fields = {k: v for k, v in _FIELDS.items() if k != "company_id"}
    fake = _CreateClient([1, 3], fields=fields)
    payload = _create(tmp_path, fake, dry_run=True)
    assert "notes" not in payload
    assert fake.company_lookups == 0


def test_company_id_to_another_model_is_not_a_company(tmp_path: Path) -> None:
    fields = dict(_FIELDS, company_id={"type": "many2one", "relation": "x.branch"})
    fake = _CreateClient([1, 3], fields=fields)
    payload = _create(tmp_path, fake, dry_run=True)
    assert "notes" not in payload
    assert fake.company_lookups == 0
