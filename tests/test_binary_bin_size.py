"""Binary fields are probed for their size, not fetched and thrown away.

Without ``include_binary`` the redactor replaces every binary value with a
``<binary:N bytes>`` placeholder. Before this change the client still asked
Odoo for the full base64 content (an ``ir.attachment.datas`` page or a
product ``image_1920`` column can be megabytes per row) and the redactor
discarded it. The client now sends Odoo's ``bin_size`` context on ``read`` /
``search_read`` unless the caller opted in, so Odoo answers with a size
string instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import _FROZEN_CONTEXT, OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.fields import _binary_size_hint, redact_response
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


def _instance_config() -> InstanceConfig:
    return InstanceConfig(
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


# ---------------------------------------------------------------------------
# Client: which context reaches execute_kw
# ---------------------------------------------------------------------------


class _RecordingObject:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def execute_kw(
        self,
        _db: str,
        _uid: int,
        _key: str,
        model: str,
        method: str,
        _args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        self.calls.append((model, method, kwargs))
        return self.result


def _client(result: Any) -> tuple[OdooClient, _RecordingObject]:
    cfg = _instance_config()
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    client._uid = 1  # type: ignore[attr-defined]
    obj = _RecordingObject(result)
    client._object = obj  # type: ignore[assignment]
    return client, obj


def test_search_read_asks_for_sizes_by_default() -> None:
    client, obj = _client([])
    client.search_read("ir.attachment", [], ["datas"], 10, 0, None)
    assert obj.calls[-1][2]["context"] == {**_FROZEN_CONTEXT, "bin_size": True}


def test_read_asks_for_sizes_by_default() -> None:
    client, obj = _client([])
    client.read("product.template", [1], ["image_1920"])
    assert obj.calls[-1][2]["context"] == {**_FROZEN_CONTEXT, "bin_size": True}


@pytest.mark.parametrize("method", ["search_read", "read"])
def test_opt_out_sends_the_frozen_context_unchanged(method: str) -> None:
    client, obj = _client([])
    if method == "search_read":
        client.search_read("ir.attachment", [], ["datas"], 10, 0, None, bin_size=False)
    else:
        client.read("ir.attachment", [1], ["datas"], bin_size=False)
    assert obj.calls[-1][2]["context"] == _FROZEN_CONTEXT


def test_writes_never_carry_bin_size() -> None:
    """``bin_size`` is a read-time flag; every other call keeps the frozen context."""
    client, obj = _client(7)
    client.create("res.partner", {"name": "x"})
    client.search_count("res.partner", [])
    assert all(call[2]["context"] == _FROZEN_CONTEXT for call in obj.calls)


def test_the_frozen_context_itself_is_not_mutated() -> None:
    client, _obj = _client([])
    client.read("product.template", [1], ["image_1920"])
    assert "bin_size" not in _FROZEN_CONTEXT


# ---------------------------------------------------------------------------
# Redactor: the placeholder size from each shape Odoo returns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # human_size(), attachment-stored binaries under bin_size (17.0 - saas-19.4)
        ("123.00 bytes", 123),
        ("12.50 Kb", 12_800),
        ("2.00 Mb", 2 * 1024**2),
        # pg_size_pretty(), column-stored binaries under bin_size
        ("512 bytes", 512),
        ("12 kB", 12 * 1024),
        ("4 MB", 4 * 1024**2),
        ("1 GB", 1024**3),
        # Odoo 20.0 ignores bin_size and returns a dict
        ({"content": "QUJD", "size": 3}, 3),
        ({"filename": "a.pdf", "content": "QUJDRA==", "size": 4}, 4),
        ({"content": "QUJDRA=="}, 6),
        # content (include_binary off but an older path, or an unknown shape)
        ("QUJDRA==", 6),
        (b"abcd", 4),
        (12, 0),
    ],
)
def test_binary_size_hint(value: Any, expected: int) -> None:
    assert _binary_size_hint(value) == expected


def test_a_base64_string_is_never_read_as_a_size() -> None:
    # base64 has no spaces, so it cannot fullmatch the size pattern
    assert _binary_size_hint("MTIga0I=") == 6


def test_odoo_20_dict_is_replaced_by_the_placeholder() -> None:
    out = redact_response(
        "product.template",
        [{"id": 1, "image_1920": {"content": "QUJD" * 1000, "size": 3000}}],
        {"id": "integer", "image_1920": "binary"},
        allow_sensitive=frozenset(),
        include_binary=False,
    )
    assert out[0]["image_1920"] == "<binary:3000 bytes>"


# ---------------------------------------------------------------------------
# Dispatcher: include_binary decides bin_size
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.bin_size_seen: list[bool] = []
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "image_1920": {"type": "binary"},
        }

    def _row(self, bin_size: bool) -> dict[str, Any]:
        self.bin_size_seen.append(bin_size)
        return {"id": 1, "name": "x", "image_1920": "12.50 Kb" if bin_size else "QUJD" * 10}

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
        *,
        bin_size: bool = True,
    ) -> list[dict[str, Any]]:
        return [self._row(bin_size)]

    def read(
        self, model: str, ids: list[int], fields: list[str], *, bin_size: bool = True
    ) -> list[dict[str, Any]]:
        return [self._row(bin_size)]


def _dispatcher(tmp_path: Path, fake: _FakeClient) -> Dispatcher:
    cfg = _instance_config()
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rate_limiter = RateLimiter()
    rate_limiter.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rate_limiter,
        instances={cfg.name: rt},
    )
    return Dispatcher(app)


def _call(dispatcher: Dispatcher, name: str, args: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(asyncio.run(dispatcher.call(name, args))[0].text)
    return payload


_READS = [
    ("odoo_search_read", {}, "records"),
    ("odoo_read", {"ids": [1]}, "records"),
]


@pytest.mark.parametrize(("tool", "extra", "key"), _READS)
def test_default_read_probes_size_only(
    tmp_path: Path, tool: str, extra: dict[str, Any], key: str
) -> None:
    fake = _FakeClient()
    args = {"instance": "dev", "model": "product.template", "fields": ["name", "image_1920"]}
    payload = _call(_dispatcher(tmp_path, fake), tool, {**args, **extra})
    assert payload["ok"] is True, payload
    assert fake.bin_size_seen == [True]
    assert payload[key][0]["image_1920"] == "<binary:12800 bytes>"


@pytest.mark.parametrize(("tool", "extra", "key"), _READS)
def test_include_binary_fetches_content(
    tmp_path: Path, tool: str, extra: dict[str, Any], key: str
) -> None:
    fake = _FakeClient()
    args = {
        "instance": "dev",
        "model": "product.template",
        "fields": ["name", "image_1920"],
        "include_binary": True,
    }
    payload = _call(_dispatcher(tmp_path, fake), tool, {**args, **extra})
    assert payload["ok"] is True, payload
    assert fake.bin_size_seen == [False]
    assert payload[key][0]["image_1920"] == "QUJD" * 10
