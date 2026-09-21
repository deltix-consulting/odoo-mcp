"""A commit Odoo rejects must not cost a unit of the per-unlock burst budget.

Four surfaces state the contract: the v0.19.1 changelog ("Only a successful
commit decrements the burst budget"), the ``commits_remaining_note`` stamped
on every production preview, the ``ProdGuardError`` raised when the budget is
exhausted ("Dry-runs do NOT count toward this budget; only successful commits
do"), and README ("caps real commits per unlock"). ``consume_pending``
decrements the counter BEFORE the RPC — deliberately, so a commit whose
outcome is unknown stays counted — and nothing gave the unit back when Odoo
answered with a fault, i.e. when its RPC layer had already rolled the
transaction back and nothing was written.

These tests pin the refund to the one case where the rejection is certain
(an XML-RPC ``Fault`` that is not the post-commit marshalling failure) and
pin the charge everywhere else.
"""

from __future__ import annotations

import asyncio
import json
import xmlrpc.client
from typing import Any

import pytest

from odoo_mcp.dispatcher import Dispatcher, OdooMcpApp
from odoo_mcp.errors import OdooRemoteError, OdooTransportError
from odoo_mcp.security.prod_guard import ProdGuard

_FIELDS = {
    "id": {"type": "integer", "string": "ID"},
    "name": {"type": "char", "string": "Name"},
    "active": {"type": "boolean", "string": "Active"},
}


def _fault(text: str) -> OdooRemoteError:
    """Shape ``OdooClient._execute`` produces for an Odoo fault."""
    cause = xmlrpc.client.Fault(1, text)
    err = OdooRemoteError(f"Odoo fault on res.partner.write: {text}", server_text=True)
    err.__cause__ = cause
    return err


class _RejectingClient:
    """Fake client whose write-family calls raise what the test configures."""

    def __init__(self, raise_with: Exception | None) -> None:
        self.raise_with = raise_with
        self.calls: list[str] = []
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return dict(_FIELDS)

    def _maybe_raise(self, name: str) -> None:
        self.calls.append(name)
        if self.raise_with is not None:
            raise self.raise_with

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self._maybe_raise("write")
        return True

    def create(self, model: str, values: dict[str, Any]) -> int:
        self._maybe_raise("create")
        return 42

    def unlink(self, model: str, ids: list[int]) -> bool:
        self._maybe_raise("unlink")
        return True

    def call_document_action(self, model: str, method: str, ids: list[int]) -> Any:
        self._maybe_raise("call_document_action")
        return True

    def message_post(self, model: str, record_id: int, body: str, **kwargs: Any) -> int:
        self._maybe_raise("message_post")
        return 7

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        return [{"id": i, "state": "draft"} for i in ids]


def _call(dispatcher: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(dispatcher.call(tool, args))
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


def _prod_app(make_app: Any, raise_with: Exception | None, *, max_commits: int = 3) -> OdooMcpApp:
    app: OdooMcpApp = make_app(
        client=_RejectingClient(raise_with),
        name="prod",
        credentials_env_prefix="ODOO_MCP_PROD",
        production=True,
    )
    app.prod_guard.unlock("prod", production=True, max_commits=max_commits)
    return app


def _write_args(**extra: Any) -> dict[str, Any]:
    return {
        "instance": "prod",
        "model": "res.partner",
        "ids": [1],
        "values": {"name": "x"},
        **extra,
    }


def _preview_then_commit(app: OdooMcpApp, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    dispatcher = Dispatcher(app)
    preview = _call(dispatcher, tool, args)
    assert preview["ok"] is True and preview["preview"] is True, preview
    return _call(
        dispatcher,
        tool,
        {**args, "dry_run": False, "confirmation_token": preview["confirmation_token"]},
    )


# -- ProdGuard unit level ------------------------------------------------------


def test_refund_restores_the_unit_consume_pending_charged() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=100.0, max_commits=3)
    token = guard.create_pending("prod", "write", "res.partner", "s", now=100.0)
    guard.consume_pending(token, "prod", "write", "res.partner", now=101.0)
    assert guard.commits_remaining("prod", now=101.0) == 2

    assert guard.refund_commit("prod", now=102.0) == 3
    assert guard.commits_remaining("prod", now=102.0) == 3


def test_refund_never_exceeds_the_window_budget() -> None:
    """A refund with nothing charged (or after an in-place renewal reset the
    counter) must not hand the operator more commits than they configured."""
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=100.0, max_commits=3)
    assert guard.refund_commit("prod", now=101.0) is None
    assert guard.commits_remaining("prod", now=101.0) == 3

    token = guard.create_pending("prod", "write", "res.partner", "s", now=101.0)
    guard.consume_pending(token, "prod", "write", "res.partner", now=102.0)
    guard.unlock("prod", production=True, now=103.0, max_commits=3)  # renew in place
    assert guard.refund_commit("prod", now=104.0) is None
    assert guard.commits_remaining("prod", now=104.0) == 3


def test_refund_after_the_window_expired_is_a_noop() -> None:
    guard = ProdGuard()
    guard.unlock("prod", production=True, now=100.0, max_commits=3, ttl_seconds=10)
    token = guard.create_pending("prod", "write", "res.partner", "s", now=100.0)
    guard.consume_pending(token, "prod", "write", "res.partner", now=101.0)
    assert guard.refund_commit("prod", now=200.0) is None
    assert guard.commits_remaining("prod", now=200.0) is None


# -- Dispatcher level ----------------------------------------------------------


