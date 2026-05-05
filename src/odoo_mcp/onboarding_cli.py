"""``odoo-mcp onboarding`` — guided first-run flow for new public users.

Wraps the three commands a fresh installer would otherwise have to learn —
``setup``, ``doctor``, ``scan-custom`` — into one interactive flow:

1. If no config exists, run the setup wizard's first-time prompts
   (URL, database, username, API key + Keychain storage + Claude Desktop
   registration).
2. Run doctor against the resulting config. If doctor fails, abort
   loudly with the doctor output and stop before the scan.
3. Run scan-custom against the just-configured (or already-configured)
   primary instance and write the suggested TOML snippet to
   ``~/.odoo-mcp/suggestions.toml`` (chmod 600). Don't auto-apply it —
   the user reviews and copies what they want into ``config.toml``.
4. Print a final summary block telling the user what to do next
   (restart Cowork, review suggestions, try ``odoo_help``).

If a config already exists, ask whether to (a) onboard a NEW instance
on top of it (delegates to ``setup --add``), or (b) re-scan the existing
primary instance for an updated suggestions file.
"""

from __future__ import annotations

import contextlib
import sys

from . import setup_wizard
from .config import DEFAULT_CONFIG_PATH

_SUGGESTIONS_PATH = DEFAULT_CONFIG_PATH.parent / "suggestions.toml"


_USAGE = """\
Usage: odoo-mcp onboarding

Guided first-run flow: setup wizard -> doctor -> custom-surface scan.
Writes a paste-ready TOML suggestions file to ~/.odoo-mcp/suggestions.toml.
"""


def _print_intro() -> None:
    print("Welcome to odoo-mcp.")
    print()
    print("This will:")
    print("  1. Connect you to your Odoo (asks URL, database, your username, your API key)")
    print("  2. Verify the connection (doctor)")
    print("  3. Scan your Odoo for what's reachable")
    print("  4. Save a starting config + suggestions")
    print()
    with contextlib.suppress(EOFError):
        input("Press Enter to begin (or Ctrl+C to abort)...")


def _pick_primary_instance() -> str | None:
    """Return the first configured instance name (treated as 'primary')."""
    if not DEFAULT_CONFIG_PATH.exists():
        return None
    try:
        _, instances = setup_wizard._load_raw_config()
    except Exception:  # noqa: BLE001 — config issues surface on the doctor step
        return None
    if not instances:
        return None
    return next(iter(instances))


def _run_setup_first_time() -> int:
    """Delegate to the wizard's first-time setup flow."""
    return setup_wizard._cmd_setup()


def _run_setup_add() -> int:
    """Delegate to the wizard's add-an-instance flow."""
    return setup_wizard._cmd_add()


def _run_doctor() -> int:
    """Run the doctor checks. Returns the doctor exit code."""
    from . import doctor

    return doctor.run_doctor()


def _run_scan(instance: str) -> tuple[int, str | None]:
    """Run the scan against *instance*.

    Returns ``(exit_code, toml_text)``. ``toml_text`` is None if the scan
    failed before producing a result.
    """
    from .scan_cli import perform_scan, render_toml
    from .server import build_app

    try:
        app = build_app()
        rt = app.instance(instance)
        rt.client.ensure_authenticated()
    except Exception as exc:  # noqa: BLE001 — surface any startup error cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1, None

    try:
        result = perform_scan(rt.client, instance)
    except Exception as exc:  # noqa: BLE001
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1, None

    toml_text = render_toml(result)
    n_models = result.models_total
    n_custom_models = len(result.custom_models)
    n_custom_fields = len(result.custom_fields_on_standard)
    print()
    print(
        f"Scan complete: {n_models} models total, "
        f"{n_custom_models} custom models, "
        f"{n_custom_fields} custom fields on standard models."
    )

    uid = getattr(rt.client, "uid", None)
    creds = getattr(rt.client, "_credentials", None)
    login = getattr(creds, "username", None) if creds is not None else None

    summary = {
        "models_total": n_models,
        "custom_models": n_custom_models,
        "custom_fields": n_custom_fields,
        "uid": uid,
        "login": login,
    }
    # Stash the summary on the function for the caller to pick up via the
    # return value channel — keep it simple: re-format in main.
    _run_scan.last_summary = summary  # type: ignore[attr-defined]
    return 0, toml_text


