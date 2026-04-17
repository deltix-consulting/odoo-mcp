"""Tests for the update-check helpers and doctor's update notice."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from odoo_mcp import doctor
from odoo_mcp.update_check import (
    _is_newer,
    _parse_version,
    check_for_update,
    extract_security_section,
    read_changelog_security,
)

# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------


def test_parse_version_plain():
    assert _parse_version("0.2.0") == (0, 2, 0)


def test_parse_version_with_v_prefix():
    assert _parse_version("v0.2.0") == (0, 2, 0)


def test_parse_version_capital_v():
    assert _parse_version("V1.4.10") == (1, 4, 10)


def test_parse_version_dev():
    assert _parse_version("dev") == ()


def test_parse_version_empty():
    assert _parse_version("") == ()


def test_parse_version_non_numeric_tail():
    # "0.2.0rc1" stops at the last parseable numeric — chunk "0rc1" parses
    # as 0 and then breaks.
    assert _parse_version("0.2.0rc1") == (0, 2, 0)


def test_parse_version_two_components():
    assert _parse_version("1.4") == (1, 4)


# ---------------------------------------------------------------------------
# _is_newer
# ---------------------------------------------------------------------------


def test_is_newer_true():
    assert _is_newer((0, 1, 0), (0, 2, 0)) is True


def test_is_newer_false_reverse():
    assert _is_newer((0, 2, 0), (0, 1, 0)) is False


def test_is_newer_false_equal():
    assert _is_newer((0, 1, 0), (0, 1, 0)) is False


def test_is_newer_false_empty_candidate():
    assert _is_newer((0, 1, 0), ()) is False


def test_is_newer_false_empty_current():
    # Unknown local version should never be treated as older.
    assert _is_newer((), (0, 2, 0)) is False


# ---------------------------------------------------------------------------
# CHANGELOG security parser
# ---------------------------------------------------------------------------


_CHANGELOG_WITH_SECURITY = """\
# Changelog

## [0.2.0]

### Added

- New thing.

### Security

- Patched CVE-XXXX-YYYY in the XML-RPC client.
- Tightened domain sandbox.

### Fixed

- Something unrelated.

## [0.1.0]

### Added

- Initial release.
"""


_CHANGELOG_NO_SECURITY = """\
# Changelog

## [0.2.0]

### Added

- New thing.

## [0.1.0]

### Added

- Initial release.
"""


def test_extract_security_present():
    body = extract_security_section(_CHANGELOG_WITH_SECURITY)
    assert body is not None
    assert "CVE-XXXX-YYYY" in body
    assert "domain sandbox" in body
    # Must not bleed into the next subsection.
    assert "unrelated" not in body


def test_extract_security_absent():
    assert extract_security_section(_CHANGELOG_NO_SECURITY) is None


def test_extract_security_no_versions():
    assert extract_security_section("# Changelog\n\nNothing here.\n") is None


def test_read_changelog_security_missing_file(tmp_path: Path):
    assert read_changelog_security(tmp_path) is None


def test_read_changelog_security_reads_file(tmp_path: Path):
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG_WITH_SECURITY, encoding="utf-8")
    body = read_changelog_security(tmp_path)
    assert body is not None
    assert "CVE-XXXX-YYYY" in body


# ---------------------------------------------------------------------------
# check_for_update — network mocked
# ---------------------------------------------------------------------------


def test_check_for_update_network_down():
    with patch("odoo_mcp.update_check.urllib.request.urlopen") as m:
        m.side_effect = urllib.error.URLError("offline")
        assert check_for_update("0.1.0") is None


def test_check_for_update_reports_newer():
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"tag_name": "v0.2.0"}'

    with patch("odoo_mcp.update_check.urllib.request.urlopen", return_value=_Resp()):
        result = check_for_update("0.1.0")
    assert result == ("0.1.0", "0.2.0")


def test_check_for_update_already_current():
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"tag_name": "0.1.0"}'

    with patch("odoo_mcp.update_check.urllib.request.urlopen", return_value=_Resp()):
        assert check_for_update("0.1.0") is None


# ---------------------------------------------------------------------------
# doctor's update check is safely no-op when the network is down
# ---------------------------------------------------------------------------


def test_doctor_update_check_network_error_is_silent(capsys: pytest.CaptureFixture[str]):
    with patch("odoo_mcp.update_check.urllib.request.urlopen") as m:
        m.side_effect = urllib.error.URLError("offline")
        doctor._print_update_check()
    out = capsys.readouterr().out
    assert "skipped" in out
    # Must not include the "Update available" nag.
    assert "Update available" not in out


def test_doctor_update_check_reports_available(capsys: pytest.CaptureFixture[str]):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"tag_name": "v99.0.0"}'

    with patch("odoo_mcp.update_check.urllib.request.urlopen", return_value=_Resp()):
        doctor._print_update_check()
    out = capsys.readouterr().out
    assert "Update available: 99.0.0" in out
