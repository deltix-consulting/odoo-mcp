"""Regenerate ``src/odoo_mcp/_odoo_reference.py`` from an Odoo source tree.

Usage::

    python scripts/regen_odoo_reference.py [--odoo-src /tmp/odoo-audit/odoo]

Walks every Python file under ``addons/`` and ``odoo/addons/`` of the Odoo
source checkout, parses model declarations (``_name`` / ``_inherit``) and
field assignments (``foo = fields.Char(...)``), then emits a Python data
module that the MCP imports at runtime.

The walker uses :mod:`ast` (not regex) so it understands multi-line and
parenthesised expressions correctly. Test-only modules (``addons/test_*``,
``odoo/addons/test_*``) are skipped — they pollute the reference set with
fake models that real klanten won't have.

Inheritance handling: when a class declares ``_inherit = "base.model"`` (and
no ``_name``, or ``_name == _inherit``), its fields are merged INTO that
inherited model's field set. Multi-inherit (``_inherit = ["a", "b"]``) is
also handled.
"""

from __future__ import annotations

import argparse
import ast
import sys
from datetime import UTC, datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_OUTPUT_PATH = _REPO_ROOT / "src" / "odoo_mcp" / "_odoo_reference.py"

# Odoo's `fields` module names. We accept both the bare attribute and the
# qualified form (``odoo.fields.X``, ``fields.X``).
_FIELDS_MODULE_NAMES = {"fields"}


def _is_test_addon(path: Path, root: Path) -> bool:
    """True if any path component under *root* starts with ``test_``."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith("test_") or part == "tests" for part in rel.parts)


def _find_addon_dirs(odoo_src: Path) -> list[Path]:
    roots: list[Path] = []
    for candidate in (odoo_src / "addons", odoo_src / "odoo" / "addons"):
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def _str_value(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _list_of_strs(node: ast.expr) -> list[str]:
    if isinstance(node, ast.List | ast.Tuple):
        out: list[str] = []
        for el in node.elts:
            s = _str_value(el)
            if s is not None:
                out.append(s)
        return out
    s = _str_value(node)
    return [s] if s is not None else []


def _is_fields_call(node: ast.expr) -> bool:
    """Return True if *node* is a call like ``fields.Char(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # ``fields.Char(...)``
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Name) and value.id in _FIELDS_MODULE_NAMES:
            return True
        # ``odoo.fields.Char(...)``
        if isinstance(value, ast.Attribute) and value.attr in _FIELDS_MODULE_NAMES:
            return True
    return False


def _extract_class_info(
    cls: ast.ClassDef,
) -> tuple[str | None, list[str], list[str]]:
    """Return ``(_name, _inherit_list, field_names)`` for a class node."""
    own_name: str | None = None
    inherit: list[str] = []
    field_names: list[str] = []
    for stmt in cls.body:
        # _name / _inherit assignments
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                if target.id == "_name":
                    s = _str_value(stmt.value)
                    if s is not None:
                        own_name = s
                elif target.id == "_inherit":
                    inherit = _list_of_strs(stmt.value)
        # foo = fields.X(...)
        if isinstance(stmt, ast.Assign):
            if not _is_fields_call(stmt.value):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    field_names.append(target.id)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and stmt.value is not None
            and _is_fields_call(stmt.value)
            and isinstance(stmt.target, ast.Name)
            and not stmt.target.id.startswith("_")
        ):
            field_names.append(stmt.target.id)
    return own_name, inherit, field_names


