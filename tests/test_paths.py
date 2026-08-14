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
    install_default_config,
    lock_path,
    tool_home,
)


class RuntimePathTests(unittest.TestCase):
    def test_frozen_home_is_the_executable_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "proxytools"
            with patch.dict(os.environ, {}, clear=True), patch(
                "sys.frozen", True, create=True
            ), patch("sys.executable", str(executable)):
                self.assertEqual(tool_home(), Path(directory).resolve())

    def test_frozen_default_config_is_created_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "proxytools.conf").write_text("WORKERS=10\n")
            target = root / "runtime" / "proxytools.conf"
            target.parent.mkdir()
            with patch("sys.frozen", True, create=True), patch(
                "sys._MEIPASS", str(bundle), create=True
            ):
                install_default_config(target)
                self.assertEqual(target.read_text(), "WORKERS=10\n")
                target.write_text("WORKERS=20\n")
                install_default_config(target)
                self.assertEqual(target.read_text(), "WORKERS=20\n")

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
