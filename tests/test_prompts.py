"""Tests for the MCP prompts library."""

from __future__ import annotations

import pytest

from odoo_mcp import prompts


def test_list_prompts_returns_static_list() -> None:
    items = prompts.list_prompts()
    assert len(items) >= 5
    names = [p.name for p in items]
    assert "odoo_month_end_check" in names
    assert "odoo_overdue_invoices" in names
    assert "odoo_find_duplicate_partners" in names
    assert "odoo_pipeline_review" in names
    assert "odoo_recent_changes" in names
    assert "odoo_diagnose_permissions" in names


def test_every_prompt_requires_instance_argument() -> None:
    for prompt in prompts.list_prompts():
        names = {a.name for a in (prompt.arguments or [])}
        assert "instance" in names, f"{prompt.name} missing instance arg"
        # 'instance' must be required.
        for arg in prompt.arguments or []:
            if arg.name == "instance":
                assert arg.required is True


def test_get_prompt_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="Unknown prompt"):
        prompts.get_prompt("not_a_real_prompt", {"instance": "dev"})


def test_get_prompt_missing_instance_raises() -> None:
    with pytest.raises(ValueError, match="instance"):
        prompts.get_prompt("odoo_month_end_check", {})


def test_get_prompt_month_end_includes_instance_name() -> None:
    result = prompts.get_prompt("odoo_month_end_check", {"instance": "prod"})
    assert result.messages
    text = result.messages[0].content.text  # type: ignore[union-attr]
    assert "'prod'" in text
    # Should reference real tools by name to anchor Claude.
    assert "odoo_search_count" in text
    assert "odoo_read_group" in text


def test_get_prompt_overdue_default_days() -> None:
    text = _body(prompts.get_prompt("odoo_overdue_invoices", {"instance": "dev"}))
    assert "30 days" in text


def test_get_prompt_overdue_custom_days() -> None:
    text = _body(
        prompts.get_prompt("odoo_overdue_invoices", {"instance": "dev", "days_overdue": "60"})
    )
    assert "60 days" in text


def test_get_prompt_dup_partners_known_field() -> None:
    text = _body(
        prompts.get_prompt(
            "odoo_find_duplicate_partners",
            {"instance": "dev", "match_field": "email"},
        )
    )
    assert "email" in text.lower()
    assert "odoo_read_group" in text


def test_get_prompt_dup_partners_invalid_field_falls_back_to_vat() -> None:
    text = _body(
        prompts.get_prompt(
            "odoo_find_duplicate_partners",
            {"instance": "dev", "match_field": "WHATEVER"},
        )
    )
    assert "vat" in text.lower()


def test_get_prompt_pipeline_uses_stalled_days() -> None:
    text = _body(
        prompts.get_prompt("odoo_pipeline_review", {"instance": "dev", "stalled_days": "21"})
    )
    assert "21 days" in text


def test_get_prompt_diagnose_with_model() -> None:
    text = _body(
        prompts.get_prompt(
            "odoo_diagnose_permissions",
            {"instance": "dev", "model": "account.move"},
        )
    )
    assert "account.move" in text
    assert "odoo_diagnose_access" in text


def test_get_prompt_low_stock_includes_relevant_models(_marker: None = None) -> None:
    text = _body(prompts.get_prompt("odoo_low_stock_check", {"instance": "dev"}))
    assert "product.product" in text
    assert "qty_available" in text


def test_get_prompt_low_stock_with_warehouse() -> None:
    text = _body(
        prompts.get_prompt("odoo_low_stock_check", {"instance": "dev", "warehouse_id": "3"})
    )
    assert "warehouse_id=3" in text


def test_get_prompt_open_manufacturing() -> None:
    text = _body(prompts.get_prompt("odoo_open_manufacturing_orders", {"instance": "dev"}))
    assert "mrp.production" in text


def test_get_prompt_hr_leave_filters_department() -> None:
    text = _body(
        prompts.get_prompt(
            "odoo_hr_leave_overview",
            {"instance": "dev", "department_id": "5"},
        )
    )
    assert "hr.leave" in text
    assert "department_id" in text
    assert "5" in text


def test_get_prompt_timesheet_uses_weeks() -> None:
    text = _body(
        prompts.get_prompt("odoo_timesheet_review", {"instance": "dev", "weeks_back": "4"})
    )
    assert "4 week" in text
    assert "account.analytic.line" in text


def test_get_prompt_unposted_journals() -> None:
    text = _body(prompts.get_prompt("odoo_unposted_journal_entries", {"instance": "dev"}))
    assert "account.move" in text
    assert "draft" in text


def test_get_prompt_top_revenue_default_args() -> None:
    text = _body(prompts.get_prompt("odoo_top_revenue_customers", {"instance": "dev"}))
    assert "Top 10" in text
    assert "90 days" in text


def test_get_prompt_top_revenue_custom_args() -> None:
    text = _body(
        prompts.get_prompt(
            "odoo_top_revenue_customers",
            {"instance": "dev", "days_back": "30", "top_n": "5"},
        )
    )
    assert "Top 5" in text
    assert "30 days" in text


def test_industry_prompts_registered() -> None:
    names = {p.name for p in prompts.list_prompts()}
    expected = {
        "odoo_low_stock_check",
        "odoo_open_manufacturing_orders",
        "odoo_hr_leave_overview",
        "odoo_timesheet_review",
        "odoo_unposted_journal_entries",
        "odoo_top_revenue_customers",
    }
    assert expected.issubset(names)


def test_my_changes_today_registered_and_renders() -> None:
    names = {p.name for p in prompts.list_prompts()}
    assert "odoo_my_changes_today" in names
    text = _body(prompts.get_prompt("odoo_my_changes_today", {"instance": "dev"}))
    assert "write_uid" in text
    assert "odoo_diagnose_access" in text
    # Must be read-only.
    assert "odoo_create" not in text
    assert "odoo_write" not in text


def test_get_prompt_diagnose_without_model() -> None:
    text = _body(prompts.get_prompt("odoo_diagnose_permissions", {"instance": "dev"}))
    assert "odoo_list_instances" in text


def _body(result: object) -> str:
    msgs = getattr(result, "messages", None)
    assert msgs and len(msgs) >= 1
    content = getattr(msgs[0], "content", None)
    assert content is not None
    text: str = getattr(content, "text", "")
    return text
