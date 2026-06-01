"""Production write guard.

Rules (all independently enforced — a bug in one layer won't defeat the rest):

1. **Writes disabled by default on prod.** Attempting a ``create`` or
   ``write`` against an instance flagged ``production = true`` raises
   :class:`ProdGuardError` unless the instance has been explicitly unlocked
   in this MCP process.

2. **Unlock is explicit, time-limited, and audited.**
   :meth:`ProdGuard.unlock` must be called (via the ``odoo_enable_prod_writes``
   meta-tool) to allow writes. The unlock auto-expires after 15 minutes of
   inactivity — each successful write extends it.

3. **Dry-run default on prod.** Even once unlocked, the first call to a
   write tool must pass ``dry_run`` explicitly. If the caller omits it, the
   dispatcher treats it as ``True`` — i.e. a validation-only preview — and
   surfaces a note in the response reminding the caller to pass
   ``dry_run=False`` for a real commit.

4. **Confirmation tokens.** The non-dry-run path returns a "pending"
   response containing a confirmation token. The caller must re-invoke with
   the token to actually commit. This gives the user a chance to review the
   exact values being written before approving.

The guard's state lives on a single instance held by the server, reset on
process restart.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from ..errors import ProdGuardError

_UNLOCK_TTL_SECONDS = 15 * 60
_PENDING_TOKEN_TTL_SECONDS = 5 * 60
DEFAULT_MAX_COMMITS_PER_UNLOCK = 10


def compute_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 digest of a write payload.

    The dispatcher computes this at dry-run time (binding the issued
    confirmation token to the previewed payload) and again at commit
    time (re-binding from the current call's arguments). Any drift —
    extra ids, swapped values, an added partner, a flipped mode/action
    — produces a different digest and the token is rejected.

    Canonicalisation rules:

    * JSON dump with ``sort_keys=True`` so dict-key order on either
      side of the wire does not matter.
    * No whitespace separators, so a pretty-printed re-call cannot
      pass under the same digest as the compact dry-run payload.
    * ``default=str`` for non-JSON-native values (we currently feed
      only ints / strs / lists / dicts, but a stray ``date`` from a
      future caller should fail closed, not crash).

    The digest is purely a fingerprint — it is never used to derive a
    key, never sent over the wire, and never stored beyond the token's
    5-minute TTL. SHA-256 is overkill for collision resistance at this
    use, but it's the cheapest "obviously enough" hash in the stdlib.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _UnlockState:
    """Per-instance unlock window: expiry plus remaining commit budget."""

    expires_at: float
    commits_remaining: int
    # Monotonic timestamp at which this unlock was created. Acts as a
    # stable identity for the unlock window: confirmation tokens record
    # this value at issue time, and the consume path requires the
    # current unlock to still carry the same identity. ``touch()`` only
    # extends ``expires_at`` and leaves this field untouched — touching
    # is not a fresh unlock.
    unlocked_at: float


@dataclass(slots=True)
class _PendingWrite:
    token: str
    instance: str
    op: str
    model: str
    summary: str
    expires_at: float
    # Identity of the unlock window under which this token was issued.
    # ``None`` means the token was created with no active unlock — the
    # legacy non-prod / unit-test path; consume_pending then skips the
    # identity check. A non-None value pins the token to that specific
    # unlock window: the consume path requires the current unlock to
    # still carry the same ``unlocked_at`` timestamp, otherwise a
    # token issued during a previous unlock could be replayed against a
    # fresh unlock and defeat the review-then-commit property of the
    # dry-run flow.
    unlock_id: float | None
    # Canonical SHA-256 digest of the write payload (ids, values, body,
    # partner_ids, mode, action, ...) captured at dry-run time. ``None``
    # means the token was created without payload binding — the legacy
    # path used by unit tests of ``consume_pending`` directly; the
    # consume path then skips the digest check. A non-None value pins
    # the token to the previewed payload: the consume path requires the
    # current call's payload to digest to the same value, otherwise an
    # agent could re-call with a wider scope (more ids, different
    # values, an added partner, a swapped mode/action) and slip past
    # the dry-run review.
    payload_digest: str | None


class ProdGuard:
    """Per-process state machine for production write protection."""

    def __init__(self) -> None:
        self._unlocked: dict[str, _UnlockState] = {}
        self._pending: dict[str, _PendingWrite] = {}
        self._lock = Lock()

    # --- Unlock lifecycle ---------------------------------------------------

    def unlock(
        self,
        instance: str,
        production: bool,
        *,
        now: float | None = None,
        max_commits: int = DEFAULT_MAX_COMMITS_PER_UNLOCK,
    ) -> float:
        """Unlock prod writes for ``instance``.

        Returns the expiry timestamp (monotonic seconds) so the caller can
        communicate when writes will auto-relock. The unlock also gets a
        burst budget of ``max_commits`` real commits; dry-runs don't count.
        """
        if not production:
            raise ProdGuardError(
                f"Instance {instance!r} is not flagged as production — no unlock needed."
            )
        current = now if now is not None else time.monotonic()
        expiry = current + _UNLOCK_TTL_SECONDS
        with self._lock:
            self._unlocked[instance] = _UnlockState(
                expires_at=expiry,
                commits_remaining=max_commits,
                unlocked_at=current,
            )
        return expiry

    def is_unlocked(self, instance: str, *, now: float | None = None) -> bool:
        current = now if now is not None else time.monotonic()
        with self._lock:
            state = self._unlocked.get(instance)
            if state is None:
                return False
            if state.expires_at < current:
                # Auto-relock.
                del self._unlocked[instance]
                return False
            return True

    def commits_remaining(self, instance: str, *, now: float | None = None) -> int | None:
        """Return the remaining commit budget, or ``None`` if not unlocked."""
        current = now if now is not None else time.monotonic()
        with self._lock:
            state = self._unlocked.get(instance)
            if state is None or state.expires_at < current:
                return None
            return state.commits_remaining

    def touch(self, instance: str, *, now: float | None = None) -> None:
        """Extend the unlock window on activity."""
        current = now if now is not None else time.monotonic()
        with self._lock:
            state = self._unlocked.get(instance)
            if state is not None:
                state.expires_at = current + _UNLOCK_TTL_SECONDS

    # --- Write gate ---------------------------------------------------------

    def check_write(self, instance: str, production: bool, *, now: float | None = None) -> None:
        """Raise :class:`ProdGuardError` if a write against prod is not allowed.

        No-op for non-production instances. ``now`` is injectable for tests.
        """
        if not production:
            return
        if not self.is_unlocked(instance, now=now):
            raise ProdGuardError(
                f"Writes to production instance {instance!r} are blocked. "
                f"Call odoo_enable_prod_writes(instance={instance!r}) first, "
                f"then retry the write as a dry run (dry_run=True) to preview, "
                f"and only then as a real commit."
            )
        self.touch(instance, now=now)

    def effective_dry_run(self, requested: bool | None, production: bool) -> bool:
        """Resolve the effective ``dry_run`` flag.

        On prod, ``None`` means ``True`` (safe default). Off prod, ``None``
        means ``False`` (the caller asked for a real write).
        """
        if requested is not None:
            return bool(requested)
        return production

    # --- Pending-write confirmation tokens ----------------------------------

    def create_pending(
        self,
        instance: str,
        op: str,
        model: str,
        summary: str,
        *,
        now: float | None = None,
        payload_digest: str | None = None,
    ) -> str:
        """Register a pending write and return a one-time confirmation token.

        Tokens are bound to the active unlock window via the unlock's
        ``unlocked_at`` timestamp (the unlock identity). At consume time
        the current unlock must still carry the same identity, otherwise
        the token is rejected — this prevents a token issued under one
        unlock from being replayed against a later, separate unlock.

        For non-production instances there is no unlock state to bind
        against; in that case the token records ``unlock_id=None`` and
        the consume path's identity check is short-circuited (non-prod
        never goes through ``_consume_token_on_prod`` in the dispatcher
        anyway).

        ``payload_digest`` (canonical SHA-256 of the previewed payload —
        see :func:`compute_payload_digest`) further pins the token to the
        exact payload that was reviewed. Omit it and the consume path
        skips the digest check, matching the no-binding test path; in
        production the dispatcher always supplies one so an agent cannot
        upgrade scope between the dry run and the commit.
        """
        current = now if now is not None else time.monotonic()
        token = "conf_" + secrets.token_urlsafe(16)
        with self._lock:
            # Trim expired tokens opportunistically.
            expired = [t for t, p in self._pending.items() if p.expires_at < current]
            for t in expired:
                del self._pending[t]
            state = self._unlocked.get(instance)
            # Bind to the current unlock window if one is active. We do
            # NOT raise when no unlock is present — the dispatcher's
            # check_write is the gate that requires an unlock for prod
            # writes, and unit tests of consume_pending without a prior
            # unlock should keep working (non-prod codepaths). We
            # record ``unlock_id=None`` in that case; consume_pending
            # treats ``None`` as "no binding" and skips the identity
            # check, matching the no-unlock test path.
            unlock_id: float | None = None
            if state is not None and state.expires_at >= current:
                unlock_id = state.unlocked_at
            self._pending[token] = _PendingWrite(
                token=token,
                instance=instance,
                op=op,
                model=model,
                summary=summary,
                expires_at=current + _PENDING_TOKEN_TTL_SECONDS,
                unlock_id=unlock_id,
                payload_digest=payload_digest,
            )
        return token

    def consume_pending(
        self,
        token: str,
        instance: str,
        op: str,
        model: str,
        *,
        now: float | None = None,
        payload_digest: str | None = None,
    ) -> None:
        """Validate and burn a confirmation token.

        Raises :class:`ProdGuardError` if the token is unknown, expired,
        doesn't match the (instance, op, model) it was issued for, or —
        when the issuing call supplied a ``payload_digest`` — was issued
        for a different payload than the one being committed now.
        """
        current = now if now is not None else time.monotonic()
        with self._lock:
            pending = self._pending.pop(token, None)
            if pending is None:
                # Do NOT echo the supplied token literal back into the error
                # message: the dispatcher records error messages in the audit
                # log (30-day retention), and a leaked token — even one that
                # turned out to be unknown — is a credential-shaped value
                # we'd rather keep out of long-lived storage.
                raise ProdGuardError("The supplied confirmation token is unknown or already used.")
            if pending.expires_at < current:
                raise ProdGuardError("The supplied confirmation token has expired.")
            if (pending.instance, pending.op, pending.model) != (instance, op, model):
                raise ProdGuardError(
                    f"Confirmation token does not match the current call "
                    f"(expected {pending.instance}/{pending.op}/{pending.model})."
                )
            # Payload binding: the token was issued against the previewed
            # payload (ids, values, body, partner_ids, mode, action, ...).
            # If the commit re-call carries a different payload, reject —
            # this is the gate against an agent dry-running a narrow
            # operation (e.g. ``ids=[1]``) and then committing with the
            # same token against a wider scope (e.g. ``ids=[1..1000]``).
            # ``payload_digest=None`` on the stored token means the
            # issuing call opted out of binding (unit tests of
            # consume_pending without a dispatcher); the check is then
            # skipped, matching the no-binding test path.
            if pending.payload_digest is not None and pending.payload_digest != payload_digest:
                raise ProdGuardError(
                    "Confirmation token was issued for a different payload "
                    "than the one being committed — re-do the dry run with "
                    "the exact values you intend to commit, then re-use that "
                    "fresh token."
                )
            # H1: token must have been issued under the *current* unlock
            # window. If the unlock has expired and been re-acquired
            # since the dry run, reject — the dry run was reviewed under
            # a different unlock and replaying it now would let a stale
            # preview commit against a fresh window.
            if pending.unlock_id is not None:
                state = self._unlocked.get(instance)
                if (
                    state is None
                    or state.expires_at < current
                    or state.unlocked_at != pending.unlock_id
                ):
                    raise ProdGuardError(
                        "Confirmation token was issued under a different unlock "
                        "window — re-do the dry run under the current unlock."
                    )
            # Decrement the burst counter BEFORE the actual commit. The
            # dispatcher's check_write enforces that an unlock exists for
            # production calls, so when an unlock state is present we honor
            # its budget. If there is no unlock state (e.g. unit-level use of
            # consume_pending without a prior unlock, or non-prod paths) we
            # don't synthesize one — the unlock requirement is a separate
            # gate enforced earlier in the pipeline.
            state = self._unlocked.get(instance)
            if state is not None and state.expires_at >= current:
                if state.commits_remaining <= 0:
                    raise ProdGuardError(
                        "Burst limit reached for this unlock — the configured "
                        "number of commits have already been performed. "
                        "Dry-runs do NOT count toward this budget; only "
                        "successful commits do. Call odoo_enable_prod_writes "
                        "again to renew."
                    )
                state.commits_remaining -= 1
