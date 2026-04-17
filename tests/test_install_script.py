"""Smoke tests for the bootstrap installer and the changelog extractor.

These tests never touch the network. They validate that the installer
script ships with the safety flags we expect and that the changelog
helper used by the release workflow picks the right section.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"
EXTRACT_SCRIPT = REPO_ROOT / "scripts" / "extract_changelog.py"


def test_install_script_exists_and_is_executable() -> None:
    assert INSTALL_SCRIPT.is_file(), f"missing: {INSTALL_SCRIPT}"
    mode = INSTALL_SCRIPT.stat().st_mode
    assert mode & 0o100, "install.sh must be executable by owner"


def test_install_script_has_safety_flags() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text, "install.sh must set -euo pipefail"
    assert "Darwin" in text, "install.sh must guard on uname Darwin"
    assert "gh auth status" in text, "install.sh must verify gh authentication"
    assert "curl -LsSf https://astral.sh/uv/install.sh" in text, (
        "install.sh must install uv via the official installer when missing"
    )
    assert "uv sync" in text
    assert "odoo-mcp setup" in text


def _load_extract_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "extract_changelog_under_test",
        EXTRACT_SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def extractor() -> object:
    return _load_extract_module()


def test_extract_version_section(extractor: object) -> None:
    changelog = (
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "- draft line\n"
        "\n"
        "## [0.2.0] - 2026-04-17\n"
        "\n"
        "### Added\n"
        "- new thing\n"
        "\n"
        "## [0.1.0] - 2026-01-01\n"
        "\n"
        "- old thing\n"
    )
    body = extractor.extract_section(changelog, "0.2.0")  # type: ignore[attr-defined]
    assert body is not None
    assert "### Added" in body
    assert "new thing" in body
    assert "old thing" not in body
    assert "draft line" not in body


def test_extract_falls_back_to_unreleased(extractor: object) -> None:
    changelog = "## [Unreleased]\n\n- pending change\n"
    assert extractor.extract_section(changelog, "9.9.9") is None  # type: ignore[attr-defined]
    body = extractor.extract_section(changelog, "Unreleased")  # type: ignore[attr-defined]
    assert body is not None
    assert "pending change" in body


def test_extract_missing_section_returns_none(extractor: object) -> None:
    changelog = "# Changelog\n\nno sections here\n"
    assert extractor.extract_section(changelog, "0.2.0") is None  # type: ignore[attr-defined]
