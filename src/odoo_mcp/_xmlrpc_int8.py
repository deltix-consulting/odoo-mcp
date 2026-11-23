"""Send integers above 2**31 - 1 to Odoo instead of failing before the call.

The stdlib XML-RPC marshaller only knows ``<int>``, a signed 32-bit value.
Any Python ``int`` outside -2**31 .. 2**31 - 1 makes ``xmlrpc.client.dumps``
raise ``OverflowError: int exceeds XML-RPC limits`` before a byte leaves the
machine. JSON has no integer/float split the agent can see, so an amount like
``5000000000`` (five billion: an everyday figure in IDR, VND, KRW, CLP, COP)
arrives as an ``int`` and every tool that carries it — a domain on a Monetary
field, a create or write of ``expected_revenue`` — ended in a bare
``internal_error``. On production a dry-run of that write still issued a
confirmation token, because the preview never sends the values to Odoo, so
the approved commit was the first call that could fail.

Odoo parses requests with ``xmlrpc.client.loads`` on every version this
server supports (16.0 - master: ``odoo/addons/base/controllers/rpc.py`` up to
18.0, ``addons/rpc/controllers/xmlrpc.py`` from 19.0), and the stdlib
unmarshaller has read the Apache ``<i8>`` extension since Python 3.5. Odoo's
own server marshaller on master writes ``<i8>`` for the same range. So the
value goes out as ``<i8>`` and Odoo receives the exact integer: a Float or
Monetary field takes it as is, an Integer column answers with PostgreSQL's
own "integer out of range", which is the true error.

Outside the signed 64-bit range there is no XML-RPC integer at all; that is a
refusal with a hint, not an ``internal_error``.
"""

from __future__ import annotations

import xmlrpc.client
from typing import Any, Final

from .errors import LimitExceededError

_INT4_MIN: Final[int] = -(1 << 31)
_INT4_MAX: Final[int] = (1 << 31) - 1
_INT8_MIN: Final[int] = -(1 << 63)
_INT8_MAX: Final[int] = (1 << 63) - 1


class IntegerOutOfRangeError(LimitExceededError):
    """An integer outside the signed 64-bit range: no XML-RPC type carries it.

    The message gives the digit count, never the value: the audit log
    records ``audit_message`` and is documented to hold no field values.
    """

    @property
    def hint(self) -> str:
        return (
            "No Odoo Integer field holds a value this large. For a Float or "
            "Monetary field, send the number as a decimal (e.g. 1.5e20)."
        )


class _Int8Marshaller(xmlrpc.client.Marshaller):
    """Marshaller that writes ``<i8>`` for integers outside the int4 range."""

    dispatch: dict[Any, Any] = dict(xmlrpc.client.Marshaller.dispatch)

    def dump_long(self, value: int, write: Any) -> None:
        if _INT4_MIN <= value <= _INT4_MAX:
            write("<value><int>")
            write(str(int(value)))
            write("</int></value>\n")
        elif _INT8_MIN <= value <= _INT8_MAX:
            write("<value><i8>")
            write(str(int(value)))
            write("</i8></value>\n")
        else:
            raise IntegerOutOfRangeError(
                f"An integer in the call has {len(str(abs(value)))} digits, outside "
                f"the signed 64-bit range XML-RPC can carry to Odoo."
            )

    dispatch[int] = dump_long


def dumps_call(params: tuple[Any, ...], methodname: str, *, allow_none: bool) -> str:
    """``xmlrpc.client.dumps`` for a method call, with :class:`_Int8Marshaller`.

    Only the UTF-8 request shape is produced, which is the only one
    :class:`OdooServerProxy` sends.
    """
    data = _Int8Marshaller("utf-8", allow_none).dumps(params)
    return (
        "<?xml version='1.0'?>\n"
        "<methodCall>\n"
        f"<methodName>{methodname}</methodName>\n"
        f"{data}"
        "</methodCall>\n"
    )


class OdooServerProxy(xmlrpc.client.ServerProxy):
    """``ServerProxy`` whose requests can carry 64-bit integers.

    Overrides the private ``__request`` (name-mangled to
    ``_ServerProxy__request``) — the one place a call is serialized. Its body
    is identical on CPython 3.11, 3.12 and 3.13 apart from the ``dumps``
    call; ``tests/test_xmlrpc_int8.py`` pins that the stdlib attributes this
    reads still exist.
    """

    def _ServerProxy__request(self, methodname: str, params: tuple[Any, ...]) -> Any:  # noqa: N802
        # ``vars`` rather than attribute access: ServerProxy.__getattr__
        # turns any unknown name into a remote method, so a typo here would
        # become an RPC call instead of an AttributeError.
        state: dict[str, Any] = vars(self)
        encoding: str = state["_ServerProxy__encoding"]
        request = dumps_call(
            params, methodname, allow_none=state["_ServerProxy__allow_none"]
        ).encode(encoding, "xmlcharrefreplace")
        response = state["_ServerProxy__transport"].request(
            state["_ServerProxy__host"],
            state["_ServerProxy__handler"],
            request,
            verbose=state["_ServerProxy__verbose"],
        )
        if len(response) == 1:
            response = response[0]
        return response
