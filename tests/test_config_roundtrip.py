"""The wizard must be able to rewrite every config its own loader accepts.

``odoo-mcp setup --add`` / ``--remove`` / ``acknowledge-admin`` all work by
reading config.toml into plain dicts, editing one entry, and regenerating the
whole file. Two documented instance keys are TOML sub-tables —
``sensitive_fields`` (the per-instance redaction policy) and
``smart_fields_overrides`` — so the generator has to survive a nested dict.

Every pre-existing wizard test builds a flat scalar-only config, which is why
the gap held: ``config.py`` accepted the sub-tables and ``_generate_toml``
could not write them back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from odoo_mcp import setup_wizard
from odoo_mcp.config import load_config

# A config exercising both sub-table keys, plus a scalar declared *after*
# them in the source file — the reparenting trap: if the generator emits a
# sub-table before its parent's remaining scalars, ``production`` is read
# back as a member of ``smart_fields_overrides`` on the next load.
_NESTED_CONFIG = """\
[defaults]
timeout_seconds = 30
allowed_models = ["*"]

[instances.main]
url = "https://klantx.odoo.com"
database = "klantx-prod"
credentials_env_prefix = "ODOO_MCP_MAIN"
production = true
custom_sensitive_field_patterns = ["^x_secret_"]

[instances.main.sensitive_fields]
"res.partner" = ["vat", "mobile"]
"hr.employee" = []

[instances.main.smart_fields_overrides]
"crm.lead" = ["name", "partner_id", "expected_revenue"]

[instances.dev]
url = "https://dev.example.com"
database = "dev_db"
credentials_env_prefix = "ODOO_MCP_DEV"
production = false
"""


@pytest.fixture
def nested_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_NESTED_CONFIG)
    cfg_path.chmod(0o600)
    monkeypatch.setattr(setup_wizard, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(setup_wizard, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(setup_wizard, "_LAUNCH_SH", tmp_path / "launch.sh")
    monkeypatch.setattr(setup_wizard, "_run_doctor", lambda: None)
    return cfg_path


def _snapshot(path: Path) -> dict[str, Any]:
    """Parse via the real loader and reduce to comparable plain values."""
    app = load_config(path)
    return {
        name: {
            "url": inst.url,
            "database": inst.database,
            "production": inst.production,
            "sensitive_fields": {m: sorted(f) for m, f in inst.sensitive_fields.items()},
            "smart_fields_overrides": {m: list(f) for m, f in inst.smart_fields_overrides.items()},
            "custom_sensitive_field_patterns": list(inst.custom_sensitive_field_patterns),
        }
        for name, inst in app.instances.items()
    }


def test_generator_round_trips_a_config_the_loader_accepts(nested_config: Path) -> None:
    """The invariant: regenerate → reload must preserve every parsed value."""
    before = _snapshot(nested_config)

    defaults, instances = setup_wizard._load_raw_config()
    setup_wizard._write_config(defaults, instances)

    assert _snapshot(nested_config) == before


def test_sub_table_does_not_swallow_its_parents_scalars(nested_config: Path) -> None:
    """A scalar sitting after a sub-table must stay on the parent instance.

    ``custom_sensitive_field_patterns`` is declared before the sub-tables in
    the source file but iterates after them in dict order only by luck; the
    generator must group scalars ahead of sub-tables regardless.
    """
    defaults, instances = setup_wizard._load_raw_config()
    setup_wizard._write_config(defaults, instances)

    reloaded = load_config(nested_config).instances["main"]
    assert reloaded.production is True
    assert reloaded.custom_sensitive_field_patterns == ("^x_secret_",)
    assert reloaded.smart_fields_overrides == {
        "crm.lead": ("name", "partner_id", "expected_revenue")
    }


def test_dotted_model_key_stays_one_key(nested_config: Path) -> None:
    """``res.partner`` must be quoted, not emitted as two nested tables."""
    defaults, instances = setup_wizard._load_raw_config()
    setup_wizard._write_config(defaults, instances)

    text = nested_config.read_text()
    assert '"res.partner" = ' in text
    assert load_config(nested_config).instances["main"].sensitive_fields[
        "res.partner"
    ] == frozenset({"vat", "mobile"})


def test_remove_succeeds_on_a_config_with_a_redaction_policy(
    nested_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing 'dev' must not be blocked by 'main' having sensitive_fields."""
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_wizard, "_keychain_delete", lambda i, s: deleted.append((i, s)))
    answers = iter(["2", "y"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))

    assert setup_wizard._cmd_remove() == 0

    app = load_config(nested_config)
    assert set(app.instances) == {"main"}
    # main's policy survived the rewrite that removed a different instance.
    assert app.instances["main"].sensitive_fields["res.partner"] == frozenset({"vat", "mobile"})
    assert {pair[0] for pair in deleted} == {"dev"}


def test_remove_does_not_destroy_credentials_when_the_config_write_fails(
    nested_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The irreversible step must not run before the step that can fail.

    An API key cannot be read back out of Odoo, so deleting it before the
    config rewrite means a failed removal leaves an instance that is still
    configured but can no longer authenticate. The load-bearing assertion is
    that the credential store was never touched — not the exit code.
    """
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_wizard, "_keychain_delete", lambda i, s: deleted.append((i, s)))

    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(setup_wizard, "_atomic_write_text", _boom)
    answers = iter(["2", "y"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))

    before = nested_config.read_text()
    with pytest.raises(OSError):
        setup_wizard._cmd_remove()

    assert deleted == []
    assert nested_config.read_text() == before
    assert set(load_config(nested_config).instances) == {"main", "dev"}


def test_add_preserves_an_existing_instances_redaction_policy(
    nested_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`setup --add` regenerates the whole file — it must not drop sub-tables."""
    monkeypatch.setattr(setup_wizard, "_store_credentials", lambda *a: None)
    monkeypatch.setattr(setup_wizard, "_acknowledge_admin_or_abort", lambda _n: True)
    monkeypatch.setattr(setup_wizard, "_check_user_is_internal", lambda _n: None)
    monkeypatch.setattr(
        setup_wizard,
        "_ask_instance",
        lambda: {
            "name": "staging",
            "url": "https://staging.example.com",
            "database": "staging_db",
            "username": "bot",
            "api_key": "k",
            "production": False,
        },
    )

    assert setup_wizard._cmd_add() == 0

    app = load_config(nested_config)
    assert set(app.instances) == {"main", "dev", "staging"}
    assert app.instances["main"].sensitive_fields["res.partner"] == frozenset({"vat", "mobile"})
    assert app.instances["main"].smart_fields_overrides == {
        "crm.lead": ("name", "partner_id", "expected_revenue")
    }


def test_toml_value_serialises_a_dict_as_an_inline_table() -> None:
    """The serialiser must be total over every type tomllib can produce."""
    rendered = setup_wizard._toml_value({"res.partner": ["vat"], "plain": 1})
    assert rendered == '{"res.partner" = ["vat"], plain = 1}'
