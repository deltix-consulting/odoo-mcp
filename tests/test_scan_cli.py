"""End-to-end tests for the scan CLI's render path.

The OdooClient is replaced with a fake — we don't talk to a real Odoo. The
fake exposes the same ``_execute`` chokepoint the real CLI uses, so the
contract under test is exactly what production hits.
"""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pytest

from odoo_mcp.scan_cli import (
    ScanResult,
    perform_scan,
    render_human,
    render_json,
    render_toml,
)


class _FakeClient:
    """Stand-in for OdooClient — only ``_execute`` is consulted."""

    def __init__(self, models: list[dict[str, Any]], schemas: dict[str, dict[str, Any]]) -> None:
        self._models = models
        self._schemas = schemas
        self.uid = 42
        self._credentials = type("C", (), {"username": "scan@klantx.be"})()

    def _execute(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        if model == "ir.model" and method == "search_read":
            return self._models
        if method == "fields_get":
            return self._schemas.get(model, {})
        raise AssertionError(f"unexpected {model}.{method}")

    def ensure_authenticated(self) -> None:
        return None


def _build_fixture() -> _FakeClient:
    models = [
        {"id": 1, "model": "res.partner", "name": "Contact"},
        {"id": 2, "model": "hr.employee", "name": "Employee"},
        {"id": 3, "model": "klantx.contract_addon", "name": "Klant Contract Addon"},
        {"id": 4, "model": "x_studio_inspection", "name": "Inspection"},
    ]
    schemas: dict[str, dict[str, Any]] = {
        "res.partner": {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "email": {"type": "char"},
            "vat": {"type": "char"},
        },
        "hr.employee": {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "x_studio_salary_grade": {"type": "many2one", "help": ""},
            "x_loon_groep": {"type": "selection", "help": ""},
            "x_klantx_pin": {"type": "char", "help": ""},
            "x_studio_billing_rate": {"type": "float", "help": ""},
            "x_studio_internal_note": {
                "type": "text",
                "help": "Internal use only — do not share with employee",
            },
        },
        "klantx.contract_addon": {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "amount": {"type": "monetary"},
        },
        "x_studio_inspection": {
            "id": {"type": "integer"},
            "x_name": {"type": "char"},
        },
    }
    return _FakeClient(models, schemas)


def test_perform_scan_classifies_known_models() -> None:
    client = _build_fixture()
    result = perform_scan(client, "prod")
    assert result.models_total == 4

    custom_model_names = {m.name for m in result.custom_models}
    # res.partner and hr.employee are standard; the other two are custom.
    assert "klantx.contract_addon" in custom_model_names
    assert "x_studio_inspection" in custom_model_names
    assert "res.partner" not in custom_model_names
    assert "hr.employee" not in custom_model_names

    studio_finding = next(m for m in result.custom_models if m.name == "x_studio_inspection")
    assert studio_finding.studio is True


def test_perform_scan_finds_custom_fields_on_standard() -> None:
    result = perform_scan(_build_fixture(), "prod")
    by_name = {f.name: f for f in result.custom_fields_on_standard}
    # hr.employee custom fields must be detected
    assert "x_studio_salary_grade" in by_name
    assert "x_loon_groep" in by_name
    assert "x_klantx_pin" in by_name
    # Standard fields must NOT be reported
    for std in ("name", "email", "id"):
        assert std not in by_name

    # res.partner.vat is a standard field — not reported
    for f in result.custom_fields_on_standard:
        assert not (f.model == "res.partner" and f.name == "vat")


def test_perform_scan_sensitivity_assignment() -> None:
    result = perform_scan(_build_fixture(), "prod")
    by_name = {f.name: f for f in result.custom_fields_on_standard}
    assert by_name["x_studio_salary_grade"].verdict.sensitivity.value == "BLOCKED"
    # 'salary' matches always-redacted in the policy module; "loon" does not
    # so it should fall through to the heuristic LIKELY_SENSITIVE bucket.
    assert by_name["x_loon_groep"].verdict.sensitivity.value == "LIKELY_SENSITIVE"
    assert by_name["x_studio_billing_rate"].verdict.sensitivity.value == "LIKELY_FINANCIAL"
    assert by_name["x_studio_internal_note"].verdict.sensitivity.value == "LIKELY_SENSITIVE"
    assert by_name["x_klantx_pin"].verdict.sensitivity.value == "UNCERTAIN"


def test_render_human_contains_summary() -> None:
    result = perform_scan(_build_fixture(), "prod")
    out = render_human(result, uid=42, login="scan@klantx.be")
    assert "Scanning instance 'prod'" in out
    assert "Custom models" in out
    assert "klantx.contract_addon" in out
    assert "x_studio_salary_grade" in out
    assert "Summary" in out
    assert "scan@klantx.be" in out


def test_render_toml_round_trips() -> None:
    result = perform_scan(_build_fixture(), "prod")
    snippet = render_toml(result)
    parsed = tomllib.loads(snippet)
    assert "instances" in parsed
    inst = parsed["instances"]["prod"]
    assert "custom_sensitive_field_patterns" in inst
    assert isinstance(inst["custom_sensitive_field_patterns"], list)
    # x_loon_groep should show up because it's LIKELY_SENSITIVE
    assert any("x_loon_groep" in p for p in inst["custom_sensitive_field_patterns"])
    sf = inst["sensitive_fields"]
    # The hr.employee key must list the flagged custom fields
    assert "hr.employee" in sf


def test_render_json_round_trips() -> None:
    result = perform_scan(_build_fixture(), "prod")
    snippet = render_json(result)
    parsed = json.loads(snippet)
    assert parsed["instance"] == "prod"
    assert parsed["stats"]["models_custom"] == 2
    assert parsed["odoo_reference_version"]
    assert "suggested_config" in parsed
    assert isinstance(parsed["suggested_config"]["custom_sensitive_field_patterns"], list)


def test_render_toml_no_findings() -> None:
    """Empty / clean instance should still produce a valid TOML comment block."""
    empty = ScanResult(
        instance="clean",
        scanned_at="2026-01-01T00:00:00Z",
        odoo_reference_version="18.0",
        models_total=0,
        fields_total=0,
    )
    snippet = render_toml(empty)
    # Must parse — even if it's all comments.
    tomllib.loads(snippet)
    assert "No flagged sensitive custom fields" in snippet


def test_main_help_no_instance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import scan_cli

    rc = scan_cli.main([])
    captured = capsys.readouterr()
    assert "Usage" in captured.out
    assert rc == 2  # missing instance is an error


def test_main_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from odoo_mcp import scan_cli

    rc = scan_cli.main(["--help"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Usage" in captured.out


def test_main_mutually_exclusive_flags(capsys: pytest.CaptureFixture[str]) -> None:
    from odoo_mcp import scan_cli

    # Stub build_app so we never actually try to connect.
    rc = scan_cli.main(["prod", "--toml", "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "mutually exclusive" in captured.err


class _FakeApp:
    def __init__(self, client: _FakeClient) -> None:
        self._rt = type("RT", (), {"client": client})()

    def instance(self, name: str) -> Any:
        if name != "prod":
            raise RuntimeError(f"unknown instance {name}")
        return self._rt


def test_main_full_flow_human(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import scan_cli, server

    fake_app = _FakeApp(_build_fixture())
    monkeypatch.setattr(server, "build_app", lambda: fake_app)

    rc = scan_cli.main(["prod"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Custom models" in captured.out
    assert "klantx.contract_addon" in captured.out


def test_main_full_flow_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import scan_cli, server

    fake_app = _FakeApp(_build_fixture())
    monkeypatch.setattr(server, "build_app", lambda: fake_app)

    rc = scan_cli.main(["prod", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert parsed["instance"] == "prod"


def test_main_full_flow_toml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import scan_cli, server

    fake_app = _FakeApp(_build_fixture())
    monkeypatch.setattr(server, "build_app", lambda: fake_app)

    rc = scan_cli.main(["prod", "--toml"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = tomllib.loads(captured.out)
    assert "instances" in parsed
