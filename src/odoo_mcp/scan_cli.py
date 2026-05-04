"""``odoo-mcp scan-custom INSTANCE`` — discover klant-custom models and fields.

Connects to a configured Odoo instance, enumerates every model and field,
diffs against the embedded Odoo Community standard reference
(:mod:`odoo_mcp._odoo_reference`), classifies each non-standard field on
sensitivity (:mod:`odoo_mcp._scan_heuristics`), and prints a report.

This command **deliberately bypasses the dispatcher denylist**. The denylist
exists to constrain the Claude-facing tool surface; this is admin tooling
operated by the consultant on their own machine, not a Claude tool call. We
go straight through :meth:`OdooClient._execute` against ``ir.model`` and
``ir.model.fields``.

The scan only inspects schema. It never reads record contents.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ._odoo_reference import (
    ODOO_REFERENCE_VERSION,
    ODOO_STANDARD_FIELDS,
    ODOO_STANDARD_MODELS,
)
from ._scan_heuristics import (
    FieldVerdict,
    Sensitivity,
    classify_field,
    is_custom_field_name,
    is_studio_field_name,
)
from .security.fields import is_always_redacted, is_default_hidden

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FieldFinding:
    model: str
    name: str
    type: str
    studio: bool
    verdict: FieldVerdict


@dataclass(slots=True)
class ModelFinding:
    name: str
    label: str
    field_count: int
    studio: bool


@dataclass(slots=True)
class ScanResult:
    instance: str
    scanned_at: str
    odoo_reference_version: str
    models_total: int
    fields_total: int
    custom_models: list[ModelFinding] = field(default_factory=list)
    custom_fields_on_standard: list[FieldFinding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _list_models(client: Any) -> list[dict[str, Any]]:
    rows = client._execute(
        "ir.model",
        "search_read",
        [[]],
        {"fields": ["model", "name"]},
    )
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("model"), str)]


def _fields_get(client: Any, model: str) -> dict[str, dict[str, Any]]:
    """Schema for *model*. Returns a dict, even if the call fails for one model."""
    try:
        result = client._execute(
            model,
            "fields_get",
            [],
            {"attributes": ["type", "string", "help", "manual"]},
        )
    except Exception:  # noqa: BLE001 — never fail the whole scan on one model
        return {}
    if not isinstance(result, dict):
        return {}
    return {k: v for k, v in result.items() if isinstance(k, str) and isinstance(v, dict)}


def perform_scan(client: Any, instance_name: str) -> ScanResult:
    """Drive the scan. Stateless wrt the dispatcher; uses ``_execute`` directly."""
    scanned_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    models = _list_models(client)
    custom_models: list[ModelFinding] = []
    custom_fields: list[FieldFinding] = []
    fields_total = 0

    for row in models:
        model_name = str(row["model"])
        model_label = str(row.get("name") or model_name)
        schema = _fields_get(client, model_name)
        fields_total += len(schema)

        is_standard_model = model_name in ODOO_STANDARD_MODELS
        if not is_standard_model:
            studio = is_studio_field_name(model_name) or model_name.startswith("x_")
            custom_models.append(
                ModelFinding(
                    name=model_name,
                    label=model_label,
                    field_count=len(schema),
                    studio=studio,
                )
            )
            # Don't also list every field on a custom model as a "custom field
            # on a standard model" — that double-counts.
            continue

        standard_fields = ODOO_STANDARD_FIELDS.get(model_name, frozenset())
        for fname, meta in schema.items():
            if fname in standard_fields:
                continue
            if not is_custom_field_name(fname) and not bool(meta.get("manual")):
                # Field isn't in our reference AND isn't a Studio/x_ name AND
                # isn't marked manual=True. This usually means the reference
                # missed it (regex parser limitation, not a real custom field).
                # Skip to keep noise down.
                continue
            verdict = classify_field(
                model_name,
                fname,
                meta,
                is_blocked=is_always_redacted(fname),
                is_gated=is_default_hidden(model_name, fname),
            )
            ftype_raw = meta.get("type")
            ftype = ftype_raw if isinstance(ftype_raw, str) else "unknown"
            custom_fields.append(
                FieldFinding(
                    model=model_name,
                    name=fname,
                    type=ftype,
                    studio=is_studio_field_name(fname),
                    verdict=verdict,
                )
            )

    return ScanResult(
        instance=instance_name,
        scanned_at=scanned_at,
        odoo_reference_version=ODOO_REFERENCE_VERSION,
        models_total=len(models),
        fields_total=fields_total,
        custom_models=custom_models,
        custom_fields_on_standard=custom_fields,
    )


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

_SENSITIVE_VERDICTS: frozenset[Sensitivity] = frozenset(
    {Sensitivity.LIKELY_SENSITIVE, Sensitivity.LIKELY_FINANCIAL}
)


def _group_fields_by_model(findings: Iterable[FieldFinding]) -> dict[str, list[FieldFinding]]:
    grouped: dict[str, list[FieldFinding]] = {}
    for f in findings:
        grouped.setdefault(f.model, []).append(f)
    for v in grouped.values():
        v.sort(key=lambda x: x.name)
    return grouped


def render_human(result: ScanResult, *, uid: int | None, login: str | None) -> str:
    out: list[str] = []
    header = f"Scanning instance {result.instance!r}..."
    out.append(header)
    if uid is not None and login is not None:
        out.append(f"  Connected as {login} (uid={uid})")
    out.append(
        f"  Found {result.models_total} models, {result.fields_total} fields total "
        f"(reference: Odoo {result.odoo_reference_version})"
    )
    out.append("")

    # Custom models
    out.append("== Custom models (not in Odoo Community standard) ==")
    if not result.custom_models:
        out.append("  (none)")
    else:
        for m in sorted(result.custom_models, key=lambda x: x.name):
            tag = " Studio" if m.studio else ""
            out.append(f"  {m.name:<45}[{m.field_count} fields,{tag} label={m.label!r}]")
    out.append("")

    # Custom fields on standard models
    out.append("== Custom fields on standard models ==")
    grouped = _group_fields_by_model(result.custom_fields_on_standard)
    if not grouped:
        out.append("  (none)")
    else:
        for model in sorted(grouped):
            fields = grouped[model]
            out.append(f"  {model}  [{len(fields)} custom field(s)]")
            for f in fields:
                marker = f.verdict.sensitivity.value
                out.append(f"    {f.name:<32} [{f.type:<10}] {marker} — {f.verdict.reason}")
    out.append("")

    # Summary
    counts: dict[Sensitivity, int] = {s: 0 for s in Sensitivity}
    for f in result.custom_fields_on_standard:
        counts[f.verdict.sensitivity] += 1
    out.append("== Summary ==")
    out.append(f"  Custom models:                {len(result.custom_models)}")
    out.append(f"  Custom fields on standard:    {len(result.custom_fields_on_standard)}")
    out.append(f"    BLOCKED (built-in):         {counts[Sensitivity.BLOCKED]}")
    out.append(f"    GATED (default-hidden):     {counts[Sensitivity.GATED]}")
    out.append(f"    LIKELY_SENSITIVE:           {counts[Sensitivity.LIKELY_SENSITIVE]}")
    out.append(f"    LIKELY_FINANCIAL:           {counts[Sensitivity.LIKELY_FINANCIAL]}")
    out.append(f"    BINARY_AUTO_STRIPPED:       {counts[Sensitivity.BINARY_AUTO_STRIPPED]}")
    out.append(f"    UNCERTAIN — review:         {counts[Sensitivity.UNCERTAIN]}")
    out.append("")
    out.append("Run with --toml to get a paste-ready config snippet, or --json for scripting.")
    return "\n".join(out)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_toml(result: ScanResult) -> str:
    """Emit a TOML snippet ready to paste into ``[instances.<name>]``."""
    out: list[str] = []
    out.append(f"# Generated by `odoo-mcp scan-custom {result.instance}` on {result.scanned_at}")
    out.append(f"# against Odoo {result.odoo_reference_version} reference data.")
    out.append("# Review UNCERTAIN entries manually before deploying.")
    out.append("")

    # Build per-model lists for sensitive_fields, plus a flat list of regex
    # patterns for fields we want to nuke globally on the instance.
    per_model: dict[str, list[FieldFinding]] = {}
    custom_patterns: list[FieldFinding] = []
    for f in result.custom_fields_on_standard:
        if f.verdict.sensitivity not in _SENSITIVE_VERDICTS:
            continue
        per_model.setdefault(f.model, []).append(f)
        custom_patterns.append(f)

    if not custom_patterns:
        out.append(f"# No flagged sensitive custom fields found for {result.instance!r}.")
        out.append("# (UNCERTAIN findings still warrant manual review — see the human report.)")
        return "\n".join(out)

    # custom_sensitive_field_patterns — escape names into regex literals.
    out.append(f"[instances.{result.instance}]")
    out.append("custom_sensitive_field_patterns = [")
    seen: set[str] = set()
    for f in sorted(custom_patterns, key=lambda x: (x.model, x.name)):
        # Use the literal field name as the pattern (escaped for regex).
        # That's stricter than a substring match and matches the
        # field-policy semantics (`fullmatch`).
        pattern = _toml_escape(f.name)
        if pattern in seen:
            continue
        seen.add(pattern)
        out.append(f'    "{pattern}",  # {f.model}: {f.verdict.reason}')
    out.append("]")
    out.append("")
    out.append(f"[instances.{result.instance}.sensitive_fields]")
    for model in sorted(per_model):
        fields = per_model[model]
        names = sorted({f.name for f in fields})
        joined = ", ".join(f'"{_toml_escape(n)}"' for n in names)
        out.append(f'"{_toml_escape(model)}" = [{joined}]')
    return "\n".join(out)


def render_json(result: ScanResult) -> str:
    payload: dict[str, Any] = {
        "instance": result.instance,
        "scanned_at": result.scanned_at,
        "odoo_reference_version": result.odoo_reference_version,
        "stats": {
            "models_total": result.models_total,
            "fields_total": result.fields_total,
            "models_custom": len(result.custom_models),
            "fields_custom_on_standard": len(result.custom_fields_on_standard),
        },
        "custom_models": [
            {
                "name": m.name,
                "label": m.label,
                "field_count": m.field_count,
                "studio": m.studio,
            }
            for m in sorted(result.custom_models, key=lambda x: x.name)
        ],
        "custom_fields_on_standard": [
            {
                "model": f.model,
                "name": f.name,
                "type": f.type,
                "studio": f.studio,
                "sensitivity": f.verdict.sensitivity.value,
                "reason": f.verdict.reason,
            }
            for f in sorted(result.custom_fields_on_standard, key=lambda x: (x.model, x.name))
        ],
        "suggested_config": _suggested_config(result),
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def _suggested_config(result: ScanResult) -> dict[str, Any]:
    patterns: list[str] = []
    per_model: dict[str, list[str]] = {}
    seen: set[str] = set()
    for f in result.custom_fields_on_standard:
        if f.verdict.sensitivity not in _SENSITIVE_VERDICTS:
            continue
        if f.name not in seen:
            patterns.append(f.name)
            seen.add(f.name)
        per_model.setdefault(f.model, []).append(f.name)
    return {
        "custom_sensitive_field_patterns": sorted(patterns),
        "sensitive_fields": {m: sorted(set(v)) for m, v in per_model.items()},
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_USAGE = """\
Usage: odoo-mcp scan-custom INSTANCE [--toml | --json] [--help]

