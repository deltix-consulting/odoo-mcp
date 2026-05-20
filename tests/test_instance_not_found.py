"""Safety tests for unknown-instance handling.

Real-world failure: a user asked Claude to "search in the Odoo demo
environment" while the MCP install had only ``prod`` configured. The
AI saw the error listing configured instances, "helpfully" picked
``prod``, and started reading there. For a read that's a data-exposure
incident; for a write it would have triggered a prod-write preview
against the wrong dataset.

The fix lives in two places:

- ``InstanceNotFoundError.hint`` carries an explicit behavioural
  instruction to the AI: do NOT substitute, ask the user.
- The dispatcher's error message still lists configured instances so a
  human with a typo can self-correct, but the substitute-prevention
  language is in the hint that the dispatcher surfaces alongside.

These tests pin both halves so the safety message can't be quietly
weakened by a future refactor.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from odoo_mcp.audit import AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.errors import InstanceNotFoundError
from odoo_mcp.security.allowlist import ALLOWLIST_WILDCARD
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


def _build(tmp_path: Path) -> OdooMcpApp:
    cfg = InstanceConfig(
        name="prod",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_PROD",
        production=True,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({ALLOWLIST_WILDCARD}),
    )
    creds = Credentials(instance_name=cfg.name, username="u", _api_key="k" * 10)
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: InstanceRuntime(config=cfg, client=real)},
    )


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Direct exception shape
# ---------------------------------------------------------------------------


def test_hint_tells_ai_to_stop_and_ask_user() -> None:
    """The hint is the behavioural directive — pin every key phrase."""
    err = InstanceNotFoundError("Instance 'demo' is not configured.")
    hint = err.hint
    assert hint is not None
    # The AI must be told explicitly NOT to substitute.
    assert "STOP" in hint
    assert "do not silently retry" in hint.lower()
    assert "substitute" in hint.lower()
    # And it must be told the right next step: ask the user.
    assert "ask the user" in hint.lower()
    # Production is named-and-shamed so an AI weighing trade-offs
    # sees the worst case in the message it actually reads.
    assert "production" in hint.lower()


# ---------------------------------------------------------------------------
# End-to-end via the dispatcher: a tool call with a wrong instance name
# ---------------------------------------------------------------------------


def test_unknown_instance_in_read_call_returns_safety_hint(tmp_path: Path) -> None:
    """The exact scenario from the production report: user says 'demo',
    only 'prod' exists. The dispatcher must return an error AND the
    safety hint — not a quiet success."""
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {
            "instance": "demo",
            "model": "res.partner",
            "domain": [],
            "fields": ["id", "name"],
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "instance_not_found"
    # The factual message mentions the configured-instances list so a
    # human-with-typo can self-correct.
    assert "demo" in payload["error"]
    assert "prod" in payload["error"]
    # The hint carries the safety instruction.
    assert "hint" in payload
    assert "STOP" in payload["hint"]
    assert "ask the user" in payload["hint"].lower()


def test_unknown_instance_in_write_call_blocks_before_prod_guard(tmp_path: Path) -> None:
    """A write against a non-existent instance must fail at the instance
    lookup, BEFORE any prod-guard logic — and the safety hint is shown."""
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_write",
        {
            "instance": "staging",  # also not configured
            "model": "res.partner",
            "ids": [1],
            "values": {"name": "X"},
        },
    )
    assert payload["ok"] is False
    assert payload["error_code"] == "instance_not_found"
    # Hint must be present — even on a write path, the AI must NOT be
    # tempted to retry against prod.
    assert "hint" in payload
    assert "substitute" in payload["hint"].lower()


def test_empty_instance_name_also_refused(tmp_path: Path) -> None:
    """Bonus: an empty / non-string instance is its own error path."""
    app = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        "odoo_search_read",
        {"instance": "", "model": "res.partner", "fields": ["id"]},
    )
    assert payload["ok"] is False
    # The dispatcher rejects empty instance names with a separate message
    # path; the error_code is still instance_not_found but the message
    # path is the "name must be a non-empty string" one.
    assert payload["error_code"] in {"instance_not_found", "odoo_mcp_error"}
