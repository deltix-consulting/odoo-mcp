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
   d. A smoke-test ``fields_get`` on one allowed model succeeds.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .audit import AuditLog
from .client import OdooClient
from .config import DEFAULT_CONFIG_PATH, load_config
from .credentials import load_credentials
from .errors import CredentialsError, OdooMcpError
from .update_check import check_for_update


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


def run_doctor(config_path: Path | None = None) -> int:
    """Run the doctor checks and return a process exit code."""
    report = _Report()

    # --- Config -----------------------------------------------------------
    cfg_path = config_path or DEFAULT_CONFIG_PATH
    try:
        cfg = load_config(cfg_path)
    except OdooMcpError as exc:
        report.add("Load config", False, exc.user_message)
        report.print()
        return 1
    report.add("Load config", True, f"from {cfg.path}")

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

        # Smoke test: fields_get on one allowed model. In open mode the
        # config holds the wildcard sentinel, which isn't a real model — pick
        # a known-existing one (res.partner) instead.
        from .security.allowlist import ALLOWLIST_WILDCARD

        if ALLOWLIST_WILDCARD in inst_cfg.allowed_models:
            probe_model = "res.partner"
        else:
            probe_model = next(iter(inst_cfg.allowed_models))
        try:
            fg = client.fields_get(probe_model)
        except OdooMcpError as exc:
            report.add(f"{section} fields_get({probe_model})", False, exc.user_message)
            continue
        report.add(
            f"{section} fields_get({probe_model})",
            True,
            f"{len(fg)} fields",
        )

    report.print()
    _print_update_check()
    return 0 if report.ok else 1


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
    override = None
    if args and args[0] == "--config":
        if len(args) < 2:
            print("Usage: odoo-mcp doctor [--config PATH]", file=sys.stderr)
            return 2
        override = Path(args[1]).expanduser()
    return run_doctor(override)


if __name__ == "__main__":
    raise SystemExit(main())
