"""MCP prompt library — pre-canned consultant workflows.

Prompts (the MCP feature, distinct from tools) appear in clients like
Claude Desktop as slash-commands. Each prompt expands into a short
instruction message that nudges Claude into running the right sequence
of tool calls against the configured Odoo instances. The prompts here
are deltix-flavoured: they assume an Odoo Community / Enterprise install
and use the model names that are common across our consulting base.

Design rules:

* Prompts only generate text. They never themselves call Odoo or do
  side effects — Claude does, via the existing tool surface.
* Every prompt accepts ``instance`` as a required argument so the user
  picks dev vs prod explicitly.
* The generated prompt text references the relevant tools by name so
  Claude doesn't have to guess.
* Keep them short — the goal is to point Claude at the right tools, not
  to write a runbook in the prompt body.
"""

from __future__ import annotations

from typing import Final

from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
)

# ---------------------------------------------------------------------------
# Prompt definitions
# ---------------------------------------------------------------------------


_PROMPTS: Final[list[Prompt]] = [
    Prompt(
        name="odoo_month_end_check",
        description=(
            "Quick month-end health check on an Odoo instance: open invoices, "
            "draft journal entries, unposted moves, and follow-up flags."
        ),
        arguments=[
            PromptArgument(
                name="instance",
                description="Configured Odoo instance name (e.g. 'prod', 'dev').",
                required=True,
            ),
        ],
    ),
    Prompt(
        name="odoo_overdue_invoices",
        description=(
            "List overdue customer invoices older than N days, sorted by "
            "amount. Useful for credit-control reviews."
        ),
        arguments=[
            PromptArgument(
                name="instance",
                description="Configured Odoo instance name.",
                required=True,
            ),
            PromptArgument(
                name="days_overdue",
                description="Minimum days past due date. Defaults to 30.",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="odoo_find_duplicate_partners",
        description=(
            "Find probable duplicate partners in res.partner using VAT, "
            "email, or normalized name. Read-only."
        ),
        arguments=[
            PromptArgument(
                name="instance",
                description="Configured Odoo instance name.",
                required=True,
            ),
            PromptArgument(
                name="match_field",
                description="One of 'vat', 'email', 'name'. Defaults to 'vat'.",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="odoo_pipeline_review",
        description=(
            "Review the CRM pipeline: opportunities per stage, stalled deals "
            "(no activity recently), and pipeline value."
        ),
        arguments=[
            PromptArgument(
                name="instance",
                description="Configured Odoo instance name.",
                required=True,
            ),
            PromptArgument(
                name="stalled_days",
                description="Days without activity to flag as stalled. Defaults to 14.",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="odoo_recent_changes",
        description=(
            "Show records created or modified today across the most "
            "common business models (partners, leads, sales orders, invoices)."
        ),
        arguments=[
            PromptArgument(
                name="instance",
                description="Configured Odoo instance name.",
                required=True,
            ),
        ],
    ),
    Prompt(
        name="odoo_low_stock_check",
        description=(
            "Wholesale / inventory: list products with on-hand quantity at "
            "or below their reordering minimum. Read-only."
        ),
        arguments=[
            PromptArgument(name="instance", description="Instance name.", required=True),
            PromptArgument(
                name="warehouse_id",
                description="Optional warehouse id to scope to. Omit for all warehouses.",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="odoo_open_manufacturing_orders",
        description=(
            "Manufacturing: list open / in-progress MOs, their components, "
            "and any with raw-material shortages. Read-only."
        ),
        arguments=[
            PromptArgument(name="instance", description="Instance name.", required=True),
        ],
    ),
    Prompt(
        name="odoo_hr_leave_overview",
        description=(
            "HR: pending leave requests + leave balance per employee for "
            "the current calendar year. Read-only."
        ),
        arguments=[
            PromptArgument(name="instance", description="Instance name.", required=True),
            PromptArgument(
                name="department_id",
                description="Optional hr.department id to scope to.",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="odoo_timesheet_review",
        description=(
            "Professional services: timesheet entries last week per "
            "user / project, plus any unbilled timesheets. Read-only."
        ),
        arguments=[
            PromptArgument(name="instance", description="Instance name.", required=True),
            PromptArgument(
                name="weeks_back",
                description="Number of weeks to include. Defaults to 1.",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="odoo_unposted_journal_entries",
        description=(
            "Accounting: list draft / unposted account.move records grouped "
            "by journal, with totals. Read-only."
        ),
        arguments=[
            PromptArgument(name="instance", description="Instance name.", required=True),
        ],
    ),
    Prompt(
        name="odoo_top_revenue_customers",
        description=(
            "List top N customers by posted invoice revenue in a given date range. Read-only."
        ),
        arguments=[
            PromptArgument(name="instance", description="Instance name.", required=True),
            PromptArgument(
                name="days_back",
                description="Date range — last N days. Defaults to 90.",
                required=False,
            ),
            PromptArgument(
                name="top_n",
                description="How many customers to show. Defaults to 10.",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="odoo_my_changes_today",
        description=(
            "Show records the MCP-authenticated user wrote to today across "
            "the most common business models. Useful for end-of-day recap."
        ),
        arguments=[
            PromptArgument(name="instance", description="Instance name.", required=True),
        ],
    ),
    Prompt(
        name="odoo_diagnose_permissions",
        description=(
            "Diagnose what the MCP's authenticated Odoo user can do: which "
            "models are reachable, what access rights apply, and whether "
            "the credentials are admin-level."
        ),
        arguments=[
            PromptArgument(
                name="instance",
                description="Configured Odoo instance name.",
                required=True,
            ),
            PromptArgument(
                name="model",
                description=(
                    "Odoo model to check in detail (e.g. 'account.move'). "
                    "If omitted, only instance-level info is reported."
                ),
                required=False,
            ),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Prompt body templates
# ---------------------------------------------------------------------------


def _month_end_check(instance: str) -> str:
    return (
        f"Run a month-end health check on Odoo instance '{instance}'. "
        "Use the existing odoo-mcp tools — do not invent new ones. "
        "Report on:\n"
        "1. Draft customer invoices (account.move, move_type='out_invoice', state='draft'): count + total.\n"
        "2. Unposted vendor bills (account.move, move_type='in_invoice', state='draft'): count + total.\n"
        "3. Open AR > 0 (account.move with payment_state in ('not_paid','partial')): count + total per partner top 10.\n"
        "4. Sales orders confirmed but uninvoiced (sale.order, state='sale', invoice_status='to invoice'): count + total.\n"
        "Use odoo_search_count for tallies and odoo_read_group for the top-10 breakdowns. "
        "Use odoo_search_read only when you need a specific record-level list. "
        "Surface anything unusual at the end as a short bulleted list."
    )


def _overdue_invoices(instance: str, days_overdue: str | None) -> str:
    days = days_overdue or "30"
    return (
        f"List overdue customer invoices on Odoo instance '{instance}' that are at "
        f"least {days} days past their due date.\n"
        "1. Use odoo_search_read on account.move with domain "
        "[['move_type','=','out_invoice'], ['state','=','posted'], "
        "['payment_state','in',['not_paid','partial']], ['invoice_date_due','<', <today - N days>]].\n"
        "2. Request fields: id, name, partner_id, invoice_date, invoice_date_due, "
        "amount_residual, currency_id.\n"
        "3. Sort by amount_residual desc, limit 50.\n"
        "4. Group the result by partner_id and present a short table: partner, "
        "open count, total residual, oldest invoice date.\n"
        "Read-only — no writes."
    )


def _find_duplicate_partners(instance: str, match_field: str | None) -> str:
    field = (match_field or "vat").lower()
    if field not in ("vat", "email", "name"):
        field = "vat"
    if field == "vat":
        body = (
            "Use odoo_read_group on res.partner with fields=['id:count'] and "
            "groupby=['vat'] to count partners per VAT. "
            "Filter to groups with id_count > 1 and vat != False.\n"
            "Then for each duplicate VAT, odoo_search_read partners with that vat "
            "to show id, name, parent_id, country_id, email, write_date."
        )
    elif field == "email":
        body = (
            "Use odoo_read_group on res.partner with fields=['id:count'] and "
            "groupby=['email'] to count partners per email. "
            "Filter to groups with id_count > 1 and email != False. "
            "Beware that lowercasing happens server-side; the result is best-effort.\n"
            "For each duplicate email, odoo_search_read to show id, name, parent_id, write_date."
        )
    else:
        body = (
            "Use odoo_read_group on res.partner with fields=['id:count'] and "
            "groupby=['name'] to count partners per exact name. "
            "Filter to groups with id_count > 1.\n"
            "Note: name matching is exact and case-sensitive at the DB level — fuzzy "
            "duplicate detection is out of scope for this prompt."
        )
    return (
        f"Find probable duplicate partners on Odoo instance '{instance}', "
        f"matching by {field}. Read-only.\n" + body + "\n"
        "Surface the top 10 duplicate clusters as a short table. The user will "
        "decide whether to merge — do NOT call odoo_archive_or_delete or odoo_write "
        "without explicit instruction."
    )


def _pipeline_review(instance: str, stalled_days: str | None) -> str:
    days = stalled_days or "14"
    return (
        f"Review the CRM pipeline on Odoo instance '{instance}'.\n"
        "1. odoo_read_group on crm.lead with fields=['id:count','expected_revenue:sum'], "
        "groupby=['stage_id'], domain=[['type','=','opportunity'], ['active','=',True]] "
        "to get count and value per stage.\n"
        "2. odoo_search_read on crm.lead with domain "
        f"[['type','=','opportunity'], ['active','=',True], ['date_last_stage_update','<', <today - {days} days>]], "
        "fields=['id','name','partner_id','user_id','stage_id','expected_revenue','date_last_stage_update'], "
        "limit 25, order='expected_revenue desc' "
        "to surface stalled deals.\n"
        "3. Present (a) a per-stage table and (b) the top-10 stalled deals. "
        "Read-only."
    )


def _recent_changes(instance: str) -> str:
    return (
        f"Show what changed today on Odoo instance '{instance}'.\n"
        "For each of res.partner, crm.lead, sale.order, and account.move, run "
        "odoo_search_count with domain=[['write_date','>=', <today midnight>]] "
        "and report the count.\n"
        "For the model with the most activity, also run odoo_search_read with "
        "fields=['id','name','write_date','user_id'], order='write_date desc', limit 10 "
        "to show the most recent records. Read-only."
    )


def _low_stock(instance: str, warehouse_id: str | None) -> str:
    wh_clause = f" Filter to warehouse_id={warehouse_id} where supported." if warehouse_id else ""
    return (
        f"Find low-stock products on Odoo instance '{instance}'.{wh_clause}\n"
        "1. odoo_describe_model on product.product to confirm the qty fields "
        "exposed in this Odoo version (qty_available, virtual_available, "
        "reordering_min_qty are typical).\n"
        "2. odoo_search_read on product.product with domain "
        "[['type','=','product'], ['active','=',True]], "
        "fields=['id','default_code','name','qty_available',"
        "'virtual_available','reordering_min_qty'], "
        "order='qty_available asc', limit=50.\n"
        "3. Filter client-side: keep rows where reordering_min_qty > 0 and "
        "qty_available <= reordering_min_qty.\n"
        "4. Present a table: SKU, name, on-hand, virtual, min. Read-only."
    )


def _open_manufacturing(instance: str) -> str:
    return (
        f"Review open manufacturing orders on Odoo instance '{instance}'.\n"
        "1. odoo_search_count mrp.production with domain "
        "[['state','in',['confirmed','progress','to_close']]] for the headline.\n"
        "2. odoo_read_group mrp.production by ['state'] with "
        "fields=['id:count'] for the breakdown.\n"
        "3. odoo_search_read mrp.production with domain "
        "[['state','in',['confirmed','progress']]], "
        "fields=['id','name','product_id','product_qty','date_planned_start',"
        "'state','user_id'], order='date_planned_start asc', limit=25.\n"
        "4. If the schema exposes it, also surface MOs flagged with raw-material "
        "shortages via the components_availability_state field. Read-only."
    )


def _hr_leave(instance: str, department_id: str | None) -> str:
    dept = f"['department_id','=',{department_id}], " if department_id else ""
    return (
        f"Review HR leave on Odoo instance '{instance}'.\n"
        "1. odoo_search_read hr.leave with domain "
        f"[{dept}['state','=','confirm']], "
        "fields=['id','employee_id','holiday_status_id','date_from','date_to',"
        "'number_of_days','state'], order='date_from asc', limit=50 — pending "
        "approval queue.\n"
        "2. odoo_read_group hr.leave.allocation with fields=['number_of_days:sum'] "
        "and groupby=['employee_id','holiday_status_id'] over the current year "
        "for entitlement totals.\n"
        "3. Present (a) pending requests table, (b) per-employee balance table. "
        "Read-only."
    )


def _timesheet_review(instance: str, weeks_back: str | None) -> str:
    weeks = weeks_back or "1"
    return (
        f"Review timesheets on Odoo instance '{instance}' for the last "
        f"{weeks} week(s).\n"
        "1. odoo_read_group account.analytic.line (Odoo's timesheet model) with "
        "fields=['unit_amount:sum'], groupby=['user_id','project_id'], domain="
        f"[['date','>=', <today - {weeks}*7 days>]] for time per consultant per project.\n"
        "2. odoo_search_count account.analytic.line with domain "
        f"[['date','>=', <today - {weeks}*7 days>], ['timesheet_invoice_id','=',False]] "
        "for unbilled timesheets count.\n"
        "3. Present per-consultant totals + flag the unbilled count. Read-only."
    )


def _unposted_journals(instance: str) -> str:
    return (
        f"Find unposted journal entries on Odoo instance '{instance}'.\n"
        "1. odoo_read_group account.move with fields=['id:count','amount_total:sum'], "
        "groupby=['journal_id'], domain=[['state','=','draft']] for the per-journal "
        "draft count + total.\n"
        "2. odoo_search_read account.move with domain [['state','=','draft']], "
        "fields=['id','name','journal_id','date','amount_total','partner_id'], "
        "order='date asc', limit=50 for the oldest 50 drafts.\n"
        "3. Surface anything older than 30 days as a follow-up list. Read-only."
    )


def _top_revenue_customers(instance: str, days_back: str | None, top_n: str | None) -> str:
    days = days_back or "90"
    top = top_n or "10"
    return (
        f"Top {top} revenue customers on Odoo instance '{instance}' over the "
        f"last {days} days.\n"
        "1. odoo_read_group account.move with fields=['amount_total_signed:sum'], "
        "groupby=['partner_id'], domain="
        f"[['move_type','=','out_invoice'], ['state','=','posted'], "
        f"['invoice_date','>=', <today - {days} days>]], "
        f"orderby='amount_total_signed desc', limit={top}.\n"
        "2. Present a single table: rank, partner name, total revenue, currency. "
        "Note that amount_total_signed is in the company currency. Read-only."
    )


def _my_changes_today(instance: str) -> str:
    return (
        f"Show what records I wrote to today on Odoo instance '{instance}'.\n"
        "1. odoo_diagnose_access(instance, model='res.partner') first to "
        "capture the authenticated uid.\n"
        "2. For each of res.partner, crm.lead, sale.order, account.move, "
        "project.task: odoo_search_count with domain "
        "[['write_uid','=', <uid from step 1>], ['write_date','>=', <today midnight>]] "
        "for the count.\n"
        "3. For the model with the highest count, also odoo_search_read with "
        "fields=['id','name','write_date','model_specific_field'] order='write_date desc' "
        "limit 10 for a recap list.\n"
        "Read-only — strictly a recap, no writes."
    )


def _diagnose_permissions(instance: str, model: str | None) -> str:
    if model:
        return (
            f"Diagnose Odoo MCP permissions on instance '{instance}' for model '{model}'.\n"
            f"1. odoo_list_instances — confirm production flag and admin warning.\n"
            f"2. odoo_diagnose_access(instance='{instance}', model='{model}') — "
            f"report read/write/create/unlink access for the authenticated user.\n"
            f"3. If admin_warning is set, recommend creating a dedicated non-admin user.\n"
            f"Summarize the result in 3-4 lines, no padding."
        )
    return (
        f"Diagnose Odoo MCP permissions on instance '{instance}'.\n"
        "1. odoo_list_instances — show production flag, allowlist mode, admin warning.\n"
        "2. If the user wants a model-specific check, ask which model and then call "
        "odoo_diagnose_access for that model.\n"
        "Summarize in 3-4 lines."
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def list_prompts() -> list[Prompt]:
    """Return the static prompt list. Order is the order Claude sees."""
    return list(_PROMPTS)


def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    """Render the prompt body for ``name`` with ``arguments``.

    Returns a :class:`GetPromptResult` with a single user message — clients
    like Claude Desktop wrap that into a chat turn.
    """
    args = arguments or {}
    instance = args.get("instance") or ""
    if not instance:
        raise ValueError("Prompt argument 'instance' is required.")
    if name == "odoo_month_end_check":
        body = _month_end_check(instance)
    elif name == "odoo_overdue_invoices":
        body = _overdue_invoices(instance, args.get("days_overdue"))
    elif name == "odoo_find_duplicate_partners":
        body = _find_duplicate_partners(instance, args.get("match_field"))
    elif name == "odoo_pipeline_review":
        body = _pipeline_review(instance, args.get("stalled_days"))
    elif name == "odoo_recent_changes":
        body = _recent_changes(instance)
    elif name == "odoo_low_stock_check":
        body = _low_stock(instance, args.get("warehouse_id"))
    elif name == "odoo_open_manufacturing_orders":
        body = _open_manufacturing(instance)
    elif name == "odoo_hr_leave_overview":
        body = _hr_leave(instance, args.get("department_id"))
    elif name == "odoo_timesheet_review":
        body = _timesheet_review(instance, args.get("weeks_back"))
    elif name == "odoo_unposted_journal_entries":
        body = _unposted_journals(instance)
    elif name == "odoo_top_revenue_customers":
        body = _top_revenue_customers(instance, args.get("days_back"), args.get("top_n"))
    elif name == "odoo_my_changes_today":
        body = _my_changes_today(instance)
    elif name == "odoo_diagnose_permissions":
        body = _diagnose_permissions(instance, args.get("model"))
    else:
        raise ValueError(f"Unknown prompt: {name!r}")
    return GetPromptResult(
        description=f"odoo-mcp prompt: {name}",
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=body),
            ),
        ],
    )