def _scan_python_file(
    path: Path,
    models: dict[str, set[str]],
) -> None:
    """Parse one .py file and merge its model/field declarations into *models*."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        own_name, inherit, field_names = _extract_class_info(node)

        # Resolve the target model(s) this class contributes fields to.
        targets: list[str] = []
        if own_name is not None:
            targets.append(own_name)
            # ``_name`` + ``_inherit`` is a "delegate" / "rename" — fields
            # still belong on the new ``_name``, not the parent.
        elif inherit:
            # Pure ``_inherit`` extension: contribute to every inherited model.
            targets.extend(inherit)
        else:
            continue

        for tgt in targets:
            # Odoo model names are dotted (e.g. ``res.partner``). A bare
            # identifier is almost always a python helper class, not a model.
            # ``base`` is the one bare-name exception.
            if not isinstance(tgt, str):
                continue
            if "." not in tgt and tgt != "base":
                continue
            models.setdefault(tgt, set()).update(field_names)


# Implicit fields that every Odoo model gets via the ORM but that don't appear
# as ``Field`` declarations in source. ``id`` is the most important — every
# Odoo record has it, and klant scans must not flag it as "custom".
_IMPLICIT_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
        "display_name",
        "__last_update",
    }
)


def collect_reference(odoo_src: Path) -> dict[str, frozenset[str]]:
    models: dict[str, set[str]] = {}
    addon_roots = _find_addon_dirs(odoo_src)
    if not addon_roots:
        raise SystemExit(f"No addons/ found under {odoo_src}.")
    for root in addon_roots:
        for py in root.rglob("*.py"):
            if _is_test_addon(py, root):
                continue
            _scan_python_file(py, models)

    out: dict[str, frozenset[str]] = {}
    for name, fields in models.items():
        out[name] = frozenset(fields | _IMPLICIT_FIELDS)
    return out


def _format_module(
    reference: dict[str, frozenset[str]],
    odoo_version: str,
    generated_at: str,
) -> str:
    models_sorted = sorted(reference.keys())
    lines: list[str] = []
    lines.append('"""Auto-generated reference of Odoo Community standard models and fields.\n')
    lines.append("DO NOT EDIT BY HAND. Regenerate via::")
    lines.append("")
    lines.append("    python scripts/regen_odoo_reference.py\n")
    lines.append(f"Generated against Odoo Community {odoo_version} at {generated_at}.\n")
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from typing import Final")
    lines.append("")
    lines.append(f'ODOO_REFERENCE_VERSION: Final[str] = "{odoo_version}"')
    lines.append(f'ODOO_REFERENCE_GENERATED_AT: Final[str] = "{generated_at}"')
    lines.append("")

    # Models set
    lines.append("ODOO_STANDARD_MODELS: Final[frozenset[str]] = frozenset({")
    for name in models_sorted:
        lines.append(f"    {name!r},")
    lines.append("})")
    lines.append("")

    # Fields per model
    lines.append("ODOO_STANDARD_FIELDS: Final[dict[str, frozenset[str]]] = {")
    for name in models_sorted:
        fields = sorted(reference[name])
        if not fields:
            lines.append(f"    {name!r}: frozenset(),")
            continue
        lines.append(f"    {name!r}: frozenset({{")
        for f in fields:
            lines.append(f"        {f!r},")
        lines.append("    }),")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--odoo-src",
        type=Path,
        default=Path("/tmp/odoo-audit/odoo"),  # noqa: S108 — local dev convention
        help="Path to the Odoo source checkout (default: /tmp/odoo-audit/odoo).",
    )
    parser.add_argument(
        "--version",
        default="18.0",
        help="Odoo version label embedded in the generated module.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_OUTPUT_PATH,
        help=f"Output file (default: {_OUTPUT_PATH}).",
    )
    args = parser.parse_args(argv)

    odoo_src: Path = args.odoo_src
    if not odoo_src.is_dir():
        print(f"odoo source not found: {odoo_src}", file=sys.stderr)
        return 2

    reference = collect_reference(odoo_src)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = _format_module(reference, args.version, generated_at)
    args.output.write_text(body, encoding="utf-8")
    total_fields = sum(len(v) for v in reference.values())
    print(
        f"Wrote {args.output} — {len(reference)} models, "
        f"{total_fields} fields, {args.output.stat().st_size / 1024:.1f} KB."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
