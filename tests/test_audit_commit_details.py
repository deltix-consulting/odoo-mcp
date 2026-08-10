"""The committed audit row must not say less than its own dry run.

Every write tool audits twice: once for the dry run that changed
nothing, once for the commit that did. The dry-run row is a record of
an intention; the commit row is the only forensic record that the side
effect happened. So the commit row must carry at least the descriptive
detail the dry-run row carried — anything the preview knew and the
commit dropped is a hole in the audit trail exactly where it matters.

Two handlers violated that:

- ``odoo_send_message`` recorded ``body_length`` on the dry run and
  omitted it on the commit — the tool that actually emails external
  partners. Nothing else in the row carried the size: ``_args_shape``
  reduces ``body`` to a ``{present, type}`` dict and
  ``_sanitize_details`` then drops that as a non-leaf, so the only
  surviving trace of the body is its name in ``args.keys``. Its sibling
  ``odoo_log_note`` (internal note, physically cannot email) has always
  recorded it on both rows, which is what makes this an oversight
  rather than a policy: the *lower*-risk tool documented more.
- ``odoo_create_attachment`` recorded ``mimetype`` on the dry run and
  omitted it on the commit, though mimetype is what decides how Odoo
  serves the stored file back.

These tests pin the property in both directions: the specific keys, and
the general "commit ⊇ dry run, minus nothing" shape. They do NOT assert
the body/mimetype *values* — the audit log records shape, never
values (see ``_args_shape``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_create_attachment import _AttachFake, _b64
from test_create_attachment import _build as _build_attach
from test_create_attachment import _call as _call_attach
from test_log_note import _build as _build_note
from test_log_note import _call as _call_note
from test_log_note import _LogNoteFake
from test_send_message import _build as _build_msg
from test_send_message import _call as _call_msg

from odoo_mcp.dispatcher import Dispatcher

# Long enough that a truncated-vs-full body would be visibly different
# in the log, and past the 2000-char preview truncation boundary.
_BODY = "x" * 2500


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    """Audit entries for real tool calls, newest last.

    ``AuditLog._open`` writes an ``audit_log_open`` marker as the first
    line, which has no ``tool`` key — filter it or every reader
    KeyErrors on row 0.
    """
    raw = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    return [e for e in (json.loads(line) for line in raw) if "tool" in e]


def _split(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (dry_run_row, commit_row) for a two-call sequence."""
    rows = _rows(tmp_path)
    dry = [r for r in rows if r["dry_run"]]
    commit = [r for r in rows if not r["dry_run"]]
    assert len(dry) == 1, f"expected one dry-run row, got {len(dry)}"
    assert len(commit) == 1, f"expected one commit row, got {len(commit)}"
    return dry[0], commit[0]


@pytest.fixture
def external_comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")


# ---------------------------------------------------------------------------
# odoo_send_message — the outbound-email tool
# ---------------------------------------------------------------------------


def _send(tmp_path: Path) -> None:
    """Dry-run then commit one message on a dev instance."""
    app, fake = _build_msg(tmp_path)
    disp = Dispatcher(app)
    base = {
        "instance": "dev",
        "model": "res.partner",
        "record_id": 7,
        "body": _BODY,
        "partner_ids": [11, 12],
    }
    preview = _call_msg(disp, {**base, "dry_run": True})
    assert preview["preview"] is True
    out = _call_msg(disp, {**base, "dry_run": False})
    assert out["committed"] is True
    # Proves the commit actually reached Odoo, so the row under test is
    # the record of a real send.
    assert len(fake.message_post_calls) == 1
    assert fake.message_post_calls[0]["body"] == _BODY


def test_send_message_commit_records_body_length(tmp_path: Path, external_comms_env: None) -> None:
    """The row for the email that was actually sent must carry the size."""
    _send(tmp_path)
    _, commit = _split(tmp_path)
    assert commit["details"]["body_length"] == len(_BODY)


def test_send_message_commit_details_superset_of_dry_run(
    tmp_path: Path, external_comms_env: None
) -> None:
    """The general property, not just the one key.

    Every descriptive key the preview recorded must survive into the
    commit row. The commit may add outcome keys (``message_id``); it
    may not drop anything.
    """
    _send(tmp_path)
    dry, commit = _split(tmp_path)
    missing = set(dry["details"]) - set(commit["details"])
    assert not missing, f"commit row dropped keys the dry run recorded: {sorted(missing)}"
    assert "message_id" in commit["details"]


