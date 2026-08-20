"""Tests for the audit log (fail-closed, append-only, no field values)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from odoo_mcp.audit import AuditEvent, AuditLog
from odoo_mcp.client import OdooClient
from odoo_mcp.config import AppConfig, Defaults, InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.dispatcher import Dispatcher, InstanceRuntime, OdooMcpApp
from odoo_mcp.errors import AuditLogError
from odoo_mcp.security.limits import RateLimiter
from odoo_mcp.security.prod_guard import ProdGuard


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


def test_log_preserves_nested_args_dict(tmp_path: Path) -> None:
    """The ``args`` sub-dict (from server._args_shape) must round-trip."""
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    audit.log(
        AuditEvent(
            instance="dev",
            tool="odoo_search_read",
            op="search_read",
            model="res.partner",
            result="ok",
            record_count=5,
            duration_ms=4,
            dry_run=False,
            details={
                "record_count": 5,
                "args": {
                    "model": "res.partner",
                    "field_count": 2,
                    "field_names": ["id", "name"],
                    "domain_leaves": 1,
                },
            },
        )
    )
    lines = _read_lines(log_path)
    details = lines[-1]["details"]
    assert details["args"]["model"] == "res.partner"
    assert details["args"]["field_count"] == 2
    assert details["args"]["field_names"] == ["id", "name"]


def _build_dispatcher(tmp_path: Path) -> tuple[Dispatcher, OdooMcpApp]:
    inst_cfg = InstanceConfig(
        name="dev",
        url="https://example.odoo.com",
        database="db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=60,
        allow_self_signed=False,
        allowed_models=frozenset({"res.partner"}),
    )
    creds = Credentials(instance_name=inst_cfg.name, username="u", _api_key="k" * 10)
    client = OdooClient(inst_cfg, credentials=creds)
    app_cfg = AppConfig(
        path=tmp_path / "config.toml",
        defaults=Defaults(),
        instances={inst_cfg.name: inst_cfg},
        audit_log_path=tmp_path / "audit.jsonl",
    )
    audit = AuditLog(app_cfg.audit_log_path)
    app = OdooMcpApp(
        config=app_cfg,
        audit=audit,
        prod_guard=ProdGuard(),
        rate_limiter=RateLimiter(),
        instances={inst_cfg.name: InstanceRuntime(config=inst_cfg, client=client)},
    )
    return Dispatcher(app), app


def test_audit_failure_swallow_logs_error_to_stderr(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """H3: when the audit-log write fails on the FAILURE path, the
    dispatcher must still surface the original tool error to the
    caller AND emit a logging.ERROR record so operators see audit
    breakage. Pre-fix the audit failure was silently swallowed.
    """
    dispatcher, app = _build_dispatcher(tmp_path)

    # Replace audit.log with one that always fails. We must hit the
    # FAILURE path (a tool call that raises an OdooMcpError); the
    # easiest way is to call an unknown tool, which the dispatcher
    # rejects with OdooMcpError.
    def _broken_log(_event: AuditEvent) -> None:
        raise AuditLogError("disk full (simulated)")

    app.audit.log = _broken_log  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="odoo_mcp.dispatcher"):
        contents = asyncio.run(dispatcher.call("not_a_real_tool", {"instance": "dev"}))

    # Caller still sees the original tool error, NOT an audit failure.
    assert len(contents) == 1
    payload = json.loads(contents[0].text)
    assert payload["ok"] is False
    # The ORIGINAL error message — "Unknown tool" — survives. The
    # broken audit log must not mask it.
    assert "Unknown tool" in payload["error"] or payload.get("error_code") == "internal_error"

    # And the audit-system breakage was emitted at ERROR level rather
    # than swallowed silently.
    matching = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "audit log write failed" in r.getMessage()
    ]
    assert matching, (
        f"Expected ERROR-level 'audit log write failed' record; got: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


@pytest.mark.skipif(os.name != "posix", reason="Permission test relies on POSIX chmod semantics")
def test_open_fails_closed_when_unwritable(tmp_path: Path) -> None:
    # Create a read-only directory; AuditLog._open must refuse.
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir(mode=0o500)
    try:
        with pytest.raises(AuditLogError):
            AuditLog(ro_dir / "audit.jsonl")
    finally:
        ro_dir.chmod(0o700)  # let tmp_path clean up


@pytest.mark.skipif(os.name != "posix", reason="File-mode test relies on POSIX chmod semantics")
def test_open_creates_owner_only_file(tmp_path: Path) -> None:
    import stat

    log_path = tmp_path / "audit.jsonl"
    AuditLog(log_path)
    mode = stat.S_IMODE(log_path.stat().st_mode)
    assert mode & 0o077 == 0, f"audit log must not be group/world accessible, got {oct(mode)}"


@pytest.mark.skipif(os.name != "posix", reason="File-mode test relies on POSIX chmod semantics")
def test_open_remediates_existing_world_readable_file(tmp_path: Path) -> None:
    import stat

    log_path = tmp_path / "audit.jsonl"
    # Simulate a log created by a pre-hardening version: world-readable.
    log_path.write_text("")
    log_path.chmod(0o644)
    AuditLog(log_path)
    mode = stat.S_IMODE(log_path.stat().st_mode)
    assert mode & 0o077 == 0, f"existing loose-mode log should be tightened, got {oct(mode)}"


@pytest.mark.skipif(os.name != "posix", reason="File-mode test relies on POSIX chmod semantics")
def test_rotated_and_fresh_files_are_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat
    from datetime import UTC, datetime, timedelta

    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    yesterday = datetime.now(tz=UTC) - timedelta(days=1)
    os.utime(log_path, (yesterday.timestamp(), yesterday.timestamp()))
    audit._last_rotation_check = yesterday.date()

    audit.log(
        AuditEvent(
            instance="dev",
            tool="odoo_search_read",
            op="search_read",
            model="res.partner",
            result="ok",
            record_count=1,
            duration_ms=1,
            dry_run=False,
            details={},
        )
    )

    rotated = log_path.with_name(f"audit-{yesterday.date().isoformat()}.jsonl")
    for path in (log_path, rotated):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0, f"{path.name} must be owner-only, got {oct(mode)}"


def test_log_rotates_on_date_change_mid_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-running MCP that crosses midnight must rotate yesterday's log.

    The fix in v0.8.0 makes ``AuditLog.log`` re-check the date cheaply on
    each call and rotate when it has rolled over since the last check.
    """
    from datetime import UTC, datetime, timedelta

    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    # Force the file's mtime to "yesterday" so the rotation check fires.
    yesterday = datetime.now(tz=UTC) - timedelta(days=1)
    yesterday_ts = yesterday.timestamp()
    os.utime(log_path, (yesterday_ts, yesterday_ts))

    # Pretend we last verified rotation yesterday — the log() call should
    # notice today's date is different and rotate.
    audit._last_rotation_check = yesterday.date()

    audit.log(
        AuditEvent(
            instance="dev",
            tool="odoo_search_read",
            op="search_read",
            model="res.partner",
            result="ok",
            record_count=1,
            duration_ms=1,
            dry_run=False,
            details={},
        )
    )

    # After the write, yesterday's content should live in a dated rotated
    # file, and audit.jsonl should contain only today's new event.
    rotated = log_path.with_name(f"audit-{yesterday.date().isoformat()}.jsonl")
    assert rotated.exists(), "yesterday's file should have been rotated"
    today_lines = _read_lines(log_path)
    # Exactly one line: the just-written event (no startup marker, no
    # yesterday data — that all rotated out).
    assert len(today_lines) == 1
    assert today_lines[0]["tool"] == "odoo_search_read"


