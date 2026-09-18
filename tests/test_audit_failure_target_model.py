"""The failure audit row must name the model the call targeted.

``_audit_failure`` only ever sees the caller's RAW arguments — the
handler's internal ``res_model`` -> ``model`` translation has not run
(and, for a refusal raised inside ``_begin``, never will). Reading
``arguments["model"]`` directly therefore logged ``model: null`` for
every refused ``odoo_create_attachment`` call, whose public schema
names the target ``res_model``.

That inverted the disclosure: the row an operator actually reviews —
the refused one — said less about its target than the row the same
call would have written on success. ``odoo_create_attachment`` is the
only tool that reads the operator's filesystem, so "a file read was
refused" without "aimed at which record" is the least useful place to
drop the target.

The tests below pin three things: the alias resolves on the failure
path, tools that already use ``model`` are unaffected, and genuinely
model-less tools still log ``null`` rather than an invented value.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

# tests/ has no __init__.py — import the attachment harness absolutely.
from test_create_attachment import _AttachFake, _b64, _build, _call

from odoo_mcp.dispatcher import Dispatcher, _target_model


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    """Audit rows, minus the ``audit_log_open`` marker line."""
    raw = (tmp_path / "audit.jsonl").read_text().splitlines()
    return [e for e in (json.loads(line) for line in raw) if "tool" in e]


def _attach_args(res_model: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "instance": "dev",
        "res_model": res_model,
        "res_id": 1,
        "filename": "quarterly.pdf",
        "datas_base64": _b64(b"x"),
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# The bug: refused attachment calls dropped their target model
# ---------------------------------------------------------------------------


def test_refused_attachment_audit_row_names_target_model(tmp_path: Path) -> None:
    """A denylist refusal must record the model it refused."""
    dispatcher = Dispatcher(_build(tmp_path, _AttachFake()))
    payload = _call(dispatcher, _attach_args("ir.model"))

    assert payload["ok"] is False
    (row,) = _rows(tmp_path)
    assert row["result"] == "model_not_allowed"
    assert row["model"] == "ir.model"


def test_refused_source_path_read_names_the_record_it_targeted(tmp_path: Path) -> None:
    """The security-relevant case.

    A refused ``source_path`` read is an attempt to pull a file off the
    operator's disk into an Odoo record. The refusal message is about
    the *path*, so before the fix nothing in the row said which record
    the file was headed for — the operator could not tell an
    ``/etc/passwd`` probe aimed at a customer-visible record from a
    fat-fingered path on an internal one.
    """
    dispatcher = Dispatcher(_build(tmp_path, _AttachFake()))
    payload = _call(
        dispatcher,
        _attach_args("account.move", source_path="/etc/passwd", datas_base64=None),
    )

    assert payload["ok"] is False
    (row,) = _rows(tmp_path)
    assert row["model"] == "account.move"
    # The path itself must NOT be logged; only the target it was aimed at.
    assert "/etc/passwd" not in json.dumps(row)


def test_refused_attachment_records_res_model_in_args_shape(tmp_path: Path) -> None:
    """``res_model`` is a non-secret identifier, like ``model``.

    Without it in ``_IDENTIFIER_KEYS`` the arg fell to the
    ``{present, type}`` summary, which ``_sanitize_details`` drops
    outright (it keeps only primitive leaves inside ``args``) — so the
    value never reached the log at all, under any key.
    """
    dispatcher = Dispatcher(_build(tmp_path, _AttachFake()))
    _call(dispatcher, _attach_args("res.users"))

    (row,) = _rows(tmp_path)
    assert row["details"]["args"]["res_model"] == "res.users"


def test_failed_and_successful_attachment_agree_on_the_model(tmp_path: Path) -> None:
    """The parity this dimension is actually about.

    Same target, one refused and one committed: the two rows must name
    the same model. Before the fix the refused row said ``None`` while
    the committed row said ``res.partner``.
    """
    fake = _AttachFake()
    dispatcher = Dispatcher(_build(tmp_path, fake))

    # Refused: filename with a path separator, target model is valid.
    _call(dispatcher, _attach_args("res.partner", filename="../../etc/x"))
    # Committed against the same model (dev instance -> dry_run defaults off).
    _call(dispatcher, _attach_args("res.partner", dry_run=False))

    failed, committed = _rows(tmp_path)
    assert failed["result"] != "ok"
    assert committed["result"] == "ok"
    assert failed["model"] == committed["model"] == "res.partner"


# ---------------------------------------------------------------------------
# Controls: no regression, and no invented models
# ---------------------------------------------------------------------------


def test_refused_model_arg_tool_still_names_its_model(tmp_path: Path) -> None:
    """Tools whose schema already uses ``model`` are unaffected.

    ``odoo_write`` against a denylisted model is refused in ``_begin``,
    before the fake client is touched.
    """
    dispatcher = Dispatcher(_build(tmp_path, _AttachFake()))
    contents = asyncio.run(
        dispatcher.call(
            "odoo_write",
            {"instance": "dev", "model": "ir.model", "ids": [1], "values": {"x": 1}},
        )
    )
    assert json.loads(contents[0].text)["ok"] is False

    (row,) = _rows(tmp_path)
    assert row["model"] == "ir.model"


def test_model_less_tool_failure_still_logs_null_model(tmp_path: Path) -> None:
    """``odoo_diagnose_routing`` takes no model — the row must stay null.

    Guards against a fallback chain that invents a model from some
    unrelated argument.
    """
    dispatcher = Dispatcher(_build(tmp_path, _AttachFake()))
    contents = asyncio.run(
        dispatcher.call(
            "odoo_diagnose_routing",
            {"instance": "nope", "product_id": 8, "warehouse_id": 2},
        )
    )
    assert json.loads(contents[0].text)["ok"] is False

    (row,) = _rows(tmp_path)
    assert row["model"] is None


def test_target_model_prefers_canonical_key_and_ignores_non_strings() -> None:
    """``model`` wins over the alias; junk resolves to ``None``."""
    assert _target_model({"model": "a", "res_model": "b"}) == "a"
    assert _target_model({"res_model": "b"}) == "b"
    assert _target_model({"model": "", "res_model": "b"}) == "b"
    assert _target_model({"model": 7, "res_model": "b"}) == "b"
    assert _target_model({"res_model": ["not-a-string"]}) is None
    assert _target_model({}) is None
    assert _target_model("not-a-dict") is None
    assert _target_model(None) is None
