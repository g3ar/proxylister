import contextlib
import io
import unittest

from proxytools import __version__
from proxytools import cli
from proxytools.commands import list as list_command
from proxytools.commands import monitor


class TopLevelCliTests(unittest.TestCase):
    def test_help_lists_every_public_command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--help"]), 0)
        for command in ("list", "monitor"):
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
        self.assertEqual(list_command.build_parser().prog, "proxytools list")
        self.assertEqual(monitor.build_parser().prog, "proxytools monitor")

    def test_monitor_uses_forgiving_stability_defaults(self):
        settings = monitor.load_config()
        args = monitor.build_parser(settings=settings).parse_args([])
        self.assertEqual(args.max_latency, 500)
        self.assertEqual(settings.max_jitter, 500)
        self.assertEqual(settings.alive_failure_tolerance, 2)
        self.assertFalse(args.debug)
        self.assertTrue(monitor.build_parser().parse_args(["--debug"]).debug)

    def test_list_and_monitor_share_url_option(self):
        url = "https://example.com"
        self.assertEqual(list_command.build_parser().parse_args(["--url", url]).url, url)
        self.assertEqual(monitor.build_parser().parse_args(["--url", url]).url, url)

    def test_list_browser_flags_require_their_dependencies(self):
        parser = list_command.build_parser()
        for arguments in (["--browser-check"], ["--headless"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                list_command.validate_args(parser, parser.parse_args(arguments))
        args = parser.parse_args([
            "--url", "https://example.com", "--browser-check", "--headless"
        ])
        list_command.validate_args(parser, args)

    def test_public_command_option_surface_stays_small(self):
        list_options = {
            option
            for action in list_command.build_parser()._actions
            for option in action.option_strings
        }
        monitor_options = {
            option
            for action in monitor.build_parser()._actions
            for option in action.option_strings
        }
        self.assertEqual(list_options, {
            "-h", "--help", "--debug", "--url", "--max-latency",
            "--browser-check", "--headless",
        })
        self.assertEqual(monitor_options, {
            "-h", "--help", "--debug", "--url", "--max-latency",
        })


if __name__ == "__main__":
    unittest.main()