Connect to the configured Odoo instance and produce a report of every model
and field that is NOT part of the Odoo Community standard reference. Each
custom field is classified on sensitivity using name and help-text
heuristics that include Dutch / Flemish equivalents.

Output formats (mutually exclusive):
  default  Human-readable report.
  --toml   TOML snippet for the instance's config.toml block.
  --json   Machine-readable JSON for scripting.

Note: this command bypasses the Claude-facing dispatcher denylist on
purpose. It is admin tooling — the operator is the consultant, not Claude.
The denylist exists to constrain Claude, not the operator.
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="odoo-mcp scan-custom",
        description="Discover klant-custom models and fields.",
        add_help=False,
    )
    parser.add_argument("instance", nargs="?", help="Configured instance name")
    parser.add_argument("--toml", action="store_true", help="Emit TOML config snippet")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args(argv)

    if args.help or not args.instance:
        sys.stdout.write(_USAGE)
        return 0 if args.help else 2

    if args.toml and args.json:
        print("error: --toml and --json are mutually exclusive", file=sys.stderr)
        return 2

    # Build the app via the standard entry point — same config + credential
    # plumbing as the server uses.
    from .server import build_app

    try:
        app = build_app()
        rt = app.instance(args.instance)
        rt.client.ensure_authenticated()
    except Exception as exc:  # noqa: BLE001 — surface any startup error cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        result = perform_scan(rt.client, args.instance)
    except Exception as exc:  # noqa: BLE001
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        sys.stdout.write(render_json(result))
        sys.stdout.write("\n")
        return 0
    if args.toml:
        sys.stdout.write(render_toml(result))
        sys.stdout.write("\n")
        return 0

    uid = getattr(rt.client, "uid", None)
    creds = getattr(rt.client, "_credentials", None)
    login = getattr(creds, "username", None) if creds is not None else None
    sys.stdout.write(render_human(result, uid=uid, login=login))
    sys.stdout.write("\n")
    return 0
