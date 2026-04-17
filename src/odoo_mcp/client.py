"""XML-RPC client wrapper for one Odoo instance.

This module is the only place that talks to Odoo. Everything else in the
codebase interacts with Odoo via :class:`OdooClient`, which gives us a single
place to enforce:

* TLS posture (verified default, dev-only ``allow_self_signed``).
* Per-call timeouts via a custom :class:`socket` transport.
* A fixed, minimal ``context`` dict — never forwarding caller input to Odoo.
* Translation of :mod:`xmlrpc.client` faults and transport errors into our
  typed, redaction-aware error hierarchy.
* A cached ``fields_get`` so domain/field validation doesn't do an extra
  round-trip on every call.

The class exposes only the primitives the dispatcher actually needs:
``authenticate`` / ``ensure_authenticated``, ``search_read``, ``read``,
``create``, ``write``, and ``fields_get``.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import xmlrpc.client
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import urlparse

from .config import InstanceConfig
from .credentials import Credentials
from .errors import OdooAuthError, OdooRemoteError, OdooTransportError

# The one and only context we ever pass to Odoo. Deliberately minimal — no
# active_test override, no tracking_disable, no mail.create_nolog, no company
# override. If a caller needs any of these, they should file an issue and we
# can add a vetted opt-in, not a pass-through.
_FROZEN_CONTEXT: Final[dict[str, Any]] = {"lang": "en_US"}


class _TimeoutHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection subclass with a mandatory socket timeout."""

    def __init__(self, host: str, timeout: float) -> None:
        super().__init__(host, timeout=timeout)
        self._forced_timeout = timeout

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self._forced_timeout)


