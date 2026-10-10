"""Tests for the stage-template email gate on odoo_create / odoo_write.

When a tracked ``stage_id`` moves to a stage with a mail template, Odoo posts
that template to the recipients it names (the customer, the applicant, the
speaker) from inside create/write itself (mail.thread ``_track_template``,
16.0 to 20.0). On project.task, a stage in rating mode also sends a rating
request to the task's customer with ``force_send=True``. Without a gate, a
generic create/write sends outbound email that ``odoo_send_message`` keeps
behind two opt-ins. These tests pin that the same two opt-ins apply, before
the dry run, and that payloads Odoo sends nothing for stay allowed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from odoo_mcp._odoo_reference import ODOO_STANDARD_FIELDS
from odoo_mcp.dispatcher import (
    _STAGE_FIELD,
    _STAGE_TEMPLATE_FIELDS,
    _TASK_MODEL,
    _TASK_RATING_FLAGS,
    _TASK_RATING_TEMPLATE_FIELD,
    Dispatcher,
)

_MAILING_STAGE = 10  # has a mail template
_QUIET_STAGE = 11  # has no template
_RATING_STAGE = 12  # rating template only
_CUSTOMER = 7
_PROJECT = 20

# The stage model each gated model's stage_id points to (16.0 to 20.0).
_STAGE_MODELS: dict[str, str] = {
    "project.task": "project.task.type",
    "project.project": "project.project.stage",
    "hr.applicant": "hr.recruitment.stage",
    "event.track": "event.track.stage",
}


def _meta(*, rating_on_stage: bool) -> dict[str, dict[str, dict[str, Any]]]:
    task_type: dict[str, dict[str, Any]] = {
        "id": {"type": "integer"},
        "mail_template_id": {"type": "many2one", "relation": "mail.template"},
        "rating_template_id": {"type": "many2one", "relation": "mail.template"},
    }
    project: dict[str, dict[str, Any]] = {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "stage_id": {"type": "many2one", "relation": "project.project.stage"},
        "task_ids": {"type": "one2many", "relation": "project.task"},
    }
    if rating_on_stage:  # 19.0 and later
        task_type["rating_active"] = {"type": "boolean"}
        task_type["rating_status"] = {"type": "selection"}
    else:  # up to 18.0
        project["rating_active"] = {"type": "boolean"}
        project["rating_status"] = {"type": "selection"}
    return {
        "project.task": {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "stage_id": {"type": "many2one", "relation": "project.task.type"},
            "project_id": {"type": "many2one", "relation": "project.project"},
            "partner_id": {"type": "many2one", "relation": "res.partner"},
        },
        "project.task.type": task_type,
        "project.project": project,
        "project.project.stage": {
            "id": {"type": "integer"},
            "mail_template_id": {"type": "many2one", "relation": "mail.template"},
        },
        "hr.applicant": {
            "id": {"type": "integer"},
            "stage_id": {"type": "many2one", "relation": "hr.recruitment.stage"},
        },
        "hr.recruitment.stage": {
            "id": {"type": "integer"},
            "template_id": {"type": "many2one", "relation": "mail.template"},
        },
    }


class _FakeClient:
    def __init__(
        self,
        *,
        rating_on_stage: bool,
        rating_status: str,
        task_partner: int | None,
        task_stage: int,
    ) -> None:
        self.is_admin: bool | None = False
        self.admin_reason: str | None = None
        self.username = "u"
        self.uid = 1
        self.meta = _meta(rating_on_stage=rating_on_stage)
        self.rating_status = rating_status
        self.task_partner = task_partner
        self.task_stage = task_stage
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.written: list[tuple[str, list[int], dict[str, Any]]] = []

    def ensure_authenticated(self) -> None:
        return None

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        return self.meta[model]

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        # Every field the gate asks for must exist on the model (#62).
        assert set(fields) <= set(self.meta[model]), (model, fields)
        rows = [self._row(model, i) for i in ids]
        return [{"id": row["id"], **{f: row.get(f, False) for f in fields}} for row in rows]

    def _row(self, model: str, rid: int) -> dict[str, Any]:
        if model in ("project.task", "hr.applicant", "project.project") and rid != _PROJECT:
            return {
                "id": rid,
                "stage_id": [self.task_stage, "Stage"],
                "project_id": [_PROJECT, "P"],
                "partner_id": [self.task_partner, "C"] if self.task_partner else False,
            }
        if model == "project.project":
            return {"id": rid, "rating_active": True, "rating_status": self.rating_status}
        template = [99, "T"] if rid == _MAILING_STAGE else False
        return {
            "id": rid,
            "mail_template_id": template,
            "template_id": template,
            "rating_template_id": [98, "R"] if rid == _RATING_STAGE else False,
            "rating_active": True,
            "rating_status": self.rating_status,
        }

    def create(self, model: str, values: dict[str, Any]) -> int:
        self.created.append((model, values))
        return 42

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        self.written.append((model, ids, values))
        return True


@pytest.fixture
def build(
    make_app: Callable[..., Any],
) -> Callable[..., tuple[Dispatcher, _FakeClient]]:
    def _build(
        *,
        external_comms_enabled: bool = False,
        rating_on_stage: bool = True,
        rating_status: str = "stage",
        task_partner: int | None = _CUSTOMER,
        task_stage: int = _QUIET_STAGE,
    ) -> tuple[Dispatcher, _FakeClient]:
        fake = _FakeClient(
            rating_on_stage=rating_on_stage,
            rating_status=rating_status,
            task_partner=task_partner,
            task_stage=task_stage,
        )
        app = make_app(client=fake, external_comms_enabled=external_comms_enabled)
        return Dispatcher(app), fake

    return _build


@pytest.fixture(autouse=True)
def _no_comms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", raising=False)


def _call(disp: Dispatcher, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    contents = asyncio.run(disp.call(tool, {"instance": "dev", **args}))
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _move(model: str, stage: int, *, dry_run: bool = False, **extra: Any) -> dict[str, Any]:
    return {"model": model, "ids": [5], "values": {"stage_id": stage, **extra}, "dry_run": dry_run}


# --- refused without the opt-ins ---------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("model", ["project.task", "project.project", "hr.applicant"])
def test_move_to_templated_stage_refused(build: Any, model: str, dry_run: bool) -> None:
    disp, fake = build()
    payload = _call(disp, "odoo_write", _move(model, _MAILING_STAGE, dry_run=dry_run))
    assert payload["ok"] is False
    assert payload["error_code"] == "prod_guard"
    assert "email template" in payload["error"]
    assert "ODOO_MCP_ENABLE_EXTERNAL_COMMS" in payload["error"]
    assert fake.written == []


def test_create_in_templated_stage_refused(build: Any) -> None:
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_create",
        {"model": "hr.applicant", "values": {"stage_id": _MAILING_STAGE}, "dry_run": False},
    )
    assert payload["ok"] is False
    assert "new hr.applicant" in payload["error"]
    assert fake.created == []


def test_nested_task_stage_move_refused(build: Any) -> None:
    # project.project -> task_ids (1, id, vals) is a project.task write.
    disp, fake = build()
    payload = _call(
        disp,
        "odoo_write",
        {
            "model": "project.project",
            "ids": [_PROJECT],
            "values": {"task_ids": [(1, 5, {"stage_id": _MAILING_STAGE})]},
            "dry_run": False,
        },
    )
    assert payload["ok"] is False
    assert "project.task [5]" in payload["error"]
    assert fake.written == []


@pytest.mark.parametrize("rating_on_stage", [True, False])
def test_task_rating_request_refused(build: Any, rating_on_stage: bool) -> None:
    # 19.0+ keeps the rating flags on the stage, up to 18.0 on the project.
    disp, fake = build(rating_on_stage=rating_on_stage)
    payload = _call(disp, "odoo_write", _move(_TASK_MODEL, _RATING_STAGE))
    assert payload["ok"] is False
    assert "rating request" in payload["error"]
    assert fake.written == []


# --- payloads Odoo sends nothing for stay allowed -----------------------------


def test_write_to_current_stage_allowed(build: Any) -> None:
    # No tracked change, so Odoo posts nothing.
    disp, fake = build(task_stage=_MAILING_STAGE)
    payload = _call(disp, "odoo_write", _move(_TASK_MODEL, _MAILING_STAGE))
    assert payload["committed"] is True
    assert "odoo_will_email" not in payload


def test_move_to_quiet_stage_allowed(build: Any) -> None:
    disp, fake = build()
    payload = _call(disp, "odoo_write", _move(_TASK_MODEL, _QUIET_STAGE))
    assert payload["committed"] is True
    assert len(fake.written) == 1


def test_create_without_stage_allowed(build: Any) -> None:
    disp, fake = build()
    payload = _call(
        disp, "odoo_create", {"model": _TASK_MODEL, "values": {"name": "x"}, "dry_run": False}
    )
    assert payload["committed"] is True
    assert "odoo_will_email" not in payload


@pytest.mark.parametrize(
    ("kwargs", "extra"),
    [
        ({"task_partner": None}, {}),  # no customer on the task
        ({}, {"partner_id": False}),  # the same write clears the customer
        ({"rating_status": "periodic"}, {}),  # rating on a schedule, not on stage
        ({"rating_on_stage": False, "rating_status": "periodic"}, {}),
    ],
)
def test_rating_stage_without_rating_mail_allowed(
    build: Any, kwargs: dict[str, Any], extra: dict[str, Any]
) -> None:
    disp, fake = build(**kwargs)
    payload = _call(disp, "odoo_write", _move(_TASK_MODEL, _RATING_STAGE, **extra))
    assert payload["committed"] is True
    assert "odoo_will_email" not in payload


# --- with both opt-ins: allowed, and disclosed in preview AND commit ---------


def test_opted_in_preview_and_commit_both_disclose(
    build: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODOO_MCP_ENABLE_EXTERNAL_COMMS", "1")
    disp, fake = build(external_comms_enabled=True)
    preview = _call(disp, "odoo_write", _move(_TASK_MODEL, _MAILING_STAGE, dry_run=True))
    assert preview["preview"] is True
    assert fake.written == []
    commit = _call(disp, "odoo_write", _move(_TASK_MODEL, _MAILING_STAGE))
    assert commit["committed"] is True
    assert preview["odoo_will_email"] == commit["odoo_will_email"]
    assert "email template" in commit["odoo_will_email"][0]


# --- every name the gate sends or keys on exists in Odoo ---------------------


def test_gate_names_exist_in_odoo_reference() -> None:
    assert set(_STAGE_TEMPLATE_FIELDS) == set(_STAGE_MODELS)
    for model, template_field in _STAGE_TEMPLATE_FIELDS.items():
        assert _STAGE_FIELD in ODOO_STANDARD_FIELDS[model], model
        assert template_field in ODOO_STANDARD_FIELDS[_STAGE_MODELS[model]], model
    assert {"project_id", "partner_id"} <= ODOO_STANDARD_FIELDS[_TASK_MODEL]
    assert _TASK_RATING_TEMPLATE_FIELD in ODOO_STANDARD_FIELDS["project.task.type"]
    # The reference is 18.0, where the rating flags are still on the project.
    assert set(_TASK_RATING_FLAGS) <= ODOO_STANDARD_FIELDS["project.project"]
