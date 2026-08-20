"""A scan that could not read a model must never report that model as clean.

``scan-custom`` output is not a report, it is a *policy source*: the operator
pastes the emitted ``sensitive_fields`` / ``custom_sensitive_field_patterns``
into ``config.toml`` and the redaction layer runs off it. So "read the model,
found nothing custom" and "never got the model's schema" must not render
identically — the second one leaves the model's custom fields flowing to
Claude unredacted.

The fake below drives the same ``_execute`` chokepoint the real CLI uses,
raising for one model the way a stale ``ir.model`` row or a mid-scan
connection drop does.
"""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pytest

from odoo_mcp.errors import OdooRemoteError
from odoo_mcp.scan_cli import (
    perform_scan,
    render_human,
    render_json,
    render_toml,
)

_MODELS = [
    {"id": 1, "model": "res.partner", "name": "Contact"},
    {"id": 2, "model": "hr.employee", "name": "Employee"},
]

_SCHEMAS: dict[str, dict[str, Any]] = {
    "res.partner": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
    },
    "hr.employee": {
        "id": {"type": "integer"},
        # A klant-custom salary field: exactly what the scan exists to flag.
        "x_loon_groep": {"type": "selection", "help": "Salarisschaal"},
    },
}


class _FakeClient:
    """``_execute`` chokepoint; ``fail_on`` models raise the way Odoo does."""

    def __init__(
        self,
        models: list[dict[str, Any]] | Any = None,
        schemas: dict[str, dict[str, Any]] | None = None,
        *,
        fail_on: frozenset[str] = frozenset(),
    ) -> None:
        self._models = _MODELS if models is None else models
        self._schemas = _SCHEMAS if schemas is None else schemas
        self._fail_on = fail_on
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
            if model in self._fail_on:
                raise OdooRemoteError(f"Odoo fault on {model}.fields_get: Object does not exist")
            return self._schemas.get(model, {})
        raise AssertionError(f"unexpected {model}.{method}")

    def ensure_authenticated(self) -> None:
        return None


class _FakeApp:
    def __init__(self, client: _FakeClient) -> None:
        self._rt = type("RT", (), {"client": client})()

    def instance(self, name: str) -> Any:
        return self._rt


# ---------------------------------------------------------------------------
# perform_scan
# ---------------------------------------------------------------------------


def test_unreadable_model_is_recorded_not_swallowed() -> None:
    result = perform_scan(_FakeClient(fail_on=frozenset({"hr.employee"})), "prod")

    assert result.complete is False
    assert [e.model for e in result.unscanned_models] == ["hr.employee"]
    assert "Object does not exist" in result.unscanned_models[0].error
    # The load-bearing consequence: the salary field is NOT in the policy.
    assert result.custom_fields_on_standard == []


def test_a_scan_that_read_everything_is_complete() -> None:
    result = perform_scan(_FakeClient(), "prod")

    assert result.complete is True
    assert result.unscanned_models == []
    assert [f.name for f in result.custom_fields_on_standard] == ["x_loon_groep"]


def test_malformed_ir_model_response_fails_closed() -> None:
    """An empty model list must never be inferred from a broken response."""
    with pytest.raises(OdooRemoteError):
        perform_scan(_FakeClient(models={"not": "a list"}), "prod")


def test_unusable_ir_model_rows_are_counted() -> None:
    rows = [*_MODELS, {"id": 3, "name": "no model key"}, "junk"]
    result = perform_scan(_FakeClient(models=rows), "prod")

    assert result.unusable_rows == 2
    assert result.complete is False


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_human_report_warns_and_names_the_model() -> None:
    result = perform_scan(_FakeClient(fail_on=frozenset({"hr.employee"})), "prod")
    text = render_human(result, uid=42, login="scan@klantx.be")

    assert "INCOMPLETE SCAN" in text
    assert "hr.employee" in text
    assert "Models unreadable:            1" in text


def test_human_report_of_a_complete_scan_stays_quiet() -> None:
    text = render_human(perform_scan(_FakeClient(), "prod"), uid=42, login="scan@klantx.be")

    assert "INCOMPLETE" not in text


