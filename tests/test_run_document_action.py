"""Tests for the odoo_run_document_action tool.

Covers the (model, action) -> method map as the security boundary, the
prod-guard dry-run + token flow, the wizard-dict ("needs manual") return
path, and refusal of unmapped pairs.
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
from odoo_mcp.security.document_actions import (
    DOCUMENT_ACTION_VERBS,
    resolve_document_action,
    supported_pairs,
)
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard
from odoo_mcp.tools import build_tools


class _FakeClient:
    def __init__(self, *, action_return: Any = True) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self._action_return = action_return
        self.action_calls: list[tuple[str, str, list[int]]] = []
        self._states: dict[int, str] = {}

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {"id": {"type": "integer"}, "state": {"type": "selection"}}

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        return [{"id": i, "state": self._states.get(i, "draft")} for i in ids]

    def call_document_action(self, model: str, method: str, record_ids: list[int]) -> Any:
        self.action_calls.append((model, method, list(record_ids)))
        return self._action_return


def _build(
    tmp_path: Path, *, production: bool = False, action_return: Any = True
) -> tuple[OdooMcpApp, _FakeClient]:
    cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=production,
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
    fake = _FakeClient(action_return=action_return)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    app = OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )
    return app, fake


def _call(disp: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_run_document_action", args))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------


def test_map_resolves_known_pairs() -> None:
    assert resolve_document_action("purchase.order", "confirm") == "button_confirm"
    assert resolve_document_action("sale.order", "confirm") == "action_confirm"
    assert resolve_document_action("account.move", "post") == "action_post"
    assert resolve_document_action("stock.picking", "validate") == "button_validate"


def test_map_resolves_cancel_pairs() -> None:
    """Pin every cancel target so a refactor can't drop one silently.

    Reviewing operators / auditors should be able to read this list and
    immediately see which production records an agent can cancel. The
    inverse-coverage test below pins what we deliberately DO NOT expose.
    """
    assert resolve_document_action("purchase.order", "cancel") == "button_cancel"
    assert resolve_document_action("sale.order", "cancel") == "action_cancel"
    assert resolve_document_action("account.move", "cancel") == "button_cancel"
    assert resolve_document_action("stock.picking", "cancel") == "action_cancel"
    # v0.20.0 additions:
    assert resolve_document_action("mrp.production", "cancel") == "action_cancel"
    assert resolve_document_action("account.payment", "cancel") == "action_cancel"
    assert resolve_document_action("hr.leave", "cancel") == "action_cancel"
    assert resolve_document_action("hr.expense.sheet", "cancel") == "action_cancel"


def test_deliberately_excluded_cancel_targets() -> None:
    """Pin what we DO NOT expose, so a future "let's just add this row"
    PR has to delete this test (and explain why) instead of slipping
    past code review.

    - ``hr.leave`` cancel is the user-side withdraw. ``action_refuse``
      is the manager-side rejection — that's an HR decision, not an
      agent action.
    - ``hr.expense`` (singular) is a single line on a sheet. Cancelling
      a line out of band would leave the parent sheet inconsistent;
      callers must cancel the sheet.
    """
    from odoo_mcp.errors import OperationNotAllowedError

    # hr.leave does NOT expose refuse — only the user's own cancel.
    with pytest.raises(OperationNotAllowedError):
        resolve_document_action("hr.leave", "refuse")

    # hr.expense (singular) is intentionally not in the map; callers
    # cancel via hr.expense.sheet.
    with pytest.raises(OperationNotAllowedError):
        resolve_document_action("hr.expense", "cancel")


def test_map_rejects_unknown_pair() -> None:
    from odoo_mcp.errors import OperationNotAllowedError

    with pytest.raises(OperationNotAllowedError, match="Supported"):
        resolve_document_action("res.partner", "confirm")
    with pytest.raises(OperationNotAllowedError):
        resolve_document_action("purchase.order", "explode")


def test_action_verbs_match_map() -> None:
    assert set(DOCUMENT_ACTION_VERBS) == {"confirm", "cancel", "post", "validate"}
    assert "purchase.order:confirm" in supported_pairs()


def test_tool_registered() -> None:
    assert "odoo_run_document_action" in {t.name for t in build_tools()}


# ---------------------------------------------------------------------------
# Dev flow
# ---------------------------------------------------------------------------


def test_dev_dry_run_returns_preview_with_states(tmp_path: Path) -> None:
    app, fake = _build(tmp_path, production=False)
    fake._states = {977: "draft"}
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "purchase.order",
            "record_ids": [977],
            "action": "confirm",
            "dry_run": True,
        },
    )
    assert payload["ok"] is True
    assert payload["preview"] is True
    assert payload["odoo_method"] == "button_confirm"
    assert payload["current_states"] == [{"id": 977, "state": "draft"}]
    assert "confirmation_token" in payload
    # Nothing actually called.
    assert fake.action_calls == []


def test_dev_commit_runs_the_method(tmp_path: Path) -> None:
    app, fake = _build(tmp_path, production=False)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "purchase.order",
            "record_ids": [977],
            "action": "confirm",
            "dry_run": False,
        },
    )
    assert payload["ok"] is True
    assert payload["committed"] is True
    assert fake.action_calls == [("purchase.order", "button_confirm", [977])]


def test_unmapped_pair_refused_via_tool(tmp_path: Path) -> None:
    app, fake = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "record_ids": [1],
            "action": "confirm",
        },
    )
    assert payload["ok"] is False
    assert "res.partner" in payload["error"]
    assert fake.action_calls == []


def test_wizard_dict_return_flags_needs_manual(tmp_path: Path) -> None:
    """stock.picking.button_validate can return a wizard dict — surface it."""
    app, fake = _build(tmp_path, production=False, action_return={"type": "ir.actions.act_window"})
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "stock.picking",
            "record_ids": [5],
            "action": "validate",
            "dry_run": False,
        },
    )
    assert payload["ok"] is True
    assert payload["committed"] is False
    assert payload["needs_manual_completion"] is True
    assert "wizard" in payload["note"].lower()


# ---------------------------------------------------------------------------
# Wizard completion: sale.order.cancel auto-completes the wizard
# ---------------------------------------------------------------------------
#
# Real incident (logged 2026-06-16): an agent ran cancel on a sale.order
# with linked pickings, Odoo returned the sale.order.cancel wizard, and
# the agent had no way to drive it from MCP. It defaulted to "create a
# new SO instead" — costly when the operator just wanted a cancel.
# The dispatcher now auto-completes a specific allowlisted set of these
# wizards: read the act_window dict, create the wizard record with the
# right default field, call its method. Same two steps the UI runs when
# the user clicks the confirm button.


class _CancelWizardFakeClient(_FakeClient):
    """Simulates the Odoo SO-cancel-with-wizard flow.

    Round one: ``sale.order.action_cancel`` returns the wizard
    descriptor dict (linked pickings present). Round two: dispatcher
    ``create`` on ``sale.order.cancel`` returns a fresh wizard id;
    ``call_document_action`` on the wizard's ``action_cancel`` returns
    True (= cancel actually applied).
    """

    def __init__(self) -> None:
        super().__init__(action_return=None)
        self._next_action_returns: list[Any] = [
            {
                "type": "ir.actions.act_window",
                "res_model": "sale.order.cancel",
                "context": {"default_order_id": 1199},
            },
            True,
        ]
        self.create_calls: list[tuple[str, dict[str, Any]]] = []
        self._next_create_id = 555

    def call_document_action(self, model: str, method: str, record_ids: list[int]) -> Any:
        self.action_calls.append((model, method, list(record_ids)))
        if self._next_action_returns:
            return self._next_action_returns.pop(0)
        return True

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.create_calls.append((model, dict(values)))
        new_id = self._next_create_id
        self._next_create_id += 1
        return new_id


def _build_with_client(tmp_path: Path, fake: _FakeClient) -> OdooMcpApp:
    """Like ``_build`` but accepts a pre-constructed client. The shared
    helper hardcodes _FakeClient, but the wizard tests need a fake
    that also records ``create`` calls."""
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
    real = OdooClient(cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={cfg.name: cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rl = RateLimiter()
    rl.configure(cfg.name, cfg.rate_limit_per_minute)
    rt = InstanceRuntime(config=cfg, client=real)
    rt.client = fake  # type: ignore[assignment]
    return OdooMcpApp(
        config=app_cfg,
        audit=AuditLog(app_cfg.audit_log_path),
        prod_guard=ProdGuard(),
        rate_limiter=rl,
        instances={cfg.name: rt},
    )


def test_sale_order_cancel_drives_the_wizard_to_completion(tmp_path: Path) -> None:
    """End-to-end: the SO cancel returns a wizard, the dispatcher
    creates the sale.order.cancel record with default_order_id and
    calls the wizard's action_cancel. The final response reports
    ``committed=true`` — the agent doesn't need to flip to the UI."""
    fake = _CancelWizardFakeClient()
    app = _build_with_client(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "sale.order",
            "record_ids": [1199],
            "action": "cancel",
            "dry_run": False,
        },
    )
    assert payload["ok"] is True
    assert payload["committed"] is True
    assert payload.get("needs_manual_completion") is not True
    # And it tells the operator exactly which wizard ran.
    assert payload["wizard"]["wizard_model"] == "sale.order.cancel"
    assert payload["wizard"]["wizard_method"] == "action_cancel"
    assert len(payload["wizard"]["steps"]) == 1
    step = payload["wizard"]["steps"][0]
    assert step["origin_id"] == 1199
    assert step["ok"] is True
    # The create call carried the origin_field correctly.
    assert len(fake.create_calls) == 1
    create_model, create_values = fake.create_calls[0]
    assert create_model == "sale.order.cancel"
    assert create_values == {"order_id": 1199}
    # Two action calls: the SO's action_cancel (returned the wizard
    # dict), then the wizard's action_cancel (returned True).
    assert fake.action_calls == [
        ("sale.order", "action_cancel", [1199]),
        ("sale.order.cancel", "action_cancel", [555]),
    ]


