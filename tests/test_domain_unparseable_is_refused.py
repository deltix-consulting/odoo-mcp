"""A domain the server cannot parse must be refused, never dropped.

The domain sandbox has always rejected a non-list domain. The gap this
file pins was one level up, in the dispatcher's *wiring*: all three
domain-taking tools read the argument as ``args.get("domain") or []``,
and ``or`` also swallows every **falsy** non-list — ``""``, ``{}``,
``0``, ``False``. Those never reached the sandbox at all. They reached
Odoo as ``[]``, which matches **every record in the model**, so a filter
the server could not understand silently widened the search instead of
being refused.

The distinction that has to survive: *absent* / ``None`` / ``[]`` are the
documented ways to ask for no filter and must keep working. Only a
present-but-unparseable domain is refused.

The load-bearing assertion in the refusal tests is not the error code —
it is that the Odoo client was **never called**. An unfiltered query must
not reach the server at all.
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
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard

_META: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "string": "ID"},
    "name": {"type": "char", "string": "Name"},
    "state": {"type": "selection", "string": "State"},
}


class _FakeClient:
    """Records the domain each read primitive was called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return _META

    def search_read(
        self,
        model: str,
        domain: Any,
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("search_read", domain))
        return [{"id": 1, "name": "Acme"}]

    def search_count(self, model: str, domain: Any) -> int:
        self.calls.append(("search_count", domain))
        return 12345

    def read_group(
        self,
        model: str,
        domain: Any,
        fields: list[str],
        groupby: list[str],
        limit: int | None = None,
        offset: int | None = None,
        orderby: str | None = None,
        lazy: bool = True,
    ) -> list[dict[str, Any]]:
        self.calls.append(("read_group", domain))
        return [{"state": "draft", "id_count": 9}]


def _build(tmp_path: Path, fake: _FakeClient) -> OdooMcpApp:
    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=3000,
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
    rt = InstanceRuntime(config=cfg, client=OdooClient(cfg, credentials=creds))
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _args(tool: str) -> dict[str, Any]:
    """Minimum valid arguments for each domain-taking tool."""
    base: dict[str, Any] = {"instance": "dev", "model": "res.partner"}
    if tool == "odoo_search_read":
        return {**base, "fields": ["name"]}
    if tool == "odoo_read_group":
        return {**base, "fields": ["id:count"], "groupby": ["state"]}
    return base


# Every tool that accepts a caller-supplied domain. Kept explicit rather than
# derived so that adding a fourth domain-taking tool fails here loudly.
DOMAIN_TOOLS = ("odoo_search_read", "odoo_search_count", "odoo_read_group")

# Falsy non-lists: the exact values `args.get("domain") or []` used to swallow.
FALSY_NON_LISTS: list[Any] = ["", {}, 0, False]

# Truthy non-lists: these always reached the sandbox and were always refused.
# Kept as controls so a future refactor cannot regress them either.
TRUTHY_NON_LISTS: list[Any] = [
    "[('name','=','x')]",
    "draft",
    {"conditions": [{"field": "name", "operator": "=", "value": "x"}]},
    42,
]


@pytest.mark.parametrize("tool", DOMAIN_TOOLS)
@pytest.mark.parametrize("domain", FALSY_NON_LISTS, ids=repr)
def test_falsy_non_list_domain_is_refused_and_never_reaches_odoo(
    tmp_path: Path, tool: str, domain: Any
) -> None:
    fake = _FakeClient()
    payload = _call(Dispatcher(_build(tmp_path, fake)), tool, {**_args(tool), "domain": domain})

    assert payload["ok"] is False
    assert payload["error_code"] == "domain_sandbox"
    # The point of the fix: no unfiltered query was issued.
    assert fake.calls == []


@pytest.mark.parametrize("tool", DOMAIN_TOOLS)
@pytest.mark.parametrize("domain", TRUTHY_NON_LISTS, ids=repr)
def test_truthy_non_list_domain_is_refused_and_never_reaches_odoo(
    tmp_path: Path, tool: str, domain: Any
) -> None:
    """Control: these were already refused before the fix, and must stay so."""
    fake = _FakeClient()
    payload = _call(Dispatcher(_build(tmp_path, fake)), tool, {**_args(tool), "domain": domain})

    assert payload["ok"] is False
    assert payload["error_code"] == "domain_sandbox"
    assert fake.calls == []


@pytest.mark.parametrize("tool", DOMAIN_TOOLS)
@pytest.mark.parametrize(
    ("label", "extra"),
    [
        ("omitted", {}),
        ("explicit None", {"domain": None}),
        ("explicit empty list", {"domain": []}),
    ],
)
def test_absent_none_and_empty_list_still_mean_no_filter(
    tmp_path: Path, tool: str, label: str, extra: dict[str, Any]
) -> None:
    """The documented ways to ask for no filter must keep working."""
    fake = _FakeClient()
    payload = _call(Dispatcher(_build(tmp_path, fake)), tool, {**_args(tool), **extra})

    assert payload["ok"] is not False, f"{label} should be accepted"
    assert [d for _, d in fake.calls] == [[]]


@pytest.mark.parametrize("tool", DOMAIN_TOOLS)
def test_valid_domain_is_passed_through(tmp_path: Path, tool: str) -> None:
    fake = _FakeClient()
    payload = _call(
        Dispatcher(_build(tmp_path, fake)),
        tool,
        {**_args(tool), "domain": [["name", "=", "Acme"]]},
    )

    assert payload["ok"] is not False
    assert [d for _, d in fake.calls] == [[("name", "=", "Acme")]]


def test_refusal_names_the_way_to_ask_for_no_filter(tmp_path: Path) -> None:
    """An agent that passed "" must be told how to express 'match everything'.

    Without this, the refusal is a dead end: the agent knows the domain was
    rejected but not that ``[]`` is the accepted spelling.
    """
    fake = _FakeClient()
    payload = _call(
        Dispatcher(_build(tmp_path, fake)),
        "odoo_search_read",
        {**_args("odoo_search_read"), "domain": ""},
    )

    assert payload["ok"] is False
    assert "pass [] or omit" in payload["error"]


def test_every_domain_taking_tool_is_covered(tmp_path: Path) -> None:
    """Guard: if a fourth tool starts taking a domain, cover it above.

    Counts the dispatcher's ``sandbox_domain`` call sites, which is the
    definition of 'accepts a caller-supplied domain'.
    """
    import inspect
    import re

    from odoo_mcp import dispatcher as dispatcher_module

    source = inspect.getsource(dispatcher_module)
    # The call is multi-line on main (model / allow_sensitive / overrides
    # keywords), so match the opening and the first argument regardless
    # of the whitespace between them.
    call_sites = len(re.findall(r"sandbox_domain\(\s*_domain_arg\(args\)", source))
    fail_open_sites = len(re.findall(r"sandbox_domain\(\s*args\.get\(", source))
    assert fail_open_sites == 0, "a sandbox_domain call still uses the fail-open coercion"
    assert call_sites == len(DOMAIN_TOOLS), (
        f"{call_sites} dispatcher sandbox_domain call sites but "
        f"{len(DOMAIN_TOOLS)} tools listed in DOMAIN_TOOLS"
    )
