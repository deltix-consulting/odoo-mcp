"""Persistent fields-cache inspector CLI.

Invoked via ``python -m odoo_mcp cache``. Provides three actions on the
SQLite-backed L2 cache that lives at ``~/.odoo-mcp/fields-cache.db`` (or
wherever ``fields_cache_path`` points):

* ``--info`` — file size, row count, oldest / newest entry timestamps.
* ``--clear`` — drop every row. With ``--instance NAME``, drop only rows
  for that instance.

We deliberately don't read the TOML config here: this CLI must work even
if the user has a broken config (typo, bad permissions). The cache path
falls back to the default constant.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .config import DEFAULT_FIELDS_CACHE, load_config
from .errors import ConfigError
from .fields_cache import PersistentFieldsCache


def _resolve_cache_path() -> Path | None:
    """Honor the user's ``fields_cache_path`` setting if reachable.

    Falls back to the hardcoded default if the config file isn't loadable.
    Returns ``None`` if the user has explicitly disabled the cache.
    """
    try:
        cfg = load_config()
    except ConfigError:
        return Path(DEFAULT_FIELDS_CACHE).expanduser()
    return cfg.fields_cache_path


def _format_ts(value: float | None) -> str:
    if value is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))


def _print_info(cache: PersistentFieldsCache, *, as_json: bool = False) -> int:
    info = cache.info()
    if as_json:
        print(json.dumps(info, separators=(",", ":"), default=str))
        return 0
    print(f"path:       {info['path']}")
    print(f"file size:  {info['file_size_bytes']} bytes")
    print(f"rows:       {info['row_count']}")
    print(f"ttl:        {info['ttl_seconds']}s")
    print(f"oldest:     {_format_ts(info['oldest_fetched_at'])}")
    print(f"newest:     {_format_ts(info['newest_fetched_at'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="odoo-mcp cache",
        description="Inspect or clear the persistent fields_get cache.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--info", action="store_true", help="Show cache stats.")
    group.add_argument("--clear", action="store_true", help="Drop cache rows.")
    parser.add_argument(
        "--instance",
        type=str,
        default=None,
        help="Restrict --clear to one instance (otherwise drops everything).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON for --info instead of the formatted lines.",
    )
    ns = parser.parse_args(argv)

    cache_path = _resolve_cache_path()
    if cache_path is None:
        print('Persistent fields cache is disabled in config (fields_cache_path = "").')
        return 0

    cache = PersistentFieldsCache(cache_path)
    if ns.info:
        return _print_info(cache, as_json=ns.json)

    # --clear path:
    if ns.instance:
        cache.invalidate(ns.instance)
        print(f"Cleared fields cache rows for instance {ns.instance!r}.")
    else:
        cache.clear()
        print("Cleared all fields cache rows.")
    return 0
