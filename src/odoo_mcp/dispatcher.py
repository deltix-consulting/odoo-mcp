"""Security dispatcher for MCP tool calls.

Every tool goes through the same pipeline in the same order. The steps are
deliberately implemented as small functions in :mod:`odoo_mcp.security` so
they can be unit-tested in isolation; this module just wires them up.

Pipeline::

    [tool call]
    -> resolve_instance            (config lookup)
    -> rate_limit                  (token bucket per instance)
    -> model_allowlist             (per-instance frozenset)
    -> op_allowlist                (closed enum)
    -> prod_guard                  (write gate + dry-run default + confirmation tokens)
    -> sandbox_domain              (for search_read)
    -> validate_fields             (for read/search_read: explicit field list + redaction policy)
    -> validate_values             (for create/write: no protected fields, no wildcard keys)
    -> cap_limit                   (clamp record limit to the instance cap)
    -> call_odoo                   (the only place that touches XML-RPC)
    -> redact_response             (drop protected/default-hidden fields, replace binaries)
    -> audit_success               (one JSONL line, no field values)
    -> return
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.types import TextContent

from . import __version__
from .audit import AuditEvent, AuditLog
from .client import OdooClient
from .config import AppConfig, InstanceConfig
from .errors import FieldPolicyError, InstanceNotFoundError, OdooMcpError, ProdGuardError
from .security.allowlist import (
    ALLOWLIST_WILDCARD,
    MODEL_DENYLIST,
    Operation,
    check_model,
    check_operation,
)
from .security.domain import sandbox_domain
from .security.fields import (
    redact_fields_get,
    redact_response,
    validate_aggregate_fields,
    validate_groupby,
    validate_requested_fields,
    validate_write_values,
)
from .security.limits import RateLimiter, clamp_limit
from .security.prod_guard import ProdGuard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared application state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InstanceRuntime:
    """Everything the dispatcher needs for one configured instance."""

    config: InstanceConfig
    client: OdooClient


@dataclass(slots=True)
class _Ctx:
    """Per-call context built by ``Dispatcher._begin``.

    Carries the identity of the call (tool name, operation, instance, model,
    runtime, start time) so handlers don't have to repeat them to every
    audit/token helper.
    """

    tool: str
    op: Operation
    instance: str
    model: str | None
    rt: InstanceRuntime
    started: float


@dataclass(slots=True)
class OdooMcpApp:
    config: AppConfig
    audit: AuditLog
    prod_guard: ProdGuard
    rate_limiter: RateLimiter
    instances: dict[str, InstanceRuntime] = field(default_factory=dict)

    def instance(self, name: str) -> InstanceRuntime:
        if not isinstance(name, str) or not name:
            raise InstanceNotFoundError("Instance name must be a non-empty string.")
        inst = self.instances.get(name)
        if inst is None:
            raise InstanceNotFoundError(
                f"Instance {name!r} is not configured. "
                f"Known instances: {sorted(self.instances.keys())}"
            )
        return inst


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    """Wires MCP tool names to handlers.

    Each handler returns a plain JSON-serializable dict; the MCP layer wraps
    it in a :class:`TextContent` with compact JSON.
    """

    def __init__(self, app: OdooMcpApp) -> None:
        self.app = app

    # ---- Entry point ------------------------------------------------------

    async def call(self, name: str, arguments: dict[str, Any]) -> list[TextContent]:
        started = time.monotonic()
        instance = arguments.get("instance") if isinstance(arguments, dict) else None
        logger.info(
            "tool call: %s instance=%s",
            name,
            instance if isinstance(instance, str) else "-",
        )
        try:
            result = self._dispatch(name, arguments)
        except OdooMcpError as exc:
            logger.warning("tool %s failed: %s (%s)", name, exc.code, exc.user_message)
            self._audit_failure(name, arguments, exc, _elapsed_ms(started))
            payload: dict[str, Any] = {
                "ok": False,
                "error_code": exc.code,
                "error": exc.user_message,
            }
            if exc.hint:
                payload["hint"] = exc.hint
            return [_text(payload)]
        except Exception as exc:  # noqa: BLE001 — last-resort safety net
            wrapped = OdooMcpError(f"Unhandled error in {name}: {type(exc).__name__}: {exc}")
            self._audit_failure(name, arguments, wrapped, _elapsed_ms(started))
            return [
                _text({"ok": False, "error_code": "internal_error", "error": wrapped.user_message})
            ]
        return [_text({"ok": True, **result}, default=str)]

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = _HANDLERS.get(name)
        if handler is None:
            raise OdooMcpError(f"Unknown tool: {name!r}")
        return handler(self, arguments)

    # ---- Shared setup -----------------------------------------------------

    def _begin(
        self, tool: str, args: dict[str, Any], op: Operation, *, require_model: bool = True
    ) -> _Ctx:
        """Resolve instance, take rate token, ensure auth, validate op, validate model."""
        instance = _require_str(args, "instance")
        rt = self.app.instance(instance)
        started = time.monotonic()
        self.app.rate_limiter.take(instance)
        rt.client.ensure_authenticated()
        check_operation(op)
        model: str | None = None
        if require_model:
            model = _require_str(args, "model")
            check_model(model, rt.config.allowed_models)
        return _Ctx(tool=tool, op=op, instance=instance, model=model, rt=rt, started=started)

    def _audit_ok(
        self,
        ctx: _Ctx,
        details: dict[str, Any],
        args: dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
    ) -> None:
        """Log a successful call with elapsed time computed from ``ctx``."""
        self._audit(
            ctx.tool,
            ctx.op,
            ctx.instance,
            ctx.model,
            _elapsed_ms(ctx.started),
            dry_run,
            details,
            args,
        )

    # ---- Handlers ---------------------------------------------------------

    def _help(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Return a capability overview. Never authenticates, never contacts Odoo."""
        instances = [
            _instance_summary(name, rt, self.app.prod_guard.is_unlocked(name))
            for name, rt in self.app.instances.items()
        ]
        payload: dict[str, Any] = {
            "version": __version__,
            "summary": _HELP_SUMMARY,
            "common_patterns": _HELP_COMMON_PATTERNS,
            "gotchas": _HELP_GOTCHAS,
            "denylist_size": len(MODEL_DENYLIST),
            "instances": instances,
        }
        self._audit("odoo_help", Operation.FIELDS_GET, None, None, 0, False, {})
        return payload

    def _list_instances(self, _args: dict[str, Any]) -> dict[str, Any]:
        out = [
            {
                **_instance_summary(name, rt, self.app.prod_guard.is_unlocked(name)),
                "max_records_default": rt.config.max_records_default,
                "max_records_hard_cap": rt.config.max_records_hard_cap,
                "rate_limit_per_minute": rt.config.rate_limit_per_minute,
            }
            for name, rt in self.app.instances.items()
        ]
        self._audit("odoo_list_instances", Operation.FIELDS_GET, None, None, 0, False, {})
        return {
            "mcp_version": __version__,
            "denylist_size": len(MODEL_DENYLIST),
            "instances": out,
        }

    def _describe_model(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_describe_model", args, Operation.FIELDS_GET)
        assert ctx.model is not None
        # Keep only the schema-relevant bits so the response stays compact.
        keep = {"type", "string", "required", "readonly", "help", "relation", "_sensitive", "_note"}
        raw = redact_fields_get(
            ctx.model,
            ctx.rt.client.fields_get(ctx.model),
            instance_overrides=ctx.rt.config.sensitive_fields,
        )
        filtered = {
            fname: {k: v for k, v in meta.items() if k in keep} for fname, meta in raw.items()
        }
        self._audit_ok(ctx, {"field_count": len(filtered)}, args)
        return {"model": ctx.model, "fields": filtered}

    def _search_read(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_search_read", args, Operation.SEARCH_READ)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        allow_sensitive = frozenset(args.get("allow_sensitive_fields") or [])
        offset = _offset(args)

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        overrides = rt.config.sensitive_fields
        fields = validate_requested_fields(
            model,
            _require_list_of_str(args, "fields"),
            known,
            allow_sensitive=allow_sensitive,
            instance_overrides=overrides,
        )
        domain = sandbox_domain(args.get("domain") or [], known)
        limit = clamp_limit(
            args.get("limit"), rt.config.max_records_default, rt.config.max_records_hard_cap
        )
        records = rt.client.search_read(
            model, domain, fields, limit, offset, _optional_str(args, "order")
        )
        redacted = redact_response(
            model,
            records,
            {n: m.get("type", "") for n, m in fields_meta.items()},
            allow_sensitive=allow_sensitive,
            include_binary=bool(args.get("include_binary") or False),
            instance_overrides=overrides,
        )
        self._audit_ok(
            ctx,
            {
                "record_count": len(redacted),
                "limit": limit,
                "offset": offset,
                "field_count": len(fields),
                "domain_leaves": sum(1 for e in domain if not isinstance(e, str)),
            },
            args,
        )
        return {
            "instance": ctx.instance,
            "model": model,
            "records": redacted,
            "count": len(redacted),
        }

    def _search_count(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_search_count", args, Operation.SEARCH_COUNT)
        assert ctx.model is not None
        known = frozenset(ctx.rt.client.fields_get(ctx.model).keys())
        domain = sandbox_domain(args.get("domain") or [], known)
        count = ctx.rt.client.search_count(ctx.model, domain)
        self._audit_ok(
            ctx,
            {
                "record_count": count,
                "domain_leaves": sum(1 for e in domain if not isinstance(e, str)),
            },
            args,
        )
        return {"instance": ctx.instance, "model": ctx.model, "count": count}

    def _read_group(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_read_group", args, Operation.READ_GROUP)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        allow_sensitive = frozenset(args.get("allow_sensitive_fields") or [])
        offset = _offset(args)
        lazy = bool(args.get("lazy", True))

        known = frozenset(rt.client.fields_get(model).keys())
        overrides = rt.config.sensitive_fields
        fields = validate_aggregate_fields(
            model,
            _require_list_of_str(args, "fields"),
            known,
            allow_sensitive=allow_sensitive,
            instance_overrides=overrides,
        )
        groupby = validate_groupby(
            model,
            _require_list_of_str(args, "groupby"),
            known,
            allow_sensitive=allow_sensitive,
            instance_overrides=overrides,
        )
        domain = sandbox_domain(args.get("domain") or [], known)
        # Clamp group count to the hard cap regardless of caller input.
        cap = rt.config.max_records_hard_cap
        limit = clamp_limit(args.get("limit"), cap, cap)

        rows = rt.client.read_group(
            model,
            domain,
            fields,
            groupby,
            limit=limit,
            offset=offset,
            orderby=_optional_str(args, "orderby"),
            lazy=lazy,
        )
        self._audit_ok(
            ctx,
            {
                "record_count": len(rows),
                "limit": limit,
                "offset": offset,
                "field_count": len(fields),
                "groupby_count": len(groupby),
                "domain_leaves": sum(1 for e in domain if not isinstance(e, str)),
                "lazy": lazy,
            },
            args,
        )
        return {"instance": ctx.instance, "model": model, "groups": rows, "count": len(rows)}

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_read", args, Operation.READ)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        ids = _require_list_of_int(args, "ids")
        allow_sensitive = frozenset(args.get("allow_sensitive_fields") or [])
        cap = rt.config.max_records_hard_cap
        if len(ids) > cap:
            raise OdooMcpError(f"Cannot read more than {cap} ids at once.")

        fields_meta = rt.client.fields_get(model)
        overrides = rt.config.sensitive_fields
        fields = validate_requested_fields(
            model,
            _require_list_of_str(args, "fields"),
            frozenset(fields_meta.keys()),
            allow_sensitive=allow_sensitive,
            instance_overrides=overrides,
        )
        redacted = redact_response(
            model,
            rt.client.read(model, ids, fields),
            {n: m.get("type", "") for n, m in fields_meta.items()},
            allow_sensitive=allow_sensitive,
            include_binary=bool(args.get("include_binary") or False),
            instance_overrides=overrides,
        )
        self._audit_ok(
            ctx,
            {"record_count": len(redacted), "field_count": len(fields), "id_count": len(ids)},
            args,
        )
        return {
            "instance": ctx.instance,
            "model": model,
            "records": redacted,
            "count": len(redacted),
        }

    def _create(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_create", args, Operation.CREATE)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        values = _require_dict(args, "values")
        self.app.prod_guard.check_write(ctx.instance, rt.config.production)

        known = frozenset(rt.client.fields_get(model).keys())
        validated = validate_write_values(model, values, known)
        n = len(validated)

        if self.app.prod_guard.effective_dry_run(args.get("dry_run"), rt.config.production):
            token = self.app.prod_guard.create_pending(
                ctx.instance,
                ctx.op.value,
                model,
                summary=f"create {model} (+{n} fields)",
            )
            self._audit_ok(ctx, {"field_count": n}, args, dry_run=True)
            return {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "would_write_fields": sorted(validated.keys()),
                "confirmation_token": token,
                "note": _DRY_RUN_NOTE.format(tool="odoo_create"),
            }

        self._consume_token_on_prod(ctx, args)
        new_id = rt.client.create(model, validated)
        self._audit_ok(ctx, {"field_count": n, "new_id": new_id}, args)
        return {"instance": ctx.instance, "model": model, "id": new_id, "committed": True}

    def _write(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_write", args, Operation.WRITE)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        ids = _require_list_of_int(args, "ids")
        values = _require_dict(args, "values")
        self.app.prod_guard.check_write(ctx.instance, rt.config.production)

        cap = rt.config.max_records_hard_cap
        if len(ids) > cap:
            raise OdooMcpError(f"Cannot write to more than {cap} ids at once.")

        known = frozenset(rt.client.fields_get(model).keys())
        validated = validate_write_values(model, values, known)
        n = len(validated)

        if self.app.prod_guard.effective_dry_run(args.get("dry_run"), rt.config.production):
            id_preview = f"ids={ids[:5]}{'...' if len(ids) > 5 else ''}"
            token = self.app.prod_guard.create_pending(
                ctx.instance,
                ctx.op.value,
                model,
                summary=f"write {model} {id_preview} (+{n} fields)",
            )
            self._audit_ok(ctx, {"field_count": n, "id_count": len(ids)}, args, dry_run=True)
            return {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "id_count": len(ids),
                "would_update_fields": sorted(validated.keys()),
                "confirmation_token": token,
                "note": _DRY_RUN_NOTE.format(tool="odoo_write"),
            }

        self._consume_token_on_prod(ctx, args)
        ok = rt.client.write(model, ids, validated)
        self._audit_ok(ctx, {"id_count": len(ids), "field_count": n}, args)
        return {"instance": ctx.instance, "model": model, "ids": ids, "committed": ok}

    def _archive_or_delete(self, args: dict[str, Any]) -> dict[str, Any]:
        """Archive (active=False) or permanently unlink records.

        Claude must ask the user which path they want. Archive is reversible
        and preserves history; delete is permanent. Same prod-guard +
        dry-run + confirmation-token flow as odoo_write / odoo_create.
        """
        mode = args.get("mode")
        if mode not in ("archive", "delete"):
            raise OdooMcpError("mode must be 'archive' or 'delete'.")
        op = Operation.ARCHIVE if mode == "archive" else Operation.UNLINK
        ctx = self._begin("odoo_archive_or_delete", args, op)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        ids = _require_list_of_int(args, "ids")

        cap = rt.config.max_records_hard_cap
        if len(ids) > cap:
            raise OdooMcpError(f"Cannot {mode} more than {cap} ids at once.")

        # Archive needs an 'active' field on the model; delete is unconditional.
        if mode == "archive":
            fields_meta = rt.client.fields_get(model)
            if "active" not in fields_meta:
                raise FieldPolicyError(
                    f"Model {model!r} has no 'active' field — cannot archive. "
                    f"If deletion is intended, call with mode='delete' (permanent)."
                )

        self.app.prod_guard.check_write(ctx.instance, rt.config.production)
        dry_run = self.app.prod_guard.effective_dry_run(args.get("dry_run"), rt.config.production)

        if dry_run:
            summary = f"{mode} {len(ids)} record(s) of {model}"
            token = self.app.prod_guard.create_pending(
                ctx.instance, ctx.op.value, model, summary=summary
            )
            reminder = (
                "Archiving is reversible — to restore, odoo_write values={'active': true}."
                if mode == "archive"
                else (
                    "Deletion is PERMANENT and cannot be undone. "
                    "Archiving (mode='archive') is usually safer. Are you sure?"
                )
            )
            self._audit_ok(
                ctx,
                {"mode": mode, "id_count": len(ids)},
                args,
                dry_run=True,
            )
            return {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "mode": mode,
                "id_count": len(ids),
                "confirmation_token": token,
                "reminder": reminder,
                "note": _DRY_RUN_NOTE.format(tool="odoo_archive_or_delete"),
            }

        self._consume_token_on_prod(ctx, args)
        if mode == "archive":
            ok = rt.client.write(model, ids, {"active": False})
        else:
            ok = rt.client.unlink(model, ids)
        self._audit_ok(ctx, {"mode": mode, "id_count": len(ids)}, args)
        return {
            "instance": ctx.instance,
            "model": model,
            "mode": mode,
            "ids": ids,
            "committed": ok,
        }

    def _consume_token_on_prod(self, ctx: _Ctx, args: dict[str, Any]) -> None:
        """On prod, a valid confirmation token from a prior dry run is required."""
        if not ctx.rt.config.production:
            return
        token = args.get("confirmation_token")
        if not isinstance(token, str) or not token:
            raise ProdGuardError(
                "Commits against production require a confirmation_token from a prior dry run."
            )
        assert ctx.model is not None
        self.app.prod_guard.consume_pending(token, ctx.instance, ctx.op.value, ctx.model)

    def _enable_prod_writes(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        rt = self.app.instance(instance_name)
        expiry = self.app.prod_guard.unlock(instance_name, rt.config.production)
        self._audit(
            "odoo_enable_prod_writes",
            Operation.WRITE,
            instance_name,
            None,
            0,
            False,
            {"event": "WRITE_UNLOCK"},
            args,
        )
        return {
            "instance": instance_name,
            "writes_unlocked": True,
            "expires_in_seconds": int(expiry - time.monotonic()),
            "note": (
                "Writes are unlocked for 15 minutes of activity. Every write still "
                "defaults to dry_run=true on prod; you must pass dry_run=false and a "
                "confirmation_token to commit."
            ),
        }

    # ---- Audit ------------------------------------------------------------

    def _audit(
        self,
        tool: str,
        op: Operation,
        instance: str | None,
        model: str | None,
        duration_ms: int,
        dry_run: bool,
        details: dict[str, Any],
        arguments: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {}
        if arguments is not None:
            merged["args"] = _args_shape(arguments)
        merged.update(details)
        rc = details.get("record_count")
        self.app.audit.log(
            AuditEvent(
                instance=instance or "-",
                tool=tool,
                op=op.value,
                model=model,
                result="ok",
                record_count=rc if isinstance(rc, int) else None,
                duration_ms=duration_ms,
                dry_run=dry_run,
                details=_sanitize_details(merged),
            )
        )

    def _audit_failure(
        self,
        tool: str,
        arguments: dict[str, Any],
        error: OdooMcpError,
        duration_ms: int,
    ) -> None:
        instance = arguments.get("instance") if isinstance(arguments, dict) else None
        model = arguments.get("model") if isinstance(arguments, dict) else None
        raw: dict[str, Any] = {"error": error.user_message[:500]}
        if isinstance(arguments, dict):
            raw["args"] = _args_shape(arguments)
        # Audit is fail-closed at the dispatcher level (call() surfaces the
        # underlying error to the caller); don't double-raise here.
        with contextlib.suppress(OdooMcpError):
            self.app.audit.log(
                AuditEvent(
                    instance=instance if isinstance(instance, str) else "-",
                    tool=tool,
                    op="-",
                    model=model if isinstance(model, str) else None,
                    result=error.code,
                    record_count=None,
                    duration_ms=duration_ms,
                    dry_run=False,
                    details=_sanitize_details(raw),
                )
            )


# Name -> handler dispatch table. Bound methods resolve via the instance arg.
_HANDLERS: dict[str, Callable[[Dispatcher, dict[str, Any]], dict[str, Any]]] = {
    "odoo_help": Dispatcher._help,
    "odoo_list_instances": Dispatcher._list_instances,
    "odoo_describe_model": Dispatcher._describe_model,
    "odoo_search_read": Dispatcher._search_read,
    "odoo_search_count": Dispatcher._search_count,
    "odoo_read_group": Dispatcher._read_group,
    "odoo_read": Dispatcher._read,
    "odoo_create": Dispatcher._create,
    "odoo_write": Dispatcher._write,
    "odoo_archive_or_delete": Dispatcher._archive_or_delete,
    "odoo_enable_prod_writes": Dispatcher._enable_prod_writes,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _instance_summary(name: str, rt: InstanceRuntime, writes_unlocked: bool) -> dict[str, Any]:
    """Render an instance's metadata including allowlist mode.

    In open mode (``"*"`` in ``allowed_models``), we return ``allowlist_mode``:
    ``"open"`` and omit the allowlist enumeration — it would be misleading to
    list ``["*"]`` as if it were a concrete set of allowed models. In strict
    mode we enumerate the concrete set.
    """
    open_mode = ALLOWLIST_WILDCARD in rt.config.allowed_models
    summary: dict[str, Any] = {
        "name": name,
        "url": rt.config.url,
        "database": rt.config.database,
        "production": rt.config.production,
        "writes_unlocked": writes_unlocked,
        "allowlist_mode": "open" if open_mode else "strict",
    }
    if open_mode:
        summary["allowed_models_note"] = (
            f"Open mode: any non-denylisted model is reachable "
            f"({len(MODEL_DENYLIST)} models blocked by denylist)."
        )
    else:
        summary["allowed_models"] = sorted(rt.config.allowed_models)
    return summary


def _text(payload: dict[str, Any], *, default: Callable[[Any], Any] | None = None) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(payload, separators=(",", ":"), default=default),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


_DRY_RUN_NOTE = (
    "This was a dry run. To commit, call {tool} again with "
    "dry_run=false and confirmation_token set to the token above."
)


# ---------------------------------------------------------------------------
# odoo_help content — hardcoded knowledge, no Odoo calls
# ---------------------------------------------------------------------------


_HELP_SUMMARY = (
    "This MCP exposes a security-gated slice of Odoo over XML-RPC. Every call "
    "targets a named instance, goes through model validation (a small hardcoded "
    "denylist of auth / ACL / code / config models is always blocked; each "
    "instance then runs in open mode — every other model allowed — or strict "
    "mode with an explicit allowlist), then through a domain sandbox and a "
    "field-redaction policy, and is audit-logged. Production writes are blocked "
    "by default and require an explicit unlock plus a dry-run confirmation "
    "token before committing."
)


_HELP_COMMON_PATTERNS: list[dict[str, Any]] = [
    {
        "goal": "Count records matching criteria",
        "use": "odoo_search_count",
        "example": {
            "instance": "prod",
            "model": "crm.lead",
            "domain": [
                ["type", "=", "opportunity"],
                ["stage_id.name", "!=", "Won"],
            ],
        },
    },
    {
        "goal": "Dashboard-style aggregation (leads per stage, revenue per month)",
        "use": "odoo_read_group",
        "example": {
            "instance": "prod",
            "model": "crm.lead",
            "fields": ["id:count", "expected_revenue:sum"],
            "groupby": ["stage_id"],
        },
    },
    {
        "goal": "Read records with specific fields",
        "use": "odoo_search_read",
        "example": {
            "instance": "prod",
            "model": "res.partner",
            "domain": [["is_company", "=", True]],
            "fields": ["id", "name", "email"],
            "limit": 20,
        },
    },
    {
        "goal": "Update a record on production",
        "use": "odoo_enable_prod_writes then odoo_write",
        "example": (
            "1) odoo_enable_prod_writes(instance='prod'). "
            "2) odoo_write(dry_run=true) — returns preview + token. "
            "3) odoo_write(dry_run=false, confirmation_token=TOKEN)."
        ),
    },
    {
        "goal": "See what fields exist on a model",
        "use": "odoo_describe_model",
    },
]


_HELP_GOTCHAS: list[str] = [
    "Dotted-field traversal in domains is rejected (no 'create_uid.login'). "
    "Filter by relation value instead.",
    "Sensitive fields (vat, ssnid, bank_ids, private_email, ...) require "
    "allow_sensitive_fields=['NAME', ...] per-call.",
    "Password/api_key/token fields are ALWAYS redacted. Opting in does not unlock them.",
    "Model access: each instance is either in 'open' mode (any model allowed "
    "except a hardcoded denylist of ~25 auth / ACL / code / config models "
    "like res.users, ir.config_parameter, ir.actions.server, mail.template, "
    "ir.attachment) or 'strict' mode (enumerated allowlist). Check "
    "odoo_list_instances for the mode per instance.",
    "On production, writes default to dry_run=true. Pass dry_run=false AND a "
    "confirmation_token from a prior dry run to commit.",
    "To remove records, use odoo_archive_or_delete. Always offer archive "
    "(reversible: active=False) before permanent delete (unlink).",
]


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise OdooMcpError(f"Argument {key!r} must be a non-empty string.")
    return value


def _require_list_of_str(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise OdooMcpError(f"Argument {key!r} must be a non-empty list of strings.")
    for item in value:
        if not isinstance(item, str):
            raise OdooMcpError(f"{key!r} must contain only strings.")
    return list(value)


def _require_list_of_int(args: dict[str, Any], key: str) -> list[int]:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise OdooMcpError(f"Argument {key!r} must be a non-empty list of integers.")
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise OdooMcpError(f"{key!r} must contain only integers.")
    return list(value)


def _require_dict(args: dict[str, Any], key: str) -> dict[str, Any]:
    value = args.get(key)
    if not isinstance(value, dict):
        raise OdooMcpError(f"{key} must be an object/dict")
    return value


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OdooMcpError(f"{key} must be a string")
    return value


def _offset(args: dict[str, Any]) -> int:
    offset = int(args.get("offset") or 0)
    if offset < 0:
        raise OdooMcpError("offset must be >= 0")
    return offset


# ---------------------------------------------------------------------------
# Audit shape helpers
#
# We record the SHAPE of tool arguments (keys, counts, declared types), never
# the values. Values would defeat the whole "no PII in logs" property.
# Exception: field NAMES, model strings, instance names and groupby specs
# (buckets included) are not secret — they're already in public schemas and
# help operators understand what happened.
# ---------------------------------------------------------------------------


_IDENTIFIER_KEYS = frozenset({"instance", "model", "tool", "order", "orderby", "mode"})
_SCALAR_KEYS = frozenset({"limit", "offset", "dry_run", "include_binary", "lazy"})


def _present(value: Any) -> dict[str, Any]:
    return {"present": True, "type": type(value).__name__}


def _args_shape(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized shape of ``arguments`` for the audit log.

    Keys, counts, types, and non-secret identifiers (model, instance, field
    names, groupby specs) are preserved; values of ``domain``, ``ids``, and
    ``values`` are replaced with counts / sorted key lists. Unknown args are
    reduced to a ``{present: True, type: <typename>}`` summary.
    """
    out: dict[str, Any] = {"keys": sorted(arguments.keys())}
    for key, value in arguments.items():
        if key in _IDENTIFIER_KEYS:
            out[key] = value if isinstance(value, str) else _present(value)
        elif key == "domain":
            if isinstance(value, list):
                out["domain_leaves"] = sum(1 for e in value if not isinstance(e, str))
                out["domain_operators"] = sum(1 for e in value if isinstance(e, str))
            else:
                out["domain"] = _present(value)
        elif key == "fields":
            if isinstance(value, list):
                out["field_count"] = len(value)
                out["field_names"] = sorted(v for v in value if isinstance(v, str))
            else:
                out["fields"] = _present(value)
        elif key == "groupby":
            if isinstance(value, list):
                out["groupby_count"] = len(value)
                out["groupby_specs"] = [v for v in value if isinstance(v, str)]
            else:
                out["groupby"] = _present(value)
        elif key == "ids":
            if isinstance(value, list):
                out["id_count"] = len(value)
            else:
                out["ids"] = _present(value)
        elif key == "values":
            if isinstance(value, dict):
                out["value_count"] = len(value)
                out["value_keys"] = sorted(k for k in value if isinstance(k, str))
            else:
                out["values"] = _present(value)
        elif key == "allow_sensitive_fields":
            # Never log the contents — just the count.
            out["allow_sensitive_count"] = len(value) if isinstance(value, list) else 0
        elif key == "confirmation_token":
            out["confirmation_token_present"] = bool(value)
        elif key in _SCALAR_KEYS:
            out[key] = (
                value if isinstance(value, (str, int, bool)) or value is None else _present(value)
            )
        else:
            # Unknown / future arg — record presence + type name only.
            out[key] = _present(value)
    return out


def _sanitize_details(details: dict[str, Any]) -> dict[str, Any]:
    """Filter a details dict down to the audit-log schema.

    Top-level values may be primitives (``str``/``int``/``bool``/``None``) or
    a single level of dict whose values are the same primitives (or lists of
    strings). Anything else is dropped to keep the audit log tight.
    """

    def _leaf_ok(v: Any) -> bool:
        if isinstance(v, (bool, int, str)) or v is None:
            return True
        if isinstance(v, list):
            return all(isinstance(item, str) for item in v)
        return False

    out: dict[str, Any] = {}
    for k, v in details.items():
        if _leaf_ok(v):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = {ik: iv for ik, iv in v.items() if _leaf_ok(iv)}
    return out
