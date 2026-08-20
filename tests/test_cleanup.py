import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from proxylister import cleanup
from proxylister.process_lock import ProcessLock


class CleanupTests(unittest.TestCase):
    def test_clear_removes_generated_state_but_preserves_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            generated_files = (
                "proxylister.db", "proxylister.db-wal", "proxylister-geoip.mmdb",
                "proxylister-geoip.version", "proxylister.lock",
                "working_proxies.txt", ".coverage",
            )
            for name in generated_files:
                (home / name).write_text("generated")
            for name in (
                "proxydb/proxylister.db", "proxydb/proxylister.db-wal",
                "proxydb/proxylister.db-shm", "proxydb/browser-capabilities.json",
                "geodb/geoip.mmdb", "geodb/version",
            ):
                path = home / name
                path.parent.mkdir(exist_ok=True)
                path.write_text("generated")
            for name in (".venv", ".pytest_cache", "build", "src/pkg/__pycache__"):
                path = home / name
                path.mkdir(parents=True)
                (path / "artifact").write_text("generated")
            (home / ".env").write_text("SECRET=preserved")
            (home / "results.txt").write_text("preserved")
            (home / ".proxylister-geoip-interrupted").write_text("generated")

            cleanup.clear_runtime(home)

            for name in generated_files:
                self.assertFalse((home / name).exists())
            self.assertFalse((home / ".venv").exists())
            self.assertFalse((home / "src/pkg/__pycache__").exists())
            self.assertFalse((home / ".proxylister-geoip-interrupted").exists())
            self.assertFalse((home / "proxydb").exists())
            self.assertFalse((home / "geodb").exists())
            self.assertTrue((home / ".env").exists())
            self.assertTrue((home / "results.txt").exists())

    def test_clear_runtime_leaves_the_active_lock_for_its_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            lock = home / "proxydb" / "proxylister.lock"
            lock.parent.mkdir()
            with ProcessLock("clear", lock):
                cleanup.clear_runtime(home)
                self.assertTrue(lock.exists())

    def test_clear_refuses_while_clone_is_locked(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PROXYLISTER_HOME": directory}
        ):
            database = Path(directory) / "proxydb" / "proxylister.db"
            database.parent.mkdir()
            database.write_text("keep")
            output = io.StringIO()
            with ProcessLock("monitor"), contextlib.redirect_stderr(output):
                self.assertEqual(cleanup.main(), 1)
            self.assertTrue(database.exists())
            self.assertIn("refusing to clear", output.getvalue())


if __name__ == "__main__":
    unittest.main()