def test_unknown_wizard_still_falls_back_to_needs_manual(tmp_path: Path) -> None:
    """Hard guarantee: actions NOT in _WIZARD_COMPLETIONS still surface
    the old needs_manual_completion signal. A refactor that wrongly
    sent every wizard dict through auto-completion would silently run
    arbitrary wizard methods — exactly the surface we wanted to bound.

    stock.picking.button_validate IS NOT in the wizard-completion map
    (its backorder wizard has user-meaningful fields), so the old
    behaviour must still hold."""
    app, fake = _build(tmp_path, production=False, action_return={"type": "ir.actions.act_window"})
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "stock.picking",
            "record_ids": [5],
            "action": "validate",
            "dry_run": False,
        },
    )
    assert payload["needs_manual_completion"] is True
    assert payload["committed"] is False
    # The "wizard" block in the response is ONLY populated when we
    # actually drove the wizard. Otherwise it must be absent.
    assert "wizard" not in payload


def test_sale_order_cancel_without_linked_records_skips_wizard(tmp_path: Path) -> None:
    """action_cancel returns True directly when there are no linked
    pickings/invoices (Odoo's ``_show_cancel_wizard()`` returns False).
    The dispatcher must NOT then try to auto-create a sale.order.cancel
    record — that would create a stray wizard row on every cancel."""
    fake = _CancelWizardFakeClient()
    # Override: the SO action returns True directly, no wizard.
    fake._next_action_returns = [True]
    app = _build_with_client(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "sale.order",
            "record_ids": [42],
            "action": "cancel",
            "dry_run": False,
        },
    )
    assert payload["committed"] is True
    assert "wizard" not in payload
    assert fake.create_calls == []


