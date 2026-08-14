"""Prevent concurrent working commands inside one project clone.

The lock uses Portalocker's native advisory file lock rather than the mere
presence of a file, so ownership is released automatically after crashes on
both POSIX and Windows. The retained file contains human-readable ownership
details useful in an error message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone

import portalocker

from proxytools.paths import database_path, lock_path, tool_home


class AlreadyRunning(RuntimeError):
    """Raised when another command owns this clone's process lock."""


class ProcessLock:
    def __init__(self, command: str, path: Path | None = None):
        self.command = command
        self.path = path or lock_path()
        self._file = None

    def __enter__(self):
        try:
            self._file = self.path.open("a+", encoding="utf-8")
            portalocker.lock(self._file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (OSError, portalocker.exceptions.LockException):
            details = ""
            if self._file is not None:
                try:
                    self._file.seek(0)
                    details = self._file.read().strip()
                except OSError:
                    pass
                finally:
                    self._file.close()
                    self._file = None
            suffix = f" ({details})" if details else ""
            raise AlreadyRunning(f"another proxytools process is already running in {tool_home()}{suffix}")
        metadata = {
            "pid": os.getpid(),
            "command": self.command,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "database": str(database_path()),
        }
        self._file.seek(0)
        self._file.truncate()
        json.dump(metadata, self._file, ensure_ascii=False)
        self._file.flush()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._file is not None:
            portalocker.unlock(self._file)
            self._file.close()
            self._file = None
