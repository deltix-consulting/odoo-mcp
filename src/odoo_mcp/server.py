"""MCP server: tool definitions and the security dispatcher.

Every tool goes through the same pipeline in the same order. The steps are
deliberately implemented as small functions in :mod:`odoo_mcp.security` so
they can be unit-tested in isolation; this module just wires them up.

Pipeline::

    [tool call]
    → resolve_instance            (config lookup)
    → rate_limit                  (token bucket per instance)
    → model_allowlist             (per-instance frozenset)
    → op_allowlist                (closed enum)
    → prod_guard                  (write gate + dry-run default + confirmation tokens)
    → sandbox_domain              (for search_read)
    → validate_fields             (for read/search_read: explicit field list + redaction policy)
    → validate_values             (for create/write: no protected fields, no wildcard keys)
    → cap_limit                   (clamp record limit to the instance cap)
    → call_odoo                   (the only place that touches XML-RPC)
    → redact_response             (drop protected/default-hidden fields, replace binaries)
    → audit_success               (one JSONL line, no field values)
    → return
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .audit import AuditEvent, AuditLog
from .client import OdooClient
from .config import AppConfig, InstanceConfig, load_config
from .credentials import Credentials, load_credentials
from .errors import (
    InstanceNotFoundError,
    OdooMcpError,
    ProdGuardError,
)
from .security.allowlist import Operation, check_model, check_operation
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

# ---------------------------------------------------------------------------
# Shared application state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InstanceRuntime:
    """Everything the dispatcher needs for one configured instance."""

    config: InstanceConfig
    credentials: Credentials
    client: OdooClient


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


def build_app(config_path: Any = None) -> OdooMcpApp:
    """Load config, credentials, audit log, and per-instance clients.

    This is the single startup function. Any failure here (config perms,
    missing env var, audit log unwritable, Odoo auth failure) raises an
    :class:`OdooMcpError` subclass and the process refuses to run.
    """
    cfg = load_config(config_path)
    audit = AuditLog(cfg.audit_log_path)
    prod_guard = ProdGuard()
    rate_limiter = RateLimiter()

    instances: dict[str, InstanceRuntime] = {}
    for name, inst_cfg in cfg.instances.items():
        creds = load_credentials(name, inst_cfg.credentials_env_prefix)
        client = OdooClient(inst_cfg, creds)
        client.authenticate()
        rate_limiter.configure(name, inst_cfg.rate_limit_per_minute)
        instances[name] = InstanceRuntime(config=inst_cfg, credentials=creds, client=client)

    return OdooMcpApp(
        config=cfg,
        audit=audit,
        prod_guard=prod_guard,
        rate_limiter=rate_limiter,
        instances=instances,
    )


# ---------------------------------------------------------------------------
# Tool schemas
#
# We hand-write JSON Schema for each tool because the ergonomics for
# Claude are much better when descriptions explain the security model
# rather than just the mechanical argument types.
# ---------------------------------------------------------------------------


def _build_tools() -> list[Tool]:
    return [
        Tool(
            name="odoo_list_instances",
            description=(
                "List the configured Odoo instances the MCP can talk to. Returns for each "
                "instance: name, production flag, whether prod writes are currently unlocked, "
                "and the allowed-models set. Safe, read-only."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="odoo_describe_model",
            description=(
                "Return the field metadata (fields_get) for one allowlisted Odoo model. "
                "Permanently-redacted fields (passwords, tokens) are omitted entirely; "
                "default-hidden sensitive fields (VAT, IBAN, employee PII) are marked "
                "with `_sensitive: true` so you know they require explicit unlock."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance", "model"],
                "additionalProperties": False,
                "properties": {
                    "instance": {"type": "string", "description": "Configured instance name."},
                    "model": {"type": "string", "description": "Odoo model name (e.g. 'res.partner')."},
                },
            },
        ),
        Tool(
            name="odoo_search_read",
            description=(
                "Run Odoo search_read against an allowlisted model. You MUST pass an explicit "
                "`fields` list — no wildcard reads. Domain filters are sandboxed: dotted field "
                "traversal (e.g. 'create_uid.login') is rejected. Results have default-hidden "
                "fields stripped unless you pass `allow_sensitive_fields`. Binary fields are "
                "replaced with a size placeholder unless you pass `include_binary=true`."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance", "model", "fields"],
                "additionalProperties": False,
                "properties": {
                    "instance": {"type": "string"},
                    "model": {"type": "string"},
                    "domain": {
                        "type": "array",
                        "description": (
                            "Odoo domain: list of (field, op, value) tuples plus optional "
                            "'&'/'|'/'!' prefix operators. Dotted fields are rejected."
                        ),
                        "default": [],
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Explicit list of fields to return. Required.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Max records to return. Defaults to the instance default, clamped to the hard cap.",
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "order": {"type": "string", "description": "Odoo order string, e.g. 'name asc'."},
                    "allow_sensitive_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Per-call opt-in to default-hidden sensitive fields.",
                        "default": [],
                    },
                    "include_binary": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="odoo_search_count",
            description=(
                "Count records matching a domain on an allowlisted model. Returns a "
                "single integer — much cheaper than fetching records just to count them. "
                "Same domain sandbox as odoo_search_read: dotted fields and unknown "
                "operators are rejected. Read-only."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance", "model"],
                "additionalProperties": False,
                "properties": {
                    "instance": {"type": "string"},
                    "model": {"type": "string"},
                    "domain": {
                        "type": "array",
                        "description": "Odoo domain; same rules as odoo_search_read.",
                        "default": [],
                    },
                },
            },
        ),
        Tool(
            name="odoo_read_group",
            description=(
                "Aggregate records via Odoo's read_group. Use this for dashboards and "
                "summaries instead of fetching records to count / sum them yourself. "
                "Aggregate `fields` syntax: 'field' (default agg) or 'field:AGG' where "
                "AGG is sum|avg|count|count_distinct|max|min. `groupby` syntax: 'field' "
                "or 'date_field:GRAN' where GRAN is day|week|month|quarter|year|hour. "
                "At most 4 groupby dimensions. Sensitive fields require "
                "allow_sensitive_fields opt-in; always-redacted fields are blocked."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance", "model", "fields", "groupby"],
                "additionalProperties": False,
                "properties": {
                    "instance": {"type": "string"},
                    "model": {"type": "string"},
                    "domain": {"type": "array", "default": []},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Aggregate specs, e.g. ['amount_total:sum', 'id:count'].",
                    },
                    "groupby": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 4,
                        "description": "Dimensions, e.g. ['stage_id'] or ['date_order:month', 'user_id'].",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Max groups to return. Clamped to the instance hard cap.",
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "orderby": {
                        "type": "string",
                        "description": "Order string, e.g. 'amount_total desc'.",
                    },
                    "lazy": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Odoo lazy flag: if true, only the first groupby dimension is "
                            "aggregated and child groups are returned as drill-down specs. "
                            "Set to false to compute all dimensions eagerly."
                        ),
                    },
                    "allow_sensitive_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
            },
        ),
        Tool(
            name="odoo_read",
            description=(
                "Fetch specific records by ID from an allowlisted model. Same field-level "
                "policies as odoo_search_read: explicit fields required, sensitive fields "
                "gated, binary fields stripped by default."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance", "model", "ids", "fields"],
                "additionalProperties": False,
                "properties": {
                    "instance": {"type": "string"},
                    "model": {"type": "string"},
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "description": "Record IDs to read.",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "allow_sensitive_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "include_binary": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="odoo_create",
            description=(
                "Create a record in an allowlisted model. Writes to production instances "
                "are blocked unless odoo_enable_prod_writes has been called first, AND the "
                "call defaults to dry_run=true on prod so you MUST pass dry_run=false AND a "
                "confirmation_token from a prior dry-run call to actually commit. Protected "
                "fields (passwords, tokens) cannot be written."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance", "model", "values"],
                "additionalProperties": False,
                "properties": {
                    "instance": {"type": "string"},
                    "model": {"type": "string"},
                    "values": {
                        "type": "object",
                        "description": "Field → value mapping. Field names must be known on the model.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview only — validates and returns what would happen. Default: true on prod, false on dev.",
                    },
                    "confirmation_token": {
                        "type": "string",
                        "description": "Token from a prior dry-run call, required to commit on prod.",
                    },
                },
            },
        ),
        Tool(
            name="odoo_write",
            description=(
                "Update existing records on an allowlisted model. Same prod guardrails as "
                "odoo_create: blocked on prod without unlock, dry_run default on prod, "
                "confirmation_token required for real commit."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance", "model", "ids", "values"],
                "additionalProperties": False,
                "properties": {
                    "instance": {"type": "string"},
                    "model": {"type": "string"},
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                    },
                    "values": {"type": "object"},
                    "dry_run": {"type": "boolean"},
                    "confirmation_token": {"type": "string"},
                },
            },
        ),
        Tool(
            name="odoo_enable_prod_writes",
            description=(
                "Unlock writes to a production instance for the next 15 minutes. Every "
                "subsequent write still defaults to dry_run=true on prod and requires a "
                "confirmation_token to commit. This tool is the explicit step that moves "
                "the session from 'prod is read-only' to 'prod writes allowed'."
            ),
            inputSchema={
                "type": "object",
                "required": ["instance"],
                "additionalProperties": False,
                "properties": {"instance": {"type": "string"}},
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    """Wires the MCP tool names to typed handlers.

    Each handler returns a plain JSON-serializable dict; the MCP layer wraps
    it in a :class:`TextContent` with compact JSON.
    """

    def __init__(self, app: OdooMcpApp) -> None:
        self.app = app

    # ---- Entry point ------------------------------------------------------

    async def call(self, name: str, arguments: dict[str, Any]) -> list[TextContent]:
        started = time.monotonic()
        try:
            result = self._dispatch(name, arguments)
        except OdooMcpError as exc:
            duration = int((time.monotonic() - started) * 1000)
            self._audit_failure(name, arguments, exc, duration)
            payload = {
                "ok": False,
                "error_code": exc.code,
                "error": exc.user_message,
            }
            return [TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))]
        except Exception as exc:  # noqa: BLE001 — last-resort safety net
            duration = int((time.monotonic() - started) * 1000)
            # Wrap in our error so redaction applies.
            wrapped = OdooMcpError(f"Unhandled error in {name}: {type(exc).__name__}: {exc}")
            self._audit_failure(name, arguments, wrapped, duration)
            payload = {
                "ok": False,
                "error_code": "internal_error",
                "error": wrapped.user_message,
            }
            return [TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))]

        payload = {"ok": True, **result}
        return [TextContent(type="text", text=json.dumps(payload, separators=(",", ":"), default=str))]

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "odoo_list_instances":
            return self._list_instances()
        if name == "odoo_describe_model":
            return self._describe_model(arguments)
        if name == "odoo_search_read":
            return self._search_read(arguments)
        if name == "odoo_search_count":
            return self._search_count(arguments)
        if name == "odoo_read_group":
            return self._read_group(arguments)
        if name == "odoo_read":
            return self._read(arguments)
        if name == "odoo_create":
            return self._create(arguments)
        if name == "odoo_write":
            return self._write(arguments)
        if name == "odoo_enable_prod_writes":
            return self._enable_prod_writes(arguments)
        raise OdooMcpError(f"Unknown tool: {name!r}")

    # ---- Handlers ---------------------------------------------------------

    def _list_instances(self) -> dict[str, Any]:
        out = []
        for name, rt in self.app.instances.items():
            out.append(
                {
                    "name": name,
                    "url": rt.config.url,
                    "database": rt.config.database,
                    "production": rt.config.production,
                    "writes_unlocked": self.app.prod_guard.is_unlocked(name),
                    "allowed_models": sorted(rt.config.allowed_models),
                    "max_records_default": rt.config.max_records_default,
                    "max_records_hard_cap": rt.config.max_records_hard_cap,
                    "rate_limit_per_minute": rt.config.rate_limit_per_minute,
                }
            )
        self._audit_success(
            "odoo_list_instances", Operation.FIELDS_GET, None, None, 0, False, {}
        )
        return {"instances": out}

    def _describe_model(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        model = _require_str(args, "model")
        rt = self.app.instance(instance_name)
        started = time.monotonic()
        self.app.rate_limiter.take(instance_name)
        check_model(model, rt.config.allowed_models)
        fields_get = rt.client.fields_get(model)
        # Keep only type/string/required/help so the response is compact.
        filtered = {
            fname: {k: v for k, v in meta.items() if k in {"type", "string", "required", "readonly", "help", "relation", "_sensitive", "_note"}}
            for fname, meta in redact_fields_get(model, fields_get).items()
        }
        duration = int((time.monotonic() - started) * 1000)
        self._audit_success(
            "odoo_describe_model",
            Operation.FIELDS_GET,
            instance_name,
            model,
            duration,
            False,
            {"field_count": len(filtered)},
        )
        return {"model": model, "fields": filtered}

    def _search_read(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        model = _require_str(args, "model")
        fields = _require_list_of_str(args, "fields")
        domain = args.get("domain") or []
        limit = args.get("limit")
        offset = int(args.get("offset") or 0)
        if offset < 0:
            raise OdooMcpError("offset must be >= 0")
        order = args.get("order")
        if order is not None and not isinstance(order, str):
            raise OdooMcpError("order must be a string")
        allow_sensitive = frozenset(args.get("allow_sensitive_fields") or [])
        include_binary = bool(args.get("include_binary") or False)

        rt = self.app.instance(instance_name)
        started = time.monotonic()
        self.app.rate_limiter.take(instance_name)
        op = check_operation(Operation.SEARCH_READ)
        check_model(model, rt.config.allowed_models)

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        validated_fields = validate_requested_fields(
            model, fields, known, allow_sensitive=allow_sensitive
        )
        validated_domain = sandbox_domain(domain, known)
        effective_limit = clamp_limit(
            limit, rt.config.max_records_default, rt.config.max_records_hard_cap
        )

        records = rt.client.search_read(
            model, validated_domain, validated_fields, effective_limit, offset, order
        )
        field_types = {n: meta.get("type", "") for n, meta in fields_meta.items()}
        redacted = redact_response(
            model,
            records,
            field_types,
            allow_sensitive=allow_sensitive,
            include_binary=include_binary,
        )

        duration = int((time.monotonic() - started) * 1000)
        self._audit_success(
            "odoo_search_read",
            op,
            instance_name,
            model,
            duration,
            False,
            {
                "record_count": len(redacted),
                "limit": effective_limit,
                "offset": offset,
                "field_count": len(validated_fields),
                "domain_leaves": sum(1 for e in validated_domain if not isinstance(e, str)),
            },
        )
        return {
            "instance": instance_name,
            "model": model,
            "records": redacted,
            "count": len(redacted),
        }

    def _search_count(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        model = _require_str(args, "model")
        domain = args.get("domain") or []

        rt = self.app.instance(instance_name)
        started = time.monotonic()
        self.app.rate_limiter.take(instance_name)
        op = check_operation(Operation.SEARCH_COUNT)
        check_model(model, rt.config.allowed_models)

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        validated_domain = sandbox_domain(domain, known)

        count = rt.client.search_count(model, validated_domain)
        duration = int((time.monotonic() - started) * 1000)
        self._audit_success(
            "odoo_search_count",
            op,
            instance_name,
            model,
            duration,
            False,
            {
                "record_count": count,
                "domain_leaves": sum(
                    1 for e in validated_domain if not isinstance(e, str)
                ),
            },
        )
        return {"instance": instance_name, "model": model, "count": count}

    def _read_group(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        model = _require_str(args, "model")
        fields = _require_list_of_str(args, "fields")
        groupby = _require_list_of_str(args, "groupby")
        domain = args.get("domain") or []
        limit = args.get("limit")
        offset = int(args.get("offset") or 0)
        if offset < 0:
            raise OdooMcpError("offset must be >= 0")
        orderby = args.get("orderby")
        if orderby is not None and not isinstance(orderby, str):
            raise OdooMcpError("orderby must be a string")
        lazy = bool(args.get("lazy", True))
        allow_sensitive = frozenset(args.get("allow_sensitive_fields") or [])

        rt = self.app.instance(instance_name)
        started = time.monotonic()
        self.app.rate_limiter.take(instance_name)
        op = check_operation(Operation.READ_GROUP)
        check_model(model, rt.config.allowed_models)

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        validated_fields = validate_aggregate_fields(
            model, fields, known, allow_sensitive=allow_sensitive
        )
        validated_groupby = validate_groupby(
            model, groupby, known, allow_sensitive=allow_sensitive
        )
        validated_domain = sandbox_domain(domain, known)
        # Clamp group count to the hard cap regardless of caller input.
        effective_limit = clamp_limit(
            limit, rt.config.max_records_hard_cap, rt.config.max_records_hard_cap
        )

        rows = rt.client.read_group(
            model,
            validated_domain,
            validated_fields,
            validated_groupby,
            limit=effective_limit,
            offset=offset,
            orderby=orderby,
            lazy=lazy,
        )

        duration = int((time.monotonic() - started) * 1000)
        self._audit_success(
            "odoo_read_group",
            op,
            instance_name,
            model,
            duration,
            False,
            {
                "record_count": len(rows),
                "limit": effective_limit,
                "offset": offset,
                "field_count": len(validated_fields),
                "groupby_count": len(validated_groupby),
                "domain_leaves": sum(
                    1 for e in validated_domain if not isinstance(e, str)
                ),
                "lazy": lazy,
            },
        )
        return {
            "instance": instance_name,
            "model": model,
            "groups": rows,
            "count": len(rows),
        }

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        model = _require_str(args, "model")
        ids = _require_list_of_int(args, "ids")
        fields = _require_list_of_str(args, "fields")
        allow_sensitive = frozenset(args.get("allow_sensitive_fields") or [])
        include_binary = bool(args.get("include_binary") or False)

        rt = self.app.instance(instance_name)
        started = time.monotonic()
        self.app.rate_limiter.take(instance_name)
        op = check_operation(Operation.READ)
        check_model(model, rt.config.allowed_models)
        if len(ids) > rt.config.max_records_hard_cap:
            raise OdooMcpError(
                f"Cannot read more than {rt.config.max_records_hard_cap} ids at once."
            )

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        validated_fields = validate_requested_fields(
            model, fields, known, allow_sensitive=allow_sensitive
        )
        records = rt.client.read(model, ids, validated_fields)
        field_types = {n: meta.get("type", "") for n, meta in fields_meta.items()}
        redacted = redact_response(
            model,
            records,
            field_types,
            allow_sensitive=allow_sensitive,
            include_binary=include_binary,
        )

        duration = int((time.monotonic() - started) * 1000)
        self._audit_success(
            "odoo_read",
            op,
            instance_name,
            model,
            duration,
            False,
            {"record_count": len(redacted), "field_count": len(validated_fields), "id_count": len(ids)},
        )
        return {
            "instance": instance_name,
            "model": model,
            "records": redacted,
            "count": len(redacted),
        }

    def _create(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        model = _require_str(args, "model")
        values = args.get("values")
        if not isinstance(values, dict):
            raise OdooMcpError("values must be an object/dict")
        requested_dry_run = args.get("dry_run")
        confirmation_token = args.get("confirmation_token")

        rt = self.app.instance(instance_name)
        started = time.monotonic()
        self.app.rate_limiter.take(instance_name)
        op = check_operation(Operation.CREATE)
        check_model(model, rt.config.allowed_models)
        self.app.prod_guard.check_write(instance_name, rt.config.production)

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        validated_values = validate_write_values(model, values, known)

        dry_run = self.app.prod_guard.effective_dry_run(requested_dry_run, rt.config.production)

        if dry_run:
            # Return a preview + a confirmation token. No XML-RPC commit.
            token = self.app.prod_guard.create_pending(
                instance_name,
                op.value,
                model,
                summary=f"create {model} (+{len(validated_values)} fields)",
            )
            duration = int((time.monotonic() - started) * 1000)
            self._audit_success(
                "odoo_create",
                op,
                instance_name,
                model,
                duration,
                True,
                {"field_count": len(validated_values)},
            )
            return {
                "preview": True,
                "instance": instance_name,
                "model": model,
                "would_write_fields": sorted(validated_values.keys()),
                "confirmation_token": token,
                "note": (
                    "This was a dry run. To commit, call odoo_create again with "
                    "dry_run=false and confirmation_token set to the token above."
                ),
            }

        # Non-dry-run path: on prod, we require a confirmation token that was
        # issued by a prior dry run of the exact same (instance, op, model).
        if rt.config.production:
            if not isinstance(confirmation_token, str) or not confirmation_token:
                raise ProdGuardError(
                    "Commits against production require a confirmation_token from a prior dry run."
                )
            self.app.prod_guard.consume_pending(
                confirmation_token, instance_name, op.value, model
            )

        new_id = rt.client.create(model, validated_values)
        duration = int((time.monotonic() - started) * 1000)
        self._audit_success(
            "odoo_create",
            op,
            instance_name,
            model,
            duration,
            False,
            {"field_count": len(validated_values), "new_id": new_id},
        )
        return {
            "instance": instance_name,
            "model": model,
            "id": new_id,
            "committed": True,
        }

    def _write(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        model = _require_str(args, "model")
        ids = _require_list_of_int(args, "ids")
        values = args.get("values")
        if not isinstance(values, dict):
            raise OdooMcpError("values must be an object/dict")
        requested_dry_run = args.get("dry_run")
        confirmation_token = args.get("confirmation_token")

        rt = self.app.instance(instance_name)
        started = time.monotonic()
        self.app.rate_limiter.take(instance_name)
        op = check_operation(Operation.WRITE)
        check_model(model, rt.config.allowed_models)
        self.app.prod_guard.check_write(instance_name, rt.config.production)

        if len(ids) > rt.config.max_records_hard_cap:
            raise OdooMcpError(
                f"Cannot write to more than {rt.config.max_records_hard_cap} ids at once."
            )

        fields_meta = rt.client.fields_get(model)
        known = frozenset(fields_meta.keys())
        validated_values = validate_write_values(model, values, known)

        dry_run = self.app.prod_guard.effective_dry_run(requested_dry_run, rt.config.production)

        if dry_run:
            token = self.app.prod_guard.create_pending(
                instance_name,
                op.value,
                model,
                summary=f"write {model} ids={ids[:5]}{'...' if len(ids) > 5 else ''} (+{len(validated_values)} fields)",
            )
            duration = int((time.monotonic() - started) * 1000)
            self._audit_success(
                "odoo_write",
                op,
                instance_name,
                model,
                duration,
                True,
                {"field_count": len(validated_values), "id_count": len(ids)},
            )
            return {
                "preview": True,
                "instance": instance_name,
                "model": model,
                "id_count": len(ids),
                "would_update_fields": sorted(validated_values.keys()),
                "confirmation_token": token,
                "note": (
                    "This was a dry run. To commit, call odoo_write again with "
                    "dry_run=false and confirmation_token set to the token above."
                ),
            }

        if rt.config.production:
            if not isinstance(confirmation_token, str) or not confirmation_token:
                raise ProdGuardError(
                    "Commits against production require a confirmation_token from a prior dry run."
                )
            self.app.prod_guard.consume_pending(
                confirmation_token, instance_name, op.value, model
            )

        ok = rt.client.write(model, ids, validated_values)
        duration = int((time.monotonic() - started) * 1000)
        self._audit_success(
            "odoo_write",
            op,
            instance_name,
            model,
            duration,
            False,
            {"id_count": len(ids), "field_count": len(validated_values)},
        )
        return {
            "instance": instance_name,
            "model": model,
            "ids": ids,
            "committed": ok,
        }

    def _enable_prod_writes(self, args: dict[str, Any]) -> dict[str, Any]:
        instance_name = _require_str(args, "instance")
        rt = self.app.instance(instance_name)
        expiry = self.app.prod_guard.unlock(instance_name, rt.config.production)
        self._audit_success(
            "odoo_enable_prod_writes",
            Operation.WRITE,
            instance_name,
            None,
            0,
            False,
            {"event": "WRITE_UNLOCK"},
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

    # ---- Audit helpers ----------------------------------------------------

    def _audit_success(
        self,
        tool: str,
        op: Operation,
        instance: str | None,
        model: str | None,
        duration_ms: int,
        dry_run: bool,
        details: dict[str, Any],
    ) -> None:
        self.app.audit.log(
            AuditEvent(
                instance=instance or "-",
                tool=tool,
                op=op.value,
                model=model,
                result="ok",
                record_count=details.get("record_count") if isinstance(details.get("record_count"), int) else None,
                duration_ms=duration_ms,
                dry_run=dry_run,
                details={k: v for k, v in details.items() if isinstance(v, (str, int, bool)) or v is None},
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
        # Audit is fail-closed at the dispatcher level (call() will surface
        # the underlying error to the caller); don't double-raise here.
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
                    details={"error": error.user_message[:500]},
                )
            )


# ---------------------------------------------------------------------------
# MCP wiring + run
# ---------------------------------------------------------------------------


def build_server(app: OdooMcpApp) -> Server:
    server: Server = Server("odoo-mcp")
    dispatcher = Dispatcher(app)
    tools = _build_tools()

    # The mcp SDK's decorators aren't typed; mypy --strict flags the
    # resulting wrapped function. We accept that at this single boundary
    # point.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[Tool]:
        return tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        return await dispatcher.call(name, arguments or {})

    return server


async def run() -> None:
    """Entry point for ``python -m odoo_mcp`` (server mode)."""
    app = build_app()
    server = build_server(app)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# ---------------------------------------------------------------------------
# Argument coercion helpers
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
