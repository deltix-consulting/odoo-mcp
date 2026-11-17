"""A write to a company_dependent field must say whose value it sets.

Odoo stores a ``company_dependent`` field once per company and reads and
writes the value of ``env.company`` (``ir.property`` up to 17.0, a jsonb
column keyed on ``env.company.id`` from 18.0). odoo-mcp sends no
``allowed_company_ids``, so ``env.company`` is the API user's DEFAULT
company, whatever company the record belongs to. A user in a BE and an NL
company who writes a per-company value on an NL record therefore changes
the BE value, and the NL users see nothing change.

``odoo_write`` and ``odoo_create`` now report this in ``company_scope``,
in the dry run and in the commit. These tests drive the real handlers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.errors import OdooRemoteError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

_META: dict[str, dict[str, dict[str, Any]]] = {
    "product.product": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "standard_price": {"type": "float", "company_dependent": True},
        "property_cost_method": {"type": "selection", "company_dependent": True},
        "company_id": {"type": "many2one", "relation": "res.company"},
    },
    # company_dependent field, no company_id on the model.
    "res.partner.category": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "x_rate": {"type": "float", "company_dependent": True},
    },
}

BE = [1, "deltix BE"]
NL = [2, "deltix NL"]


class _FakeClient:
    def __init__(self, company_ids: list[int], record_company: dict[int, Any]) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 7
        self.company_ids = company_ids
        self.record_company = record_company
        self.calls: list[str] = []
        self.fail_record_read = False

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _META[model]

    def user_default_company(self) -> tuple[list[Any], list[int]] | None:
        self.calls.append("user_default_company")
        return (BE, self.company_ids)

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        self.calls.append(f"read:{model}")
        if self.fail_record_read:
            raise OdooRemoteError("boom")
        assert fields == ["company_id"]
        return [{"id": i, "company_id": self.record_company.get(i, False)} for i in ids]

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.calls.append("create")
        return 42

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.calls.append("write")
        return True


def _build(
    tmp_path: Path,
    *,
    company_ids: list[int] | None = None,
    record_company: dict[int, Any] | None = None,
) -> tuple[Dispatcher, _FakeClient]:
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
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    fake = _FakeClient(
        company_ids if company_ids is not None else [1, 2],
        record_company if record_company is not None else {},
    )
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )
    return Dispatcher(app), fake


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _write(disp: Dispatcher, ids: list[int], values: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return _call(
        disp,
        "odoo_write",
        {"instance": "dev", "model": "product.product", "ids": ids, "values": values, **kw},
    )


# --- odoo_write --------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_write_names_the_company_whose_value_changes(tmp_path: Path, dry_run: bool) -> None:
    disp, _ = _build(tmp_path, record_company={10: BE})
    out = _write(disp, [10], {"standard_price": 12.5}, dry_run=dry_run)
    scope = out["company_scope"]
    assert scope["company_dependent_fields"] == ["standard_price"]
    assert scope["value_company"] == BE
    assert "'deltix BE' (id 1)" in scope["note"]
    assert "default company" in scope["note"]
    # The record is in the default company: no mismatch reported.
    assert "other_company_ids" not in scope
    assert "will NOT see" not in scope["note"]


@pytest.mark.parametrize("dry_run", [True, False])
def test_write_on_a_record_of_another_company_says_it_will_not_see_the_value(
    tmp_path: Path, dry_run: bool
) -> None:
    disp, fake = _build(tmp_path, record_company={10: BE, 11: NL, 12: False, 13: NL})
    out = _write(disp, [10, 11, 12, 13], {"standard_price": 12.5}, dry_run=dry_run)
    scope = out["company_scope"]
    # 12 is shared between companies (company_id False): not a mismatch.
    assert scope["other_company_ids"] == [11, 13]
    assert scope["other_company_id_count"] == 2
    assert "2 record(s) belong to another company" in scope["note"]
    assert "will NOT see the new value" in scope["note"]
    assert ("write" in fake.calls) is (not dry_run)


def test_write_lists_every_company_dependent_field_and_only_those(tmp_path: Path) -> None:
    disp, _ = _build(tmp_path, record_company={10: BE})
    out = _write(disp, [10], {"name": "x", "standard_price": 1.0, "property_cost_method": "fifo"})
    scope = out["company_scope"]
    assert scope["company_dependent_fields"] == ["property_cost_method", "standard_price"]
    assert "property_cost_method, standard_price hold one value" in scope["note"]


def test_other_company_ids_are_capped_and_counted(tmp_path: Path) -> None:
    ids = list(range(100, 140))
    disp, _ = _build(tmp_path, record_company=dict.fromkeys(ids, NL))
    scope = _write(disp, ids, {"standard_price": 1.0})["company_scope"]
    assert scope["other_company_id_count"] == 40
    assert scope["other_company_ids"] == ids[:20]


def test_write_without_a_company_dependent_field_makes_no_lookup(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path, record_company={10: NL})
    out = _write(disp, [10], {"name": "x"})
    assert "company_scope" not in out
    assert fake.calls == ["write"]


def test_user_in_one_company_gets_no_scope_and_no_record_read(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path, company_ids=[1], record_company={10: BE})
    out = _write(disp, [10], {"standard_price": 1.0})
    assert "company_scope" not in out
    assert fake.calls == ["user_default_company", "write"]


def test_model_without_company_id_reports_the_company_and_reads_no_record(
    tmp_path: Path,
) -> None:
    disp, fake = _build(tmp_path)
    out = _call(
        disp,
        "odoo_write",
        {
            "instance": "dev",
            "model": "res.partner.category",
            "ids": [5],
            "values": {"x_rate": 2.0},
        },
    )
    assert out["company_scope"]["value_company"] == BE
    assert "other_company_ids" not in out["company_scope"]
    assert fake.calls == ["user_default_company", "write"]


def test_a_failed_record_read_degrades_to_the_plain_note(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path, record_company={10: NL})
    fake.fail_record_read = True
    out = _write(disp, [10], {"standard_price": 1.0})
    assert out["committed"] is True
    assert out["company_scope"]["value_company"] == BE
    assert "other_company_ids" not in out["company_scope"]


def test_a_failed_company_lookup_never_blocks_the_write(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path, record_company={10: NL})
    fake.user_default_company = lambda: None  # type: ignore[method-assign]
    out = _write(disp, [10], {"standard_price": 1.0})
    assert out["committed"] is True
    assert "company_scope" not in out


# --- odoo_create -------------------------------------------------------------


def _create(disp: Dispatcher, values: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return _call(
        disp,
        "odoo_create",
        {"instance": "dev", "model": "product.product", "values": values, **kw},
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_create_for_another_company_says_that_company_will_not_see_the_value(
    tmp_path: Path, dry_run: bool
) -> None:
    disp, fake = _build(tmp_path)
    out = _create(disp, {"name": "P", "standard_price": 9.0, "company_id": 2}, dry_run=dry_run)
    scope = out["company_scope"]
    assert scope["value_company"] == BE
    assert scope["record_company_id"] == 2
    assert "company id 2" in scope["note"]
    assert "will NOT see the value" in scope["note"]
    # create never reads a record back for this.
    assert not [c for c in fake.calls if c.startswith("read:")]


def test_create_in_the_default_company_reports_the_company_only(tmp_path: Path) -> None:
    disp, _ = _build(tmp_path)
    for values in (
        {"name": "P", "standard_price": 9.0, "company_id": 1},
        {"name": "P", "standard_price": 9.0},
        {"name": "P", "standard_price": 9.0, "company_id": False},
    ):
        scope = _create(disp, values)["company_scope"]
        assert scope["value_company"] == BE
        assert "record_company_id" not in scope
        assert "will NOT see" not in scope["note"]


def test_create_without_a_company_dependent_field_makes_no_lookup(tmp_path: Path) -> None:
    disp, fake = _build(tmp_path)
    out = _create(disp, {"name": "P", "company_id": 2})
    assert "company_scope" not in out
    assert fake.calls == ["create"]


# --- OdooClient.user_default_company ----------------------------------------


def _client(reply: Any) -> tuple[OdooClient, list[Any]]:
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
    client = OdooClient(
        cfg, credentials=Credentials(instance_name="dev", username="u", _api_key="k" * 10)
    )
    client._uid = 7
    seen: list[Any] = []

    def fake_read(model: str, ids: list[int], fields: list[str]) -> Any:
        seen.append((model, ids, fields))
        if isinstance(reply, Exception):
            raise reply
        return reply

    client.read = fake_read  # type: ignore[method-assign]
    return client, seen


def test_client_reads_the_users_own_company_once() -> None:
    client, seen = _client([{"id": 7, "company_id": [1, "deltix BE"], "company_ids": [1, 2]}])
    assert client.user_default_company() == ([1, "deltix BE"], [1, 2])
    assert client.user_default_company() == ([1, "deltix BE"], [1, 2])
    assert seen == [("res.users", [7], ["company_id", "company_ids"])]


@pytest.mark.parametrize(
    "reply",
    [
        OdooRemoteError("denied"),
        [],
        [{"id": 7, "company_id": False, "company_ids": [1]}],
        [{"id": 7, "company_id": [1, "x"], "company_ids": "1,2"}],
        [{"id": 7, "company_id": [True, "x"], "company_ids": [1]}],
    ],
)
def test_client_returns_none_on_a_failed_or_odd_reply_and_does_not_cache_it(reply: Any) -> None:
    client, seen = _client(reply)
    assert client.user_default_company() is None
    assert client.user_default_company() is None
    assert len(seen) == 2
