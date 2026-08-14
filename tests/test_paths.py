"""Tests for clone-local runtime layout and root-file migration."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from proxylister.paths import (
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
            executable = Path(directory) / "proxylister"
            with patch.dict(os.environ, {}, clear=True), patch(
                "sys.frozen", True, create=True
            ), patch("sys.executable", str(executable)):
                self.assertEqual(tool_home(), Path(directory).resolve())

    def test_frozen_default_config_is_created_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "proxylister.conf").write_text("WORKERS=10\n")
            target = root / "runtime" / "proxylister.conf"
            target.parent.mkdir()
            with patch("sys.frozen", True, create=True), patch(
                "sys._MEIPASS", str(bundle), create=True
            ):
                install_default_config(target)
                self.assertEqual(target.read_text(), "WORKERS=10\n")
                target.write_text("WORKERS=20\n")
                install_default_config(target)
                self.assertEqual(target.read_text(), "WORKERS=20\n")

    def test_root_runtime_files_move_into_runtime_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PROXYLISTER_HOME": directory},
            clear=True,
        ):
            home = Path(directory)
            root_files = {
                "proxylister.db": b"database",
                "proxylister.db-wal": b"wal",
                "proxylister.db-shm": b"shm",
                "proxylister.lock": b"lock",
                "proxylister-geoip.mmdb": b"geoip",
                "proxylister-geoip.version": b"2026-08\n",
            }
            for name, content in root_files.items():
                (home / name).write_bytes(content)

            self.assertEqual(tool_home(), home.resolve())
            self.assertEqual(database_path(), home / "proxydb" / "proxylister.db")
            self.assertEqual(lock_path(), home / "proxydb" / "proxylister.lock")
            self.assertEqual(geoip_database_path(), home / "geodb" / "geoip.mmdb")
            self.assertEqual(geoip_version_path(), home / "geodb" / "version")
            self.assertEqual((home / "proxydb" / "proxylister.db").read_bytes(), b"database")
            self.assertEqual((home / "proxydb" / "proxylister.db-wal").read_bytes(), b"wal")
            self.assertEqual((home / "proxydb" / "proxylister.db-shm").read_bytes(), b"shm")
            self.assertEqual((home / "proxydb" / "proxylister.lock").read_bytes(), b"lock")
            self.assertEqual((home / "geodb" / "geoip.mmdb").read_bytes(), b"geoip")
            self.assertEqual((home / "geodb" / "version").read_bytes(), b"2026-08\n")
            self.assertFalse(any((home / name).exists() for name in root_files))


if __name__ == "__main__":
    unittest.main()