# --- fail-closed BEFORE the mutation ---------------------------------------


class _RecordingClient:
    """Fake Odoo client that records whether a mutation was attempted."""

    def __init__(self) -> None:
        self.is_admin: bool | None = None
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.creates: list[tuple] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict:
        return {"id": {"type": "integer"}, "name": {"type": "char"}}

    def create(self, model, values):
        self.creates.append((model, values))
        return 7


def test_unwritable_audit_refuses_write_before_touching_odoo(tmp_path: Path) -> None:
    """A write we cannot audit must never reach Odoo.

    Auditing happens after the commit, so a broken log at that point can only
    report an unaudited change — not prevent it. The preflight in ``_begin``
    is what makes the fail-closed claim real.
    """
    dispatcher, app = _build_dispatcher(tmp_path)
    fake = _RecordingClient()
    app.instances["dev"].client = fake  # type: ignore[assignment]
    # _build_dispatcher leaves the limiter unconfigured; configure it so the
    # call reaches the audit preflight rather than tripping the rate limit.
    app.rate_limiter.configure("dev", 300)

    def _broken_preflight() -> None:
        raise AuditLogError("disk full (simulated)")

    app.audit.preflight = _broken_preflight  # type: ignore[method-assign]

    contents = asyncio.run(
        dispatcher.call(
            "odoo_create",
            {
                "instance": "dev",
                "model": "res.partner",
                "values": {"name": "Acme"},
                "dry_run": False,
            },
        )
    )
    payload = json.loads(contents[0].text)
    assert payload["ok"] is False
    assert payload["error_code"] == "audit_log_error"
    # The decisive assertion: Odoo was never mutated.
    assert fake.creates == [], "write reached Odoo despite an unwritable audit log"


def test_preflight_passes_on_writable_log(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    audit.preflight()  # does not raise
    # And it emits no record — preflight is a check, not an event.
    before = len(_read_lines(log_path))
    audit.preflight()
    assert len(_read_lines(log_path)) == before


def test_odoo_fault_text_withheld_from_audit_log() -> None:
    """Odoo validation errors quote record values; those must not land in a
    30-day log documented as containing no field values. The caller still
    gets the full text in the tool response."""
    from odoo_mcp.errors import OdooRemoteError

    exc = OdooRemoteError(
        "Odoo fault on res.partner.create: The email 'jan@acme.com' is already taken",
        server_text=True,
    )
    # Caller-facing message keeps the detail.
    assert "jan@acme.com" in exc.user_message
    # Audit-facing message does not.
    assert "jan@acme.com" not in exc.audit_message
    assert "withheld" in exc.audit_message


def test_non_server_errors_keep_their_audit_message() -> None:
    """Messages we construct ourselves carry no record data, so they are
    logged verbatim — the withholding is targeted, not blanket."""
    from odoo_mcp.errors import ModelNotAllowedError, OdooRemoteError

    ours = ModelNotAllowedError("Model 'ir.cron' is blocked by the built-in denylist")
    assert ours.audit_message == ours.user_message

    shape = OdooRemoteError("fields_get for 'res.partner' returned unexpected type str")
    assert shape.audit_message == shape.user_message
