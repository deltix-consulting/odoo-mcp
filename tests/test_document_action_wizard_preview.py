"""The dry-run preview must disclose the wizard the commit would drive.

``odoo_run_document_action`` auto-completes a hardcoded set of follow-up
wizards (``security.document_actions._WIZARD_COMPLETIONS``). On commit that
is a second write, on a DIFFERENT model, under the SAME confirmation token:
the dispatcher creates a transient ``wizard_model`` row per record id and
calls ``wizard_method`` on it. The commit response has always reported this
(the ``wizard`` block); the dry-run preview — the thing the operator
actually approves — said nothing.

These tests pin the preview side of that pair. ``tests/`` has no
``__init__.py``, so the shared harness is imported by module name.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from test_run_document_action import (  # type: ignore[import-not-found]
    _build,
    _build_with_client,
    _call,
    _CancelWizardFakeClient,
)

from odoo_mcp.dispatcher import Dispatcher
from odoo_mcp.security.document_actions import (
    _WIZARD_COMPLETIONS,
    resolve_wizard_completion,
    supported_wizard_completion_pairs,
)

# ``dry_run`` is explicit: the shared harness builds a NON-production
# instance, where ``effective_dry_run`` defaults to False and an unqualified
# call would commit.
_WIZARD_ARGS: dict[str, Any] = {
    "instance": "dev",
    "model": "sale.order",
    "record_ids": [1199],
    "action": "cancel",
    "dry_run": True,
}


def _audit_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """Parsed audit rows, minus the ``audit_log_open`` marker line.

    ``AuditLog._open`` writes a first line with no ``tool`` key; every test
    that reads the log has to filter it or KeyError on row zero.
    """
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [r for r in rows if "tool" in r]


# ---------------------------------------------------------------------------
# The preview names the wizard
# ---------------------------------------------------------------------------


def test_dry_run_discloses_the_wizard_it_would_drive(tmp_path: Path) -> None:
    """sale.order:cancel is in the wizard-completion map, so the preview has
    to name the model and method the commit would additionally touch."""
    app, _fake = _build(tmp_path)
    preview = _call(Dispatcher(app), dict(_WIZARD_ARGS))

    assert preview["preview"] is True
    wizard = preview["wizard_completion"]
    assert wizard["wizard_model"] == "sale.order.cancel"
    assert wizard["wizard_method"] == "action_cancel"
    assert wizard["origin_field"] == "order_id"


def test_dry_run_reports_how_many_wizard_records_it_would_create(
    tmp_path: Path,
) -> None:
    """One wizard row per record id — the operator approving a 3-record
    cancel is approving 3 transient creates, not one."""
    app, _fake = _build(tmp_path)
    preview = _call(
        Dispatcher(app),
        {**_WIZARD_ARGS, "record_ids": [1199, 1200, 1201]},
    )

    assert preview["wizard_completion"]["records"] == 3


def test_dry_run_warns_that_no_second_token_is_taken(tmp_path: Path) -> None:
    """``_complete_returned_wizard`` deliberately skips a second prod-guard
    token. That is only defensible if the preview says so."""
    app, _fake = _build(tmp_path)
    preview = _call(Dispatcher(app), dict(_WIZARD_ARGS))

    note = preview["wizard_completion"]["note"].lower()
    assert "allowlist" in note
    assert "confirmation token" in note


def test_dry_run_creates_no_wizard_record(tmp_path: Path) -> None:
    """Disclosure is a dict lookup, not a rehearsal. A preview that created
    a transient row would leave stray wizard records behind on every
    unapproved dry run."""
    fake = _CancelWizardFakeClient()
    app = _build_with_client(tmp_path, fake)
    preview = _call(Dispatcher(app), dict(_WIZARD_ARGS))

    assert "wizard_completion" in preview
    assert fake.create_calls == []
    assert fake.action_calls == []


# ---------------------------------------------------------------------------
# ... and stays quiet when there is no wizard
# ---------------------------------------------------------------------------


def test_action_without_a_wizard_omits_the_key(tmp_path: Path) -> None:
    """Omitted, not ``None``/``{}``: an empty key reads as a claim about
    this action rather than "not applicable". Same reasoning as
    ``states_after``."""
    app, _fake = _build(tmp_path)
    preview = _call(
        Dispatcher(app),
        {**_WIZARD_ARGS, "model": "purchase.order", "action": "confirm"},
    )

    assert preview["preview"] is True
    assert "wizard_completion" not in preview


def test_stock_picking_validate_omits_the_key(tmp_path: Path) -> None:
    """stock.picking:validate returns a backorder wizard in real Odoo but is
    deliberately NOT auto-completed, so the preview must not promise it."""
    app, _fake = _build(tmp_path)
    preview = _call(
        Dispatcher(app),
        {**_WIZARD_ARGS, "model": "stock.picking", "action": "validate"},
    )

    assert "wizard_completion" not in preview
    assert resolve_wizard_completion("stock.picking", "validate") is None


# ---------------------------------------------------------------------------
# Preview / commit / audit agree
# ---------------------------------------------------------------------------


def test_preview_names_the_same_wizard_the_commit_drives(tmp_path: Path) -> None:
    """The asymmetry that made this bug-shaped: the commit already reported
    the wizard. Pin that the two now describe the same thing."""
    fake = _CancelWizardFakeClient()
    app = _build_with_client(tmp_path, fake)
    disp = Dispatcher(app)

    preview = _call(disp, dict(_WIZARD_ARGS))
    committed = _call(disp, {**_WIZARD_ARGS, "dry_run": False})

    assert committed["committed"] is True
    assert preview["wizard_completion"]["wizard_model"] == committed["wizard"]["wizard_model"]
    assert preview["wizard_completion"]["wizard_method"] == committed["wizard"]["wizard_method"]
    # And the disclosure was accurate: one create on the named model.
    assert fake.create_calls == [("sale.order.cancel", {"order_id": 1199})]


def test_dry_run_audit_row_records_the_wizard_model(tmp_path: Path) -> None:
    """The commit audit row carries ``wizard_model``; the dry-run row did
    not, so an operator reconstructing an approval from audit.jsonl alone
    could not see the wizard was in scope."""
    app, _fake = _build(tmp_path)
    _call(Dispatcher(app), dict(_WIZARD_ARGS))

    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["dry_run"] is True
    assert rows[0]["details"]["wizard_model"] == "sale.order.cancel"


def test_dry_run_audit_row_omits_wizard_model_without_a_wizard(
    tmp_path: Path,
) -> None:
    app, _fake = _build(tmp_path)
    _call(
        Dispatcher(app),
        {**_WIZARD_ARGS, "model": "purchase.order", "action": "confirm"},
    )

    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    assert "wizard_model" not in rows[0]["details"]


# ---------------------------------------------------------------------------
# The map stays covered
# ---------------------------------------------------------------------------


def test_every_wizard_completion_pair_is_disclosed_in_its_preview(
    tmp_path: Path,
) -> None:
    """Adding a row to ``_WIZARD_COMPLETIONS`` grants the agent a second
    write on a new model. Drive every mapped pair through a dry run so a
    future row cannot ship undisclosed."""
    app, _fake = _build(tmp_path)
    disp = Dispatcher(app)

    assert _WIZARD_COMPLETIONS, "map is empty — this test would pass vacuously"
    for model, action in _WIZARD_COMPLETIONS:
        preview = _call(
            disp,
            {
                "instance": "dev",
                "model": model,
                "record_ids": [1],
                "action": action,
                "dry_run": True,
            },
        )
        spec = resolve_wizard_completion(model, action)
        assert spec is not None
        assert preview["wizard_completion"]["wizard_model"] == spec.wizard_model


def test_supported_wizard_completion_pairs_matches_the_map() -> None:
    """The map's public summary helper, pinned so the two can't drift."""
    assert supported_wizard_completion_pairs() == sorted(
        f"{model}:{action}" for (model, action) in _WIZARD_COMPLETIONS
    )


def test_read_only_session_never_reaches_the_preview(tmp_path: Path, monkeypatch: Any) -> None:
    """The disclosure is added after the read-only gate, not before it."""
    monkeypatch.setenv("ODOO_MCP_READ_ONLY", "1")
    app, _fake = _build(tmp_path)
    payload = _call(Dispatcher(app), dict(_WIZARD_ARGS))

    assert payload["ok"] is False
    assert "wizard_completion" not in payload


def test_asyncio_harness_smoke(tmp_path: Path) -> None:
    """Guard against the harness silently not dispatching at all."""
    app, _fake = _build(tmp_path)
    contents = asyncio.run(Dispatcher(app).call("odoo_run_document_action", dict(_WIZARD_ARGS)))
    assert json.loads(contents[0].text)["ok"] is True
