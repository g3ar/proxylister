import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "proxylister"
LAUNCHER_NAMESPACE = runpy.run_path(os.fspath(LAUNCHER))


class LauncherTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX executable mode is not used on Windows")
    def test_root_launcher_is_executable(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))

    def test_virtual_environment_interpreter_is_platform_specific(self):
        venv = Path("checkout") / ".venv"
        resolve = LAUNCHER_NAMESPACE["_venv_python"]
        self.assertEqual(resolve(venv, "posix"), venv / "bin/python")
        self.assertEqual(resolve(venv, "nt"), venv / "Scripts/python.exe")

    def test_options_without_a_mode_are_routed_to_list(self):
        route = LAUNCHER_NAMESPACE["_command"]
        self.assertEqual(route([]), ("list", []))
        self.assertEqual(
            route(["--max-latency", "300"]),
            ("list", ["--max-latency", "300"]),
        )
        self.assertEqual(route(["monitor", "--help"]), ("monitor", ["--help"]))

    def test_help_needs_no_environment_and_works_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, LAUNCHER, "--help"],
                cwd=directory,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: ./proxylister", result.stdout)
        self.assertFalse(result.stderr)

    def test_unknown_command_returns_usage_error_without_bootstrap(self):
        result = subprocess.run(
            [sys.executable, LAUNCHER, "unknown"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown command: unknown", result.stderr)


if __name__ == "__main__":
    unittest.main()
