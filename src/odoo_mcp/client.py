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
import logging
import re
import socket
import ssl
import threading
import xmlrpc.client
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import urlparse

from .config import InstanceConfig
from .credentials import Credentials
from .errors import OdooAuthError, OdooRemoteError, OdooTransportError
from .fields_cache import PersistentFieldsCache

logger = logging.getLogger(__name__)

# The one and only context we ever pass to Odoo. Deliberately minimal — no
# active_test override, no tracking_disable, no mail.create_nolog, no company
# override. If a caller needs any of these, they should file an issue and we
# can add a vetted opt-in, not a pass-through.
_FROZEN_CONTEXT: Final[dict[str, Any]] = {"lang": "en_US"}

# Default cap for the per-process L1 fields cache. A long-running MCP server
# that touches many distinct models (an Odoo with hundreds of installed
# modules) would otherwise grow ``_fields_cache`` without bound. 64 covers
# typical interactive sessions; bump via constructor kwarg if needed.
_DEFAULT_FIELDS_CACHE_MAX_SIZE: Final[int] = 64


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


# Matches a Python traceback stack-frame line, e.g.
#   File "/usr/lib/python3/.../models.py", line 1234, in check_field_access
_TRACEBACK_FRAME_RE: Final[re.Pattern[str]] = re.compile(r'^\s+File ".*", line \d+')


