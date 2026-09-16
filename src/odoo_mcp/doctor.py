"""Pre-flight health check for the Odoo MCP.

Invoked via ``python -m odoo_mcp doctor``. Walks the startup sequence without
launching the MCP server, so the user can verify their setup is correct before
wiring it into Claude Code. Reports green/yellow/red for each step and exits
non-zero if anything failed.

Specifically checks:

1. Config file exists, is a regular file, and has ``chmod 600``.
2. Config TOML parses and conforms to the schema.
3. Audit log directory is writable.
4. For each instance:
   a. Credential env vars are present.
   b. TLS connects and the remote cert is valid (unless ``allow_self_signed``).
   c. Odoo ``authenticate`` succeeds.
   d. A smoke-test ``fields_get`` succeeds — on ``res.partner`` in open
      mode, on **every** listed model in strict mode, so a typo'd
      ``allowed_models`` entry is caught here and not by the agent.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .audit import AuditLog
from .client import OdooClient
from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .credentials import load_credentials
from .errors import CredentialsError, ModelNotAllowedError, OdooMcpError, OdooRemoteError
from .update_check import check_for_update

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Step:
    name: str
    ok: bool
    detail: str


@dataclass(slots=True)
class _Report:
    steps: list[_Step] = field(default_factory=list)
    warnings: list[_Step] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(_Step(name=name, ok=ok, detail=detail))

    def add_warning(self, name: str, detail: str = "") -> None:
        """Informational signal that does NOT cause exit-code failure."""
        self.warnings.append(_Step(name=name, ok=False, detail=detail))

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def print(self) -> None:
        for step in self.steps:
            mark = "✓" if step.ok else "✗"
            line = f"  {mark} {step.name}"
            if step.detail:
                line += f" — {step.detail}"
            print(line)
        for step in self.warnings:
            line = f"  ! {step.name}"
            if step.detail:
                line += f" — {step.detail}"
            print(line)
        print()
        print("OK" if self.ok else "FAILED")

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable view for ``--json``."""
        return {
            "ok": self.ok,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in self.steps],
            "warnings": [{"name": w.name, "detail": w.detail} for w in self.warnings],
        }


