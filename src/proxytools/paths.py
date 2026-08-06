"""Resolve and prepare per-clone runtime directories and files.

The root launcher exports ``PROXYTOOLS_HOME``. Keeping the database and process
lock relative to that canonical directory lets two separate clones operate
independently while every invocation of one clone shares the same state.
Proxy history lives under ``proxydb/`` and GeoIP data under ``geodb/`` so the
project root contains only source and user-facing files. Legacy root-level
runtime files are moved into the new layout on first access.
"""

from __future__ import annotations

import os
from pathlib import Path


def tool_home() -> Path:
    """Return the canonical directory containing the ``proxytools`` launcher."""
    configured = os.environ.get("PROXYTOOLS_HOME")
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def _runtime_directory(name: str) -> Path:
    directory = tool_home() / name
    directory.mkdir(exist_ok=True)
    return directory


def _migrate_legacy(target: Path, legacy_name: str) -> Path:
    legacy = tool_home() / legacy_name
    if not target.exists() and legacy.exists():
        legacy.replace(target)
    return target


def database_path() -> Path:
    directory = _runtime_directory("proxydb")
    for suffix in ("", "-wal", "-shm"):
        _migrate_legacy(directory / f"proxytools.db{suffix}", f"proxytools.db{suffix}")
    return directory / "proxytools.db"


def lock_path() -> Path:
    return _migrate_legacy(_runtime_directory("proxydb") / "proxytools.lock", "proxytools.lock")


def geoip_database_path() -> Path:
    return _migrate_legacy(_runtime_directory("geodb") / "geoip.mmdb", "proxytools-geoip.mmdb")


def geoip_version_path() -> Path:
    return _migrate_legacy(_runtime_directory("geodb") / "version", "proxytools-geoip.version")
