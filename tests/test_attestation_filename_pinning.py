"""Pin the relationship between ``pyproject.toml`` and the attestation
tarball filename.

``odoo_mcp.attestation._tarball_url`` builds the GitHub release download URL
as ``odoo_mcp-{version}.tar.gz`` — note the underscore. ``pip``/``uv``
normalize hyphens in package names to underscores when producing sdist
filenames, so ``project.name = "odoo-mcp"`` produces ``odoo_mcp-X.Y.Z.tar.gz``.

A rename of ``project.name`` (or a switch to a different normalization
scheme) would silently break ``odoo-mcp update``: the verifier would
download the wrong file (or none) and reject the install. This test fails
fast in CI if anyone ever changes the package name without remembering to
update the attestation module accordingly.

This is a defensive regression test — no code change in the attestation
module is required.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from odoo_mcp.attestation import _tarball_url


def _project_name() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(raw["project"]["name"])


def test_attestation_filename_matches_pyproject_normalization() -> None:
    """The URL builder must produce ``<normalized_name>-<version>.tar.gz``."""
    name = _project_name()
    # pip/uv sdist normalization: hyphens become underscores. If the package
    # is ever renamed, update this assertion AND the attestation module.
    expected_stem = name.replace("-", "_")
    url = _tarball_url("deltix-consulting/odoo-mcp", "1.2.3")
    assert url.endswith(f"/{expected_stem}-1.2.3.tar.gz"), (
        f"Attestation URL {url!r} does not match the package name in pyproject.toml. "
        f"If you renamed the project, update odoo_mcp.attestation._tarball_url."
    )


def test_attestation_filename_uses_underscore_for_odoo_mcp() -> None:
    """Belt-and-braces: pin the literal expected filename for the current name."""
    assert _project_name() == "odoo-mcp", (
        "Package name changed in pyproject.toml — update the attestation tarball "
        "filename in odoo_mcp.attestation._tarball_url and this test."
    )
    url = _tarball_url("deltix-consulting/odoo-mcp", "0.8.0")
    assert url.endswith("/odoo_mcp-0.8.0.tar.gz")


def test_attestation_filename_strips_v_prefix() -> None:
    """``v`` tags must produce a version-only filename."""
    url = _tarball_url("deltix-consulting/odoo-mcp", "v0.8.0")
    assert url.endswith("/odoo_mcp-0.8.0.tar.gz")
    assert "/v0.8.0/" in url, "Tag path component should retain the leading 'v'."
