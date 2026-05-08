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

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.types import TextContent

from . import __version__
from ._scan_heuristics import is_custom_field_name, is_studio_field_name
from .audit import AuditEvent, AuditLog
from .client import OdooClient
from .config import AppConfig, InstanceConfig
from .errors import (
    FieldPolicyError,
    InstanceNotFoundError,
    ModelNotAllowedError,
    OdooMcpError,
    ProdGuardError,
)
from .security.allowlist import (
    ALLOWLIST_WILDCARD,
    MODEL_DENYLIST,
    MODEL_WRITE_BLOCKLIST,
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
from .security.smart_fields import select_smart_fields

logger = logging.getLogger(__name__)


def _latency_budget_ms() -> int | None:
    """Return the per-tool latency budget in ms, or ``None`` if unset.

    Driven by ``ODOO_MCP_TOOL_LATENCY_BUDGET_MS``. When a successful call
    exceeds the budget, the dispatcher emits a WARNING log line tagged
    ``slow_tool_call``. Pure observability — never fails the call. Set
    to a non-positive integer to disable; absent / unparseable values
    also disable.
    """
    raw = os.environ.get("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _read_only_session() -> bool:
    """True iff ``ODOO_MCP_READ_ONLY`` is set to a truthy value at process start.

    Read each call (cheap) so tests can set/unset the env var dynamically.
    Truthy values are ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case-insensitive).
    Anything else — including unset — disables the gate.
    """
    raw = os.environ.get("ODOO_MCP_READ_ONLY", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Shared application state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InstanceRuntime:
    """Everything the dispatcher needs for one configured instance."""

    config: InstanceConfig
    client: OdooClient
    extra_redacted: tuple[re.Pattern[str], ...] = ()


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
            # Token discipline: if the hint is already a substring of the
            # error message, the caller would just see duplicated text —
            # drop it. Only include hints that add new information.
            if exc.hint and exc.hint not in exc.user_message:
                payload["hint"] = exc.hint
            return [_text(payload)]
        except Exception as exc:  # noqa: BLE001 — last-resort safety net
            wrapped = OdooMcpError(f"Unhandled error in {name}: {type(exc).__name__}: {exc}")
            self._audit_failure(name, arguments, wrapped, _elapsed_ms(started))
            return [
                _text({"ok": False, "error_code": "internal_error", "error": wrapped.user_message})
            ]
        elapsed = _elapsed_ms(started)
        budget = _latency_budget_ms()
        if budget is not None and elapsed > budget:
            instance_str = instance if isinstance(instance, str) else "-"
            logger.warning(
                "slow_tool_call: tool=%s instance=%s elapsed_ms=%d budget_ms=%d",
                name,
                instance_str,
                elapsed,
                budget,
            )
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

    def _help(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return a capability overview. Never authenticates, never contacts Odoo.

        Default response is a terse summary + tool one-liners + instance list.
        Pass ``verbose=true`` to include the cookbook (common_patterns with
        examples) and gotchas — useful at the start of a session, but ~3x the
        token cost.
        """
        verbose = bool(args.get("verbose") or False)
        instances = [
            _instance_summary(name, rt, self.app.prod_guard.is_unlocked(name))
            for name, rt in self.app.instances.items()
        ]
        payload: dict[str, Any]
        if verbose:
            payload = {
                "version": __version__,
                "summary": _HELP_SUMMARY,
                "common_patterns": _HELP_COMMON_PATTERNS,
                "gotchas": _HELP_GOTCHAS,
                "denylist_size": len(MODEL_DENYLIST),
                "instances": instances,
            }
        else:
            payload = {
                "version": __version__,
                "summary": _HELP_SUMMARY_TERSE,
                "tools": _HELP_TOOLS_TERSE,
                "instances": instances,
            }
        self._audit("odoo_help", Operation.HELP, None, None, 0, False, {})
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
        self._audit("odoo_list_instances", Operation.LIST_INSTANCES, None, None, 0, False, {})
        result: dict[str, Any] = {
            "mcp_version": __version__,
            "denylist_size": len(MODEL_DENYLIST),
            "instances": out,
        }
        if _read_only_session():
            result["session_read_only"] = True
        return result

    def _describe_model(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_describe_model", args, Operation.FIELDS_GET)
        assert ctx.model is not None
        verbose = bool(args.get("verbose") or False)
        # Default: only the bits Claude actually needs to choose fields.
        # Verbose: full schema (help text, relation, readonly, _note).
        if verbose:
            keep = {
                "type",
                "string",
                "required",
                "readonly",
                "help",
                "relation",
                "_sensitive",
                "_note",
            }
        else:
            keep = {"type", "string", "required", "_sensitive"}
        raw = redact_fields_get(
            ctx.model,
            ctx.rt.client.fields_get(ctx.model),
            instance_overrides=ctx.rt.config.sensitive_fields,
            extra_redacted=ctx.rt.extra_redacted,
        )
        if verbose:
            filtered = {
                fname: {k: v for k, v in meta.items() if k in keep} for fname, meta in raw.items()
            }
        else:
            # Drop falsy required / _sensitive entries — they add no signal
            # but ~15 chars per field. type + string are always kept.
            filtered = {
                fname: {
                    k: v for k, v in meta.items() if k in keep and (k in {"type", "string"} or v)
                }
                for fname, meta in raw.items()
            }
        # Tag custom and Studio-origin fields so Claude knows they're not part
        # of standard Odoo. Cheap heuristic via name prefix; matches the same
        # logic used by the scan-custom CLI. Only emit truthy markers — keeps
        # the response tight when there are no custom fields on the model.
        custom_count = 0
        studio_count = 0
        for fname, meta in filtered.items():
            if is_studio_field_name(fname):
                meta["_studio"] = True
                meta["_custom"] = True
                custom_count += 1
                studio_count += 1
            elif is_custom_field_name(fname):
                meta["_custom"] = True
                custom_count += 1
        details: dict[str, Any] = {"field_count": len(filtered)}
        if custom_count:
            details["custom_field_count"] = custom_count
        if studio_count:
            details["studio_field_count"] = studio_count
        self._audit_ok(ctx, details, args)
        return {"model": ctx.model, "fields": filtered}

    def _lookup(self, args: dict[str, Any]) -> dict[str, Any]:
        """Fast `name ilike` lookup returning only id + display_name.

        The domain shape is fixed (``[("name", "ilike", query)]``), so the
        domain sandbox is intentionally bypassed — there is nothing the
        caller can mutate that would change which records are searchable.
        Sensitive-field redaction still runs on the result so a model whose
        ``display_name`` resolves to a redacted field doesn't leak it.
        """
        ctx = self._begin("odoo_lookup", args, Operation.LOOKUP)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        query = _require_str(args, "query")
        raw_limit = args.get("limit", 10)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise OdooMcpError("limit must be an integer.")
        if raw_limit < 1:
            raise OdooMcpError("limit must be >= 1.")
        # Clamp the caller's limit to the instance's hard cap.
        effective_limit = min(raw_limit, rt.config.max_records_hard_cap)

        fields_meta = rt.client.fields_get(model)
        results = rt.client.lookup(model, query, effective_limit)
        # Even though we only requested id + display_name, run the result
        # through the redactor so a model whose display_name is sensitive
        # (or where extra patterns flag it) still gets stripped.
        redacted = redact_response(
            model,
            results,
            {n: m.get("type", "") for n, m in fields_meta.items()},
            allow_sensitive=frozenset(),
            include_binary=False,
            instance_overrides=rt.config.sensitive_fields,
            extra_redacted=rt.extra_redacted,
        )
        self._audit_ok(
            ctx,
            {
                "query_len": len(query),
                "result_count": len(redacted),
                "limit": effective_limit,
            },
            args,
        )
        return {
            "instance": ctx.instance,
            "model": model,
            "results": redacted,
            "count": len(redacted),
        }

    def _search_read(self, args: dict[str, Any]) -> dict[str, Any]:
        ctx = self._begin("odoo_search_read", args, Operation.SEARCH_READ)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        allow_sensitive = frozenset(args.get("allow_sensitive_fields") or [])
        offset = _offset(args)

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        overrides = rt.config.sensitive_fields
        smart = False
        raw_fields = args.get("fields")
        if raw_fields is None:
            override = rt.config.smart_fields_overrides.get(model)
            if override is not None:
                # Validate the override against the live schema and the
                # sensitive-field policy. Trusted source (config.toml,
                # chmod 600) but we still apply the same checks so a typo
                # surfaces clearly and a sensitive field can't be opened
                # via override + omitted ``allow_sensitive_fields``.
                fields = validate_requested_fields(
                    model,
                    list(override),
                    known,
                    allow_sensitive=allow_sensitive,
                    instance_overrides=overrides,
                    extra_redacted=rt.extra_redacted,
                )
            else:
                fields = select_smart_fields(
                    model,
                    fields_meta,
                    instance_overrides=overrides,
                    extra_redacted=rt.extra_redacted,
                )
            smart = True
        else:
            fields = validate_requested_fields(
                model,
                _require_list_of_str(args, "fields"),
                known,
                allow_sensitive=allow_sensitive,
                instance_overrides=overrides,
                extra_redacted=rt.extra_redacted,
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
            extra_redacted=rt.extra_redacted,
        )
        # Token discipline: Odoo sometimes returns extras like __last_update
        # or display_name that the caller didn't ask for. Strip anything not
        # explicitly requested, but always keep id (the record key).
        redacted = _strip_extra_fields(redacted, fields)
        self._audit_ok(
            ctx,
            {
                "record_count": len(redacted),
                "limit": limit,
                "offset": offset,
                "field_count": len(fields),
                "domain_leaves": sum(1 for e in domain if not isinstance(e, str)),
                "smart_fields": smart,
            },
            args,
        )
        result: dict[str, Any] = {
            "instance": ctx.instance,
            "model": model,
            "records": redacted,
            "count": len(redacted),
        }
        # If the page came back full, signal there may be more so Claude
        # knows to either bump ``offset`` or narrow the domain. Doesn't
        # cost a round trip — purely a flag based on what we already have.
        # When we returned fewer records than the limit, ``has_more`` is
        # known false; when equal, we don't know — keep it as a hint.
        if len(redacted) >= limit:
            result["has_more"] = True
            # Use the actual count of records we received, not the requested
            # limit. If Odoo defensively returned more than ``limit`` (shouldn't
            # happen on stock Odoo, but a third-party module could) and we used
            # ``offset + limit``, the next page would skip records. Anchoring
            # on the live count is correct in both the normal and the over-
            # delivery case.
            result["next_offset"] = offset + len(redacted)
        else:
            result["has_more"] = False
        if smart:
            result["smart_fields_used"] = fields
        return result

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
            extra_redacted=rt.extra_redacted,
        )
        groupby = validate_groupby(
            model,
            _require_list_of_str(args, "groupby"),
            known,
            allow_sensitive=allow_sensitive,
            instance_overrides=overrides,
            extra_redacted=rt.extra_redacted,
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
        # Token discipline: Odoo returns a literal __domain per group for
        # drill-down, but Claude rarely uses it. Drop unless caller opts in.
        if not bool(args.get("include_domain") or False):
            for row in rows:
                if isinstance(row, dict):
                    row.pop("__domain", None)
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
        known = frozenset(fields_meta.keys())
        overrides = rt.config.sensitive_fields
        smart = False
        raw_fields = args.get("fields")
        if raw_fields is None:
            override = rt.config.smart_fields_overrides.get(model)
            if override is not None:
                fields = validate_requested_fields(
                    model,
                    list(override),
                    known,
                    allow_sensitive=allow_sensitive,
                    instance_overrides=overrides,
                    extra_redacted=rt.extra_redacted,
                )
            else:
                fields = select_smart_fields(
                    model,
                    fields_meta,
                    instance_overrides=overrides,
                    extra_redacted=rt.extra_redacted,
                )
            smart = True
        else:
            fields = validate_requested_fields(
                model,
                _require_list_of_str(args, "fields"),
                known,
                allow_sensitive=allow_sensitive,
                instance_overrides=overrides,
                extra_redacted=rt.extra_redacted,
            )
        redacted = redact_response(
            model,
            rt.client.read(model, ids, fields),
            {n: m.get("type", "") for n, m in fields_meta.items()},
            allow_sensitive=allow_sensitive,
            include_binary=bool(args.get("include_binary") or False),
            instance_overrides=overrides,
            extra_redacted=rt.extra_redacted,
        )
        # Strip extras Odoo added (__last_update, display_name when not asked).
        redacted = _strip_extra_fields(redacted, fields)
        self._audit_ok(
            ctx,
            {
                "record_count": len(redacted),
                "field_count": len(fields),
                "id_count": len(ids),
                "smart_fields": smart,
            },
            args,
        )
        result: dict[str, Any] = {
            "instance": ctx.instance,
            "model": model,
            "records": redacted,
            "count": len(redacted),
        }
        if smart:
            result["smart_fields_used"] = fields
        return result

    def _create(self, args: dict[str, Any]) -> dict[str, Any]:
        _refuse_if_read_only_session()
        ctx = self._begin("odoo_create", args, Operation.CREATE)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        _refuse_write_blocklisted(model)
        values = _require_dict(args, "values")
        self.app.prod_guard.check_write(ctx.instance, rt.config.production)

        known = frozenset(rt.client.fields_get(model).keys())
        validated = validate_write_values(model, values, known, extra_redacted=rt.extra_redacted)
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
        result: dict[str, Any] = {
            "instance": ctx.instance,
            "model": model,
            "id": new_id,
            "committed": True,
        }
        self._add_commits_remaining(result, ctx)
        return result

    def _write(self, args: dict[str, Any]) -> dict[str, Any]:
        _refuse_if_read_only_session()
        ctx = self._begin("odoo_write", args, Operation.WRITE)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        _refuse_write_blocklisted(model)
        ids = _require_list_of_int(args, "ids")
        values = _require_dict(args, "values")
        self.app.prod_guard.check_write(ctx.instance, rt.config.production)

        cap = rt.config.max_records_hard_cap
        if len(ids) > cap:
            raise OdooMcpError(f"Cannot write to more than {cap} ids at once.")

        known = frozenset(rt.client.fields_get(model).keys())
        validated = validate_write_values(model, values, known, extra_redacted=rt.extra_redacted)
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
        result: dict[str, Any] = {
            "instance": ctx.instance,
            "model": model,
            "ids": ids,
            "committed": ok,
        }
        self._add_commits_remaining(result, ctx)
        return result

    def _archive_or_delete(self, args: dict[str, Any]) -> dict[str, Any]:
        """Archive (active=False) or permanently unlink records.

        Claude must ask the user which path they want. Archive is reversible
        and preserves history; delete is permanent. Same prod-guard +
        dry-run + confirmation-token flow as odoo_write / odoo_create.
        """
        mode = args.get("mode")
        if mode not in ("archive", "delete"):
            raise OdooMcpError("mode must be 'archive' or 'delete'.")
        _refuse_if_read_only_session()
        op = Operation.ARCHIVE if mode == "archive" else Operation.UNLINK
        ctx = self._begin("odoo_archive_or_delete", args, op)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        _refuse_write_blocklisted(model)
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
        result: dict[str, Any] = {
            "instance": ctx.instance,
            "model": model,
            "mode": mode,
            "ids": ids,
            "committed": ok,
        }
        self._add_commits_remaining(result, ctx)
        return result

    def _add_commits_remaining(self, result: dict[str, Any], ctx: _Ctx) -> None:
        """If on prod, expose the post-commit burst budget to the caller."""
        if not ctx.rt.config.production:
            return
        remaining = self.app.prod_guard.commits_remaining(ctx.instance)
        if remaining is not None:
            result["commits_remaining"] = remaining

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

    def _diagnose_access(self, args: dict[str, Any]) -> dict[str, Any]:
        """Report Odoo ACL info for the authenticated user on one model.

        Calls ``check_access_rights(op, raise_exception=False)`` for the four
        canonical operations. The model still has to pass the MCP allowlist
        — diagnose is read-only but operates inside our security envelope.
        Pure introspection: no record reads, no writes.
        """
        ctx = self._begin("odoo_diagnose_access", args, Operation.DIAGNOSE_ACCESS)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        rights: dict[str, bool] = {}
        for op in ("read", "write", "create", "unlink"):
            try:
                rights[f"can_{op}"] = rt.client.check_access_rights(model, op)
            except OdooMcpError:
                # If Odoo refuses to even tell us (rare), report False rather
                # than failing the whole tool. The audit log still captures it.
                rights[f"can_{op}"] = False
        self._audit_ok(
            ctx,
            {"can_read_field": rights["can_read"]},
            args,
        )
        return {
            "instance": ctx.instance,
            "model": model,
            "uid": rt.client.uid,
            "login": rt.client.username,
            "is_admin": rt.client.is_admin,
            "admin_reason": rt.client.admin_reason,
            **rights,
        }

    def _enable_prod_writes(self, args: dict[str, Any]) -> dict[str, Any]:
        _refuse_if_read_only_session()
        instance_name = _require_str(args, "instance")
        rt = self.app.instance(instance_name)
        expiry = self.app.prod_guard.unlock(
            instance_name,
            rt.config.production,
            max_commits=rt.config.max_commits_per_unlock,
        )
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
            "commits_remaining": rt.config.max_commits_per_unlock,
            "note": (
                f"Writes are unlocked for 15 minutes of activity. Every write still "
                f"defaults to dry_run=true on prod; you must pass dry_run=false and a "
                f"confirmation_token to commit. Up to "
                f"{rt.config.max_commits_per_unlock} commits allowed in this window."
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
        """Best-effort audit-log write for a failed tool call.

        Asymmetry with :meth:`_audit_ok` is intentional. The success
        path is fail-loud: if the audit log is broken we refuse to
        return a result, because an unaudited successful side-effect
        would be a security gap. The failure path is fail-quiet
        towards the caller — we already have an error to surface and
        double-faulting would mask it — but a broken audit log is
        itself an operational concern, so we log it at ``ERROR`` via
        the standard ``logging`` module instead of swallowing it
        silently. Operators with ``ODOO_MCP_LOG_LEVEL=ERROR`` (or
        below) will see audit-system breakage even on the failure
        path.
        """
        instance = arguments.get("instance") if isinstance(arguments, dict) else None
        model = arguments.get("model") if isinstance(arguments, dict) else None
        raw: dict[str, Any] = {"error": error.user_message[:500]}
        if isinstance(arguments, dict):
            raw["args"] = _args_shape(arguments)
        try:
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
        except OdooMcpError as audit_exc:
            logger.error("audit log write failed during failure path: %s", audit_exc)


# Name -> handler dispatch table. Bound methods resolve via the instance arg.
_HANDLERS: dict[str, Callable[[Dispatcher, dict[str, Any]], dict[str, Any]]] = {
    "odoo_help": Dispatcher._help,
    "odoo_list_instances": Dispatcher._list_instances,
    "odoo_describe_model": Dispatcher._describe_model,
    "odoo_lookup": Dispatcher._lookup,
    "odoo_search_read": Dispatcher._search_read,
    "odoo_search_count": Dispatcher._search_count,
    "odoo_read_group": Dispatcher._read_group,
    "odoo_read": Dispatcher._read,
    "odoo_create": Dispatcher._create,
    "odoo_write": Dispatcher._write,
    "odoo_archive_or_delete": Dispatcher._archive_or_delete,
    "odoo_enable_prod_writes": Dispatcher._enable_prod_writes,
    "odoo_diagnose_access": Dispatcher._diagnose_access,
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
    # Admin-credential detection (set after authenticate()). Surface as a
    # warning so both Claude and the user can see they're running with
    # elevated Odoo rights, which defeats per-user ACL scoping.
    if rt.client.is_admin:
        summary["admin_warning"] = (
            f"This instance is authenticated as {rt.client.admin_reason}. "
            f"Most Odoo record rules are bypassed. The MCP's defense layers "
            f"still apply, but the Odoo-side ACL scoping that this MCP relies "
            f"on for per-user permissions is NOT in effect. Switch to a "
            f"dedicated non-admin Odoo user for production use."
        )
    return summary


def _text(payload: dict[str, Any], *, default: Callable[[Any], Any] | None = None) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(payload, separators=(",", ":"), default=default),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _refuse_if_read_only_session() -> None:
    """Refuse any write-path entry point if the session is read-only.

    The check is independent of per-instance ``production`` flags and the
    prod-guard unlock state. Setting ``ODOO_MCP_READ_ONLY=1`` in the
    process environment turns the entire MCP into a strict read-only
    surface — useful for demos, training sessions, or external
    consultants who should never be able to commit anything.
    """
    if _read_only_session():
        raise ProdGuardError(
            "Session is read-only (ODOO_MCP_READ_ONLY=1). All write paths "
            "are refused regardless of instance, prod-guard state, or unlock."
        )


def _refuse_write_blocklisted(model: str) -> None:
    """Refuse any write-path call against a model in :data:`MODEL_WRITE_BLOCKLIST`.

    Runs BEFORE prod-guard / dry-run logic so the refusal is the same on
    dev and prod, and so an unlocked prod-write window cannot be used to
    sneak a write through. The hint deliberately does NOT enumerate
    workarounds — see v0.13.1 F2 (no-suggestion error policy).
    """
    if model in MODEL_WRITE_BLOCKLIST:
        raise ModelNotAllowedError(
            f"Model {model!r} is read-only via the MCP. "
            f"This model is exposed for reading but writes (create / update / "
            f"archive / delete) are refused as a hard safety invariant."
        )


def _strip_extra_fields(
    records: list[dict[str, Any]], requested: list[str]
) -> list[dict[str, Any]]:
    """Drop fields the caller didn't explicitly request.

    Odoo's ``search_read`` / ``read`` may return ``__last_update`` and
    ``display_name`` even when those aren't in ``fields``. They bloat every
    record (display_name alone can be 50+ chars). Strip anything not in the
    caller's requested set, but always keep ``id`` — every consumer needs
    the record key.
    """
    keep: set[str] = set(requested)
    keep.add("id")
    return [{k: v for k, v in rec.items() if k in keep} for rec in records]


_DRY_RUN_NOTE = (
    "This was a dry run. To commit, call {tool} again with "
    "dry_run=false and confirmation_token set to the token above."
)


# ---------------------------------------------------------------------------
# odoo_help content — hardcoded knowledge, no Odoo calls
# ---------------------------------------------------------------------------


_HELP_SUMMARY_TERSE = (
    "Security-gated Odoo over XML-RPC: per-instance allowlists, domain sandbox, "
    "field redaction, prod-write guard with dry-run + confirmation tokens. "
    "Call odoo_help(verbose=true) for the full cookbook (patterns + gotchas)."
)


_HELP_TOOLS_TERSE: list[dict[str, str]] = [
    {"name": "odoo_help", "purpose": "Capability overview. Pass verbose=true for cookbook."},
    {"name": "odoo_list_instances", "purpose": "List configured instances + their modes."},
    {
        "name": "odoo_describe_model",
        "purpose": "Field schema for a model. Pass verbose=true for help text + relations.",
    },
    {"name": "odoo_lookup", "purpose": "Fast name ilike lookup -> id + display_name."},
    {"name": "odoo_search_read", "purpose": "Search + read with explicit fields list."},
    {"name": "odoo_search_count", "purpose": "Count records matching a domain."},
    {
        "name": "odoo_read_group",
        "purpose": "Aggregations / dashboards. Pass include_domain=true for drill-down domains.",
    },
    {"name": "odoo_read", "purpose": "Read records by id with explicit fields."},
    {"name": "odoo_create", "purpose": "Create record. Prod: dry_run -> token -> commit."},
    {"name": "odoo_write", "purpose": "Update records. Prod: dry_run -> token -> commit."},
    {
        "name": "odoo_archive_or_delete",
        "purpose": "Archive (reversible) or delete (permanent). Always ask user which.",
    },
    {"name": "odoo_enable_prod_writes", "purpose": "Unlock prod writes for 15 minutes."},
    {"name": "odoo_diagnose_access", "purpose": "Read/write/create/unlink rights on a model."},
]


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
        "goal": "Find a record by name (much faster than full search_read)",
        "use": "odoo_lookup",
        "example": {
            "instance": "prod",
            "model": "res.partner",
            "query": "Acme",
            "limit": 5,
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


def _require_int_or_default(
    args: dict[str, Any], key: str, default: int, *, minimum: int = 0
) -> int:
    """Strict integer arg with default and minimum.

    Rejects anything that isn't a real ``int`` (no implicit conversion of
    strings or floats). Booleans are subclasses of ``int`` in Python and are
    explicitly rejected — they would otherwise sneak through as 0/1.
    """
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise OdooMcpError(f"{key} must be an integer.")
    # mypy --strict can't narrow ``Any`` past the isinstance check above;
    # we know it's a real int here.
    ivalue: int = value
    if ivalue < minimum:
        raise OdooMcpError(f"{key} must be >= {minimum}.")
    return ivalue


def _offset(args: dict[str, Any]) -> int:
    return _require_int_or_default(args, "offset", 0, minimum=0)


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
