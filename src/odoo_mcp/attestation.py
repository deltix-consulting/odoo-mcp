"""Verification of GitHub Actions Build Provenance Attestations.

Release artifacts produced by ``.github/workflows/release.yml`` are signed
via Sigstore and the attestation is published to GitHub's transparency log.
End users (and the self-update flow) can verify that a given tarball was
actually built by our CI — not a tampered repo or a typosquatted tag — by
running ``gh attestation verify`` against the artifact.

This module wraps that verification in a Python helper that the
``odoo-mcp update`` command calls before applying changes. It downloads
the release tarball from GitHub, asks ``gh`` to verify its attestation
against our release workflow, and returns a boolean plus a human-readable
reason string.

Failure modes are split into two buckets:

- **Hard failures** (``verified=False`` with a non-environmental reason):
  ``gh`` ran successfully but verification was rejected — the artifact is
  not signed by our workflow, or the signature is invalid. Updates MUST
  refuse to proceed.
- **Environmental issues** (``verified=False`` with reason starting
  ``"environment:"``): ``gh`` is missing, the network is down, or
  GitHub returned an error unrelated to the artifact. Update_cli treats
  these as warnings and prompts the user.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_REPO = "deltix-consulting/odoo-mcp"
_RELEASE_WORKFLOW = ".github/workflows/release.yml"
_HTTP_TIMEOUT_SECONDS = 30.0


def _normalize_version(version: str) -> str:
    """Strip a leading ``v``/``V`` from a release tag."""
    if version.startswith(("v", "V")):
        return version[1:]
    return version


def _tarball_url(repo: str, tag: str) -> str:
    """Build the public download URL for the sdist on a GitHub release."""
    version = _normalize_version(tag)
    tag_with_v = tag if tag.startswith(("v", "V")) else f"v{tag}"
    filename = f"odoo_mcp-{version}.tar.gz"
    return f"https://github.com/{repo}/releases/download/{tag_with_v}/{filename}"


def _download(url: str, dest: Path) -> None:
    """Download *url* to *dest*. Raises ``urllib.error.URLError`` on failure."""
    req = urllib.request.Request(url)  # noqa: S310 — https URL built from inputs
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
        dest.write_bytes(resp.read())


def _run_gh_verify(artifact: Path, owner: str) -> subprocess.CompletedProcess[str]:
    """Run ``gh attestation verify`` on *artifact*. May raise ``FileNotFoundError``."""
    argv = [
        "gh",  # noqa: S607 — gh is intentionally resolved via PATH; absolute path varies by host
        "attestation",
        "verify",
        "--owner",
        owner,
        "--signer-workflow",
        _RELEASE_WORKFLOW,
        str(artifact),
    ]
    return subprocess.run(  # noqa: S603 — argv list, no shell
        argv,
        check=False,
        capture_output=True,
        text=True,
    )


def verify_release_attestation(
    version: str,
    repo: str = _DEFAULT_REPO,
) -> tuple[bool, str]:
    """Download a release tarball and verify its build provenance attestation.

    Returns ``(verified, reason)``. ``verified=True`` means ``gh`` confirmed
    the artifact was produced by our release workflow. ``verified=False``
    with a reason prefixed ``"environment:"`` means the verification could
    not be performed (no ``gh``, no network, etc.) — the caller should
    treat that as a soft warning. Any other ``verified=False`` is a hard
    failure: ``gh`` ran but rejected the artifact.
    """
    if shutil.which("gh") is None:
        return (False, "environment: gh CLI not found on PATH")

    owner = repo.split("/", 1)[0] if "/" in repo else repo
    url = _tarball_url(repo, version)

    with tempfile.TemporaryDirectory(prefix="odoo-mcp-verify-") as tmp:
        artifact = Path(tmp) / Path(url).name
        try:
            _download(url, artifact)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return (False, f"environment: download failed ({exc})")

        try:
            result = _run_gh_verify(artifact, owner)
        except FileNotFoundError:
            return (False, "environment: gh CLI not found on PATH")
        except OSError as exc:
            return (False, f"environment: gh invocation failed ({exc})")

    if result.returncode == 0:
        return (True, f"verified against {_RELEASE_WORKFLOW}")

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or f"exit code {result.returncode}"
    return (False, f"verification failed: {detail}")