def test_send_message_body_length_is_not_reconstructible_from_args_shape(
    tmp_path: Path, external_comms_env: None
) -> None:
    """Why the explicit key is required.

    ``body`` is not in ``_IDENTIFIER_KEYS`` or ``_SCALAR_KEYS``, so
    ``_args_shape`` reduces it to a ``{present, type}`` dict — and
    ``_sanitize_details`` only keeps primitive leaves inside a nested
    dict, so that summary is dropped outright. The bare key name in
    ``args.keys`` is the only trace left. If a future refactor drops the
    explicit ``body_length`` on the assumption that the args shape
    covers it, this fails.
    """
    _send(tmp_path)
    _, commit = _split(tmp_path)
    args = commit["details"]["args"]
    assert "body" in args["keys"]
    assert "body" not in args, "args shape unexpectedly carries body — rewrite this guard"


def test_log_note_commit_still_records_body_length(tmp_path: Path) -> None:
    """Regression guard on the sibling that set the precedent.

    ``odoo_log_note`` is the reference shape here. If it ever stops
    recording ``body_length`` on commit, the asymmetry this module
    exists to remove has simply moved.
    """
    fake = _LogNoteFake()
    app = _build_note(tmp_path, fake)
    disp = Dispatcher(app)
    base = {"instance": "dev", "model": "res.partner", "record_id": 7, "body": _BODY}

    _call_note(disp, {**base, "dry_run": True})
    out = _call_note(disp, {**base, "dry_run": False})
    assert out["committed"] is True

    dry, commit = _split(tmp_path)
    assert dry["details"]["body_length"] == len(_BODY)
    assert commit["details"]["body_length"] == len(_BODY)


# ---------------------------------------------------------------------------
# odoo_create_attachment
# ---------------------------------------------------------------------------


def test_create_attachment_commit_records_mimetype(tmp_path: Path) -> None:
    """mimetype decides how Odoo serves the file back — the commit row
    is the one that needs it, and only the dry run had it."""
    fake = _AttachFake()
    app = _build_attach(tmp_path, fake)
    disp = Dispatcher(app)
    base = {
        "instance": "dev",
        "res_model": "res.partner",
        "res_id": 7,
        "filename": "report.pdf",
        "datas_base64": _b64(b"%PDF-1.4 fake"),
        "mimetype": "application/pdf",
    }

    _call_attach(disp, {**base, "dry_run": True})
    out = _call_attach(disp, {**base, "dry_run": False})
    assert out["committed"] is True

    dry, commit = _split(tmp_path)
    assert dry["details"]["mimetype"] == "application/pdf"
    assert commit["details"]["mimetype"] == "application/pdf"


def test_create_attachment_commit_details_superset_of_dry_run(tmp_path: Path) -> None:
    """Same superset property as send_message."""
    fake = _AttachFake()
    app = _build_attach(tmp_path, fake)
    disp = Dispatcher(app)
    base = {
        "instance": "dev",
        "res_model": "res.partner",
        "res_id": 7,
        "filename": "report.pdf",
        "datas_base64": _b64(b"%PDF-1.4 fake"),
        "mimetype": "application/pdf",
    }

    _call_attach(disp, {**base, "dry_run": True})
    _call_attach(disp, {**base, "dry_run": False})

    dry, commit = _split(tmp_path)
    missing = set(dry["details"]) - set(commit["details"])
    assert not missing, f"commit row dropped keys the dry run recorded: {sorted(missing)}"
    assert "attachment_id" in commit["details"]


def test_create_attachment_commit_records_absent_mimetype_as_none(tmp_path: Path) -> None:
    """mimetype is optional. When the caller omits it, the key must
    still be present and null rather than silently missing — otherwise
    "no mimetype given" and "this row predates the fix" look identical
    to an operator reading the log."""
    fake = _AttachFake()
    app = _build_attach(tmp_path, fake)
    disp = Dispatcher(app)
    base = {
        "instance": "dev",
        "res_model": "res.partner",
        "res_id": 7,
        "filename": "notes.txt",
        "datas_base64": _b64(b"hello"),
    }

    _call_attach(disp, {**base, "dry_run": True})
    _call_attach(disp, {**base, "dry_run": False})

    _, commit = _split(tmp_path)
    assert "mimetype" in commit["details"]
    assert commit["details"]["mimetype"] is None