class _TimeoutHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection subclass with a mandatory socket timeout and SSL context."""

    def __init__(
        self,
        host: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, timeout=timeout, context=context)
        self._forced_timeout = timeout
        self._forced_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self._forced_timeout)
        self.sock = self._forced_context.wrap_socket(sock, server_hostname=self.host)


class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport with a per-call timeout.

    Overrides ``make_connection`` to inject our timeout-aware HTTPConnection.
    The signature deliberately matches the (loose) stdlib parent: ``host``
    can be a plain hostname string OR a ``(host, headers)`` tuple, and the
    return type is ``HTTPConnection``. We narrow it via the cached field but
    mypy can't see that, hence the targeted ignores.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host: tuple[str, dict[str, str]] | str) -> http.client.HTTPConnection:
        if self._connection and host == self._connection[0]:
            cached = self._connection[1]
            assert cached is not None  # noqa: S101 — invariant of the cache
            return cached
        chost, self._extra_headers, _ = self.get_host_info(host)
        conn = _TimeoutHTTPConnection(chost, timeout=self._timeout)
        self._connection = host, conn
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """HTTPS XML-RPC transport with a per-call timeout and explicit SSL context.

    See :class:`_TimeoutTransport` for notes on the override signature.
    """

    def __init__(self, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__()
        self._timeout = timeout
        self._ssl_context = context

    def make_connection(
        self, host: tuple[str, dict[str, str]] | str
    ) -> http.client.HTTPSConnection:
        if self._connection and host == self._connection[0]:
            cached = self._connection[1]
            assert isinstance(cached, http.client.HTTPSConnection)  # noqa: S101
            return cached
        chost, self._extra_headers, _ = self.get_host_info(host)
        conn = _TimeoutHTTPSConnection(chost, timeout=self._timeout, context=self._ssl_context)
        self._connection = host, conn
        return conn


def _build_ssl_context(allow_self_signed: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if allow_self_signed:
        # Dev-only escape hatch. Prod was already rejected in config.py.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class OdooClient:
    """Thin wrapper around ``xmlrpc.client`` for one Odoo instance.

    Instances are constructed by :mod:`odoo_mcp.server` at startup, one per
    configured instance. Authentication is deferred to the first tool call
    via :meth:`ensure_authenticated` so that one unreachable instance does
    not block the entire MCP process from starting.
    """

    def __init__(
        self,
        instance: InstanceConfig,
        credentials: Credentials | None = None,
        *,
        credential_loader: Callable[[], Credentials] | None = None,
    ) -> None:
        if credentials is None and credential_loader is None:
            raise OdooAuthError(
                f"OdooClient for {instance.name!r} requires either credentials "
                f"or credential_loader."
            )
        self._instance = instance
        self._credentials: Credentials | None = credentials
        self._credential_loader = credential_loader
        self._credential_lock = threading.Lock()
        self._ssl_context = _build_ssl_context(instance.allow_self_signed)
        self._fields_cache: dict[str, dict[str, dict[str, Any]]] = {}

        parsed = urlparse(instance.url)
        if parsed.scheme not in ("http", "https"):
            raise OdooTransportError(f"Unsupported URL scheme: {parsed.scheme!r}")

        self._common = self._make_proxy(f"{instance.url}/xmlrpc/2/common")
        self._object = self._make_proxy(f"{instance.url}/xmlrpc/2/object")
        self._uid: int | None = None
        self._auth_lock = threading.Lock()

    # --- Construction / auth ------------------------------------------------

    def _get_credentials(self) -> Credentials:
        """Return the cached :class:`Credentials`, loading them lazily on first use.

        Thread-safe. If construction was given a ``credential_loader`` closure
        rather than a concrete :class:`Credentials`, the loader is invoked here
        and its result cached for the lifetime of the client. Any exception
        raised by the loader propagates to the caller — a broken credential
        config for one instance then only fails when that instance is actually
        touched, not at process startup.
        """
        cached = self._credentials
        if cached is not None:
            return cached
        with self._credential_lock:
            cached = self._credentials
            if cached is not None:
                return cached
            if self._credential_loader is None:
                raise OdooAuthError(
                    f"OdooClient for {self._instance.name!r} has no credentials "
                    f"and no loader configured."
                )
            loaded = self._credential_loader()
            self._credentials = loaded
            return loaded

    def _make_proxy(self, url: str) -> xmlrpc.client.ServerProxy:
        parsed = urlparse(url)
        if parsed.scheme == "https":
            transport: xmlrpc.client.Transport = _TimeoutSafeTransport(
                timeout=float(self._instance.timeout_seconds),
                context=self._ssl_context,
            )
        else:
            transport = _TimeoutTransport(timeout=float(self._instance.timeout_seconds))
        return xmlrpc.client.ServerProxy(url, transport=transport, allow_none=True)

    def authenticate(self) -> int:
        """Perform the Odoo authenticate call and cache the resulting uid.

        Raises :class:`OdooAuthError` on bad credentials and
        :class:`OdooTransportError` on network / TLS / timeout errors.
        """
        creds = self._get_credentials()
        try:
            uid = self._common.authenticate(
                self._instance.database,
                creds.username,
                creds.reveal_for_rpc(),
                {},
            )
        except xmlrpc.client.Fault as exc:
            raise OdooAuthError(
                f"Odoo rejected authentication for instance {self._instance.name!r}: "
                f"{exc.faultString}"
            ) from exc
        except (OSError, ssl.SSLError, TimeoutError) as exc:
            raise OdooTransportError(
                f"Transport error authenticating against {self._instance.name!r}: {exc}"
            ) from exc

        if not uid:
            raise OdooAuthError(
                f"Authentication against {self._instance.name!r} returned no uid. "
                f"Check the username, API key, and database name."
            )
        if not isinstance(uid, int):
            raise OdooAuthError(f"Unexpected authenticate() return type: {type(uid).__name__}")
        self._uid = uid
        return uid

    def ensure_authenticated(self) -> None:
        """Authenticate lazily on first use. Thread-safe and idempotent.

        If already authenticated (uid is set), returns immediately.
        Otherwise calls :meth:`authenticate` once, guarded by a lock.

        Raises :class:`OdooAuthError` or :class:`OdooTransportError` if
        authentication fails.
        """
        if self._uid is not None:
            return
        self._do_lazy_auth()

    def _do_lazy_auth(self) -> None:
        """Lock-guarded authenticate with double-check for thread safety."""
        with self._auth_lock:
            if self._uid is not None:
                return
            self.authenticate()

    @property
    def uid(self) -> int:
        if self._uid is None:
            raise OdooAuthError(
                f"Client for {self._instance.name!r} has not been authenticated yet."
            )
        return self._uid

    # --- Odoo operations (all go through execute_kw with a tight allowlist) -

    def fields_get(self, model: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        """Return ``fields_get`` for ``model``, cached per-process.

        The cache is important: domain validation calls this on every request,
        and Odoo's ``fields_get`` is a non-trivial query.
        """
        if use_cache and model in self._fields_cache:
            return self._fields_cache[model]
        result = self._execute(
            model,
            "fields_get",
            [],
            {"attributes": ["type", "string", "required", "readonly", "help", "relation"]},
        )
        if not isinstance(result, dict):
            raise OdooRemoteError(
                f"fields_get for {model!r} returned unexpected type {type(result).__name__}"
            )
        self._fields_cache[model] = result
        return result

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int,
        offset: int,
        order: str | None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit, "offset": offset}
        if order:
            kwargs["order"] = order
        result = self._execute(model, "search_read", [domain], kwargs)
        if not isinstance(result, list):
            raise OdooRemoteError(
                f"search_read({model!r}) returned unexpected type {type(result).__name__}"
            )
        return result

    def search_count(self, model: str, domain: list[Any]) -> int:
        result = self._execute(model, "search_count", [domain], {})
        if isinstance(result, bool) or not isinstance(result, int):
            raise OdooRemoteError(
                f"search_count({model!r}) returned unexpected type {type(result).__name__}"
            )
        int_result: int = result
        return int_result

    def read_group(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        groupby: list[str],
        *,
        limit: int | None,
        offset: int,
        orderby: str | None,
        lazy: bool,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"offset": offset, "lazy": lazy}
        if limit is not None:
            kwargs["limit"] = limit
        if orderby:
            kwargs["orderby"] = orderby
        result = self._execute(model, "read_group", [domain, fields, groupby], kwargs)
        if not isinstance(result, list):
            raise OdooRemoteError(
                f"read_group({model!r}) returned unexpected type {type(result).__name__}"
            )
        return result

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        result = self._execute(model, "read", [ids], {"fields": fields})
        if not isinstance(result, list):
            raise OdooRemoteError(
                f"read({model!r}) returned unexpected type {type(result).__name__}"
            )
        return result

    def create(self, model: str, values: dict[str, Any]) -> int:
        result = self._execute(model, "create", [values], {})
        if not isinstance(result, int):
            raise OdooRemoteError(
                f"create({model!r}) returned unexpected type {type(result).__name__}"
            )
        return result

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        result = self._execute(model, "write", [ids, values], {})
        return bool(result)

    # --- Internal ----------------------------------------------------------

    def _execute(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        """Single chokepoint for every Odoo call.

        This is the ONLY place that calls ``execute_kw``. It enforces the
        fixed context and translates errors. The dispatcher is responsible
        for ensuring ``method`` is one of the allowlisted operations (see
        :mod:`odoo_mcp.security.allowlist`) — this client itself does not
        expose ``execute_kw`` to callers.
        """
        merged_kwargs = dict(kwargs)
        merged_kwargs["context"] = dict(_FROZEN_CONTEXT)
        creds = self._get_credentials()
        try:
            return self._object.execute_kw(
                self._instance.database,
                self.uid,
                creds.reveal_for_rpc(),
                model,
                method,
                args,
                merged_kwargs,
            )
        except xmlrpc.client.Fault as exc:
            raise OdooRemoteError(f"Odoo fault on {model}.{method}: {exc.faultString}") from exc
        except TimeoutError as exc:
            raise OdooTransportError(
                f"Timeout calling {model}.{method} on {self._instance.name!r} "
                f"after {self._instance.timeout_seconds}s"
            ) from exc
        except ssl.SSLError as exc:
            raise OdooTransportError(
                f"TLS error calling {model}.{method} on {self._instance.name!r}: {exc}"
            ) from exc
        except OSError as exc:
            raise OdooTransportError(
                f"Network error calling {model}.{method} on {self._instance.name!r}: {exc}"
            ) from exc
