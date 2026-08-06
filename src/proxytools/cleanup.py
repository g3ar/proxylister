"""Restore a Proxy Tools clone to its post-clone runtime state.

This command removes only artifacts owned by Proxy Tools or common Python test
and build caches. It deliberately preserves source files, Git metadata,
``.env`` files, and arbitrary user exports. Cleanup refuses to run while
another working command holds this clone's process lock.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

from proxytools.paths import lock_path, tool_home
from proxytools.process_lock import AlreadyRunning, ProcessLock

RUNTIME_FILES = (
    "proxytools.db",
    "proxytools.db-wal",
    "proxytools.db-shm",
    "proxytools-geoip.mmdb",
    "proxytools-geoip.version",
    "working_proxies.txt",
    ".coverage",
)
RUNTIME_DIRECTORIES = (
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "build",
    "dist",
)


def _remove(path: Path, removed: list[Path]) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        removed.append(path)
    elif path.exists() or path.is_symlink():
        path.unlink()
        removed.append(path)


def clear_runtime(home: Path | None = None) -> list[Path]:
    """Delete known generated artifacts below one canonical clone directory."""
    home = (home or tool_home()).resolve()
    removed = []
    for name in RUNTIME_FILES:
        _remove(home / name, removed)
    for name in RUNTIME_DIRECTORIES:
        _remove(home / name, removed)
    for pattern in ("__pycache__", "*.egg-info"):
        for path in sorted(home.rglob(pattern), key=lambda item: len(item.parts), reverse=True):
            if ".git" not in path.parts:
                _remove(path, removed)
    for pattern in ("*.pyc", "*.pyo"):
        for path in home.rglob(pattern):
            if ".git" not in path.parts:
                _remove(path, removed)
    for path in home.glob(".proxytools-geoip-*"):
        _remove(path, removed)
    return removed


def main() -> int:
    home = tool_home()
    lock = lock_path()
    try:
        with ProcessLock("clear", lock):
            removed = clear_runtime(home)
    except AlreadyRunning as error:
        print(f"proxytools: refusing to clear: {error}", file=sys.stderr)
        return 1
    lock.unlink(missing_ok=True)
    if removed:
        print(f"Removed {len(removed)} generated artifact(s). Local state cannot be recovered.")
    else:
        print("Nothing to clear; the clone is already clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
