"""Tests for the build-provenance attestation verifier and update_cli wiring."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from odoo_mcp import attestation, update_cli


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# verify_release_attestation
# ---------------------------------------------------------------------------


def test_verify_called_with_correct_args():
    """The gh command line must include owner, signer-workflow, and the artifact path."""
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _completed(0, stdout="ok")

    with (
        patch("odoo_mcp.attestation.shutil.which", return_value="/usr/local/bin/gh"),
        patch("odoo_mcp.attestation._download"),
        patch("odoo_mcp.attestation.subprocess.run", side_effect=fake_run),
    ):
        verified, _reason = attestation.verify_release_attestation(
            "v0.6.0", repo="deltix-consulting/odoo-mcp"
        )

    assert verified is True
    argv = captured["argv"]
    assert argv[0] == "gh"
    assert argv[1] == "attestation"
    assert argv[2] == "verify"
    assert "--owner" in argv
    assert argv[argv.index("--owner") + 1] == "deltix-consulting"
    assert "--signer-workflow" in argv
    assert argv[argv.index("--signer-workflow") + 1] == ".github/workflows/release.yml"
    # Last arg is the artifact path, must reference the tarball name.
    assert argv[-1].endswith("odoo_mcp-0.6.0.tar.gz")


def test_verify_returns_true_on_zero_exit():
    with (
        patch("odoo_mcp.attestation.shutil.which", return_value="/usr/local/bin/gh"),
        patch("odoo_mcp.attestation._download"),
        patch("odoo_mcp.attestation.subprocess.run", return_value=_completed(0, stdout="Verified")),
    ):
        verified, reason = attestation.verify_release_attestation("v0.6.0")
    assert verified is True
    assert "release.yml" in reason


def test_verify_returns_false_on_nonzero_exit_with_reason():
    with (
        patch("odoo_mcp.attestation.shutil.which", return_value="/usr/local/bin/gh"),
        patch("odoo_mcp.attestation._download"),
        patch(
            "odoo_mcp.attestation.subprocess.run",
            return_value=_completed(1, stderr="signature mismatch"),
        ),
    ):
        verified, reason = attestation.verify_release_attestation("v0.6.0")
    assert verified is False
    assert reason.startswith("verification failed")
    assert "signature mismatch" in reason


def test_verify_returns_false_when_gh_missing():
    with patch("odoo_mcp.attestation.shutil.which", return_value=None):
        verified, reason = attestation.verify_release_attestation("v0.6.0")
    assert verified is False
    assert reason.startswith("environment:")
    assert "gh CLI" in reason


def test_verify_returns_false_when_gh_subprocess_raises_filenotfound():
    """Race: gh disappeared between which() and subprocess.run."""
    with (
        patch("odoo_mcp.attestation.shutil.which", return_value="/usr/local/bin/gh"),
        patch("odoo_mcp.attestation._download"),
        patch("odoo_mcp.attestation.subprocess.run", side_effect=FileNotFoundError("gh")),
    ):
        verified, reason = attestation.verify_release_attestation("v0.6.0")
    assert verified is False
    assert reason.startswith("environment:")


def test_verify_returns_false_on_download_failure():
    import urllib.error

    with (
        patch("odoo_mcp.attestation.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "odoo_mcp.attestation._download",
            side_effect=urllib.error.URLError("offline"),
        ),
    ):
        verified, reason = attestation.verify_release_attestation("v0.6.0")
    assert verified is False
    assert reason.startswith("environment:")
    assert "download failed" in reason


# ---------------------------------------------------------------------------
# update_cli integration
# ---------------------------------------------------------------------------


def _stub_update_preconditions(monkeypatch: pytest.MonkeyPatch, project_dir: Any) -> MagicMock:
    """Patch enough of update_cli that main() reaches the verification gate.

    Returns a MagicMock standing in for ``_git`` so individual tests can
    assert whether ``git pull`` was reached.
    """
    monkeypatch.setattr(update_cli, "_find_project_dir", lambda: project_dir)
    monkeypatch.setattr(update_cli, "_has_local_changes", lambda _p: False)
    monkeypatch.setattr(update_cli, "_current_branch", lambda _p: "main")
    monkeypatch.setattr(update_cli, "_current_commit", lambda _p: "aaa")
    monkeypatch.setattr(update_cli, "_upstream_commit", lambda _p, _b: "bbb")
    monkeypatch.setattr(update_cli, "_confirm", lambda _msg: True)

    git_mock = MagicMock()

    def fake_git(
        _project_dir: Any, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        git_mock(*args, check=check)
        # Return empty output for fetch / log; pull/sync should never be hit.
        return _completed(0)

    monkeypatch.setattr(update_cli, "_git", fake_git)
    return git_mock


def test_update_refuses_on_verification_failure(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    git_mock = _stub_update_preconditions(monkeypatch, tmp_path)
    monkeypatch.setattr(
        update_cli,
        "verify_release_attestation",
        lambda _v, repo="deltix-consulting/odoo-mcp": (False, "verification failed: bad sig"),
    )
    monkeypatch.setattr(update_cli, "fetch_latest_tag", lambda: "v0.6.0")

    rc = update_cli.main([])
    assert rc == 1

    err = capsys.readouterr().err
    assert "Refusing update" in err

    # git pull must NOT have been called.
    pull_calls = [c for c in git_mock.call_args_list if "pull" in c.args]
    assert pull_calls == []


def test_update_proceeds_with_skip_verification_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    _stub_update_preconditions(monkeypatch, tmp_path)

    verifier_called = {"called": False}

    def fake_verify(*_args: Any, **_kw: Any) -> tuple[bool, str]:
        verifier_called["called"] = True
        return (False, "verification failed: should not be reached")

    monkeypatch.setattr(update_cli, "verify_release_attestation", fake_verify)
    # Stub _run so uv sync / pytest don't actually execute.
    monkeypatch.setattr(update_cli, "_run", lambda _cmd, cwd: _completed(0))
    monkeypatch.setattr(update_cli, "read_changelog_security", lambda _p: None)

    # Stub doctor.main to a no-op so we don't drag in real doctor logic.
    import odoo_mcp.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "main", lambda _args: 0)

    rc = update_cli.main(["--skip-verification"])
    assert rc == 0
    assert verifier_called["called"] is False

    out = capsys.readouterr().out
    assert "Skipping attestation verification" in out
