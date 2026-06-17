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

import base64
import binascii
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
    classify_model_block,
)
from .security.document_actions import (
    WizardCompletion,
    resolve_document_action,
    resolve_wizard_completion,
)
from .security.domain import sandbox_domain
from .security.fields import (
    redact_fields_get,
    redact_response,
    restrict_fields_meta,
    validate_aggregate_fields,
    validate_groupby,
    validate_requested_fields,
    validate_write_values,
)
from .security.limits import RateLimiter, clamp_limit
from .security.prod_guard import ProdGuard, compute_payload_digest
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


def _external_comms_globally_enabled() -> bool:
    """True iff ``ODOO_MCP_ENABLE_EXTERNAL_COMMS`` is set to a truthy value.

    Read each call (cheap) so tests can set / unset it dynamically.
    Outbound communications via the MCP are off by default — the
    operator must opt in *twice* (this env var AND the per-instance
    ``external_comms_enabled`` config flag) before ``odoo_send_message``
    can be invoked or even advertised.
    """
    raw = os.environ.get("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
            # Message is factual — it lists configured instances so a
            # human-with-typo can self-correct from the audit log /
            # client UI. The "do not substitute" behavioural directive
            # lives in the error's ``hint``, which the dispatcher
            # surfaces separately to the AI. See InstanceNotFoundError.
            raise InstanceNotFoundError(
                f"Instance {name!r} is not configured on this MCP install. "
                f"Configured instances: {sorted(self.instances.keys())}."
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

    def _resolve_read_fields(
        self,
        rt: InstanceRuntime,
        model: str,
        fields_meta: dict[str, dict[str, Any]],
        known: frozenset[str],
        args: dict[str, Any],
        allow_sensitive: frozenset[str],
    ) -> tuple[list[str], bool]:
        """Resolve the field list for a read-path tool.

        Returns ``(fields, smart)``. Three branches, in priority order:

        1. Caller passed ``fields=[...]`` — validate explicitly, no
           smart selection. ``smart=False``.
        2. Caller omitted ``fields`` AND the instance has a
           ``smart_fields_overrides[model]`` override — use the
           configured list, validated through the normal redaction
           policy so a typo or sensitive field surfaces clearly.
           ``smart=True``.
        3. Caller omitted ``fields`` and there's no override — fall
           back to :func:`select_smart_fields` heuristic.
           ``smart=True``.

        Shared between :meth:`_search_read` and :meth:`_read`. The
        previous duplicated 30-line branch is the bug surface this
        helper closes.
        """
        overrides = rt.config.sensitive_fields
        raw_fields = args.get("fields")
        if raw_fields is not None:
            return (
                validate_requested_fields(
                    model,
                    _require_list_of_str(args, "fields"),
                    known,
                    allow_sensitive=allow_sensitive,
                    instance_overrides=overrides,
                    extra_redacted=rt.extra_redacted,
                ),
                False,
            )
        configured = rt.config.smart_fields_overrides.get(model)
        if configured is not None:
            return (
                validate_requested_fields(
                    model,
                    list(configured),
                    known,
                    allow_sensitive=allow_sensitive,
                    instance_overrides=overrides,
                    extra_redacted=rt.extra_redacted,
                ),
                True,
            )
        return (
            select_smart_fields(
                model,
                fields_meta,
                instance_overrides=overrides,
                extra_redacted=rt.extra_redacted,
            ),
            True,
        )

    def _fields_meta(self, rt: InstanceRuntime, model: str) -> dict[str, dict[str, Any]]:
        """``fields_get`` filtered through the hard per-model read whitelist.

        The single choke point: every tool's view of a model's fields goes
        through here, so a whitelisted model (res.users) can't leak
        non-whitelisted fields via any code path — smart selection, explicit
        ``fields=``, domain leaves, groupby, and response redaction all key
        off this metadata.
        """
        return restrict_fields_meta(model, rt.client.fields_get(model))

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
            self._fields_meta(ctx.rt, ctx.model),
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

        fields_meta = self._fields_meta(rt, model)
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

        fields_meta = self._fields_meta(rt, model)
        known = frozenset(fields_meta.keys())
        overrides = rt.config.sensitive_fields
        fields, smart = self._resolve_read_fields(
            rt, model, fields_meta, known, args, allow_sensitive
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
        known = frozenset(self._fields_meta(ctx.rt, ctx.model).keys())
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

        known = frozenset(self._fields_meta(rt, model).keys())
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

        fields_meta = self._fields_meta(rt, model)
        known = frozenset(fields_meta.keys())
        overrides = rt.config.sensitive_fields
        fields, smart = self._resolve_read_fields(
            rt, model, fields_meta, known, args, allow_sensitive
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

        known = frozenset(self._fields_meta(rt, model).keys())
        validated = validate_write_values(model, values, known, extra_redacted=rt.extra_redacted)
        n = len(validated)

        if self.app.prod_guard.effective_dry_run(args.get("dry_run"), rt.config.production):
            token = self.app.prod_guard.create_pending(
                ctx.instance,
                ctx.op.value,
                model,
                summary=f"create {model} (+{n} fields)",
                payload_digest=compute_payload_digest(_token_payload(ctx.op.value, args)),
            )
            self._audit_ok(ctx, {"field_count": n}, args, dry_run=True)
            preview: dict[str, Any] = {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "would_write_fields": sorted(validated.keys()),
                "confirmation_token": token,
                "note": _DRY_RUN_NOTE.format(tool="odoo_create"),
            }
            self._add_commits_remaining(preview, ctx, dry_run=True)
            return preview

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

        known = frozenset(self._fields_meta(rt, model).keys())
        validated = validate_write_values(model, values, known, extra_redacted=rt.extra_redacted)
        n = len(validated)

        if self.app.prod_guard.effective_dry_run(args.get("dry_run"), rt.config.production):
            id_preview = f"ids={ids[:5]}{'...' if len(ids) > 5 else ''}"
            token = self.app.prod_guard.create_pending(
                ctx.instance,
                ctx.op.value,
                model,
                summary=f"write {model} {id_preview} (+{n} fields)",
                payload_digest=compute_payload_digest(_token_payload(ctx.op.value, args)),
            )
            self._audit_ok(ctx, {"field_count": n, "id_count": len(ids)}, args, dry_run=True)
            preview: dict[str, Any] = {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "id_count": len(ids),
                "would_update_fields": sorted(validated.keys()),
                "confirmation_token": token,
                "note": _DRY_RUN_NOTE.format(tool="odoo_write"),
            }
            self._add_commits_remaining(preview, ctx, dry_run=True)
            return preview

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
            fields_meta = self._fields_meta(rt, model)
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
                ctx.instance,
                ctx.op.value,
                model,
                summary=summary,
                payload_digest=compute_payload_digest(_token_payload(ctx.op.value, args)),
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
            preview: dict[str, Any] = {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "mode": mode,
                "id_count": len(ids),
                "confirmation_token": token,
                "reminder": reminder,
                "note": _DRY_RUN_NOTE.format(tool="odoo_archive_or_delete"),
            }
            self._add_commits_remaining(preview, ctx, dry_run=True)
            return preview

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

    def _send_message(self, args: dict[str, Any]) -> dict[str, Any]:
        """Post a message on an Odoo record — email or log note.

        Twice-gated outbound communications path. Disabled by default;
        requires:

        1. ``ODOO_MCP_ENABLE_EXTERNAL_COMMS=1`` in the process environment
           (else the tool isn't even advertised in ``tools/list``, and a
           direct call here is refused).
        2. ``external_comms_enabled = true`` on the targeted instance
           in ``config.toml``.

        Then it flows through the standard prod-guard pipeline:
        unlock → dry-run preview (always, even on dev) → confirmation
        token → commit. The preview includes the full message body and
        recipient list verbatim, so a human can see exactly what will go
        out before approving.
        """
        _refuse_if_read_only_session()
        if not _external_comms_globally_enabled():
            raise ProdGuardError(
                "External communications are not enabled on this MCP install. "
                "Set ODOO_MCP_ENABLE_EXTERNAL_COMMS=1 in the environment AND "
                "add 'external_comms_enabled = true' to the instance config "
                "to opt in. Both gates must be set."
            )
        ctx = self._begin("odoo_send_message", args, Operation.SEND_MESSAGE)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        if not rt.config.external_comms_enabled:
            raise ProdGuardError(
                f"Instance {ctx.instance!r} has external_comms_enabled = false. "
                f"To allow outbound emails / log notes through the MCP on this "
                f"instance, add 'external_comms_enabled = true' under "
                f"[instances.{ctx.instance}] in your config."
            )
        # Refuse on models that are read-only via the MCP: posting a
        # message to e.g. ``mail.message`` itself makes no sense, but
        # the same blocklist also catches future additions.
        _refuse_write_blocklisted(model)

        record_id = _require_int(args, "record_id")
        body = _require_str(args, "body")
        message_type = args.get("message_type", "comment")
        if message_type not in ("comment", "notification"):
            raise OdooMcpError(
                "message_type must be 'comment' (sends email to followers / "
                "recipients) or 'notification' (internal log note)."
            )
        subject = _optional_str(args, "subject")
        raw_partners = args.get("partner_ids") or []
        if not isinstance(raw_partners, list):
            raise OdooMcpError("partner_ids must be a list of integer ids.")
        partner_ids: list[int] = []
        for pid in raw_partners:
            if not isinstance(pid, int) or isinstance(pid, bool):
                raise OdooMcpError("partner_ids must contain only integers.")
            partner_ids.append(pid)

        self.app.prod_guard.check_write(ctx.instance, rt.config.production)

        # Outbound communications always default to dry-run, on prod AND
        # on dev. The cost of an accidentally-sent email is the same in
        # both environments (real human reads it), so the "no surprise
        # sends" property has to hold everywhere.
        raw_dry = args.get("dry_run")
        dry_run = True if raw_dry is None else bool(raw_dry)

        if dry_run:
            preview_body = body if len(body) <= 2000 else body[:2000] + "...[truncated]"
            summary = f"{message_type} on {model}({record_id}) to {len(partner_ids)} partner(s)"
            token = self.app.prod_guard.create_pending(
                ctx.instance,
                ctx.op.value,
                model,
                summary=summary,
                payload_digest=compute_payload_digest(_token_payload(ctx.op.value, args)),
            )
            self._audit_ok(
                ctx,
                {
                    "message_type": str(message_type),
                    "record_id": record_id,
                    "partner_count": len(partner_ids),
                    "body_length": len(body),
                },
                args,
                dry_run=True,
            )
            preview_result: dict[str, Any] = {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "record_id": record_id,
                "message_type": message_type,
                "subject": subject,
                "body_preview": preview_body,
                "partner_ids": partner_ids,
                "would_send_email": message_type == "comment" and bool(partner_ids),
                "confirmation_token": token,
                "note": _DRY_RUN_NOTE.format(tool="odoo_send_message"),
            }
            self._add_commits_remaining(preview_result, ctx, dry_run=True)
            return preview_result

        self._consume_token_on_prod(ctx, args)
        message_id = rt.client.message_post(
            model,
            record_id,
            body,
            subject=subject,
            partner_ids=partner_ids,
            message_type=str(message_type),
        )
        self._audit_ok(
            ctx,
            {
                "message_type": str(message_type),
                "record_id": record_id,
                "partner_count": len(partner_ids),
                "message_id": message_id,
            },
            args,
        )
        result: dict[str, Any] = {
            "instance": ctx.instance,
            "model": model,
            "record_id": record_id,
            "message_id": message_id,
            "message_type": message_type,
            "committed": True,
        }
        self._add_commits_remaining(result, ctx)
        return result

    def _create_attachment(self, args: dict[str, Any]) -> dict[str, Any]:
        """Attach a base64-encoded file to an Odoo record.

        Bounded surface for adding ``ir.attachment`` rows from the
        agent. ``ir.attachment`` itself stays on the global denylist —
        the agent cannot ``search_read`` arbitrary attachments (a real
        exfil risk, since attachments often carry sensitive PDFs that
        bypass record-rules) and cannot ``unlink`` them. The only
        permitted operation is *create*, gated by this method.

        Validation pipeline:

        1. ``res_model`` runs through the usual ``check_model``
           (allowlist + denylist + write-blocklist). Attaching to e.g.
           ``mail.message`` is refused — the write-blocklist applies
           because adding a file to a message is semantically a write
           against that message.
        2. ``res_id`` must point at an existing record (one ``search_count``
           round-trip). A typo'd id otherwise creates an orphan
           attachment that's useless and quietly exfil-friendly.
        3. ``filename`` is sanity-checked: non-empty, no path separators
           (defence against Odoo accidentally interpreting them), max
           255 chars (matches Odoo's own column length).
        4. ``datas_base64`` is decoded once; the decoded content size
           is capped at 25 MB. Over the cap → refuse before any write.

        Full prod-guard pipeline: dry-run → confirmation token → commit.
        The token's payload digest binds to ``(res_model, res_id,
        filename, datas_base64, mimetype, description)`` so an agent
        that dry-runs a placeholder cannot commit a different (larger,
        renamed, retargeted) file with the same token.
        """
        _refuse_if_read_only_session()
        # The tool's public schema uses ``res_model`` (matching Odoo's
        # own ir.attachment field name) but the dispatcher pipeline
        # expects ``model`` in ``args`` for the allowlist + audit +
        # rate-limit machinery. Translate up front so a single value
        # flows through both layers; the audit log records the target
        # model under the canonical key, not a tool-local alias.
        res_model = _require_str(args, "res_model")
        args = {**args, "model": res_model}
        ctx = self._begin("odoo_create_attachment", args, Operation.CREATE_ATTACHMENT)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        _refuse_write_blocklisted(model)

        res_id = _require_int(args, "res_id")
        filename = _require_str(args, "filename").strip()
        mimetype = _optional_str(args, "mimetype")
        description = _optional_str(args, "description")
        # Two input modes: inline ``datas_base64`` (small, agent-typed)
        # or ``source_path`` (server-side file read; the only viable
        # path for >5 KB payloads, since base64 in tool-input kills
        # several agent SDKs silently before our 25 MB cap kicks in).
        # Exactly one is required; both at once is a config bug, not
        # a fallback ladder, so refuse explicitly.
        raw_b64 = args.get("datas_base64")
        raw_path = args.get("source_path")
        if raw_b64 and raw_path:
            raise OdooMcpError(
                "Provide exactly one of 'datas_base64' or 'source_path', not both. "
                "Inline base64 is for tiny payloads the agent typed itself; "
                "source_path is for files the api-server already wrote to a "
                "directory on this instance's attachment_source_paths allowlist."
            )
        if not raw_b64 and not raw_path:
            raise OdooMcpError(
                "Missing input: provide either 'datas_base64' (small inline "
                "payload) or 'source_path' (absolute path inside this "
                "instance's attachment_source_paths allowlist)."
            )
        # --- filename sanity (cheap; do this BEFORE any file I/O) -----
        if not filename:
            raise OdooMcpError("filename must be a non-empty string.")
        if len(filename) > 255:
            raise OdooMcpError("filename too long (max 255 characters).")
        if "/" in filename or "\\" in filename:
            # Odoo stores ``name`` as a plain string but downstream
            # consumers (filestore filenames, S3 keys, Content-Disposition
            # headers) sometimes interpret separators. Refuse outright —
            # the caller can rename before attaching.
            raise OdooMcpError(
                "filename must not contain path separators ('/' or '\\\\'). "
                "Strip the directory and pass only the leaf name."
            )

        # --- resolve payload to (base64_string, size_bytes) -----------
        # Both input modes converge here. The source_path branch already
        # knows the byte length from its bounded read, so we use that
        # directly instead of re-decoding our own base64 just to call
        # ``len()`` on it — that was ~25 MB of pointless alloc/free per
        # call for a large invoice PDF.
        if raw_path is not None:
            datas_base64, size_bytes = _read_source_path_as_base64(raw_path, rt.config)
            # Canonicalise args: bind the payload digest to the actual
            # file CONTENT (resolved bytes) and drop the path. Preview-
            # with-source_path and commit-with-datas_base64 of the same
            # file then share a digest; a preview-vs-commit content swap
            # (different file at the same path, OR different path
            # entirely) produces a different digest and the token is
            # refused — same property the inline path had in v0.23.0.
            args = {**args, "datas_base64": datas_base64}
            args.pop("source_path", None)
        else:
            datas_base64 = _require_str(args, "datas_base64")
            decoded = _b64decode_or_raise(datas_base64)
            if len(decoded) > _ATTACHMENT_MAX_BYTES:
                raise OdooMcpError(
                    f"Attachment content is {len(decoded)} bytes, over the "
                    f"{_ATTACHMENT_MAX_BYTES}-byte cap. Split the file or "
                    f"compress it before attaching."
                )
            size_bytes = len(decoded)

        # --- target record must exist ---------------------------------
        # One round-trip — keeps the audit log honest about what was
        # being attached to (and which res_model the operator approved
        # in the dry-run preview), and refuses orphan attachments which
        # would silently slip past Odoo's per-model record-rules.
        if rt.client.search_count(model, [("id", "=", res_id)]) == 0:
            raise OdooMcpError(
                f"Target record {model}({res_id}) does not exist or is not "
                "visible to the authenticated user — refusing to create an "
                "orphan attachment."
            )

        self.app.prod_guard.check_write(ctx.instance, rt.config.production)

        if self.app.prod_guard.effective_dry_run(args.get("dry_run"), rt.config.production):
            summary = f"attach {filename!r} ({size_bytes} bytes) to {model}({res_id})"
            token = self.app.prod_guard.create_pending(
                ctx.instance,
                ctx.op.value,
                model,
                summary=summary,
                payload_digest=compute_payload_digest(_token_payload(ctx.op.value, args)),
            )
            self._audit_ok(
                ctx,
                {
                    "res_id": res_id,
                    "filename": filename,
                    "size_bytes": size_bytes,
                    "mimetype": mimetype,
                },
                args,
                dry_run=True,
            )
            preview: dict[str, Any] = {
                "preview": True,
                "instance": ctx.instance,
                "res_model": model,
                "res_id": res_id,
                "filename": filename,
                "size_bytes": size_bytes,
                "mimetype": mimetype,
                "description": description,
                "confirmation_token": token,
                "note": _DRY_RUN_NOTE.format(tool="odoo_create_attachment"),
            }
            self._add_commits_remaining(preview, ctx, dry_run=True)
            return preview

        self._consume_token_on_prod(ctx, args)
        attachment_values: dict[str, Any] = {
            "name": filename,
            "datas": datas_base64,
            "res_model": model,
            "res_id": res_id,
        }
        if mimetype:
            attachment_values["mimetype"] = mimetype
        if description:
            attachment_values["description"] = description
        # ``ir.attachment`` is on MODEL_DENYLIST. The client.create
        # call here is the only path that creates an attachment — it
        # bypasses the dispatcher-level allowlist (which we don't run
        # for ``ir.attachment``) and writes directly through the
        # XML-RPC client. The bounded inputs and the prod-guard
        # pipeline above are the security envelope.
        attachment_id = rt.client.create("ir.attachment", attachment_values)
        self._audit_ok(
            ctx,
            {
                "res_id": res_id,
                "filename": filename,
                "size_bytes": size_bytes,
                "attachment_id": attachment_id,
            },
            args,
        )
        result: dict[str, Any] = {
            "instance": ctx.instance,
            "res_model": model,
            "res_id": res_id,
            "attachment_id": attachment_id,
            "filename": filename,
            "size_bytes": size_bytes,
            "committed": True,
        }
        self._add_commits_remaining(result, ctx)
        return result

    def _peek_states(
        self, rt: InstanceRuntime, model: str, record_ids: list[int]
    ) -> list[dict[str, Any]]:
        """Best-effort read of each record's ``state`` for a dry-run preview.

        Returns ``[{"id": .., "state": ..}, ...]``. Deliberately reads
        ONLY ``id`` + ``state`` — ``state`` is a selection field that is
        never sensitive, so this needs no redaction pass. If the model
        has no ``state`` field, or the read fails, returns an empty list
        rather than failing the whole preview.
        """
        try:
            fields_meta = self._fields_meta(rt, model)
            if "state" not in fields_meta:
                return []
            rows = rt.client.read(model, record_ids, ["state"])
            return [{"id": r.get("id"), "state": r.get("state")} for r in rows]
        except OdooMcpError:
            return []

    def _run_document_action(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run a document workflow action (confirm / cancel / post / validate).

        The ``(model, action)`` pair must resolve in the hardcoded
        ``security.document_actions`` map — the caller never supplies an
        Odoo method name. Goes through the standard prod-guard pipeline:
        unlock + dry-run preview + confirmation token + audit, identical
        to ``odoo_write`` / ``odoo_archive_or_delete``.
        """
        _refuse_if_read_only_session()
        ctx = self._begin("odoo_run_document_action", args, Operation.DOCUMENT_ACTION)
        assert ctx.model is not None
        rt, model = ctx.rt, ctx.model
        _refuse_write_blocklisted(model)
        record_ids = _require_list_of_int(args, "record_ids")
        action = _require_str(args, "action")
        # Raises OperationNotAllowedError if (model, action) is not mapped.
        method = resolve_document_action(model, action)

        cap = rt.config.max_records_hard_cap
        if len(record_ids) > cap:
            raise OdooMcpError(f"Cannot run an action on more than {cap} records at once.")

        self.app.prod_guard.check_write(ctx.instance, rt.config.production)
        dry_run = self.app.prod_guard.effective_dry_run(args.get("dry_run"), rt.config.production)

        if dry_run:
            states = self._peek_states(rt, model, record_ids)
            summary = f"{action} ({method}) on {len(record_ids)} {model} record(s)"
            token = self.app.prod_guard.create_pending(
                ctx.instance,
                ctx.op.value,
                model,
                summary=summary,
                payload_digest=compute_payload_digest(_token_payload(ctx.op.value, args)),
            )
            self._audit_ok(ctx, {"action": action, "id_count": len(record_ids)}, args, dry_run=True)
            preview: dict[str, Any] = {
                "preview": True,
                "instance": ctx.instance,
                "model": model,
                "action": action,
                "odoo_method": method,
                "record_ids": record_ids,
                "current_states": states,
                "confirmation_token": token,
                "note": _DRY_RUN_NOTE.format(tool="odoo_run_document_action"),
            }
            self._add_commits_remaining(preview, ctx, dry_run=True)
            return preview

        self._consume_token_on_prod(ctx, args)
        result = rt.client.call_document_action(model, method, record_ids)
        # Some Odoo methods (notably stock.picking.button_validate and
        # sale.order.action_cancel when there are linked pickings)
        # return a dict describing a follow-up wizard instead of
        # completing. For a SPECIFIC, audited set of these we drive
        # the wizard ourselves (see :data:`_WIZARD_COMPLETIONS`); for
        # everything else we surface the same "needs manual completion"
        # signal the agent's been seeing.
        wizard_spec = resolve_wizard_completion(model, action)
        wizard_completion: dict[str, Any] | None = None
        if isinstance(result, dict) and wizard_spec is not None:
            wizard_completion = self._complete_returned_wizard(rt, wizard_spec, record_ids)
            # If every record's wizard completed without itself
            # returning another wizard, the logical action succeeded.
            still_pending = any(
                step.get("wizard_returned_wizard") for step in wizard_completion["steps"]
            )
            needs_manual = still_pending
        else:
            needs_manual = isinstance(result, dict)

        audit_details: dict[str, Any] = {"action": action, "id_count": len(record_ids)}
        if wizard_completion is not None:
            audit_details["wizard_model"] = wizard_spec.wizard_model  # type: ignore[union-attr]
            audit_details["wizard_completed"] = not needs_manual
        self._audit_ok(ctx, audit_details, args)

        out: dict[str, Any] = {
            "instance": ctx.instance,
            "model": model,
            "action": action,
            "odoo_method": method,
            "record_ids": record_ids,
            "committed": not needs_manual,
        }
        if wizard_completion is not None:
            out["wizard"] = wizard_completion
        if needs_manual:
            out["needs_manual_completion"] = True
            out["note"] = (
                "Odoo returned a follow-up wizard (e.g. backorder or "
                "immediate-transfer confirmation) we don't auto-complete. "
                "The action did NOT fully complete — finish it in the Odoo UI."
            )
        self._add_commits_remaining(out, ctx)
        return out

    def _complete_returned_wizard(
        self,
        rt: InstanceRuntime,
        spec: WizardCompletion,
        record_ids: list[int],
    ) -> dict[str, Any]:
        """Drive a follow-up wizard returned by a document action.

        Per-record: create a ``spec.wizard_model`` row with
        ``{spec.origin_field: record_id}``, then call
        ``spec.wizard_method`` on the created wizard. Returns a
        structured summary so the audit log records what the wizard
        layer did, and so a chained wizard (the wizard returning
        another wizard — rare) doesn't get silently swallowed.

        Does NOT take a second prod-guard token. The operator's
        original dry-run review approved the *logical* action; the
        wizard step is the same logical operation, just split across
        two Odoo RPC calls because of how Odoo's UI is wired. Adding
        a second token here would force every cancel-with-linked-
        pickings to be approved twice and break the audit chain.
        """
        steps: list[dict[str, Any]] = []
        for rid in record_ids:
            wizard_id = rt.client.create(spec.wizard_model, {spec.origin_field: rid})
            try:
                wresult = rt.client.call_document_action(
                    spec.wizard_model, spec.wizard_method, [wizard_id]
                )
            except OdooMcpError as exc:
                steps.append(
                    {
                        "origin_id": rid,
                        "wizard_id": wizard_id,
                        "ok": False,
                        "error": str(exc),
                    }
                )
                continue
            steps.append(
                {
                    "origin_id": rid,
                    "wizard_id": wizard_id,
                    "ok": True,
                    # A chained wizard (returns another wizard dict)
                    # is unusual for the cancel pattern; expose it so
                    # the operator can see what happened.
                    "wizard_returned_wizard": isinstance(wresult, dict),
                }
            )
        return {
            "wizard_model": spec.wizard_model,
            "wizard_method": spec.wizard_method,
            "steps": steps,
        }

    def _add_commits_remaining(
        self, result: dict[str, Any], ctx: _Ctx, *, dry_run: bool = False
    ) -> None:
        """Expose the post-commit burst budget to the caller (prod only).

        Also called from dry-run paths so the agent can SEE the budget
        is unchanged across previews. Without this, an agent that
        dry-runs five things and then hits a burst-limit error has no
        evidence that dry-runs don't count, and defensively shrinks its
        batch size — losing exactly the throughput the burst budget was
        meant to allow.

        On dry-run paths we also stamp ``commits_remaining_note``
        making the no-cost guarantee explicit. The agent reads model
        output as text; a numeric field alone isn't enough to dislodge
        an incorrect prior.
        """
        if not ctx.rt.config.production:
            return
        remaining = self.app.prod_guard.commits_remaining(ctx.instance)
        if remaining is None:
            return
        result["commits_remaining"] = remaining
        if dry_run:
            result["commits_remaining_note"] = (
                "This is a dry-run; commits_remaining is unchanged. "
                "Only a successful commit decrements the burst budget."
            )

    def _consume_token_on_prod(self, ctx: _Ctx, args: dict[str, Any]) -> None:
        """On prod, a valid confirmation token from a prior dry run is required.

        The token's payload digest, captured at ``create_pending`` time, is
        re-checked here against the current call's payload. An agent that
        previewed ``ids=[1]`` and tries to commit the same token with
        ``ids=[1..1000]`` is rejected — see :func:`compute_payload_digest`.
        """
        if not ctx.rt.config.production:
            return
        token = args.get("confirmation_token")
        if not isinstance(token, str) or not token:
            raise ProdGuardError(
                "Commits against production require a confirmation_token from a prior dry run."
            )
        assert ctx.model is not None
        digest = compute_payload_digest(_token_payload(ctx.op.value, args))
        self.app.prod_guard.consume_pending(
            token,
            ctx.instance,
            ctx.op.value,
            ctx.model,
            payload_digest=digest,
        )

    def _diagnose_access(self, args: dict[str, Any]) -> dict[str, Any]:
        """Report access state for the authenticated user on one model.

        Two layers, both covered:

        * **MCP policy** — if the model is blocked by the denylist or a
          strict-mode allowlist, this tool *reports* that (with the reason
          and the config key to change) instead of failing with the same
          error the caller is trying to diagnose. No Odoo round-trip in
          that case.
        * **Odoo ACLs** — for permitted models, calls
          ``check_access_rights(op, raise_exception=False)`` for the four
          canonical operations. Pure introspection: no record reads.
        """
        ctx = self._begin(
            "odoo_diagnose_access", args, Operation.DIAGNOSE_ACCESS, require_model=False
        )
        rt = ctx.rt
        model = _require_str(args, "model")
        block_reason = classify_model_block(model, rt.config.allowed_models)
        if block_reason is not None:
            self._audit_ok(ctx, {"model": model, "mcp_blocked": True}, args)
            return {
                "instance": ctx.instance,
                "model": model,
                "mcp_blocked": True,
                "mcp_block_reason": block_reason,
                "note": (
                    "Blocked by the MCP's own model policy — Odoo ACLs were "
                    "not consulted. Strict-mode allowlists live under the "
                    "'allowed_models' key per instance in "
                    "~/.odoo-mcp/config.toml; built-in denylist entries "
                    "cannot be re-enabled."
                ),
            }
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
            "mcp_blocked": False,
            # Models like res.users / mail.message are readable but the MCP
            # refuses every write path regardless of Odoo ACLs — surface
            # that so a "can_write: true" from Odoo isn't misread.
            "write_blocked_via_mcp": model in MODEL_WRITE_BLOCKLIST,
            "uid": rt.client.uid,
            "login": rt.client.username,
            "is_admin": rt.client.is_admin,
            "admin_reason": rt.client.admin_reason,
            **rights,
        }

    def _diagnose_routing(self, args: dict[str, Any]) -> dict[str, Any]:
        """Report Odoo's procurement routing config for (product, warehouse).

        Read-only diagnostic tool. Walks the routes that COULD apply to
        *product_id* against *warehouse_id*, then lists every
        ``stock.rule`` matching one of those routes plus the warehouse.
        The caller sees which rule(s) Odoo could fire on confirm —
        and therefore which ``picking_type_id`` the resulting transfer
        gets.

        Honest non-prediction. Odoo's runtime rule resolution involves
        rule sequence, location-chain matching, MTO chains, custom
        overrides shipped by third-party modules, and (in 17+)
        per-route warehouse selectability. Re-implementing that
        client-side would drift on every Odoo release. The tool
        deliberately returns the CANDIDATE set + relevant flags, and
        lets the operator / agent identify the winner by inspection.

        Allowlist bypass. The six models this tool reads
        (``product.product``, ``product.template``, ``stock.warehouse``,
        ``stock.route``, ``stock.rule``, ``stock.location``) are
        operator-configuration models, never carry business data, and
        are hard-coded — the tool can't be asked to read anything
        else. Bypassing the per-instance ``allowed_models`` for these
        therefore doesn't widen the data-exposure surface; it just
        makes the tool work without each operator having to remember
        to allowlist six routing tables.
        """
        ctx = self._begin(
            "odoo_diagnose_routing",
            args,
            Operation.DIAGNOSE_ROUTING,
            require_model=False,
        )
        product_id = _require_int(args, "product_id")
        warehouse_id = _require_int(args, "warehouse_id")
        rt = ctx.rt

        # --- product + template + categ-derived routes -----------------
        product_rows = rt.client.search_read(
            "product.product",
            [("id", "=", product_id)],
            ["id", "name", "default_code", "product_tmpl_id", "route_ids", "categ_id"],
            limit=1,
            offset=0,
            order=None,
        )
        if not product_rows:
            raise OdooMcpError(f"product.product id={product_id} not found.")
        product = product_rows[0]
        tmpl_ref = product.get("product_tmpl_id")
        tmpl_id = tmpl_ref[0] if isinstance(tmpl_ref, list) and tmpl_ref else None

        template: dict[str, Any] | None = None
        if isinstance(tmpl_id, int):
            tmpl_rows = rt.client.search_read(
                "product.template",
                [("id", "=", tmpl_id)],
                ["id", "name", "route_ids", "categ_id"],
                limit=1,
                offset=0,
                order=None,
            )
            template = tmpl_rows[0] if tmpl_rows else None

        # --- warehouse -------------------------------------------------
        wh_rows = rt.client.search_read(
            "stock.warehouse",
            [("id", "=", warehouse_id)],
            [
                "id",
                "name",
                "code",
                "delivery_steps",
                "reception_steps",
                "sale_route_id",
                "purchase_route_id",
                "mto_pull_id",
                "lot_stock_id",
                "view_location_id",
            ],
            limit=1,
            offset=0,
            order=None,
        )
        if not wh_rows:
            raise OdooMcpError(f"stock.warehouse id={warehouse_id} not found.")
        warehouse = wh_rows[0]

        # --- collect candidate route ids -------------------------------
        candidate_route_ids: set[int] = set()
        for ref in product.get("route_ids") or []:
            if isinstance(ref, int):
                candidate_route_ids.add(ref)
        if template:
            for ref in template.get("route_ids") or []:
                if isinstance(ref, int):
                    candidate_route_ids.add(ref)
        # ``sale_route_id`` is a stock.route — include its id in the
        # candidate route set. ``mto_pull_id`` is a stock.rule (MTO
        # chain helper), not a route, so we surface it on the warehouse
        # block above but do not add it here.
        wh_sale_ref = warehouse.get("sale_route_id")
        if isinstance(wh_sale_ref, list) and wh_sale_ref:
            candidate_route_ids.add(wh_sale_ref[0])

        routes: list[dict[str, Any]] = []
        if candidate_route_ids:
            routes = rt.client.search_read(
                "stock.route",
                [("id", "in", list(candidate_route_ids))],
                [
                    "id",
                    "name",
                    "sequence",
                    "active",
                    "product_selectable",
                    "product_categ_selectable",
                    "warehouse_selectable",
                    "sale_selectable",
                    "warehouse_ids",
                ],
                limit=50,
                offset=0,
                order="sequence asc, id asc",
            )

        # --- rules on those routes + this warehouse --------------------
        rules: list[dict[str, Any]] = []
        if candidate_route_ids:
            rules = rt.client.search_read(
                "stock.rule",
                [
                    ("route_id", "in", list(candidate_route_ids)),
                    "|",
                    ("warehouse_id", "=", warehouse_id),
                    ("warehouse_id", "=", False),
                ],
                [
                    "id",
                    "name",
                    "sequence",
                    "active",
                    "route_id",
                    "action",
                    "location_src_id",
                    "location_dest_id",
                    "picking_type_id",
                    "procure_method",
                    "group_propagation_option",
                    "auto",
                    "warehouse_id",
                ],
                limit=200,
                offset=0,
                order="sequence asc, id asc",
            )

        self._audit_ok(
            ctx,
            {
                "product_id": product_id,
                "warehouse_id": warehouse_id,
                "candidate_route_count": len(routes),
                "candidate_rule_count": len(rules),
            },
            args,
        )
        return {
            "instance": ctx.instance,
            "product": product,
            "template": template,
            "warehouse": warehouse,
            "candidate_routes": routes,
            "candidate_rules": rules,
            "note": (
                "These are the candidates Odoo evaluates at procurement "
                "time. The winning rule depends on sequence, "
                "location-chain matching, MTO chains, and any custom "
                "overrides shipped by installed modules. This tool does "
                "NOT predict the winner — inspect candidate_rules to "
                "see which picking_type_id each would produce, and "
                "which would match your scheduled procurement."
            ),
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
    "odoo_diagnose_routing": Dispatcher._diagnose_routing,
    "odoo_send_message": Dispatcher._send_message,
    "odoo_run_document_action": Dispatcher._run_document_action,
    "odoo_create_attachment": Dispatcher._create_attachment,
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


# Hard cap on the decoded size of an attachment created via
# ``odoo_create_attachment``. The base64-encoded string is ~33% larger,
# so a 25 MB cap on decoded bytes corresponds to ~33 MB of wire payload.
# That's well under typical Odoo / nginx upload limits while staying
# comfortably above the size of any invoice, contract, or screenshot
# we've seen the agent want to attach. Hardcoded — config-overridable
# would let a misconfigured tenant accept multi-GB uploads that would
# stall the XML-RPC connection.
_ATTACHMENT_MAX_BYTES: int = 25 * 1024 * 1024


def _read_source_path_as_base64(raw_path: object, cfg: InstanceConfig) -> tuple[str, int]:
    """Read a server-local file and return its content as base64.

    The only way to attach payloads larger than what fits in the agent's
    tool-call window (some SDKs silently drop turns above ~5 KB of
    inline base64). The MCP runs as a stdio subprocess in the same
    filesystem namespace as the caller, so a path drop-off works
    where inlining doesn't. The trade-off is arbitrary-file-read, so:

    Security envelope:

    - ``attachment_source_paths`` must be configured non-empty for this
      instance. Default-deny: no config means no source_path. The opt-
      in lives in TOML so a runaway agent prompt can't widen it.
    - The given path must be a string AND absolute. Relative paths
      resolve against the MCP's CWD, which is operator-confusing and
      a footgun.
    - ``os.path.realpath`` resolves symlinks once. The resolved file
      must sit under one of the (also-realpath'd, at config-load time)
      allowlisted directories. ``os.path.commonpath`` containment with
      a trailing-separator guard catches the ``/allow/../etc`` and the
      ``/allowed_dir_evil`` (prefix-of) attacks.
    - The target must be a regular file (``S_ISREG``). Block devices,
      named pipes, and directories are refused — they're not what
      attachments are.
    - File size is checked via ``stat`` BEFORE reading. Over cap → no
      read at all. Eliminates the "open a 50 GB file just to die"
      footgun.

    Raises :class:`OdooMcpError` on any policy violation. The error
    text never echoes the contents of any forbidden file, only the
    requested path string (which the caller already had).
    """
    import stat as stat_mod

    if not isinstance(raw_path, str) or not raw_path:
        raise OdooMcpError("source_path must be a non-empty string.")
    if not cfg.attachment_source_paths:
        raise OdooMcpError(
            "source_path is not enabled on this instance. Add "
            "'attachment_source_paths = [\"/abs/path/to/dir\"]' to the "
            f"[instances.{cfg.name}] TOML section, listing the "
            "directories the MCP is allowed to read from."
        )
    if not os.path.isabs(raw_path):
        raise OdooMcpError(
            f"source_path must be an absolute path; got {raw_path!r}. "
            "Relative paths would resolve against the MCP process's CWD, "
            "which is operator-confusing and a security footgun."
        )

    resolved = os.path.realpath(raw_path)
    # Trailing-separator guard prevents the ``/allowed_dir`` vs
    # ``/allowed_dir_evil`` prefix confusion.
    allowed_resolved: list[str] = []
    for allowed in cfg.attachment_source_paths:
        # Already realpath'd at config-load, but normalise the trailing
        # separator here so the containment check is exact.
        allowed_resolved.append(allowed.rstrip(os.sep))
    contained = False
    for allowed in allowed_resolved:
        if resolved == allowed:
            contained = True
            break
        if resolved.startswith(allowed + os.sep):
            contained = True
            break
    if not contained:
        raise OdooMcpError(
            f"source_path {raw_path!r} resolves to {resolved!r}, which is not "
            f"inside any of the attachment_source_paths configured for "
            f"instance {cfg.name!r}. Add the directory to the TOML allowlist "
            f"if you intended this, or move the file to an allowed location."
        )

    try:
        st = os.stat(resolved)
    except FileNotFoundError as exc:
        raise OdooMcpError(f"source_path {raw_path!r} does not exist.") from exc
    except OSError as exc:
        raise OdooMcpError(f"source_path {raw_path!r}: cannot stat: {exc}") from exc

    if not stat_mod.S_ISREG(st.st_mode):
        raise OdooMcpError(
            f"source_path {raw_path!r} is not a regular file. Devices, FIFOs, "
            f"sockets, and directories are not supported as attachments."
        )
    if st.st_size > _ATTACHMENT_MAX_BYTES:
        raise OdooMcpError(
            f"source_path {raw_path!r} is {st.st_size} bytes, over the "
            f"{_ATTACHMENT_MAX_BYTES}-byte cap. Split the file or compress "
            f"it before attaching."
        )

    try:
        with open(resolved, "rb") as fh:  # noqa: PTH123 — path already validated above
            # Memory-bounded: read at most MAX+1 bytes regardless of
            # what stat reported. If the file grew between stat and
            # open (TOCTOU), we never allocate more than 25 MB + 1.
            # The +1 trick is how we detect "still more to read" without
            # allocating a second buffer.
            content = fh.read(_ATTACHMENT_MAX_BYTES + 1)
    except OSError as exc:
        raise OdooMcpError(f"source_path {raw_path!r}: cannot read: {exc}") from exc

    if len(content) > _ATTACHMENT_MAX_BYTES:
        raise OdooMcpError(
            f"source_path {raw_path!r} grew between stat ({st.st_size} bytes) "
            f"and read ({len(content)} bytes, capped) — refusing to commit the "
            f"read. The file was likely being written concurrently."
        )
    return base64.b64encode(content).decode("ascii"), len(content)


def _b64decode_or_raise(encoded: str) -> bytes:
    """Decode a base64 string, surfacing a clean :class:`OdooMcpError` on bad input.

    Tolerates the data-URL prefix that some agents emit
    (``data:application/pdf;base64,JVBERi…``) by stripping everything
    up to and including the comma. Stops at the first invalid character
    rather than silently truncating — same fail-closed posture as the
    rest of the security layer.
    """
    if not isinstance(encoded, str) or not encoded:
        raise OdooMcpError("datas_base64 must be a non-empty base64 string.")
    payload = encoded.strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OdooMcpError(
            f"datas_base64 is not valid base64: {exc}. The string must be "
            "the standard base64 alphabet, no whitespace, no URL-safe "
            "substitutions, and padded with '='."
        ) from exc


# Per-operation list of arg keys whose values determine what gets written.
# The confirmation token's payload digest is computed over exactly these
# keys (with values taken straight from ``args``). A commit re-call with
# any of these keys changed — extra ids, swapped values, a different mode
# or action, an added partner — produces a different digest and the
# token is rejected. Operations not listed here have no payload binding
# because they have no write payload to bind (the model and op are
# already covered by the (instance, op, model) tuple).
_TOKEN_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    Operation.CREATE.value: ("values",),
    Operation.WRITE.value: ("ids", "values"),
    Operation.ARCHIVE.value: ("ids", "mode"),
    Operation.UNLINK.value: ("ids", "mode"),
    Operation.SEND_MESSAGE.value: (
        "record_id",
        "body",
        "subject",
        "partner_ids",
        "message_type",
    ),
    Operation.DOCUMENT_ACTION.value: ("record_ids", "action"),
    # ``datas_base64`` is included so the digest binds to the EXACT file
    # bytes that were previewed. An agent that dry-runs a 200-byte
    # placeholder and tries to commit a 20 MB invoice with the same
    # token fails the digest check. ``mimetype`` and ``description``
    # don't change the bytes but DO change what an operator sees in the
    # preview, so they bind too.
    Operation.CREATE_ATTACHMENT.value: (
        "res_model",
        "res_id",
        "filename",
        "datas_base64",
        "mimetype",
        "description",
    ),
}


def _token_payload(op_value: str, args: dict[str, Any]) -> dict[str, Any]:
    """Project ``args`` to the subset of keys whose values are payload-bound.

    Used by both the preview path (when issuing a confirmation token) and
    the commit path (when consuming one). Both sides MUST go through this
    function so the digest can't drift; adding a write parameter without
    binding it here would silently widen the attack surface the digest is
    supposed to close.
    """
    keys = _TOKEN_PAYLOAD_KEYS.get(op_value, ())
    return {k: args.get(k) for k in keys}


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
    {
        "name": "odoo_diagnose_access",
        "purpose": "Why is a model blocked? MCP policy + Odoo ACL rights.",
    },
    {
        "name": "odoo_diagnose_routing",
        "purpose": "Stock rules + picking types for a (product, warehouse) pair.",
    },
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
                ["probability", "<", 100],
            ],
        },
    },
    {
        "goal": "Filter on a related record's value (dotted domains are rejected)",
        "use": "odoo_lookup then odoo_search_read",
        "example": (
            "Want leads in stage 'Won'? Domains like ['stage_id.name', '=', 'Won'] "
            "are rejected by the sandbox. Two calls: "
            "1) odoo_lookup(model='crm.stage', query='Won') -> ids. "
            "2) odoo_search_read(model='crm.lead', domain=[['stage_id', 'in', [<ids>]]]). "
            "Same pattern resolves the other direction: a many2one value in a "
            "result (e.g. partner_id: [42, 'Acme']) reads in ONE batched call — "
            "odoo_read(model='res.partner', ids=[42, ...]) — never one call per id."
        ),
    },
    {
        "goal": "Explain an unexpected transfer/picking type after confirming an order",
        "use": "odoo_diagnose_routing",
        "example": (
            "An SO confirmed into the wrong operation type (two-step trailer "
            "flow instead of direct delivery)? The decision lives in routing "
            "config, not on the order. 1) odoo_diagnose_routing(instance='prod', "
            "product_id=<product.product id>, warehouse_id=<stock.warehouse id>) "
            "— lists the warehouse's delivery_steps + every candidate stock.rule "
            "with its picking_type_id. 2) Check overrides in precedence order: "
            "route_id on the sale.order.line, route_ids on the product, then the "
            "warehouse delivery route. Default reads on these models include the "
            "routing fields."
        ),
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
    "Two calls instead: resolve ids on the related model first, then filter "
    "with ('relation_field', 'in', [ids]). See common_patterns.",
    "Sensitive fields (vat, ssnid, bank_ids, private_email, ...) require "
    "allow_sensitive_fields=['NAME', ...] per-call.",
    "Password/api_key/token fields are ALWAYS redacted. Opting in does not unlock them.",
    "Model access: each instance is either in 'open' mode (any model allowed "
    "except a hardcoded denylist of ~25 auth / ACL / code / config models "
    "like ir.config_parameter, ir.actions.server, mail.template, "
    "ir.attachment) or 'strict' mode (enumerated allowlist). Check "
    "odoo_list_instances for the mode per instance; odoo_diagnose_access "
    "reports why a specific model is blocked.",
    "res.users is readable for resolving user_id values (name, login, email "
    "and a few identity fields only) but never writable through the MCP.",
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


def _require_int(args: dict[str, Any], key: str) -> int:
    """Strict integer arg, no default. Rejects booleans (subclass of int)."""
    value = args.get(key)
    if value is None:
        raise OdooMcpError(f"Argument {key!r} is required and must be an integer.")
    if isinstance(value, bool) or not isinstance(value, int):
        raise OdooMcpError(f"Argument {key!r} must be an integer.")
    ivalue: int = value
    return ivalue


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
