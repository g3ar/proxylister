"""Resolve and prepare per-clone runtime directories and files.

The root launcher exports ``PROXYLISTER_HOME``. Keeping the database and process
lock relative to that canonical directory lets two separate clones operate
independently while every invocation of one clone shares the same state.
Proxy history lives under ``proxydb/`` and GeoIP data under ``geodb/`` so the
project root contains only source and user-facing files. Runtime files created
under the former project name are moved into the current layout on first access.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def tool_home() -> Path:
    """Return the canonical directory containing the ``proxylister`` launcher."""
    configured = os.environ.get("PROXYLISTER_HOME") or os.environ.get("PROXYTOOLS_HOME")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def _migrate_first(target: Path, *legacy_names: str) -> Path:
    if target.exists():
        return target
    for legacy_name in legacy_names:
        legacy = tool_home() / legacy_name
        if legacy != target and legacy.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            legacy.replace(target)
            break
    return target


def install_default_config(target: Path) -> None:
    """Create the external config from the copy bundled in a frozen binary."""
    _migrate_first(target, "proxytools.conf")
    if target.exists() or not getattr(sys, "frozen", False):
        return
    bundle = Path(getattr(sys, "_MEIPASS")) / "proxylister.conf"
    try:
        with bundle.open("rb") as source, target.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    except FileExistsError:
        return


def _runtime_directory(name: str) -> Path:
    directory = tool_home() / name
    directory.mkdir(exist_ok=True)
    return directory


def database_path() -> Path:
    directory = _runtime_directory("proxydb")
    for suffix in ("", "-wal", "-shm"):
        _migrate_first(
            directory / f"proxylister.db{suffix}",
            f"proxydb/proxytools.db{suffix}",
            f"proxytools.db{suffix}",
            f"proxylister.db{suffix}",
        )
    return directory / "proxylister.db"


def lock_path() -> Path:
    return _migrate_first(
        _runtime_directory("proxydb") / "proxylister.lock",
        "proxydb/proxytools.lock",
        "proxytools.lock",
        "proxylister.lock",
    )


def geoip_database_path() -> Path:
    return _migrate_first(
        _runtime_directory("geodb") / "geoip.mmdb",
        "proxytools-geoip.mmdb",
        "proxylister-geoip.mmdb",
    )


def geoip_version_path() -> Path:
    return _migrate_first(
        _runtime_directory("geodb") / "version",
        "proxytools-geoip.version",
        "proxylister-geoip.version",
    )


def working_proxies_path() -> Path:
    """Return the plain-text output file written by the ``list`` command."""
    return tool_home() / "working_proxies.txt"