def _summarize_odoo_fault(fault_string: str) -> str:
    """Reduce an Odoo XML-RPC fault to its actionable exception summary.

    Odoo returns the *entire* server-side Python traceback in
    :attr:`xmlrpc.client.Fault.faultString` — routinely 20-40 lines of
    internal Odoo file paths. For an MCP client that is pure noise: it
    buries the one actionable line, costs tokens on every failed call,
    and needlessly discloses the Odoo server's filesystem layout.

    If ``fault_string`` is a Python traceback, return only the trailing
    exception summary — everything after the last stack frame, which
    keeps a multi-line exception message intact. If it is not a
    traceback (some Odoo faults are already a single line) it is
    returned stripped and otherwise unchanged. The transform is
    best-effort: any input that does not match the expected traceback
    shape falls through to the stripped original, so a fault is never
    lost.
    """
    text = fault_string.strip()
    if "Traceback (most recent call last):" not in text:
        return text
    lines = text.splitlines()
    last_frame = -1
    for i, line in enumerate(lines):
        if _TRACEBACK_FRAME_RE.match(line):
            last_frame = i
    if last_frame < 0:
        return text
    # Everything after the last stack frame is the exception summary.
    # Skip the indented source-code echo Python prints under the frame;
    # stop at the first non-indented line (the exception type + message).
    tail = lines[last_frame + 1 :]
    start = 0
    while start < len(tail) and (not tail[start].strip() or tail[start].startswith((" ", "\t"))):
        start += 1
    summary = "\n".join(tail[start:]).strip()
    return summary or text


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
        fields_cache: PersistentFieldsCache | None = None,
        fields_cache_max_size: int = _DEFAULT_FIELDS_CACHE_MAX_SIZE,
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
        if fields_cache_max_size <= 0:
            raise ValueError("fields_cache_max_size must be > 0")
        self._fields_cache_max_size = fields_cache_max_size
        # Bounded LRU keyed by model name. OrderedDict preserves insertion
        # order; ``move_to_end`` on hit promotes recency, ``popitem(last=False)``
        # evicts the least-recently-used entry on overflow.
        self._fields_cache: OrderedDict[str, dict[str, dict[str, Any]]] = OrderedDict()
        self._fields_lock = threading.Lock()
        # Optional L2 SQLite cache shared across processes / clients.
        self._persistent_fields_cache = fields_cache

        parsed = urlparse(instance.url)
        if parsed.scheme not in ("http", "https"):
            raise OdooTransportError(f"Unsupported URL scheme: {parsed.scheme!r}")

        self._common = self._make_proxy(f"{instance.url}/xmlrpc/2/common")
        self._object = self._make_proxy(f"{instance.url}/xmlrpc/2/object")
        self._uid: int | None = None
        self._is_admin: bool | None = None  # set after authenticate()
        self._admin_reason: str | None = None  # human-readable why it's admin
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
        logger.debug("authenticate start: instance=%s", self._instance.name)
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
                f"{_summarize_odoo_fault(exc.faultString)}"
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
        logger.debug("authenticate ok: instance=%s uid=%d", self._instance.name, uid)

        # Detect admin-level credentials. uid=1 is the Odoo superuser (OdooBot).
        # Any user with the ``base.group_system`` group has system-administrator
        # rights — can bypass most record rules and access nearly everything.
        # Using such credentials via the MCP is a security red flag because it
        # collapses the per-user Odoo ACL that the MCP relies on for scoping.
        # We detect and flag; we don't refuse, so existing working setups are
        # not broken. Consultants should create a dedicated non-admin Odoo user
        # for MCP use instead.
        self._detect_admin_privileges(uid)
        self._enforce_admin_refusal()

        return uid

    def _enforce_admin_refusal(self) -> None:
        """Refuse admin credentials on production unless explicitly opted out.

        Default policy as of v0.5.0: a fresh production instance authenticated
        as the Odoo superuser or a ``base.group_system`` member raises
        :class:`OdooAuthError`. Operators who knowingly need admin keys (e.g.
        for integration test rigs) can opt out by setting
        ``refuse_admin_on_production = false`` in the instance's TOML config.
        """
        if not self._is_admin:
            return
        if not self._instance.production:
            return
        if not self._instance.refuse_admin_on_production:
            logger.warning(
                "Instance %r is using admin credentials on a production instance "
                "but refuse_admin_on_production=false — opt-out acknowledged. "
                "Per-user Odoo ACL scoping is NOT in effect.",
                self._instance.name,
            )
            return
        raise OdooAuthError(
            f"Refusing to use admin credentials ({self._admin_reason}) on "
            f"production instance {self._instance.name!r}. Admin keys bypass "
            f"per-user Odoo record rules, which removes the ACL scoping the "
            f"MCP relies on. To fix: create a non-admin Odoo user, grant only "
            f"the groups it needs, generate a new API key as that user, then "
            f"run 'odoo-mcp setup --rotate-key {self._instance.name}'. To "
            f"opt out (NOT recommended — only do this if you understand the "
            f"consequences, e.g. integration test rigs), set "
            f"'refuse_admin_on_production = false' in the [instances."
            f"{self._instance.name}] TOML section."
        )

    def _detect_admin_privileges(self, uid: int) -> None:
        """Populate ``_is_admin`` and ``_admin_reason`` after a successful auth.

        Never raises — this is a best-effort security signal. If the check
        cannot complete (e.g. temporary RPC error), we leave ``_is_admin`` as
        ``None`` so callers can tell "not checked" apart from "confirmed not".
        """
        if uid == 1:
            self._is_admin = True
            self._admin_reason = "superuser (uid=1, OdooBot)"
            logger.warning(
                "Instance %r is authenticated as the Odoo superuser (uid=1). "
                "Most record rules are bypassed. Create a dedicated non-admin "
                "user for MCP use.",
                self._instance.name,
            )
            return
        try:
            has_system = self._execute("res.users", "has_group", [uid, "base.group_system"], {})
        except (OdooRemoteError, OdooTransportError):
            # Couldn't check — leave admin status unknown.
            self._is_admin = None
            return
        if bool(has_system):
            self._is_admin = True
            self._admin_reason = "system administrator (base.group_system)"
            logger.warning(
                "Instance %r is authenticated as an Odoo system administrator "
                "(uid=%d). Most record rules are bypassed. Create a dedicated "
                "non-admin user for MCP use.",
                self._instance.name,
                uid,
            )
        else:
            self._is_admin = False

    @property
    def is_admin(self) -> bool | None:
        """Admin status of the authenticated user.

        - ``True``: uid=1 or member of ``base.group_system``.
        - ``False``: regular user.
        - ``None``: not yet authenticated, or the check couldn't complete.
        """
        return self._is_admin

    @property
    def username(self) -> str | None:
        """The Odoo login of the authenticated user, if credentials are loaded.

        Returns ``None`` when credentials haven't been resolved yet (lazy
        loader hasn't fired). Useful for diagnostics where leaking the API
        key is forbidden but exposing the login is not.
        """
        if self._credentials is None:
            return None
        return self._credentials.username

    @property
    def admin_reason(self) -> str | None:
        """Short human-readable explanation of why :attr:`is_admin` is True."""
        return self._admin_reason

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
        """Return ``fields_get`` for ``model``, cached at two levels.

        Lookup order when ``use_cache`` is set:

        1. In-memory L1 dict (per-process, no I/O).
        2. SQLite L2 cache (per-host, survives restarts) — if one was wired
           in via the constructor.
        3. Odoo XML-RPC ``fields_get`` — the only path that does network I/O.

        On miss-then-hit-from-Odoo we write back to both caches so the next
        call (in this process or a future one) is a hit.
        """
        if use_cache:
            with self._fields_lock:
                cached_l1 = self._fields_cache.get(model)
                if cached_l1 is not None:
                    self._fields_cache.move_to_end(model)
                    return cached_l1
        if use_cache and self._persistent_fields_cache is not None:
            cached = self._persistent_fields_cache.get(self._instance.name, model)
            if cached is not None:
                self._cache_fields_l1(model, cached)
                return cached
        result = self._execute(
            model,
            "fields_get",
            [],
            {
                "attributes": [
                    "type",
                    "string",
                    "required",
                    "readonly",
                    "help",
                    "relation",
                    "store",
                ],
            },
        )
        if not isinstance(result, dict):
            raise OdooRemoteError(
                f"fields_get for {model!r} returned unexpected type {type(result).__name__}"
            )
        self._cache_fields_l1(model, result)
        if self._persistent_fields_cache is not None:
            self._persistent_fields_cache.put(self._instance.name, model, result)
        return result

    def _cache_fields_l1(self, model: str, payload: dict[str, dict[str, Any]]) -> None:
        """Insert into the L1 LRU, evicting the oldest entry on overflow."""
        with self._fields_lock:
            self._fields_cache[model] = payload
            self._fields_cache.move_to_end(model)
            while len(self._fields_cache) > self._fields_cache_max_size:
                self._fields_cache.popitem(last=False)

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

    def lookup(self, model: str, query: str, limit: int) -> list[dict[str, Any]]:
        """Fast name-based lookup: ``name ilike <query>`` returning id + display_name.

        The domain shape is fixed — callers do not pass an arbitrary
        ``domain``, which deliberately sidesteps the domain sandbox. The
        only knobs are the substring and the limit.
        """
        domain = [("name", "ilike", query)]
        result = self._execute(
            model,
            "search_read",
            [domain],
            {"fields": ["id", "display_name"], "limit": limit},
        )
        if not isinstance(result, list):
            raise OdooRemoteError(
                f"lookup({model!r}) returned unexpected type {type(result).__name__}"
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

    def check_access_rights(self, model: str, operation: str) -> bool:
        """Return whether the authenticated user has ``operation`` on ``model``.

        Calls Odoo's ``check_access_rights(operation, raise_exception=False)``.
        ``operation`` must be one of ``read``, ``write``, ``create``, ``unlink``.
        Used only by ``odoo_diagnose_access`` — never controls a write path.
        """
        result = self._execute(
            model,
            "check_access_rights",
            [operation],
            {"raise_exception": False},
        )
        return bool(result)

    def message_post(
        self,
        model: str,
        record_id: int,
        body: str,
        *,
        subject: str | None,
        partner_ids: list[int],
        message_type: str,
    ) -> int:
        """Post a message on a record via Odoo's ``message_post`` method.

        Calls ``model.message_post(...)`` on ``record_id``. Odoo's
        notification engine sends the email when ``message_type=='comment'``
        and at least one partner is in ``partner_ids`` or in the record's
        followers list. ``message_type=='notification'`` creates an
        internal log note without sending email.

        This is the only method-execute escape in the entire client —
        every other operation is one of the named primitives. Exposed
        only via the ``odoo_send_message`` tool, which itself is gated
        behind two independent opt-ins (env var + per-instance config).

        Returns the id of the created ``mail.message`` row.
        """
        if message_type not in ("comment", "notification"):
            raise OdooRemoteError(
                f"Refusing message_post with message_type={message_type!r}; "
                f"only 'comment' (email) and 'notification' (log note) are "
                f"allowed."
            )
        kwargs: dict[str, Any] = {
            "body": body,
            "message_type": message_type,
        }
        if subject is not None:
            kwargs["subject"] = subject
        if partner_ids:
            kwargs["partner_ids"] = partner_ids
        # ``subtype_xmlid`` controls which Odoo subtype this message
        # belongs to. ``mt_comment`` ⇒ visible email-eligible message
        # in the chatter. ``mt_note`` ⇒ internal log note. We force
        # the matching subtype so a misbehaving caller cannot post a
        # silent "note" that actually triggers an email blast.
        kwargs["subtype_xmlid"] = "mail.mt_comment" if message_type == "comment" else "mail.mt_note"
        result = self._execute(model, "message_post", [[record_id]], kwargs)
        if not isinstance(result, int):
            raise OdooRemoteError(
                f"message_post on {model!r}({record_id}) returned unexpected "
                f"type {type(result).__name__}"
            )
        return result

    def unlink(self, model: str, ids: list[int]) -> bool:
        """Permanently delete records. Exposed only via ``odoo_archive_or_delete``."""
        result = self._execute(model, "unlink", [ids], {})
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
            raise OdooRemoteError(
                f"Odoo fault on {model}.{method}: {_summarize_odoo_fault(exc.faultString)}"
            ) from exc
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