def run_doctor(config_path: Path | None = None, *, as_json: bool = False) -> int:
    """Run the doctor checks and return a process exit code.

    When ``as_json`` is set, suppresses the human report and emits a single
    JSON object on stdout instead. Useful for CI / dashboards.
    """
    report = _Report()

    # --- Config -----------------------------------------------------------
    cfg_path = config_path or DEFAULT_CONFIG_PATH
    try:
        cfg = load_config(cfg_path)
    except OdooMcpError as exc:
        report.add("Load config", False, exc.user_message)
        _emit(report, as_json=as_json)
        return 1
    report.add("Load config", True, f"from {cfg.path}")

    # --- Read-only session toggle ---------------------------------------
    # Surface ODOO_MCP_READ_ONLY=1 so a consultant who set it for a demo
    # doesn't later wonder why every write fails. Treated as informational
    # — not a failure.
    import os as _os

    if _os.environ.get("ODOO_MCP_READ_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        report.add_warning(
            "Read-only session",
            "ODOO_MCP_READ_ONLY is set — every write-path tool will refuse.",
        )
    disabled = _os.environ.get("ODOO_MCP_DISABLE_TOOLS", "").strip()
    if disabled:
        report.add_warning(
            "Disabled tools",
            f"ODOO_MCP_DISABLE_TOOLS is set: {disabled} — those tools will "
            f"be hidden from MCP clients.",
        )
    budget = _os.environ.get("ODOO_MCP_TOOL_LATENCY_BUDGET_MS", "").strip()
    if budget:
        report.add_warning(
            "Latency budget",
            f"ODOO_MCP_TOOL_LATENCY_BUDGET_MS={budget} — slow_tool_call "
            f"warnings will fire above this threshold.",
        )

    # --- Credentials from the OS credential store ------------------------
    # Doctor used to read straight from ``os.environ``, which only worked
    # when invoked under ``odoo-mcp launch`` (which calls
    # ``load_credentials_into_os`` itself). Run via ``odoo-mcp doctor``
    # the env vars are absent and every per-instance check fails with a
    # bogus "missing env vars" before it can authenticate. Pull from the
    # credstore here so doctor works standalone. Failures are non-fatal:
    # if the credstore is broken, the per-instance "credentials" check
    # below surfaces the missing-env error loudly with the existing
    # message, which is fine — we don't want a credstore hiccup to abort
    # doctor entirely.
    try:
        from . import setup_wizard

        setup_wizard.load_credentials_into_os()
    except Exception as exc:  # noqa: BLE001 — informational only, must not abort doctor
        report.add_warning(
            "Load credentials from credstore",
            f"could not preload credentials ({type(exc).__name__}: {exc})",
        )

    # --- Audit log --------------------------------------------------------
    try:
        AuditLog(cfg.audit_log_path)
    except OdooMcpError as exc:
        report.add("Audit log writable", False, exc.user_message)
    else:
        report.add("Audit log writable", True, str(cfg.audit_log_path))

    # --- Per-instance checks ---------------------------------------------
    for name, inst_cfg in cfg.instances.items():
        section = f"[{name}]"
        # Credentials
        try:
            creds = load_credentials(name, inst_cfg.credentials_env_prefix)
        except CredentialsError as exc:
            report.add(f"{section} credentials", False, exc.user_message)
            continue
        report.add(f"{section} credentials", True, f"user {creds.username}")

        # Authenticate
        try:
            client = OdooClient(inst_cfg, creds)
            client.authenticate()
        except OdooMcpError as exc:
            report.add(f"{section} authenticate", False, exc.user_message)
            continue
        report.add(f"{section} authenticate", True, f"uid={client.uid}")

        # Admin-credential warning. Set by authenticate() via has_group check.
        # Surfaced as a separate line so the signal doesn't get lost in a
        # "green" doctor run. We pass it through `add_warning` rather than a
        # hard fail because it's informational — the MCP still works, but
        # per-user ACL scoping won't.
        if client.is_admin:
            opt_out_note = ""
            if inst_cfg.production and not inst_cfg.refuse_admin_on_production:
                opt_out_note = " (opted out via refuse_admin_on_production=false)"
            report.add_warning(
                f"{section} admin check",
                f"authenticated as {client.admin_reason}. Most Odoo record "
                f"rules are bypassed. Create a dedicated non-admin user for "
                f"MCP use." + opt_out_note,
            )

        # Smoke test: fields_get on the allowed models. In open mode the
        # config holds the wildcard sentinel, which isn't a real model — pick
        # a known-existing one (res.partner) instead. In strict mode probe
        # EVERY listed model, in sorted order: the list is hand-typed
        # (SECURITY.md tells prod operators to enumerate it) and the config
        # loader only checks that entries are strings, so a typo'd model name
        # is invisible until here. Probing one arbitrary member — the old
        # ``next(iter(frozenset))`` — made doctor's verdict on such a config
        # depend on the process hash seed: red on the run that happened to
        # draw the typo, green otherwise.
        _smoke_test_allowed_models(report, name, client, inst_cfg.allowed_models)

    # --- Rotation reminders ----------------------------------------------
    # Odoo does not enforce a TTL on API keys, so the MCP records the
    # set-date locally (in the credstore) and surfaces a warning here for
    # any key older than ``rotation_warning_days``. Best-effort: if the
    # credstore lookup fails, we don't add a warning — the doctor's
    # per-instance auth check is the real signal.
    _check_rotation_warnings(report, cfg)

    _emit(report, as_json=as_json)
    if not as_json:
        _print_update_check()
    return 0 if report.ok else 1


def _smoke_test_allowed_models(
    report: _Report, name: str, client: OdooClient, allowed: frozenset[str]
) -> None:
    """Run the per-instance ``fields_get`` smoke test and record its step.

    Open mode probes ``res.partner`` only, exactly as before. Strict mode
    walks ``sorted(allowed)`` and sorts every entry into one of three bins:

    * **inert** — the MCP's own gate (:func:`check_model`) would refuse it
      before Odoo is ever asked: a denylisted model or a malformed name.
      Listing it grants nothing, so it is reported as a warning, not a
      failure; the instance still works for the other entries.
    * **rejected** — Odoo returned a fault for the name. ``fields_get`` does
      not check ACLs, so this is almost always a model that does not exist
      on the instance (a typo, or a module that is not installed). Red step,
      because the agent would hit the same fault on first use.
    * verified — ``fields_get`` succeeded; the field count is summed into the
      step detail.

    A transport/auth error on any probe fails the step immediately with that
    error, as the single-probe version did: nothing model-specific can be
    learned from it, and the remaining probes would fail the same way.
    """
    from .security.allowlist import ALLOWLIST_WILDCARD, classify_model_block

    section = f"[{name}]"
    if ALLOWLIST_WILDCARD in allowed:
        step = f"{section} fields_get(res.partner)"
        try:
            fg = client.fields_get("res.partner")
        except OdooMcpError as exc:
            report.add(step, False, exc.user_message)
            return
        report.add(step, True, f"{len(fg)} fields")
        return

    step = f"{section} fields_get(allowed_models)"
    inert: list[str] = []
    rejected: list[str] = []
    first_fault = ""
    verified = 0
    total_fields = 0
    for model in sorted(allowed):
        try:
            blocked = classify_model_block(model, allowed)
        except ModelNotAllowedError:
            blocked = "malformed"
        if blocked is not None:
            inert.append(model)
            continue
        try:
            fg = client.fields_get(model)
        except OdooRemoteError as exc:
            rejected.append(model)
            if not first_fault:
                # Faults arrive as a full server traceback; the last line is
                # the exception that names the problem.
                first_fault = exc.user_message.strip().splitlines()[-1]
            continue
        except OdooMcpError as exc:
            report.add(step, False, f"{model}: {exc.user_message}")
            return
        verified += 1
        total_fields += len(fg)

    if inert:
        report.add_warning(
            f"{section} allowed_models",
            f"{len(inert)} of {len(allowed)} listed model(s) can never be reached "
            f"through the MCP — on the built-in denylist or not a valid model name: "
            f"{', '.join(inert)}. Remove them from [instances.{name}] "
            f"allowed_models; listing them grants nothing.",
        )
    if rejected:
        report.add(
            step,
            False,
            f"{len(rejected)} of {len(allowed)} listed model(s) rejected by Odoo: "
            f"{', '.join(rejected)}. If this is a typo, fix allowed_models in "
            f"[instances.{name}]; if the module is not installed, remove "
            f"the entry. Odoo said: {first_fault}",
        )
        return
    if verified == 0:
        # Every entry was inert. Nothing was probed, so nothing is verified —
        # and the instance cannot serve a single model. Red, not green.
        report.add(step, False, "no reachable model in allowed_models to probe")
        return
    report.add(step, True, f"{verified} models verified, {total_fields} fields")


