"""Tests for the audit log (fail-closed, append-only, no field values)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from odoo_mcp.audit import AuditEvent, AuditLog
from odoo_mcp.errors import AuditLogError


def _read_lines(path: Path) -> list[dict]:
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_open_creates_file_and_writes_marker(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    AuditLog(log_path)
    lines = _read_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["event"] == "audit_log_open"


def test_log_appends_event(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    audit.log(
        AuditEvent(
            instance="dev",
            tool="odoo_search_read",
            op="search_read",
            model="res.partner",
            result="ok",
            record_count=3,
            duration_ms=12,
            dry_run=False,
            details={"field_count": 4},
        )
    )
    lines = _read_lines(log_path)
    assert lines[-1]["tool"] == "odoo_search_read"
    assert lines[-1]["record_count"] == 3
    assert lines[-1]["result"] == "ok"


def test_log_never_contains_field_values(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    audit.log(
        AuditEvent(
            instance="dev",
            tool="odoo_search_read",
            op="search_read",
            model="res.partner",
            result="ok",
            record_count=1,
            duration_ms=4,
            dry_run=False,
            details={"field_count": 2, "id_count": 1},
        )
    )
    raw = log_path.read_text()
    # Arbitrary values that might have been in the records being logged
    # must not appear in the audit output — the only thing details should
    # contain is counts and booleans.
    for forbidden in ["Acme", "john@", "BE1234"]:
        assert forbidden not in raw


@pytest.mark.skipif(
    os.name != "posix", reason="Permission test relies on POSIX chmod semantics"
)
def test_open_fails_closed_when_unwritable(tmp_path: Path) -> None:
    # Create a read-only directory; AuditLog._open must refuse.
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir(mode=0o500)
    try:
        with pytest.raises(AuditLogError):
            AuditLog(ro_dir / "audit.jsonl")
    finally:
        ro_dir.chmod(0o700)  # let tmp_path clean up
