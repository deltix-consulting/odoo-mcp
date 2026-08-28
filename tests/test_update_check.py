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


# The shape this repo's own CHANGELOG.md actually has: an empty
# ``## [Unreleased]`` sitting on top of the release being described.
# Every release tag ships it, so this — not the fixtures above — is what
# the parser meets in production.
_CHANGELOG_WITH_UNRELEASED_ON_TOP = """\
# Changelog

## [Unreleased]

## [0.27.0] - 2026-08-20

### Changed

- Something.

### Security

- Patched CVE-XXXX-YYYY in the XML-RPC client.

## [0.26.0] - 2026-06-16

### Added

- Older thing.
"""


_CHANGELOG_SPANNING_RELEASES = """\
# Changelog

## [Unreleased]

## [0.27.0] - 2026-08-20

### Added

- No security note in this one.

## [0.26.0] - 2026-06-16

### Security

- Middle-release fix.

## [0.25.0] - 2026-06-16

### Security

- Oldest-release fix.

## [0.24.0] - 2026-06-15

### Security

- Already installed, must not be reported.
"""


def test_extract_security_skips_empty_unreleased_header():
    """An empty ``[Unreleased]`` on top must not mask the release below it."""
    body = extract_security_section(_CHANGELOG_WITH_UNRELEASED_ON_TOP)
    assert body is not None
    assert "CVE-XXXX-YYYY" in body


def test_extract_security_reports_every_release_in_the_span():
    """Updating across several releases reports all of their Security notes."""
    body = extract_security_section(_CHANGELOG_SPANNING_RELEASES, since_version="0.25.0")
    assert body is not None
    assert "Middle-release fix" in body
    assert "Oldest-release fix" not in body
    assert "Already installed" not in body


def test_extract_security_includes_unreleased_content():
    """Landing on the branch tip installs ``[Unreleased]`` — report it."""
    text = _CHANGELOG_WITH_UNRELEASED_ON_TOP.replace(
        "## [Unreleased]\n",
        "## [Unreleased]\n\n### Security\n\n- Tip-only fix.\n",
    )
    body = extract_security_section(text, since_version="0.27.0")
    assert body is not None
    assert "Tip-only fix" in body
    # 0.27.0 is not newer than the version we came from.
    assert "CVE-XXXX-YYYY" not in body


def test_extract_security_dev_version_falls_back_to_newest_release():
    """The ``dev`` sentinel is unparseable — show the notice, don't withhold it."""
    body = extract_security_section(_CHANGELOG_WITH_UNRELEASED_ON_TOP, since_version="dev")
    assert body is not None
    assert "CVE-XXXX-YYYY" in body


def test_extract_security_real_repo_changelog_reports_0_27_0():
    """Guard against regressing on the file actually shipped in this repo."""
    repo_changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    body = extract_security_section(
        repo_changelog.read_text(encoding="utf-8"), since_version="0.26.0"
    )
    assert body is not None


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
    with (
        patch("odoo_mcp.update_check.shutil.which", return_value=None),
        patch("odoo_mcp.update_check.urllib.request.urlopen") as m,
    ):
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

    with (
        patch("odoo_mcp.update_check.shutil.which", return_value=None),
        patch("odoo_mcp.update_check.urllib.request.urlopen", return_value=_Resp()),
    ):
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

    with (
        patch("odoo_mcp.update_check.shutil.which", return_value=None),
        patch("odoo_mcp.update_check.urllib.request.urlopen", return_value=_Resp()),
    ):
        assert check_for_update("0.1.0") is None


# ---------------------------------------------------------------------------
# gh CLI path is preferred over anonymous urllib
# ---------------------------------------------------------------------------


def test_check_for_update_uses_gh_when_available():
    """When `gh` is on PATH and returns a tag, urllib must not be called.

    This is the whole point of the gh-first path: avoid the unauth GitHub
    rate limit that turned attestation verification into "press y" theater.
    """

    class _Result:
        returncode = 0
        stdout = "v0.2.0\n"

    with (
        patch("odoo_mcp.update_check.shutil.which", return_value="/usr/bin/gh"),
        patch("odoo_mcp.update_check.subprocess.run", return_value=_Result()) as gh,
        patch("odoo_mcp.update_check.urllib.request.urlopen") as urlopen,
    ):
        result = check_for_update("0.1.0")
    assert result == ("0.1.0", "0.2.0")
    assert gh.called
    assert not urlopen.called


def test_check_for_update_falls_back_when_gh_fails():
    """If gh is on PATH but returns non-zero, fall back to urllib."""

    class _Result:
        returncode = 1
        stdout = ""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"tag_name": "v0.2.0"}'

    with (
        patch("odoo_mcp.update_check.shutil.which", return_value="/usr/bin/gh"),
        patch("odoo_mcp.update_check.subprocess.run", return_value=_Result()),
        patch("odoo_mcp.update_check.urllib.request.urlopen", return_value=_Resp()),
    ):
        result = check_for_update("0.1.0")
    assert result == ("0.1.0", "0.2.0")


# ---------------------------------------------------------------------------
# doctor's update check is safely no-op when the network is down
# ---------------------------------------------------------------------------


def test_doctor_update_check_network_error_is_silent(capsys: pytest.CaptureFixture[str]):
    with (
        patch("odoo_mcp.update_check.shutil.which", return_value=None),
        patch("odoo_mcp.update_check.urllib.request.urlopen") as m,
    ):
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

    with (
        patch("odoo_mcp.update_check.shutil.which", return_value=None),
        patch("odoo_mcp.update_check.urllib.request.urlopen", return_value=_Resp()),
    ):
        doctor._print_update_check()
    out = capsys.readouterr().out
    assert "Update available: 99.0.0" in out
