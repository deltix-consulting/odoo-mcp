"""ORM-method compatibility across Odoo versions.

Odoo deprecated two public ORM methods this client calls and then removed
them on the saas-19.x line (``saas-19.1`` onward — what Odoo Online and
Odoo.sh databases run):

* ``read_group`` → ``formatted_read_group``
* ``check_access_rights`` → ``has_access``

Both call sites keep using the legacy method and fall back once the server
reports it missing, with ``read_group``'s result translated back into the
legacy shape.

These are unit-level tests against a faked ``_execute``: the dispatcher
tests fake ``read_group`` / ``check_access_rights`` on the client itself
and so cannot see the RPC-level method choice or the shape translation.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_mcp.client import OdooClient
from odoo_mcp.config import InstanceConfig
from odoo_mcp.credentials import Credentials
from odoo_mcp.errors import OdooRemoteError

_FIELDS_META: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "stage_id": {"type": "many2one", "relation": "crm.stage"},
    "user_id": {"type": "many2one", "relation": "res.users"},
    "expected_revenue": {"type": "monetary"},
    "create_date": {"type": "datetime"},
    "name": {"type": "char"},
    "sequence": {"type": "integer"},
}


def _make_client(name: str = "dev") -> OdooClient:
    cfg = InstanceConfig(
        name=name,
        url="https://dev.example.odoo.com",
        database="dev_db",
        credentials_env_prefix="ODOO_MCP_DEV",
        production=False,
        timeout_seconds=30,
        max_records_default=50,
        max_records_hard_cap=500,
        rate_limit_per_minute=300,
        allow_self_signed=False,
        allowed_models=frozenset({"crm.lead"}),
    )
    creds = Credentials(instance_name=name, username="u", _api_key="k" * 10)
    client = OdooClient(cfg, credentials=creds)
    client._uid = 1  # type: ignore[attr-defined]
    # Pre-warm the fields cache so the fallback path does no extra RPC.
    client._cache_fields_l1("crm.lead", _FIELDS_META)  # type: ignore[attr-defined]
    return client


class _Recorder:
    """Fake ``_execute`` that plays a legacy Odoo or a saas-19 Odoo."""

    def __init__(self, *, has_legacy: bool, formatted_rows: Any = None) -> None:
        self.has_legacy = has_legacy
        self.formatted_rows = formatted_rows if formatted_rows is not None else []
        self.calls: list[tuple[str, str, list[Any], dict[str, Any]]] = []

    def __call__(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        self.calls.append((model, method, args, kwargs))
        if method == "read_group":
            if not self.has_legacy:
                raise OdooRemoteError(
                    f"Odoo fault on {model}.read_group: AttributeError: "
                    f"'{model}' object has no attribute 'read_group'"
                )
            return [{"stage_id": (1, "New"), "stage_id_count": 3}]
        if method == "formatted_read_group":
            return self.formatted_rows
        raise AssertionError(f"unexpected method {method!r}")

    @property
    def methods(self) -> list[str]:
        return [c[1] for c in self.calls]


def _read_group(client: OdooClient, **overrides: Any) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "model": "crm.lead",
        "domain": [("active", "=", True)],
        "fields": ["expected_revenue:sum"],
        "groupby": ["stage_id"],
        "limit": None,
        "offset": 0,
        "orderby": None,
        "lazy": True,
    }
    kwargs.update(overrides)
    model = kwargs.pop("model")
    domain = kwargs.pop("domain")
    fields = kwargs.pop("fields")
    groupby = kwargs.pop("groupby")
    return client.read_group(model, domain, fields, groupby, **kwargs)


# --- Odoo 16-19.0: nothing changes ---------------------------------------


def test_legacy_read_group_is_still_preferred() -> None:
    client = _make_client()
    recorder = _Recorder(has_legacy=True)
    client._execute = recorder  # type: ignore[assignment]

    rows = _read_group(client)

    assert recorder.methods == ["read_group"]
    assert rows == [{"stage_id": (1, "New"), "stage_id_count": 3}]
    _model, _method, args, kwargs = recorder.calls[0]
    assert args == [[("active", "=", True)], ["expected_revenue:sum"], ["stage_id"]]
    assert kwargs["lazy"] is True


def test_a_non_missing_fault_is_not_swallowed() -> None:
    """An AccessError must propagate, not trigger a fallback probe."""
    client = _make_client()
    calls: list[str] = []

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        calls.append(method)
        raise OdooRemoteError(
            f"Odoo fault on {model}.read_group: You are not allowed to access 'Lead' records."
        )

    client._execute = _execute  # type: ignore[assignment]
    with pytest.raises(OdooRemoteError, match="not allowed to access"):
        _read_group(client)
    assert calls == ["read_group"]


# --- saas-19.x: fall back and translate ----------------------------------


def test_missing_read_group_falls_back_to_formatted_read_group() -> None:
    client = _make_client()
    recorder = _Recorder(
        has_legacy=False,
        formatted_rows=[
            {
                "stage_id": (1, "New"),
                "expected_revenue:sum": 1500.0,
                "__count": 3,
                "__extra_domain": [("stage_id", "=", 1)],
            }
        ],
    )
    client._execute = recorder  # type: ignore[assignment]

    rows = _read_group(client)

    assert recorder.methods == ["read_group", "formatted_read_group"]
    _model, _method, args, kwargs = recorder.calls[1]
    assert args == [
        [("active", "=", True)],
        ["stage_id"],
        ["__count", "expected_revenue:sum"],
    ]
    assert "lazy" not in kwargs
    assert kwargs["order"] == "stage_id"
    # Legacy keys: bare field name, "<first_groupby>_count", and __domain
    # recombined from the caller domain plus the group's own criteria.
    assert rows == [
        {
            "stage_id": (1, "New"),
            "stage_id_count": 3,
            "expected_revenue": 1500.0,
            "__domain": [("active", "=", True), ("stage_id", "=", 1)],
        }
    ]


def test_fallback_method_is_remembered_for_later_calls() -> None:
    client = _make_client()
    recorder = _Recorder(has_legacy=False, formatted_rows=[])
    client._execute = recorder  # type: ignore[assignment]

    _read_group(client)
    _read_group(client)
    _read_group(client)

    # One probe, then straight to the replacement.
    assert recorder.methods == [
        "read_group",
        "formatted_read_group",
        "formatted_read_group",
        "formatted_read_group",
    ]


def test_eager_grouping_uses_dunder_count_and_all_dimensions() -> None:
    client = _make_client()
    recorder = _Recorder(
        has_legacy=False,
        formatted_rows=[
            {
                "stage_id": (1, "New"),
                "user_id": (7, "Amina"),
                "__count": 2,
                "__extra_domain": [("stage_id", "=", 1), ("user_id", "=", 7)],
            }
        ],
    )
    client._execute = recorder  # type: ignore[assignment]

    rows = _read_group(client, groupby=["stage_id", "user_id"], fields=[], lazy=False)

    _model, _method, args, _kwargs = recorder.calls[1]
    assert args[1] == ["stage_id", "user_id"]
    assert rows[0]["__count"] == 2
    assert "__context" not in rows[0]


def test_lazy_grouping_echoes_the_remaining_dimensions_in_context() -> None:
    client = _make_client()
    recorder = _Recorder(
        has_legacy=False,
        formatted_rows=[{"stage_id": (1, "New"), "__count": 2, "__extra_domain": []}],
    )
    client._execute = recorder  # type: ignore[assignment]

    rows = _read_group(client, groupby=["stage_id", "user_id"], fields=[], lazy=True)

    _model, _method, args, _kwargs = recorder.calls[1]
    assert args[1] == ["stage_id"]
    assert rows[0]["__context"] == {"group_by": ["user_id"]}
    assert rows[0]["stage_id_count"] == 2


def test_date_groupby_without_granularity_defaults_to_month() -> None:
    client = _make_client()
    recorder = _Recorder(
        has_legacy=False,
        formatted_rows=[{"create_date:month": "July 2026", "__count": 4, "__extra_domain": []}],
    )
    client._execute = recorder  # type: ignore[assignment]

    rows = _read_group(client, groupby=["create_date"], fields=[])

    _model, _method, args, _kwargs = recorder.calls[1]
    assert args[1] == ["create_date:month"]
    # The caller asked for "create_date", so that is the key it gets back.
    assert rows[0]["create_date"] == "July 2026"
    assert rows[0]["create_date_count"] == 4


def test_bare_aggregate_specs_follow_odoo_type_defaults() -> None:
    client = _make_client()
    recorder = _Recorder(has_legacy=False, formatted_rows=[])
    client._execute = recorder  # type: ignore[assignment]

    _read_group(
        client,
        fields=["expected_revenue", "name", "sequence"],
        groupby=["stage_id"],
    )

    _model, _method, args, _kwargs = recorder.calls[1]
    # monetary → sum; char has no aggregator; a field named `sequence` is
    # explicitly opted out by Odoo even though it is an integer.
    assert args[2] == ["__count", "expected_revenue:sum"]


def test_orderby_is_rewritten_onto_the_explicit_specs() -> None:
    client = _make_client()
    recorder = _Recorder(has_legacy=False, formatted_rows=[])
    client._execute = recorder  # type: ignore[assignment]

    _read_group(client, orderby="expected_revenue desc")

    _model, _method, _args, kwargs = recorder.calls[1]
    assert kwargs["order"] == "expected_revenue:sum desc"


def test_fallback_rejects_a_non_mapping_group() -> None:
    client = _make_client()
    recorder = _Recorder(has_legacy=False, formatted_rows=[["stage_id", 1]])
    client._execute = recorder  # type: ignore[assignment]

    with pytest.raises(OdooRemoteError, match="expected a mapping"):
        _read_group(client)


def test_odoo_own_missing_method_wording_triggers_the_fallback() -> None:
    """Pin the detection against Odoo's real text, not a paraphrase.

    ``get_public_method`` (odoo/service/model.py) raises exactly
    ``The method '<model>.<name>' does not exist`` when the attribute is
    gone — that is the string a saas-19 database actually returns.
    """
    client = _make_client()
    calls: list[str] = []

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        calls.append(method)
        if method == "read_group":
            raise OdooRemoteError(
                f"Odoo fault on {model}.read_group: "
                f"AttributeError: The method '{model}.read_group' does not exist"
            )
        return []

    client._execute = _execute  # type: ignore[assignment]
    assert _read_group(client) == []
    assert calls == ["read_group", "formatted_read_group"]


# --- odoo_diagnose_access: check_access_rights → has_access ---------------


def test_check_access_rights_is_still_preferred() -> None:
    client = _make_client()
    calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        calls.append((method, args, kwargs))
        return True

    client._execute = _execute  # type: ignore[assignment]
    assert client.check_access_rights("crm.lead", "read") is True
    assert calls == [("check_access_rights", ["read"], {"raise_exception": False})]


def test_missing_check_access_rights_falls_back_to_has_access() -> None:
    client = _make_client()
    calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        calls.append((method, args, kwargs))
        if method == "check_access_rights":
            raise OdooRemoteError(
                f"Odoo fault on {model}.check_access_rights: AttributeError: "
                f"The method '{model}.check_access_rights' does not exist"
            )
        return True

    client._execute = _execute  # type: ignore[assignment]

    assert client.check_access_rights("crm.lead", "read") is True
    # has_access is a recordset method: execute_kw takes the ids list first,
    # and the empty recordset is the model-level check.
    assert calls[1] == ("has_access", [[], "read"], {})

    # Resolved once, then reused for the remaining operations.
    assert client.check_access_rights("crm.lead", "write") is True
    assert [c[0] for c in calls] == ["check_access_rights", "has_access", "has_access"]


def test_check_access_rights_propagates_a_real_access_error() -> None:
    client = _make_client()

    def _execute(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        raise OdooRemoteError(
            f"Odoo fault on {model}.check_access_rights: You are not allowed to "
            f"access 'Lead' records."
        )

    client._execute = _execute  # type: ignore[assignment]
    with pytest.raises(OdooRemoteError, match="not allowed to access"):
        client.check_access_rights("crm.lead", "read")
