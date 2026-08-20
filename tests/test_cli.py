import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from proxylister import __version__
from proxylister import about as about_module
from proxylister.about import AUTHORS, BUILD_DATE, DESCRIPTION, format_about
from proxylister.browser_capabilities import BrowserCapabilities
from proxylister import cli
from proxylister.commands import list as list_command
from proxylister.commands import monitor


class TopLevelCliTests(unittest.TestCase):
    def test_help_lists_every_public_command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--help"]), 0)
        for command in ("list", "monitor", "detect_browsers"):
            self.assertIn(command, output.getvalue())
        self.assertIn("--clear", output.getvalue())
        self.assertIn("--about", output.getvalue())

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

    def test_about(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--about"]), 0)
        about = output.getvalue()
        self.assertIn(f"ProxyLister {__version__}", about)
        self.assertIn(DESCRIPTION, about)
        self.assertIn(", ".join(AUTHORS), about)
        self.assertIn(f"Build date: {BUILD_DATE}", about)

    def test_frozen_about_reads_embedded_build_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "proxylister-build.txt").write_text(
                "build_utc=2026-08-09T00:00:00Z\n"
                f"source_commit={'a' * 40}\n",
                encoding="utf-8",
            )
            with mock.patch.object(about_module.sys, "frozen", True, create=True), \
                    mock.patch.object(about_module.sys, "_MEIPASS", directory, create=True):
                about = format_about()
        self.assertIn("Build date: 2026-08-09T00:00:00Z", about)
        self.assertIn(f"Source commit: {'a' * 40}", about)

    def test_subcommand_program_names(self):
        self.assertEqual(list_command.build_parser().prog, "proxylister list")
        self.assertEqual(monitor.build_parser().prog, "proxylister monitor")

    def test_monitor_uses_forgiving_stability_defaults(self):
        settings = monitor.load_config()
        args = monitor.build_parser(settings=settings).parse_args([])
        self.assertEqual(args.max_latency, 500)
        self.assertEqual(settings.max_jitter, 500)
        self.assertEqual(settings.alive_failure_tolerance, 2)

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
            "-h", "--help", "--url", "--max-latency",
        })

    def test_help_version_and_about_do_not_trigger_browser_detection(self):
        with mock.patch(
            "proxylister.browser_capabilities.ensure_browser_capabilities"
        ) as detect, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--help"]), 0)
            self.assertEqual(cli.main(["--version"]), 0)
            self.assertEqual(cli.main(["--about"]), 0)
        detect.assert_not_called()

    def test_normal_command_runs_first_detection_before_geoip_and_dispatch(self):
        events = []
        capabilities = BrowserCapabilities(
            "2026-08-20T00:00:00Z", "linux", ("chrome",), ("chrome",), ()
        )
        geoip_result = mock.Mock(
            path=Path("geoip.mmdb"), updated=False, warning=None
        )

        def ensure(on_detect):
            events.append("detect")
            on_detect()
            return capabilities, True, {}

        def prepare_geoip(**_kwargs):
            events.append("geoip")
            return geoip_result

        with (
            mock.patch.object(
                list_command,
                "main",
                side_effect=lambda _args: events.append("command") or 0,
            ),
            mock.patch.object(cli, "ProcessLock"),
            mock.patch("proxylister.config.load_config"),
            mock.patch(
                "proxylister.browser_capabilities.ensure_browser_capabilities",
                side_effect=ensure,
            ),
            mock.patch(
                "proxylister.browser_capabilities.print_detection_report"
            ) as report,
            mock.patch(
                "proxylister.geoip.ensure_geoip_database",
                side_effect=prepare_geoip,
            ),
            mock.patch("proxylister.geoip.configure_geoip"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["list"]), 0)

        self.assertEqual(events, ["detect", "geoip", "command"])
        report.assert_called_once()


if __name__ == "__main__":
    unittest.main()