def test_rejected_write_does_not_cost_a_commit(make_app: Any) -> None:
    app = _prod_app(make_app, _fault("ValidationError: Name must be unique"))
    failed = _preview_then_commit(app, "odoo_write", _write_args())
    assert failed["ok"] is False and failed["error_code"] == "odoo_remote"
    assert app.prod_guard.commits_remaining("prod") == 3
    # The error itself carries the observation, not only the next preview.
    assert failed["commits_remaining"] == 3
    assert "not charged" in failed["commits_remaining_note"]
    # A fresh preview agrees with the note it stamps.
    preview = _call(Dispatcher(app), "odoo_write", _write_args())
    assert preview["commits_remaining"] == 3


@pytest.mark.parametrize(
    "tool, args, rpc",
    [
        (
            "odoo_create",
            {"instance": "prod", "model": "res.partner", "values": {"name": "x"}},
            "create",
        ),
        (
            "odoo_archive_or_delete",
            {"instance": "prod", "model": "res.partner", "ids": [1], "mode": "delete"},
            "unlink",
        ),
        (
            "odoo_run_document_action",
            {"instance": "prod", "model": "sale.order", "action": "confirm", "record_ids": [1]},
            "call_document_action",
        ),
        (
            "odoo_log_note",
            {"instance": "prod", "model": "res.partner", "record_id": 1, "body": "hi"},
            "message_post",
        ),
    ],
)
def test_every_commit_handler_refunds_a_rejection(
    make_app: Any, tool: str, args: dict[str, Any], rpc: str
) -> None:
    app = _prod_app(make_app, _fault("AccessError: not allowed"))
    failed = _preview_then_commit(app, tool, args)
    assert failed["ok"] is False and failed["error_code"] == "odoo_remote"
    client = app.instances["prod"].client
    assert client.calls[-1] == rpc  # type: ignore[attr-defined]
    assert app.prod_guard.commits_remaining("prod") == 3
    assert failed["commits_remaining"] == 3


def test_transport_failure_after_send_keeps_the_charge(make_app: Any) -> None:
    """The write may have landed; counting it is the fail-closed direction."""
    app = _prod_app(make_app, OdooTransportError("Timeout calling res.partner.write"))
    failed = _preview_then_commit(app, "odoo_write", _write_args())
    assert failed["ok"] is False and failed["error_code"] == "odoo_transport"
    assert app.prod_guard.commits_remaining("prod") == 2
    assert "commits_remaining" not in failed


def test_post_commit_marshal_fault_keeps_the_charge(make_app: Any) -> None:
    """Odoo's RPC controller commits before it marshals the response, so a
    ``cannot marshal`` fault arrives after the write is durable."""
    app = _prod_app(make_app, _fault("TypeError: cannot marshal <class 'res.partner'> objects"))
    failed = _preview_then_commit(app, "odoo_write", _write_args())
    assert failed["ok"] is False and failed["error_code"] == "odoo_remote"
    assert app.prod_guard.commits_remaining("prod") == 2
    assert "commits_remaining" not in failed


def test_fault_in_a_follow_up_wizard_step_keeps_the_charge(make_app: Any) -> None:
    """``sale.order.cancel`` returns a wizard the dispatcher then drives with
    a second ``create``. The action itself succeeded — the cancel is durable
    on Odoo's side — so a fault in that follow-up is not a rejected commit."""

    class _WizardCreateFaults(_RejectingClient):
        def call_document_action(self, model: str, method: str, ids: list[int]) -> Any:
            self.calls.append("call_document_action")
            return {"type": "ir.actions.act_window", "res_model": "sale.order.cancel"}

        def create(self, model: str, values: dict[str, Any]) -> int:
            self.calls.append("create")
            raise _fault("AccessError: wizard")

    app: OdooMcpApp = make_app(
        client=_WizardCreateFaults(None),
        name="prod",
        credentials_env_prefix="ODOO_MCP_PROD",
        production=True,
    )
    app.prod_guard.unlock("prod", production=True, max_commits=3)
    failed = _preview_then_commit(
        app,
        "odoo_run_document_action",
        {"instance": "prod", "model": "sale.order", "action": "cancel", "record_ids": [1]},
    )
    assert failed["ok"] is False and failed["error_code"] == "odoo_remote"
    assert app.instances["prod"].client.calls[-2:] == ["call_document_action", "create"]  # type: ignore[attr-defined]
    assert app.prod_guard.commits_remaining("prod") == 2
    assert "commits_remaining" not in failed


def test_remote_error_without_a_fault_keeps_the_charge(make_app: Any) -> None:
    """``OdooRemoteError`` is also raised when the RPC *succeeded* with an
    unexpected return shape — the record may exist, so no refund."""
    app = _prod_app(make_app, OdooRemoteError("write(res.partner) returned unexpected type str"))
    failed = _preview_then_commit(app, "odoo_write", _write_args())
    assert failed["ok"] is False and failed["error_code"] == "odoo_remote"
    assert app.prod_guard.commits_remaining("prod") == 2
    assert "commits_remaining" not in failed


def test_refund_is_recorded_on_the_audit_failure_row(make_app: Any) -> None:
    app = _prod_app(make_app, _fault("UserError: nope"))
    _preview_then_commit(app, "odoo_write", _write_args())
    rows = [json.loads(line) for line in app.config.audit_log_path.read_text().splitlines()]
    failure = [r for r in rows if r.get("result") == "odoo_remote"][-1]
    assert failure["details"]["commit_budget"] == "refunded"


def test_dev_rejection_reports_nothing_about_a_budget(make_app: Any) -> None:
    """The budget is a production concept; a dev fault stays a plain fault."""
    app: OdooMcpApp = make_app(client=_RejectingClient(_fault("UserError: nope")))
    failed = _call(Dispatcher(app), "odoo_write", {**_write_args(), "instance": "dev"})
    assert failed["ok"] is False and failed["error_code"] == "odoo_remote"
    assert "commits_remaining" not in failed
