"""Per-instance ``ca_file``: verified TLS for an Odoo behind an internal CA.

Before ``ca_file`` TLS was binary — the system trust store, or (dev only)
``allow_self_signed`` with verification OFF. A production Odoo signed by a
company-internal CA had no supported path, and a dev instance on one was
pushed to turn verification off. These tests pin that ``ca_file`` keeps
verification ON, trusts only the listed CA, is proven loadable at
config-load time, and reaches every place that opens a TLS connection.
"""

from __future__ import annotations

import datetime
import os
import re
import socket
import ssl
import threading
from pathlib import Path

import pytest

from odoo_mcp import config_cli, setup_wizard
from odoo_mcp.client import _build_ssl_context
from odoo_mcp.config import load_config
from odoo_mcp.errors import ConfigError

x509 = pytest.importorskip("cryptography.x509")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

# --- fixtures --------------------------------------------------------------


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A throwaway internal CA plus a ``localhost`` leaf it signed."""
    d = tmp_path_factory.mktemp("pki")
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("odoo-mcp test internal CA"))
        .issuer_name(_name("odoo-mcp test internal CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    ca_path = d / "internal-ca.pem"
    ca_path.write_bytes(ca_cert.public_bytes(pem))
    leaf_path = d / "leaf.pem"
    leaf_path.write_bytes(leaf_cert.public_bytes(pem))
    key_path = d / "leaf.key"
    key_path.write_bytes(
        leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return {"ca": ca_path, "leaf": leaf_path, "key": key_path}


def _write_cfg(tmp_path: Path, extra: str, *, production: bool = False) -> Path:
    body = f"""
