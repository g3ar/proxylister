import contextlib
import io
import unittest

from proxytools import __version__
from proxytools import cli
from proxytools.commands import monitor, scan


class TopLevelCliTests(unittest.TestCase):
    def test_help_lists_every_public_command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main([]), 0)
        for command in ("scan", "monitor"):
            self.assertIn(command, output.getvalue())
        self.assertIn("--clear", output.getvalue())

    def test_unknown_command_returns_usage_error(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(["unknown"]), 2)
        self.assertIn("unknown command", output.getvalue())

    def test_version(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(output.getvalue().strip(), __version__)

    def test_subcommand_program_names(self):
        self.assertEqual(scan.build_parser().prog, "proxytools scan")
        self.assertEqual(monitor.build_parser().prog, "proxytools monitor")

    def test_monitor_uses_forgiving_stability_defaults(self):
        args = monitor.build_parser().parse_args([])
        self.assertEqual(args.max_latency, 500)
        self.assertEqual(args.max_jitter, 500)
        self.assertEqual(args.alive_failure_tolerance, 2)
        self.assertFalse(args.debug)
        self.assertTrue(monitor.build_parser().parse_args(["--debug"]).debug)


if __name__ == "__main__":
    unittest.main()
