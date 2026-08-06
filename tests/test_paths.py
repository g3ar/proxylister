"""Tests for clone-local runtime layout and legacy-file migration."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from proxytools.paths import (
    database_path,
    geoip_database_path,
    geoip_version_path,
    lock_path,
)


class RuntimePathTests(unittest.TestCase):
    def test_legacy_root_files_move_into_runtime_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PROXYTOOLS_HOME": directory}
        ):
            home = Path(directory)
            legacy = {
                "proxytools.db": b"database",
                "proxytools.db-wal": b"wal",
                "proxytools.db-shm": b"shm",
                "proxytools.lock": b"lock",
                "proxytools-geoip.mmdb": b"geoip",
                "proxytools-geoip.version": b"2026-08\n",
            }
            for name, content in legacy.items():
                (home / name).write_bytes(content)

            self.assertEqual(database_path(), home / "proxydb" / "proxytools.db")
            self.assertEqual(lock_path(), home / "proxydb" / "proxytools.lock")
            self.assertEqual(geoip_database_path(), home / "geodb" / "geoip.mmdb")
            self.assertEqual(geoip_version_path(), home / "geodb" / "version")

            self.assertEqual((home / "proxydb" / "proxytools.db-wal").read_bytes(), b"wal")
            self.assertEqual((home / "proxydb" / "proxytools.db-shm").read_bytes(), b"shm")
            self.assertFalse(any((home / name).exists() for name in legacy))


if __name__ == "__main__":
    unittest.main()