def test_wizard_completion_map_includes_sale_order_cancel() -> None:
    """Catalogue-level pin: the (sale.order, cancel) entry must remain
    in the map. The real incident that triggered this feature would
    re-surface if a refactor dropped it."""
    from odoo_mcp.security.document_actions import resolve_wizard_completion

    spec = resolve_wizard_completion("sale.order", "cancel")
    assert spec is not None
    assert spec.wizard_model == "sale.order.cancel"
    assert spec.wizard_method == "action_cancel"
    assert spec.origin_field == "order_id"


def test_wizard_chained_wizard_marks_still_pending(tmp_path: Path) -> None:
    """If the wizard itself returns ANOTHER wizard (rare, but observed
    on some modules), don't claim victory. ``committed`` flips back to
    false and the per-step record reports the chain."""
    fake = _CancelWizardFakeClient()
    # SO returns wizard dict; wizard's action_cancel also returns a
    # wizard dict (e.g. an additional confirmation step).
    fake._next_action_returns = [
        {"type": "ir.actions.act_window", "res_model": "sale.order.cancel"},
        {"type": "ir.actions.act_window", "res_model": "some.other.wizard"},
    ]
    app = _build_with_client(tmp_path, fake)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "sale.order",
            "record_ids": [99],
            "action": "cancel",
            "dry_run": False,
        },
    )
    assert payload["committed"] is False
    assert payload["needs_manual_completion"] is True
    step = payload["wizard"]["steps"][0]
    assert step["wizard_returned_wizard"] is True


