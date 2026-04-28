"""Tests for the OdooClient ↔ PersistentFieldsCache integration.

The L2 cache must:

* Save the caller a round trip to Odoo when the entry is fresh.
* Be shared across distinct OdooClient instances (the typical "Claude
  restarted, server re-built clients" case, modeled here as two clients
  built against the same SQLite file).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from odoo_mcp.client import OdooClient
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.fields_cache import PersistentFieldsCache


def _make_instance_config(name: str = "dev") -> InstanceConfig:
    return InstanceConfig(
        name=name,
        url="https://dev.example.odoo.com",
        database="dev_db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({"res.partner"}),
    )


def _build_client(
    cache: PersistentFieldsCache | None,
    *,
    execute_results: list[dict[str, dict[str, Any]]],
    name: str = "dev",
) -> tuple[OdooClient, dict[str, int]]:
    cfg = _make_instance_config(name)
    creds = Credentials(instance_name=name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds, fields_cache=cache)
    # Pretend the client is already authenticated so fields_get won't
    # contact Odoo for auth.
    client._uid = 1  # type: ignore[attr-defined]
    counter = {"calls": 0}

    def _fake_execute(
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        counter["calls"] += 1
        # Pop the next response (or reuse the last one).
        if execute_results:
            return execute_results.pop(0) if len(execute_results) > 1 else execute_results[0]
        return {}

    client._execute = _fake_execute  # type: ignore[assignment]
    return client, counter


def test_l1_cache_only_one_rpc_per_process(tmp_path: Path) -> None:
    """Without an L2 cache, two calls in one client still hit RPC once thanks to L1."""
    payload = {"id": {"type": "integer"}}
    client, counter = _build_client(None, execute_results=[payload])
    client.fields_get("res.partner")
    client.fields_get("res.partner")
    assert counter["calls"] == 1


def test_l2_cache_shared_across_clients(tmp_path: Path) -> None:
    """A second OdooClient sharing the same SQLite cache must NOT call Odoo."""
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    payload = {"id": {"type": "integer"}, "name": {"type": "char"}}
    # First client populates the L2 cache.
    c1, c1_counter = _build_client(cache, execute_results=[payload])
    out1 = c1.fields_get("res.partner")
    assert out1 == payload
    assert c1_counter["calls"] == 1

    # Second client (fresh L1) reads from the L2 cache: zero RPCs.
    c2, c2_counter = _build_client(cache, execute_results=[payload])
    out2 = c2.fields_get("res.partner")
    assert out2 == payload
    assert c2_counter["calls"] == 0


def test_l2_cache_miss_writes_back(tmp_path: Path) -> None:
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    payload = {"id": {"type": "integer"}}
    client, counter = _build_client(cache, execute_results=[payload])
    client.fields_get("res.partner")
    # Cache should now be populated.
    cached = cache.get("dev", "res.partner")
    assert cached == payload
    assert counter["calls"] == 1


def test_per_instance_isolation(tmp_path: Path) -> None:
    """Two instances sharing one cache must not see each other's rows."""
    cache = PersistentFieldsCache(tmp_path / "fc.db")
    payload_a = {"id": {"type": "integer"}}
    payload_b = {"name": {"type": "char"}}

    client_a, _ = _build_client(cache, execute_results=[payload_a], name="dev")
    client_b, counter_b = _build_client(cache, execute_results=[payload_b], name="other")

    client_a.fields_get("res.partner")
    # client_b uses a DIFFERENT instance name so the L2 hit must NOT happen
    # with client_a's payload.
    out_b = client_b.fields_get("res.partner")
    assert out_b == payload_b
    assert counter_b["calls"] == 1
