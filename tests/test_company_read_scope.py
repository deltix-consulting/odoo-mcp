"""A read of a company_dependent field must say whose value it shows.

Odoo reads a ``company_dependent`` field for ``env.company``. odoo-mcp sends
no ``allowed_company_ids``, so that is the API user's DEFAULT company,
whatever company the record belongs to: an NL product read by a user whose
default company is BE shows the BE cost price. ``odoo_search_read`` and
``odoo_read`` now report this in ``company_scope``. These tests drive the
real handlers with the fake of ``test_company_value_scope``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_company_value_scope import BE, NL, _build, _call

_ROWS = {
    10: {"id": 10, "name": "a", "standard_price": 5.0, "company_id": BE},
    11: {"id": 11, "name": "b", "standard_price": 6.0, "company_id": NL},
    12: {"id": 12, "name": "c", "standard_price": 7.0, "company_id": False},
}


def _patch_reads(fake: Any, rows: dict[int, dict[str, Any]] = _ROWS) -> None:
    def search_read(model, domain, fields, limit, offset, order):  # type: ignore[no-untyped-def]
        fake.calls.append(f"search_read:{model}")
        return [{k: v for k, v in r.items() if k in fields or k == "id"} for r in rows.values()]

    def read(model, ids, fields):  # type: ignore[no-untyped-def]
        fake.calls.append(f"read:{model}")
        return [{k: v for k, v in rows[i].items() if k in fields or k == "id"} for i in ids]

    fake.search_read = search_read
    fake.read = read


def _sr(disp: Any, fields: list[str], model: str = "product.product") -> dict[str, Any]:
    return _call(disp, "odoo_search_read", {"instance": "dev", "model": model, "fields": fields})


def _rd(disp: Any, ids: list[int], fields: list[str]) -> dict[str, Any]:
    return _call(
        disp,
        "odoo_read",
        {"instance": "dev", "model": "product.product", "ids": ids, "fields": fields},
    )


def test_search_read_names_the_company_whose_value_is_shown(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake)
    out = _sr(disp, ["name", "standard_price"])
    scope = out["company_scope"]
    assert scope["company_dependent_fields"] == ["standard_price"]
    assert scope["value_company"] == BE
    assert "'deltix BE' (id 1)" in scope["note"]
    assert "whatever company a record belongs to" in scope["note"]
    # company_id not read: a hint, no per-record claim, no extra round trip.
    assert "Add company_id to fields" in scope["note"]
    assert "other_company_ids" not in scope
    assert fake.calls == ["search_read:product.product", "user_default_company"]


def test_search_read_with_company_id_lists_records_of_another_company(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake)
    out = _sr(disp, ["name", "standard_price", "company_id"])
    scope = out["company_scope"]
    # 12 is shared (company_id False): it has no own company to differ.
    assert scope["other_company_ids"] == [11]
    assert scope["other_company_id_count"] == 1
    assert "1 record(s) belong to another company" in scope["note"]
    assert "Add company_id" not in scope["note"]
    assert fake.calls == ["search_read:product.product", "user_default_company"]


def test_read_names_the_company_too(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake)
    out = _rd(disp, [10, 11], ["standard_price", "property_cost_method", "company_id"])
    scope = out["company_scope"]
    assert scope["company_dependent_fields"] == ["property_cost_method", "standard_price"]
    assert scope["note"].startswith("property_cost_method, standard_price hold one value")
    assert scope["other_company_ids"] == [11]


def test_all_records_in_the_default_company_get_no_mismatch(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake)
    out = _rd(disp, [10], ["standard_price", "company_id"])
    scope = out["company_scope"]
    assert "other_company_ids" not in scope
    assert "another company" not in scope["note"]


def test_single_company_user_gets_nothing(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path, company_ids=[1])
    _patch_reads(fake)
    assert "company_scope" not in _sr(disp, ["standard_price", "company_id"])


@pytest.mark.parametrize("fields", [["name"], ["name", "company_id"]])
def test_no_company_dependent_field_no_call(tmp_path: Path, fields: list[str]) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake)
    out = _sr(disp, fields)
    assert "company_scope" not in out
    assert "user_default_company" not in fake.calls


def test_no_records_no_call(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake, rows={})
    out = _sr(disp, ["standard_price"])
    assert "company_scope" not in out
    assert "user_default_company" not in fake.calls


def test_model_without_company_id_gets_no_hint(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake, rows={1: {"id": 1, "name": "x", "x_rate": 2.0}})
    out = _sr(disp, ["x_rate"], model="res.partner.category")
    scope = out["company_scope"]
    assert scope["company_dependent_fields"] == ["x_rate"]
    assert "company_id" not in scope["note"]


def test_unreadable_user_companies_degrade_to_nothing(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    _patch_reads(fake)
    fake.user_default_company = lambda: None
    out = _sr(disp, ["standard_price"])
    assert "company_scope" not in out
    assert out["count"] == 3