# ---------------------------------------------------------------------------
# Prod flow
# ---------------------------------------------------------------------------


def test_prod_requires_unlock_then_token(tmp_path: Path) -> None:
    app, fake = _build(tmp_path, production=True)
    disp = Dispatcher(app)

    # No unlock → refused.
    blocked = _call(
        disp,
        {
            "instance": "dev",
            "model": "sale.order",
            "record_ids": [10],
            "action": "confirm",
        },
    )
    assert blocked["ok"] is False

    # Unlock.
    unlock = asyncio.run(disp.call("odoo_enable_prod_writes", {"instance": "dev"}))
    assert json.loads(unlock[0].text)["ok"] is True

    # Dry-run → token.
    preview = _call(
        disp,
        {
            "instance": "dev",
            "model": "sale.order",
            "record_ids": [10],
            "action": "confirm",
        },
    )
    assert preview["preview"] is True
    token = preview["confirmation_token"]

    # Commit without token → refused.
    no_token = _call(
        disp,
        {
            "instance": "dev",
            "model": "sale.order",
            "record_ids": [10],
            "action": "confirm",
            "dry_run": False,
        },
    )
    assert no_token["ok"] is False
    assert "confirmation_token" in no_token["error"]

    # Commit with token → runs.
    commit = _call(
        disp,
        {
            "instance": "dev",
            "model": "sale.order",
            "record_ids": [10],
            "action": "confirm",
            "dry_run": False,
            "confirmation_token": token,
        },
    )
    assert commit["ok"] is True
    assert commit["committed"] is True
    assert fake.action_calls == [("sale.order", "action_confirm", [10])]


def test_read_only_session_blocks_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_READ_ONLY", "1")
    app, fake = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "purchase.order",
            "record_ids": [1],
            "action": "confirm",
        },
    )
    assert payload["ok"] is False
    assert "read-only" in payload["error"].lower()
    assert fake.action_calls == []


def test_id_count_cap_enforced(tmp_path: Path) -> None:
    app, fake = _build(tmp_path)
    payload = _call(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "purchase.order",
            "record_ids": list(range(1, 502)),
            "action": "confirm",
        },
    )
    assert payload["ok"] is False
    assert fake.action_calls == []
