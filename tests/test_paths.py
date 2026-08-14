"""Tests for clone-local runtime layout and legacy-file migration."""

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
from proxylister.config import config_path


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

    def test_former_project_names_move_into_current_runtime_layout(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PROXYTOOLS_HOME": directory},
            clear=True,
        ):
            home = Path(directory)
            (home / "proxydb").mkdir()
            legacy = {
                "proxydb/proxytools.db": b"database",
                "proxydb/proxytools.db-wal": b"wal",
                "proxydb/proxytools.db-shm": b"shm",
                "proxydb/proxytools.lock": b"lock",
                "proxytools-geoip.mmdb": b"geoip",
                "proxytools-geoip.version": b"2026-08\n",
                "proxytools.conf": b"WORKERS=10\n",
            }
            for name, content in legacy.items():
                (home / name).write_bytes(content)

            self.assertEqual(tool_home(), home.resolve())
            self.assertEqual(database_path(), home / "proxydb" / "proxylister.db")
            self.assertEqual(lock_path(), home / "proxydb" / "proxylister.lock")
            self.assertEqual(geoip_database_path(), home / "geodb" / "geoip.mmdb")
            self.assertEqual(geoip_version_path(), home / "geodb" / "version")
            config = config_path()
            install_default_config(config)
            self.assertEqual(config, home / "proxylister.conf")

            self.assertEqual((home / "proxydb" / "proxylister.db-wal").read_bytes(), b"wal")
            self.assertEqual((home / "proxydb" / "proxylister.db-shm").read_bytes(), b"shm")
            self.assertEqual((home / "proxylister.conf").read_bytes(), b"WORKERS=10\n")
            self.assertFalse(any((home / name).exists() for name in legacy))


if __name__ == "__main__":
    unittest.main()
