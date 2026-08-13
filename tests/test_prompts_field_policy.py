"""A shipped prompt must not instruct a call the field policy refuses.

``odoo_read_group`` treats grouping by a default-hidden field as a read of
that field's distinct values, so it refuses unless the caller passes
``allow_sensitive_fields``. ``vat`` is default-hidden on ``res.partner`` and
is the *default* match field of ``odoo_find_duplicate_partners`` — so the
prompt's headline path used to render an instruction that the server
answered with ``{"ok": false, "error_code": "field_policy"}``.

The interesting test here is :func:`test_the_call_the_vat_prompt_instructs_is_accepted`:
it parses the call out of the rendered prompt body and executes it, so the
prompt and the policy are checked against each other rather than against a
copy of the expected text. :func:`test_no_prompt_groups_by_a_hidden_field_without_the_optin`
generalises that to the whole library so a future prompt cannot reintroduce
the drift.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

import pytest

from odoo_mcp import prompts
from odoo_mcp.dispatcher import Dispatcher
from odoo_mcp.security.fields import is_default_hidden

# "odoo_read_group on res.partner with ..." / "odoo_read_group account.move with ..."
# up to the next tool mention, which is where that call's arguments end.
_READ_GROUP_CALL = re.compile(
    r"odoo_read_group\s+(?:on\s+)?([a-z][a-z_]*(?:\.[a-z_]+)+)(.*?)(?=odoo_|$)", re.S
)
_GROUPBY = re.compile(r"groupby=\[([^\]]*)\]")
_FIELDS = re.compile(r"fields=\[([^\]]*)\]")
_ALLOW_SENSITIVE = re.compile(r"allow_sensitive_fields=\[([^\]]*)\]")


def _body(name: str, **args: str) -> str:
    result = prompts.get_prompt(name, {"instance": "dev", **args})
    return result.messages[0].content.text  # type: ignore[union-attr]


def _quoted(blob: str) -> list[str]:
    return re.findall(r"'([^']+)'", blob)


class _PartnerClient:
    """Fake exposing just enough of ``res.partner`` for a read_group."""

    def ensure_authenticated(self) -> None: ...

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "vat": {"type": "char", "string": "Tax ID"},
            "email": {"type": "char", "string": "Email"},
        }

    def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return [{groupby[0]: "BE0123456749", "id_count": 2}]


def _call_read_group(dispatcher: Dispatcher, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(dispatcher.call("odoo_read_group", args))
    payload: dict[str, Any] = json.loads(contents[0].text)
    return payload


# -- The regression -----------------------------------------------------------


def test_the_call_the_vat_prompt_instructs_is_accepted(make_app: Callable[..., Any]) -> None:
    """Execute the call parsed out of the rendered prompt. It must not be refused."""
    body = _body("odoo_find_duplicate_partners")
    model, tail = _READ_GROUP_CALL.search(body).groups()  # type: ignore[union-attr]
    assert model == "res.partner"

    args: dict[str, Any] = {
        "instance": "dev",
        "model": model,
        "fields": _quoted(_FIELDS.search(tail).group(1)),  # type: ignore[union-attr]
        "groupby": _quoted(_GROUPBY.search(tail).group(1)),  # type: ignore[union-attr]
    }
    optin = _ALLOW_SENSITIVE.search(tail)
    if optin:
        args["allow_sensitive_fields"] = _quoted(optin.group(1))

    app = make_app(client=_PartnerClient())
    result = _call_read_group(Dispatcher(app), args)

    assert result["ok"] is True, (
        f"the default odoo_find_duplicate_partners prompt instructs a refused call: "
        f"{result.get('error')}"
    )
    assert result["groups"] == [{"vat": "BE0123456749", "id_count": 2}]


def test_vat_prompt_names_the_sensitive_field_optin() -> None:
    body = _body("odoo_find_duplicate_partners")
    assert "groupby=['vat']" in body
    assert "allow_sensitive_fields=['vat']" in body


def test_the_same_call_without_the_optin_is_what_used_to_be_refused(
    make_app: Callable[..., Any],
) -> None:
    """Control: pin the refusal the prompt used to walk into."""
    app = make_app(client=_PartnerClient())
    result = _call_read_group(
        Dispatcher(app),
        {
            "instance": "dev",
            "model": "res.partner",
            "fields": ["id:count"],
            "groupby": ["vat"],
        },
    )
    assert result["ok"] is False
    assert result["error_code"] == "field_policy"


# -- The clause is derived from the policy, not hardcoded ---------------------


def test_groupby_clause_adds_the_optin_only_for_hidden_fields() -> None:
    assert is_default_hidden("res.partner", "vat")
    assert not is_default_hidden("res.partner", "email")

    hidden = prompts._groupby_clause("res.partner", "vat")
    plain = prompts._groupby_clause("res.partner", "email")

    assert "allow_sensitive_fields=['vat']" in hidden
    assert "allow_sensitive_fields" not in plain
    assert plain == "groupby=['email']"


@pytest.mark.parametrize("match_field", ["email", "name"])
def test_non_sensitive_match_fields_get_no_optin(match_field: str) -> None:
    body = _body("odoo_find_duplicate_partners", match_field=match_field)
    assert f"groupby=['{match_field}']" in body
    assert "allow_sensitive_fields" not in body


# -- Library-wide guard -------------------------------------------------------


def test_no_prompt_groups_by_a_hidden_field_without_the_optin() -> None:
    """Sweep every prompt: a hidden groupby field must carry its opt-in.

    Renders each prompt with every documented value of its optional
    arguments so branch-specific bodies (``match_field``) are covered too.
    Covers 6 of the library's 7 read_group call sites.

    Known blind spot: ``odoo_open_manufacturing_orders`` phrases its call as
    ``odoo_read_group mrp.production by ['state']`` rather than
    ``groupby=[...]``, so the regex does not see it. ``state`` is not hidden
    on ``mrp.production``, so nothing is missed today — but a prompt written
    in that style could slip past this guard. Prefer the ``groupby=[...]``
    spelling in new prompts.
    """
    variants: dict[str, list[dict[str, str]]] = {
        "odoo_find_duplicate_partners": [{"match_field": v} for v in ("vat", "email", "name")],
    }
    offenders: list[str] = []

    for prompt in prompts.list_prompts():
        for extra in variants.get(prompt.name, [{}]):
            body = _body(prompt.name, **extra)
            for model, tail in _READ_GROUP_CALL.findall(body):
                groupby_match = _GROUPBY.search(tail)
                if not groupby_match:
                    continue
                allowed = {
                    field for m in _ALLOW_SENSITIVE.finditer(tail) for field in _quoted(m.group(1))
                }
                for spec in _quoted(groupby_match.group(1)):
                    field = spec.split(":")[0]
                    if is_default_hidden(model, field) and field not in allowed:
                        offenders.append(f"{prompt.name}{extra}: groupby {field!r} on {model}")

    assert not offenders, (
        "prompt(s) instruct a groupby on a default-hidden field without naming "
        "allow_sensitive_fields — odoo_read_group will refuse the call:\n  "
        + "\n  ".join(offenders)
    )
