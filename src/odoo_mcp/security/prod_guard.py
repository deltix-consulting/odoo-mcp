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

import secrets
import time
from dataclasses import dataclass
from threading import Lock

from ..errors import ProdGuardError

_UNLOCK_TTL_SECONDS = 15 * 60
_PENDING_TOKEN_TTL_SECONDS = 5 * 60
DEFAULT_MAX_COMMITS_PER_UNLOCK = 10


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
    ) -> None:
        """Validate and burn a confirmation token.

        Raises :class:`ProdGuardError` if the token is unknown, expired, or
        doesn't match the (instance, op, model) it was issued for.
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
                        "number of commits have already been performed. Call "
                        "odoo_enable_prod_writes again to renew."
                    )
                state.commits_remaining -= 1
