"""Tests for the cross-platform credential store wrapper.

Mocks the ``keyring`` module so the tests are pure unit and never touch
the OS credential store on the test machine.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_mcp import _credstore


class _FakeKeyring:
    """Minimal in-memory keyring substitute."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str, str | None]] = []

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password
        self.calls.append(("set", service, username, password))

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", service, username, None))
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username, None))
        if (service, username) not in self.store:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(_credstore, "keyring", fake)
    return fake


def test_set_get_round_trip(fake_keyring: _FakeKeyring) -> None:
    _credstore.set_secret("main", "ODOO_MCP_MAIN_API_KEY", "supersecret")
    assert _credstore.get_secret("main", "ODOO_MCP_MAIN_API_KEY") == "supersecret"


def test_get_missing_returns_none(fake_keyring: _FakeKeyring) -> None:
    assert _credstore.get_secret("ghost", "ANY_KEY") is None


def test_delete_existing(fake_keyring: _FakeKeyring) -> None:
    _credstore.set_secret("main", "S", "v")
    _credstore.delete_secret("main", "S")
    assert _credstore.get_secret("main", "S") is None


def test_delete_missing_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting a non-existent entry must be a silent no-op."""

    class FakeKR:
        def delete_password(self, *_a: Any, **_k: Any) -> None:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError("not present")

    monkeypatch.setattr(_credstore, "keyring", FakeKR())
    # Must not raise.
    _credstore.delete_secret("main", "absent")


def test_service_name_format() -> None:
    """Service identifier must follow ``odoo-mcp/{instance}`` for grouping."""
    assert _credstore._service_name("foo") == "odoo-mcp/foo"
    assert _credstore._service_name("klantx") == "odoo-mcp/klantx"


def test_set_uses_correct_service_name(fake_keyring: _FakeKeyring) -> None:
    _credstore.set_secret("klantx", "ODOO_MCP_KLANTX_API_KEY", "k")
    assert ("odoo-mcp/klantx", "ODOO_MCP_KLANTX_API_KEY") in fake_keyring.store
