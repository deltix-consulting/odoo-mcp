"""Tests for the opt-in stderr logging helper."""

from __future__ import annotations

import io
import logging
import sys

import pytest

from odoo_mcp.errors import _SECRETS, register_secret
from odoo_mcp.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _reset_logger() -> None:
    """Ensure the odoo_mcp logger starts clean for each test."""
    logger = logging.getLogger("odoo_mcp")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(logging.NOTSET)
    logger.propagate = False


def _capture_stderr(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


def test_off_produces_no_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "OFF")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    logging.getLogger("odoo_mcp.dispatcher").info("should not appear")
    logging.getLogger("odoo_mcp.client").error("should not appear either")

    assert buf.getvalue() == ""


def test_unset_produces_no_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_LOG_LEVEL", raising=False)
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    logging.getLogger("odoo_mcp.dispatcher").warning("still nothing")

    assert buf.getvalue() == ""


def test_debug_produces_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "DEBUG")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    logging.getLogger("odoo_mcp.client").debug("authenticate start: instance=dev")

    output = buf.getvalue()
    assert "authenticate start: instance=dev" in output
    assert "DEBUG" in output
    assert "odoo_mcp.client" in output


def test_info_filters_out_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "INFO")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    log = logging.getLogger("odoo_mcp.dispatcher")
    log.debug("hidden")
    log.info("visible")

    output = buf.getvalue()
    assert "hidden" not in output
    assert "visible" in output


def test_registered_secret_is_scrubbed_from_log(monkeypatch: pytest.MonkeyPatch) -> None:
    # Register a unique secret so we don't collide with other tests.
    secret = "super-secret-api-key-abcdef123456"
    _SECRETS.pop(secret, None)
    register_secret(secret)

    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "DEBUG")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    try:
        log = logging.getLogger("odoo_mcp.client")
        log.debug("authenticate failed with %s on host", secret)
        log.info("raw secret leak: %s", secret)
        log.error(f"interpolated already: {secret}")  # noqa: G004 — intentional test
    finally:
        _SECRETS.pop(secret, None)

    output = buf.getvalue()
    assert secret not in output
    assert "<redacted>" in output


def test_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling configure_logging twice should not stack handlers."""
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "INFO")
    buf = _capture_stderr(monkeypatch)
    configure_logging()
    configure_logging()

    logging.getLogger("odoo_mcp.dispatcher").info("once")

    # Exactly one line of output (not two).
    assert buf.getvalue().count("once") == 1


# -- a typo in ODOO_MCP_LOG_LEVEL is an opt-in with a bad value, not OFF -------


def test_unrecognised_level_still_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator set the variable because something is already broken;
    a typo must not turn that into the one silent outcome."""
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "DEBGU")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    logging.getLogger("odoo_mcp.client").error("server died")

    assert "server died" in buf.getvalue()


def test_unrecognised_level_names_the_bad_value_and_the_valid_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "trace")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    output = buf.getvalue()
    assert "ODOO_MCP_LOG_LEVEL='TRACE'" in output
    for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "OFF"):
        assert name in output
    assert "logging at WARNING instead" in output


def test_unrecognised_level_falls_back_to_warning_not_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conservative fallback: failures show, chatter does not."""
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "VERBOSE")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    log = logging.getLogger("odoo_mcp.dispatcher")
    log.debug("hidden")
    log.info("also hidden")
    log.warning("visible")

    output = buf.getvalue()
    assert "hidden" not in output
    assert "visible" in output


def test_recognised_level_emits_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "ERROR")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    assert buf.getvalue() == ""


def test_warn_alias_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """``WARN`` is the spelling most other tools (and ``logging`` itself)
    accept; it used to be treated as a typo and therefore as OFF."""
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "WARN")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    log = logging.getLogger("odoo_mcp.client")
    log.info("hidden")
    log.error("visible")

    output = buf.getvalue()
    assert "not a recognised level" not in output
    assert "hidden" not in output
    assert "visible" in output


def test_critical_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "CRITICAL")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    log = logging.getLogger("odoo_mcp.client")
    log.error("hidden")
    log.critical("visible")

    output = buf.getvalue()
    assert "not a recognised level" not in output
    assert "hidden" not in output
    assert "visible" in output


def test_empty_value_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ODOO_MCP_LOG_LEVEL=`` (set but empty) is how a launcher clears a
    variable; it must not be read as a typo."""
    monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "")
    buf = _capture_stderr(monkeypatch)
    configure_logging()

    logging.getLogger("odoo_mcp.client").error("should not appear")

    assert buf.getvalue() == ""
