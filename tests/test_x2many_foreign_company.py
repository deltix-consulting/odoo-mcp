"""A ``company_id`` nested in an x2many line gets the same gate as a top-level one.

Odoo runs ``(1, id, vals)`` through the line model's own ``write`` and
``(0, 0, vals)`` through its ``create`` (``One2many`` / ``Many2many``
``write_real``, 17.0 and 19.0), so the rule checks are the same as a direct
call: the update stores a foreign company silently, the create raises only at
commit. Both are now refused before the dry run, like the top-level value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_write_foreign_company import _FIELDS, _run, _WriteClient

_LINE_FIELDS: dict[str, dict[str, Any]] = dict(
    _FIELDS, order_line={"type": "one2many", "relation": "sale.order.line"}
)


@pytest.mark.parametrize("tool", ["odoo_create", "odoo_write"])
@pytest.mark.parametrize("command", [(0, 0), (1, 5)])
@pytest.mark.parametrize("dry_run", [True, False])
def test_foreign_company_on_a_line_is_refused(
    tmp_path: Path, tool: str, command: tuple[int, int], dry_run: bool
) -> None:
    fake = _WriteClient([1, 3], fields=_LINE_FIELDS)
    values = {"order_line": [[*command, {"name": "L", "company_id": 2}]]}
    payload = _run(tmp_path, fake, tool, values, dry_run=dry_run)
    assert payload["ok"] is False
    assert payload["error_code"] == "field_policy"
    assert "company_id 2" in payload["error"]
    assert "sale.order.line" in payload["error"]
    assert fake.created == []
    assert fake.written == []


@pytest.mark.parametrize("tool", ["odoo_create", "odoo_write"])
def test_own_company_on_a_line_passes(tmp_path: Path, tool: str) -> None:
    fake = _WriteClient([1, 3], fields=_LINE_FIELDS)
    values = {"order_line": [[1, 5, {"company_id": 3}]]}
    payload = _run(tmp_path, fake, tool, values)
    assert payload["ok"] is True, payload
    # >= 1: #83 adds its own (cached) lookup on create.
    assert fake.company_lookups >= 1


def test_lines_without_company_id_skip_the_lookup(tmp_path: Path) -> None:
    fake = _WriteClient([1, 3], fields=_LINE_FIELDS)
    values = {"order_line": [[0, 0, {"name": "L"}], [4, 9], [6, 0, [9]]]}
    payload = _run(tmp_path, fake, "odoo_write", values)
    assert payload["ok"] is True, payload
    assert fake.company_lookups == 0


def test_unknown_companies_leave_the_line_to_odoo(tmp_path: Path) -> None:
    fake = _WriteClient(None, fields=_LINE_FIELDS)
    values = {"order_line": [[1, 5, {"company_id": 2}]]}
    payload = _run(tmp_path, fake, "odoo_write", values)
    assert payload["ok"] is True, payload
    assert fake.written == [([7], values)]