def _write_suggestions(toml_text: str) -> None:
    """Atomic chmod-600 write of the suggestions file."""
    setup_wizard._atomic_write_text(_SUGGESTIONS_PATH, toml_text + "\n", mode=0o600)


def _print_final_summary(instance: str) -> None:
    summary = getattr(_run_scan, "last_summary", None) or {}
    login = summary.get("login") or "<unknown>"
    uid = summary.get("uid")
    n_models = summary.get("models_total", 0)
    n_custom_models = summary.get("custom_models", 0)
    n_custom_fields = summary.get("custom_fields", 0)

    print()
    print(f"✓ Connected as {login} (uid={uid})")
    print("✓ Doctor: passed")
    print("✓ Scan complete:")
    print(f"    {n_models} models in your Odoo")
    print(f"    {n_custom_models} custom models")
    print(f"    {n_custom_fields} custom fields on standard models")
    print()
    print("Next steps:")
    print("  • Restart Claude Cowork (Cmd+Q + reopen) to load the MCP")
    print(f"  • Review {_SUGGESTIONS_PATH} — if you agree, copy the")
    print(f"    [instances.{instance}.sensitive_fields] block into {DEFAULT_CONFIG_PATH}")
    print('  • Try in Claude: "use odoo_help to show what you can do with my Odoo"')
    print()
    print("Anything you don't want Claude to see? Add the field name to the")
    print("sensitive_fields list and restart Cowork.")


def _onboard_existing_config() -> int:
    """Branch when ``config.toml`` already exists.

    Ask the user whether to (a) onboard another instance on top of the
    existing config, or (b) just re-scan the primary instance for an
    updated suggestions file.
    """
    print(f"An existing config was found at {DEFAULT_CONFIG_PATH}.")
    print()
    print("What would you like to do?")
    print("  1. Add a new Odoo instance (keeps existing setup)")
    print("  2. Re-scan the existing primary instance for an updated suggestions file")
    print("  3. Cancel")
    while True:
        try:
            choice = input("Choose 1, 2, or 3 [3]: ").strip() or "3"
        except EOFError:
            choice = "3"
        if choice in {"1", "2", "3"}:
            break
        print("  Please enter 1, 2, or 3.")

    if choice == "3":
        print("Cancelled.")
        return 0

    if choice == "1":
        rc = _run_setup_add()
        if rc != 0:
            return rc
        instance = _pick_primary_instance() or ""
    else:
        instance = _pick_primary_instance() or ""
        if not instance:
            print("No configured instance found to scan.", file=sys.stderr)
            return 1
        print(f"Re-scanning instance '{instance}'...")

    print()
    print("Running doctor...")
    if _run_doctor() != 0:
        print()
        print(
            "Doctor reported errors. Fix the issues above, then re-run 'odoo-mcp onboarding'.",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"Scanning instance '{instance}'...")
    rc, toml_text = _run_scan(instance)
    if rc != 0 or toml_text is None:
        return rc or 1

    _write_suggestions(toml_text)
    print(f"Wrote suggestions to {_SUGGESTIONS_PATH} (chmod 600).")
    _print_final_summary(instance)
    return 0


def _onboard_first_time() -> int:
    """Branch when no ``config.toml`` exists yet."""
    rc = _run_setup_first_time()
    if rc != 0:
        return rc

    instance = _pick_primary_instance()
    if not instance:
        print(
            "Setup completed but no instance was configured. Aborting.",
            file=sys.stderr,
        )
        return 1

    # Doctor was already run by _cmd_setup. Re-run it explicitly here so we
    # can fail cleanly before the scan if something is off.
    print()
    print("Re-verifying with doctor...")
    if _run_doctor() != 0:
        print()
        print(
            "Doctor reported errors after setup. Fix the issues above, then "
            "re-run 'odoo-mcp onboarding'.",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"Scanning instance '{instance}'...")
    rc, toml_text = _run_scan(instance)
    if rc != 0 or toml_text is None:
        return rc or 1

    _write_suggestions(toml_text)
    print(f"Wrote suggestions to {_SUGGESTIONS_PATH} (chmod 600).")
    _print_final_summary(instance)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else [])
    if args and args[0] in {"-h", "--help"}:
        print(_USAGE)
        return 0

    _print_intro()
    try:
        if DEFAULT_CONFIG_PATH.exists():
            return _onboard_existing_config()
        return _onboard_first_time()
    except KeyboardInterrupt:
        print("\n\nOnboarding cancelled.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main(sys.argv[1:]))
