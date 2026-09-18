"""``odoo_enable_prod_writes`` must disclose when it reset a live budget.

``ProdGuard.unlock`` renews an active window **in place**: expiry and the
commit budget reset while the window identity is preserved. The burst
budget is the only hard ceiling on how many production commits a single
unlock authorises, and the burst-limit error tells the agent in so many
words to call ``odoo_enable_prod_writes`` again to renew it. Before this
fix a renewal produced a byte-identical response and a byte-identical
audit row to a first unlock, so neither the agent nor an operator reading
the 30-day log could tell "a second session was opened" from "the budget
was reset mid-window and N commits were discarded".

Disclosure only — the unlock policy is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from test_run_document_action import _build, _call

from odoo_mcp.dispatcher import Dispatcher
from odoo_mcp.security.prod_guard import DEFAULT_MAX_COMMITS_PER_UNLOCK as BUDGET


def _unlock(disp: Dispatcher) -> dict[str, Any]:
    contents = asyncio.run(disp.call("odoo_enable_prod_writes", {"instance": "dev"}))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _burn_commits(disp: Dispatcher, count: int) -> None:
    """Consume ``count`` commits from the active burst budget."""
    for rid in range(100, 100 + count):
        preview = _call(
            disp,
            {"instance": "dev", "model": "sale.order", "record_ids": [rid], "action": "confirm"},
        )
        committed = _call(
            disp,
            {
                "instance": "dev",
                "model": "sale.order",
                "record_ids": [rid],
                "action": "confirm",
                "dry_run": False,
                "confirmation_token": preview["confirmation_token"],
            },
        )
        assert committed["ok"] is True


def _unlock_rows(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    # ``.get`` — the log opens with a non-event header line.
    return [r for r in rows if r.get("tool") == "odoo_enable_prod_writes"]


def test_first_unlock_states_it_is_not_a_renewal(tmp_path: Path) -> None:
    app, _ = _build(tmp_path, production=True)
    payload = _unlock(Dispatcher(app))

    assert payload["ok"] is True
    assert payload["renewed"] is False
    # Explicit null, not an omitted key: an operator must be able to tell
    # "nothing was renewed" from "this row predates the fix".
    assert "commits_remaining_before" in payload
    assert payload["commits_remaining_before"] is None
    assert payload["commits_remaining"] == BUDGET


def test_renewal_reports_the_budget_it_discarded(tmp_path: Path) -> None:
    app, _ = _build(tmp_path, production=True)
    disp = Dispatcher(app)

    _unlock(disp)
    _burn_commits(disp, 3)
    assert app.prod_guard.commits_remaining("dev") == BUDGET - 3

    payload = _unlock(disp)
    assert payload["renewed"] is True
    assert payload["commits_remaining_before"] == BUDGET - 3
    assert payload["commits_remaining"] == BUDGET


def test_renewal_note_names_the_discarded_budget(tmp_path: Path) -> None:
    """The note is what a human actually reads in the client transcript."""
    app, _ = _build(tmp_path, production=True)
    disp = Dispatcher(app)

    first = _unlock(disp)
    assert "RENEWED" not in first["note"]

    _burn_commits(disp, 4)
    renewal = _unlock(disp)
    assert "RENEWED" in renewal["note"]
    assert str(BUDGET - 4) in renewal["note"]


def test_a_renewal_is_distinguishable_from_a_first_unlock(tmp_path: Path) -> None:
    """The shape of the bug: the two responses used to be byte-identical."""
    app, _ = _build(tmp_path, production=True)
    disp = Dispatcher(app)

    first = _unlock(disp)
    _burn_commits(disp, 2)
    second = _unlock(disp)

    assert first != second


def test_audit_row_records_renewal_and_prior_budget(tmp_path: Path) -> None:
    app, _ = _build(tmp_path, production=True)
    disp = Dispatcher(app)

    _unlock(disp)
    _burn_commits(disp, 5)
    _unlock(disp)

    rows = _unlock_rows(tmp_path)
    assert len(rows) == 2

    fresh, renewal = rows
    assert fresh["details"]["event"] == "WRITE_UNLOCK"
    assert fresh["details"]["renewed"] is False
    assert fresh["details"]["commits_remaining_before"] is None

    assert renewal["details"]["event"] == "WRITE_UNLOCK"
    assert renewal["details"]["renewed"] is True
    assert renewal["details"]["commits_remaining_before"] == BUDGET - 5


def test_unlock_after_expiry_is_not_a_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``renewed`` means "a live window was reset", not "ever unlocked before".

    An expired window is gone — ``unlock`` gives it a fresh identity and
    stale tokens can never be replayed against it — so reporting that as a
    renewal would be a lie in the safer direction.
    """
    app, _ = _build(tmp_path, production=True)
    disp = Dispatcher(app)

    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    _unlock(disp)

    # 16 minutes later: past the 15-minute unlock TTL.
    monkeypatch.setattr(time, "monotonic", lambda: base + 16 * 60)
    payload = _unlock(disp)

    assert payload["renewed"] is False
    assert payload["commits_remaining_before"] is None


def test_non_production_instance_is_still_refused(tmp_path: Path) -> None:
    """Control: reading the budget before unlocking did not move the gate."""
    app, _ = _build(tmp_path, production=False)
    payload = _unlock(Dispatcher(app))

    assert payload["ok"] is False
    assert "not flagged as production" in payload["error"]
    # The refusal is audited, but as a failure row — no WRITE_UNLOCK event,
    # so a refused call can never be mistaken for a grant.
    rows = _unlock_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["result"] != "ok"
    assert "event" not in rows[0]["details"]