[instances.box]
url = "https://odoo.internal.example"
database = "db"
credentials_env_prefix = "ODOO_MCP_BOX"
production = {"true" if production else "false"}
{extra}
"""
    path = tmp_path / "config.toml"
    path.write_text(body)
    if os.name == "posix":
        path.chmod(0o600)
    return path


# --- config load -----------------------------------------------------------


def test_ca_file_defaults_to_system_store(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path, ""))
    assert cfg.instances["box"].ca_file == ""


def test_ca_file_is_stored_as_absolute_path(tmp_path: Path, pki: dict[str, Path]) -> None:
    cfg = load_config(_write_cfg(tmp_path, f'ca_file = "{pki["ca"]}"'))
    assert cfg.instances["box"].ca_file == str(pki["ca"])


def test_ca_file_expands_home(
    tmp_path: Path, pki: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(pki["ca"].parent))
    cfg = load_config(_write_cfg(tmp_path, 'ca_file = "~/internal-ca.pem"'))
    assert cfg.instances["box"].ca_file == str(pki["ca"])


def test_ca_file_is_allowed_on_production(tmp_path: Path, pki: dict[str, Path]) -> None:
    """The whole point: prod on an internal CA, with verification ON."""
    cfg = load_config(_write_cfg(tmp_path, f'ca_file = "{pki["ca"]}"', production=True))
    assert cfg.instances["box"].production is True
    assert cfg.instances["box"].ca_file == str(pki["ca"])


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ('"relative/ca.pem"', "absolute path"),
        ('"/nonexistent/odoo-mcp/ca.pem"', "does not exist"),
        ("42", "must be a string"),
        ('["/etc/ssl/cert.pem"]', "must be a string"),
    ],
)
def test_ca_file_bad_values_refuse_at_load(tmp_path: Path, value: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(_write_cfg(tmp_path, f"ca_file = {value}"))


def test_ca_file_that_is_not_pem_refuses_at_load(tmp_path: Path) -> None:
    junk = tmp_path / "not-a-ca.pem"
    junk.write_text("this is not a certificate\n")
    with pytest.raises(ConfigError, match="not a readable PEM CA bundle"):
        load_config(_write_cfg(tmp_path, f'ca_file = "{junk}"'))


def test_ca_file_and_allow_self_signed_are_exclusive(tmp_path: Path, pki: dict[str, Path]) -> None:
    with pytest.raises(ConfigError, match="both ca_file and allow_self_signed"):
        load_config(_write_cfg(tmp_path, f'ca_file = "{pki["ca"]}"\nallow_self_signed = true'))


# --- the TLS context --------------------------------------------------------


def test_context_with_ca_file_verifies_and_trusts_only_that_ca(pki: dict[str, Path]) -> None:
    ctx = _build_ssl_context(False, str(pki["ca"]))
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    # Replace semantics (curl --cacert): the system store is NOT loaded.
    subjects = [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()]
    assert subjects == [{"commonName": "odoo-mcp test internal CA"}]


def test_context_without_ca_file_is_unchanged() -> None:
    ctx = _build_ssl_context(False)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def _serve_once(pki: dict[str, Path]) -> tuple[int, threading.Thread]:
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(pki["leaf"], pki["key"])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def _run() -> None:
        with sock:
            conn, _ = sock.accept()
            try:
                with server_ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass  # the refusing client aborts the handshake

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return port, t


def _handshake(ctx: ssl.SSLContext, port: int) -> None:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        ctx.wrap_socket(raw, server_hostname="localhost"),
    ):
        pass


def test_handshake_succeeds_against_internal_ca(pki: dict[str, Path]) -> None:
    port, t = _serve_once(pki)
    _handshake(_build_ssl_context(False, str(pki["ca"])), port)
    t.join(5)


def test_handshake_without_ca_file_still_refuses_internal_ca(pki: dict[str, Path]) -> None:
    """The default posture is untouched: an unknown CA is still refused."""
    port, t = _serve_once(pki)
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(_build_ssl_context(False), port)
    t.join(5)


def test_handshake_with_ca_file_still_checks_hostname(pki: dict[str, Path]) -> None:
    """Trusting the CA must not mean trusting any name it signed."""
    port, t = _serve_once(pki)
    ctx = _build_ssl_context(False, str(pki["ca"]))
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        pytest.raises(ssl.SSLCertVerificationError),
    ):
        ctx.wrap_socket(raw, server_hostname="odoo.other.example")
    t.join(5)


# --- other consumers ---------------------------------------------------------


def out(capsys: pytest.CaptureFixture[str]) -> str:
    return "\n".join(line.strip() for line in capsys.readouterr().out.splitlines())


def test_config_show_reports_ca_file(
    tmp_path: Path,
    pki: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_cfg(tmp_path, f'ca_file = "{pki["ca"]}"')
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: load_config(path))
    monkeypatch.setattr("odoo_mcp.config_cli._keychain_get", lambda _n, _s: "x")
    assert config_cli.main(["show"]) == 0
    # Whitespace-tolerant: open PR #43 reflows this block through ``_kv``.
    assert re.search(rf"^ca_file:\s+{re.escape(str(pki['ca']))}$", out(capsys), re.M)


def test_config_show_reports_system_store_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_cfg(tmp_path, "")
    monkeypatch.setattr("odoo_mcp.config_cli.load_config", lambda _p=None: load_config(path))
    monkeypatch.setattr("odoo_mcp.config_cli._keychain_get", lambda _n, _s: "x")
    assert config_cli.main(["show"]) == 0
    assert re.search(r"^ca_file:\s+\(system trust store\)$", out(capsys), re.M)


def test_password_key_generation_verifies_against_ca_file(
    pki: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """renew-key sends a password: it must use the CA, never turn verify off."""
    seen: list[object] = []
    real = ssl.create_default_context

    def _spy(*a: object, **kw: object) -> ssl.SSLContext:
        seen.append(kw.get("cafile"))
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(ssl, "create_default_context", _spy)

    class _Stop(Exception):
        pass

    def _no_network(*_a: object, **_kw: object) -> object:
        raise _Stop

    monkeypatch.setattr("urllib.request.build_opener", _no_network)
    with pytest.raises(_Stop):
        setup_wizard._generate_api_key_via_password(
            "https://localhost", "db", "u", "pw", "k", ca_file=str(pki["ca"])
        )
    assert seen == [str(pki["ca"])]


def test_password_key_generation_bad_ca_file_is_a_readable_error() -> None:
    with pytest.raises(setup_wizard._KeyGenError, match="Could not load ca_file"):
        setup_wizard._generate_api_key_via_password(
            "https://localhost", "db", "u", "pw", "k", ca_file="/nonexistent/ca.pem"
        )
