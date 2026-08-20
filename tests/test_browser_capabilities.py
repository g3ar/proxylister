import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from proxylister import browser_capabilities as capabilities_module
from proxylister.browser_capabilities import (
    BrowserCapabilities,
    browser_candidates,
    detect_browser_capabilities,
    ensure_browser_capabilities,
    load_browser_capabilities,
    print_detection_report,
    save_browser_capabilities,
)
from proxylister.commands import detect_browsers


class BrowserCapabilityTests(unittest.TestCase):
    def _capabilities(self, **overrides):
        values = {
            "checked_at": "2026-08-20T00:00:00Z",
            "platform": sys.platform,
            "selenium": ("chrome", "firefox"),
            "headless": ("chrome",),
            "interactive": ("firefox",),
        }
        values.update(overrides)
        return BrowserCapabilities(**values)

    def test_cache_round_trip_is_atomic_and_platform_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxydb" / "browser-capabilities.json"
            expected = self._capabilities()

            self.assertEqual(save_browser_capabilities(expected, path), path)
            self.assertEqual(load_browser_capabilities(path), expected)
            self.assertFalse(path.with_name(f".{path.name}.tmp").exists())

            data = json.loads(path.read_text(encoding="utf-8"))
            data["platform"] = "different-platform"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(load_browser_capabilities(path))

    def test_invalid_or_unknown_browser_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser-capabilities.json"
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(load_browser_capabilities(path))
            path.write_text(
                json.dumps({
                    "schema": 1,
                    "checked_at": "now",
                    "platform": sys.platform,
                    "selenium": ["opera"],
                    "headless": [],
                    "interactive": [],
                }),
                encoding="utf-8",
            )
            self.assertIsNone(load_browser_capabilities(path))

    def test_detection_records_each_capability_independently(self):
        selenium_calls = []

        def selenium_probe(family, headless):
            selenium_calls.append((family, headless))
            return (family, headless) in {
                ("chrome", False),
                ("firefox", False),
                ("firefox", True),
                ("edge", True),
            }

        with patch.object(capabilities_module.sys, "platform", "linux"):
            detected, failures = detect_browser_capabilities(
                selenium_probe=selenium_probe,
                interactive_probe=lambda family: family == "edge",
            )

        self.assertEqual(detected.selenium, ("chrome", "firefox", "edge"))
        self.assertEqual(detected.headless, ("firefox", "edge"))
        self.assertEqual(detected.interactive, ("edge",))
        self.assertEqual(failures, {})
        self.assertEqual(
            selenium_calls,
            [
                ("chrome", False), ("chrome", True),
                ("firefox", False), ("firefox", True),
                ("edge", False), ("edge", True),
            ],
        )

    def test_safari_is_probed_on_macos_but_not_headless(self):
        selenium_calls = []
        with patch.object(capabilities_module.sys, "platform", "darwin"):
            detected, failures = detect_browser_capabilities(
                selenium_probe=lambda family, headless: (
                    selenium_calls.append((family, headless)) or family == "safari"
                ),
                interactive_probe=lambda _family: False,
            )

        self.assertEqual(detected.selenium, ("safari",))
        self.assertEqual(detected.headless, ())
        self.assertNotIn(("safari", True), selenium_calls)
        self.assertEqual(failures, {})

    def test_preference_filters_and_orders_verified_browsers(self):
        available = ("chrome", "firefox", "edge")
        self.assertEqual(browser_candidates("auto", available), available)
        self.assertEqual(
            browser_candidates("edge,chrome", available), ("edge", "chrome")
        )
        self.assertEqual(browser_candidates("safari", available), ())

    def test_empty_cache_prevents_repeated_automatic_detection(self):
        empty = self._capabilities(selenium=(), headless=(), interactive=())
        with patch.object(
            capabilities_module, "load_browser_capabilities", return_value=empty
        ), patch.object(capabilities_module, "detect_browser_capabilities") as detect:
            current, ran, failures = ensure_browser_capabilities()

        self.assertEqual(current, empty)
        self.assertFalse(ran)
        self.assertEqual(failures, {})
        detect.assert_not_called()

    def test_first_automatic_detection_announces_work_once(self):
        detected = self._capabilities()
        announce = Mock()
        with patch.object(
            capabilities_module, "load_browser_capabilities", return_value=None
        ), patch.object(
            capabilities_module,
            "detect_browser_capabilities",
            return_value=(detected, {}),
        ), patch.object(
            capabilities_module, "save_browser_capabilities"
        ) as save:
            current, ran, failures = ensure_browser_capabilities(announce)

        self.assertEqual(current, detected)
        self.assertTrue(ran)
        self.assertEqual(failures, {})
        announce.assert_called_once_with()
        save.assert_called_once_with(detected)

    def test_report_lists_modes_and_actionable_warnings(self):
        output = io.StringIO()
        detected = self._capabilities(
            selenium=(), headless=(), interactive=("edge",)
        )
        print_detection_report(detected, stream=output)
        report = output.getvalue()
        self.assertIn("Edge: interactive", report)
        self.assertIn("no Selenium browser was verified", report)
        self.assertNotIn("no interactive browser was verified", report)
        self.assertIn("./proxylister detect_browsers", report)

    def test_manual_report_shows_concise_probe_failures_without_traceback(self):
        output = io.StringIO()
        detected = self._capabilities(selenium=(), headless=())
        failures = {
            ("chrome", "selenium"): "WebDriverException: driver unavailable"
        }
        print_detection_report(
            detected, failures, stream=output, details=True
        )
        report = output.getvalue()
        self.assertIn(
            "Chrome/Chromium selenium: WebDriverException: driver unavailable",
            report,
        )
        self.assertNotIn("Traceback", report)

    def test_manual_command_always_refreshes_and_saves_cache(self):
        detected = self._capabilities()
        output = io.StringIO()
        saved = Path("proxydb/browser-capabilities.json")
        with patch.object(detect_browsers, "load_config"), patch.object(
            detect_browsers,
            "detect_browser_capabilities",
            return_value=(detected, {}),
        ) as detect, patch.object(
            detect_browsers, "save_browser_capabilities", return_value=saved
        ) as save, contextlib.redirect_stdout(output):
            self.assertEqual(detect_browsers.main([]), 0)

        detect.assert_called_once_with()
        save.assert_called_once_with(detected)
        self.assertIn("Saved browser capabilities", output.getvalue())


if __name__ == "__main__":
    unittest.main()
