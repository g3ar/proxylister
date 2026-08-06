"""Resolve per-clone runtime files independently of the caller's directory.

The root launcher exports ``PROXYTOOLS_HOME``. Keeping the database and process
lock relative to that canonical directory lets two separate clones operate
independently while every invocation of one clone shares the same state.
"""

from __future__ import annotations

import os
from pathlib import Path


def tool_home() -> Path:
    """Return the canonical directory containing the ``proxytools`` launcher."""
    configured = os.environ.get("PROXYTOOLS_HOME")
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def database_path() -> Path:
    return tool_home() / "proxytools.db"


def lock_path() -> Path:
    return tool_home() / "proxytools.lock"


def geoip_database_path() -> Path:
    return tool_home() / "proxytools-geoip.mmdb"


def geoip_version_path() -> Path:
    return tool_home() / "proxytools-geoip.version"