def test_toml_snippet_carries_the_warning_and_still_parses() -> None:
    """The operator pasting the snippet may never have seen the console."""
    rows = [*_MODELS, {"id": 3, "model": "account.move", "name": "Journal Entry"}]
    schemas = {**_SCHEMAS, "account.move": {"x_klant_marge": {"type": "float"}}}
    result = perform_scan(
        _FakeClient(models=rows, schemas=schemas, fail_on=frozenset({"hr.employee"})),
        "prod",
    )
    snippet = render_toml(result)

    assert "INCOMPLETE SCAN" in snippet
    assert "# !! " in snippet
    assert "hr.employee" in snippet
    parsed = tomllib.loads(snippet)
    # The findings it *could* see are still emitted, so the snippet stays useful.
    assert "x_klant_marge" in parsed["instances"]["prod"]["custom_sensitive_field_patterns"]


def test_toml_snippet_does_not_claim_clean_when_it_saw_nothing() -> None:
    result = perform_scan(_FakeClient(fail_on=frozenset({"res.partner", "hr.employee"})), "prod")
    snippet = render_toml(result)

    assert "INCOMPLETE SCAN" in snippet
    assert "No flagged sensitive custom fields found for 'prod'." not in snippet
    assert "in the models that could be read" in snippet
    assert tomllib.loads(snippet) == {}


def test_toml_snippet_of_a_complete_clean_scan_says_so_plainly() -> None:
    result = perform_scan(_FakeClient(models=[_MODELS[0]]), "prod")
    snippet = render_toml(result)

    assert "INCOMPLETE" not in snippet
    assert "No flagged sensitive custom fields found for 'prod'." in snippet


def test_json_reports_completeness_and_the_full_error_list() -> None:
    result = perform_scan(_FakeClient(fail_on=frozenset({"hr.employee"})), "prod")
    parsed = json.loads(render_json(result))

    assert parsed["scan_complete"] is False
    assert parsed["stats"]["models_unscanned"] == 1
    assert parsed["stats"]["ir_model_rows_unusable"] == 0
    assert parsed["unscanned_models"][0]["model"] == "hr.employee"
    assert parsed["suggested_config"]["sensitive_fields"] == {}


def test_json_of_a_complete_scan_reports_true() -> None:
    parsed = json.loads(render_json(perform_scan(_FakeClient(), "prod")))

    assert parsed["scan_complete"] is True
    assert parsed["unscanned_models"] == []


def test_long_error_list_is_truncated_in_text_but_whole_in_json() -> None:
    """Say when the inline list was clamped — and keep --json authoritative."""
    rows = [{"id": i, "model": f"klantx.m{i:03d}", "name": f"M{i}"} for i in range(30)]
    failing = frozenset(r["model"] for r in rows)  # type: ignore[misc]
    result = perform_scan(_FakeClient(models=rows, schemas={}, fail_on=failing), "prod")
    text = render_human(result, uid=None, login=None)

    assert "... and 10 more" in text
    assert len(json.loads(render_json(result))["unscanned_models"]) == 30


# ---------------------------------------------------------------------------
# main(): stdout stays a payload, stderr carries the diagnostic, exit code the failure
# ---------------------------------------------------------------------------


def test_main_exits_1_and_warns_on_stderr_when_incomplete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import scan_cli, server

    monkeypatch.setattr(
        server, "build_app", lambda: _FakeApp(_FakeClient(fail_on=frozenset({"hr.employee"})))
    )

    rc = scan_cli.main(["prod", "--json"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "warning: " in captured.err
    assert "hr.employee" in captured.err
    # stdout must remain pipeable into jq.
    assert json.loads(captured.out)["scan_complete"] is False


def test_main_exits_0_when_the_scan_read_everything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import scan_cli, server

    monkeypatch.setattr(server, "build_app", lambda: _FakeApp(_FakeClient()))

    rc = scan_cli.main(["prod", "--json"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""


def test_main_reports_a_broken_model_list_as_a_failed_scan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odoo_mcp import scan_cli, server

    monkeypatch.setattr(
        server, "build_app", lambda: _FakeApp(_FakeClient(models={"not": "a list"}))
    )

    rc = scan_cli.main(["prod"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "scan failed" in captured.err
    assert captured.out == ""
