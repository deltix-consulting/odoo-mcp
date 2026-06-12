"""The help cookbook must never teach a pattern the MCP itself rejects.

Regression guard: v0.21.0 and earlier shipped a common_patterns example
using a dotted domain (``stage_id.name``) that the domain sandbox refuses
— the single most frequent rejection in real-world audit logs traced back
to agents copying that exact example.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.dispatcher import _HELP_COMMON_PATTERNS, _HELP_GOTCHAS, _HELP_TOOLS_TERSE
from odoo_mcp.security.domain import sandbox_domain


def _domains_in(obj: Any) -> list[list[Any]]:
    """Collect every 'domain' value in the cookbook examples."""
    found: list[list[Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "domain" and isinstance(value, list):
                found.append(value)
            else:
                found.extend(_domains_in(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_domains_in(item))
    return found


def test_every_cookbook_domain_passes_the_sandbox() -> None:
    domains = _domains_in(_HELP_COMMON_PATTERNS)
    assert domains, "cookbook should contain at least one domain example"
    for domain in domains:
        fields = frozenset(
            leaf[0] for leaf in domain if isinstance(leaf, (list, tuple)) and len(leaf) == 3
        )
        # Must not raise — the model's own help can't contradict its sandbox.
        sandbox_domain(domain, fields)


def test_no_dotted_fields_anywhere_in_cookbook_domains() -> None:
    for domain in _domains_in(_HELP_COMMON_PATTERNS):
        for leaf in domain:
            if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
                assert "." not in leaf[0], (
                    f"cookbook example uses dotted domain field {leaf[0]!r} "
                    f"— the sandbox rejects these, see _HELP_COMMON_PATTERNS"
                )


def test_cookbook_covers_relation_trace_and_routing() -> None:
    goals = " ".join(p["goal"].lower() for p in _HELP_COMMON_PATTERNS)
    assert "related record" in goals
    assert "picking type" in goals or "transfer" in goals


def test_every_tool_in_help_list_mentions_diagnose_routing() -> None:
    names = {t["name"] for t in _HELP_TOOLS_TERSE}
    assert "odoo_diagnose_routing" in names


def test_gotchas_explain_the_two_call_workaround() -> None:
    dotted = next(g for g in _HELP_GOTCHAS if "Dotted" in g)
    assert "Two calls" in dotted