def _emit(report: _Report, *, as_json: bool) -> None:
    if as_json:
        import json as _json

        print(_json.dumps(report.to_dict(), separators=(",", ":")))
    else:
        report.print()


def _check_rotation_warnings(report: _Report, cfg: AppConfig) -> None:
    """Warn for any instance whose API key was set too long ago.

    Threshold comes from ``cfg.defaults.rotation_warning_days`` (default
    90). Instances with no recorded set-time (keys created before the
    set-time tracking landed) emit a low-volume "no rotation timestamp
    on file" warning so operators know they can rotate to record one.
    """
    from datetime import UTC, datetime

    from . import _credstore

    threshold = cfg.defaults.rotation_warning_days
    now = datetime.now(UTC)
    for name, inst_cfg in cfg.instances.items():
        prefix = inst_cfg.credentials_env_prefix
        try:
            set_at = _credstore.get_secret_set_at(name, f"{prefix}_API_KEY")
        except Exception as exc:  # noqa: BLE001 — best-effort metadata lookup
            logger.debug("rotation timestamp lookup failed for %s: %s", name, exc)
            continue
        if set_at is None:
            report.add_warning(
                f"[{name}] API key rotation",
                "no rotation timestamp on file. Rotate via "
                f"'odoo-mcp setup --rotate-key {name}' to start tracking.",
            )
            continue
        age_days = (now - set_at).days
        if age_days >= threshold:
            report.add_warning(
                f"[{name}] API key rotation",
                f"API key was set {age_days} days ago "
                f"(threshold {threshold}). "
                f"Consider rotating: odoo-mcp setup --rotate-key {name}",
            )


_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _print_update_check() -> None:
    """Emit a one-line update notice. Never raises, never fails doctor."""
    from .update_check import fetch_latest_tag

    try:
        tag = fetch_latest_tag()
    except Exception:  # noqa: BLE001 — informational only, must not fail doctor
        tag = None
    if tag is None:
        print(f"{_DIM}~ update check: skipped (network){_RESET}")
        return
    try:
        result = check_for_update(__version__)
    except Exception:  # noqa: BLE001 — informational only, must not fail doctor
        return
    if result is None:
        return
    current, latest = result
    print(
        f"{_YELLOW}! Update available: {latest} (you have {current}). "
        f"Run 'odoo-mcp update' or see CHANGELOG.{_RESET}"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    override: Path | None = None
    as_json = False
    i = 0
    while i < len(args):
        if args[i] == "--config":
            if i + 1 >= len(args):
                print("Usage: odoo-mcp doctor [--config PATH] [--json]", file=sys.stderr)
                return 2
            override = Path(args[i + 1]).expanduser()
            i += 2
            continue
        if args[i] == "--json":
            as_json = True
            i += 1
            continue
        print(f"Unknown argument: {args[i]!r}", file=sys.stderr)
        print("Usage: odoo-mcp doctor [--config PATH] [--json]", file=sys.stderr)
        return 2
    return run_doctor(override, as_json=as_json)


if __name__ == "__main__":
    raise SystemExit(main())
