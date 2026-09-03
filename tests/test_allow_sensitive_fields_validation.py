"""``allow_sensitive_fields`` is the argument that unlocks hidden data.

Every other caller-supplied list on the read path is validated before use
(``fields`` and ``groupby`` through ``_require_list_of_str``, ``ids``
through ``_require_list_of_int``, ``domain`` through the domain sandbox).
The sensitive-field opt-in was handed straight to ``frozenset()``, which
does not reject a wrong shape — it reinterprets one:

* a ``dict`` collapses to its KEYS, so ``{"vat": True}`` unlocked ``vat``
  through a shape the schema forbids — and ``_args_shape`` recorded that
  call as ``allow_sensitive_count: 0``, so the audit log said no sensitive
  field had been asked for on the one call where one was granted;
* a ``str`` collapses to its CHARACTERS, so passing the field name itself
  silently did nothing and the refusal told the caller to do what it had
  just done;
* an ``int`` is not iterable at all and surfaced as a raw ``TypeError``
  through the dispatcher's last-resort handler.

These pin the refusal, the audit shape, and the schema/handler parity on
``odoo_search_count``, whose handler honours the opt-in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.errors import OdooMcpError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard
from odoo_mcp.server import _args_shape
from odoo_mcp.tools import build_tools

_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "string": "ID"},
    "name": {"type": "char", "string": "Name"},
    "vat": {"type": "char", "string": "Tax ID"},
}
_RECORDS: list[dict[str, Any]] = [{"id": 1, "name": "ACME", "vat": "BE0123456789"}]


class _FakeClient:
    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _FIELDS

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
    ) -> list[dict[str, Any]]:
        keep = set(fields) | {"id"}
        return [{k: v for k, v in r.items() if k in keep} for r in _RECORDS]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        keep = set(fields) | {"id"}
        return [{k: v for k, v in r.items() if k in keep} for r in _RECORDS if r["id"] in ids]

    def search_count(self, model: str, domain: list[Any]) -> int:
        return len(_RECORDS)

    def read_group(
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
    ) -> list[dict[str, Any]]:
        return [{"name": "ACME", "id_count": 1}]


def _dispatcher(tmp_path: Path) -> Dispatcher:
    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )
    creds = Credentials(instance_name="dev", username="u", _api_key="k" * 10)
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
    rt.client = _FakeClient()
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={"dev": cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rate = RateLimiter()
    rate.configure("dev", 300)
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rate,
        instances={"dev": rt},
    )
    return Dispatcher(app)


# Every handler that reads the opt-in, with a call that is otherwise valid.
_CALLS: dict[str, dict[str, Any]] = {
    "_search_read": {"instance": "dev", "model": "res.partner", "fields": ["id", "name"]},
    "_search_count": {"instance": "dev", "model": "res.partner"},
    "_read_group": {
        "instance": "dev",
        "model": "res.partner",
        "fields": ["id:count"],
        "groupby": ["name"],
    },
    "_read": {"instance": "dev", "model": "res.partner", "ids": [1], "fields": ["id", "name"]},
}

_BAD_SHAPES: list[Any] = [
    {"vat": True},  # dict -> keys: used to unlock 'vat' and audit as count 0
    "vat",  # str -> characters: used to be a silent no-op
    5,  # int -> not iterable: used to raise a raw TypeError
    ["vat", 7],  # list with a non-string member
]


@pytest.mark.parametrize("handler", sorted(_CALLS))
@pytest.mark.parametrize("bad", _BAD_SHAPES, ids=["dict", "str", "int", "int_member"])
def test_every_handler_refuses_a_malformed_opt_in(tmp_path: Path, handler: str, bad: Any) -> None:
    disp = _dispatcher(tmp_path)
    args = dict(_CALLS[handler])
    args["allow_sensitive_fields"] = bad
    with pytest.raises(OdooMcpError) as exc:
        getattr(disp, handler)(args)
    assert "allow_sensitive_fields" in str(exc.value)


def test_dict_shaped_opt_in_no_longer_unlocks_a_sensitive_field(tmp_path: Path) -> None:
    """The fail-open case: frozenset({'vat': True}) == {'vat'} granted the field."""
    disp = _dispatcher(tmp_path)
    with pytest.raises(OdooMcpError):
        disp._search_read(
            {
                "instance": "dev",
                "model": "res.partner",
                "fields": ["id", "vat"],
                "allow_sensitive_fields": {"vat": True},
            }
        )


def test_a_malformed_opt_in_is_a_clean_refusal_not_an_internal_error(tmp_path: Path) -> None:
    """A non-iterable used to escape as TypeError to the last-resort handler."""
    disp = _dispatcher(tmp_path)
    with pytest.raises(OdooMcpError):
        disp._search_count({"instance": "dev", "model": "res.partner", "allow_sensitive_fields": 5})


@pytest.mark.parametrize("handler", sorted(_CALLS))
@pytest.mark.parametrize("good", [None, [], ["vat"]], ids=["absent", "empty", "populated"])
def test_well_shaped_opt_in_still_passes(tmp_path: Path, handler: str, good: Any) -> None:
    disp = _dispatcher(tmp_path)
    args = dict(_CALLS[handler])
    if good is not None:
        args["allow_sensitive_fields"] = good
    getattr(disp, handler)(args)


def test_populated_opt_in_still_unlocks_the_field(tmp_path: Path) -> None:
    disp = _dispatcher(tmp_path)
    out = disp._search_read(
        {
            "instance": "dev",
            "model": "res.partner",
            "fields": ["id", "vat"],
            "allow_sensitive_fields": ["vat"],
        }
    )
    assert out["records"][0]["vat"] == "BE0123456789"


def test_audit_shape_records_a_malformed_opt_in_rather_than_a_count_of_zero() -> None:
    """`allow_sensitive_count: 0` on a non-list read as 'none were asked for'."""
    shape = _args_shape(
        {"instance": "dev", "model": "res.partner", "allow_sensitive_fields": {"vat": True}}
    )
    assert "allow_sensitive_count" not in shape
    assert shape["allow_sensitive_fields"] == {"present": True, "type": "dict"}
    # Still never the contents.
    assert "vat" not in str(shape)


def test_search_count_schema_declares_the_opt_in_its_handler_honours() -> None:
    """additionalProperties is false, so an undeclared opt-in is unsendable."""
    tool = next(t for t in build_tools() if t.name == "odoo_search_count")
    assert tool.inputSchema["additionalProperties"] is False
    prop = tool.inputSchema["properties"]["allow_sensitive_fields"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string"}
