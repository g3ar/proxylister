import os
import unittest
from unittest.mock import patch

from proxylister import external_process


class ExternalProcessEnvironmentTests(unittest.TestCase):
    def test_windows_clears_and_restores_frozen_dll_directory(self):
        with patch.object(
            external_process.sys, "frozen", True, create=True
        ), patch.object(
            external_process.sys, "platform", "win32"
        ), patch.object(
            external_process.sys, "_MEIPASS", r"C:\bundle", create=True
        ), patch.object(
            external_process, "_set_windows_dll_directory"
        ) as set_directory:
            with external_process.external_program_environment():
                set_directory.assert_called_once_with(None)

        self.assertEqual(
            [call.args[0] for call in set_directory.call_args_list],
            [None, r"C:\bundle"],
        )

    def test_posix_restores_original_loader_path_only_for_the_child_window(self):
        environment = {
            "LD_LIBRARY_PATH": "/tmp/_MEI:/system/current",
            "LD_LIBRARY_PATH_ORIG": "/system/original",
        }
        with patch.object(
            external_process.sys, "frozen", True, create=True
        ), patch.object(
            external_process.sys, "platform", "linux"
        ), patch.dict(external_process.os.environ, environment, clear=True):
            with external_process.external_program_environment():
                self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/system/original")
            self.assertEqual(
                os.environ["LD_LIBRARY_PATH"], "/tmp/_MEI:/system/current"
            )


if __name__ == "__main__":
    unittest.main()
