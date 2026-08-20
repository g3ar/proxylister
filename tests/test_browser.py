import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from proxylister.browser import BrowserUnavailable, find_browser
from proxylister.browser_session import chrome_command, edge_command, firefox_command
from proxylister.checking import browser as selenium_browser
from proxylister.models import ProxyResult


class BrowserSessionTests(unittest.TestCase):
    def test_auto_prefers_chrome_and_explicit_firefox_is_respected(self):
        paths = {"google-chrome": "/bin/chrome", "firefox": "/bin/firefox"}
        with patch("proxylister.browser.shutil.which", side_effect=paths.get):
            self.assertEqual(find_browser("auto"), ("chrome", "/bin/chrome"))
            self.assertEqual(find_browser("firefox"), ("firefox", "/bin/firefox"))

    def test_missing_browser_has_useful_error(self):
        with patch("proxylister.browser.sys.platform", "linux"), patch(
            "proxylister.browser.shutil.which", return_value=None
        ):
            with self.assertRaises(BrowserUnavailable):
                find_browser("auto")

    def test_ordered_preference_uses_first_verified_installed_browser(self):
        paths = {"google-chrome": "/bin/chrome", "firefox": "/bin/firefox"}
        with patch("proxylister.browser.shutil.which", side_effect=paths.get):
            self.assertEqual(
                find_browser("firefox,chrome", ("chrome", "firefox")),
                ("firefox", "/bin/firefox"),
            )

    def test_windows_standard_install_path_finds_edge_outside_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Microsoft/Edge/Application/msedge.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            with patch("proxylister.browser.sys.platform", "win32"), patch.dict(
                "proxylister.browser.os.environ",
                {"PROGRAMFILES": directory},
                clear=True,
            ), patch(
                "proxylister.browser.shutil.which", return_value=None
            ), patch(
                "proxylister.browser._registry_install_paths", return_value=()
            ):
                self.assertEqual(
                    find_browser("edge", ("edge",)),
                    ("edge", str(executable)),
                )

    def test_chrome_uses_incognito_temporary_profile_and_proxy(self):
        profile = Path("/tmp/profile")
        command = chrome_command("chrome", profile, "socks5", "1.2.3.4:1080", "about:blank")
        self.assertIn("--incognito", command)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--proxy-server=socks5://1.2.3.4:1080", command)

    def test_edge_uses_inprivate_temporary_profile_and_proxy(self):
        profile = Path("/tmp/profile")
        command = edge_command(
            "msedge", profile, "http", "1.2.3.4:8080", "about:blank"
        )
        self.assertIn("--inprivate", command)
        self.assertNotIn("--incognito", command)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--proxy-server=http://1.2.3.4:8080", command)

    def test_firefox_writes_isolated_http_proxy_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            command = firefox_command("firefox", profile, "http", "1.2.3.4:8080", "about:blank")
            preferences = (profile / "user.js").read_text(encoding="utf-8")
            self.assertIn('"network.proxy.http"', preferences)
            self.assertIn('"network.proxy.ssl"', preferences)
            self.assertIn("-private-window", command)
            self.assertIn("-no-remote", command)

    def test_firefox_configures_socks_version(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            firefox_command("firefox", profile, "socks5", "1.2.3.4:1080", "about:blank")
            preferences = (profile / "user.js").read_text(encoding="utf-8")
            self.assertIn('"network.proxy.socks_version", 5', preferences)

    def test_selenium_selector_falls_back_only_when_driver_cannot_start(self):
        result = ProxyResult("http", "1.2.3.4:8080", True, 50)
        driver = Mock()
        selector = selenium_browser.SeleniumBrowserSelector(("firefox", "edge"))
        with patch.object(
            selenium_browser,
            "_start_driver",
            side_effect=(RuntimeError("missing driver"), driver),
        ) as start, patch.object(
            selenium_browser, "_validate_page", return_value=True
        ) as validate:
            self.assertTrue(selector.verify(result, "https://example.com", 10, True))

        self.assertEqual([call.args[0] for call in start.call_args_list], ["firefox", "edge"])
        validate.assert_called_once_with(driver, "https://example.com", 10, True)
        self.assertEqual(selector.selected, "edge")
        driver.quit.assert_called_once_with()

        second_driver = Mock()
        with patch.object(
            selenium_browser, "_start_driver", return_value=second_driver
        ) as start, patch.object(
            selenium_browser, "_validate_page", return_value=False
        ):
            self.assertFalse(selector.verify(result, "https://example.com", 10, True))
        start.assert_called_once_with("edge", result, True)
        second_driver.quit.assert_called_once_with()

    def test_selenium_probe_closes_the_driver(self):
        driver = Mock()
        with patch.object(selenium_browser, "_start_driver", return_value=driver):
            self.assertTrue(selenium_browser.probe_selenium_browser("chrome", True))
        driver.set_page_load_timeout.assert_called_once_with(
            selenium_browser.MIN_PAGE_LOAD_TIMEOUT
        )
        driver.get.assert_called_once_with("about:blank")
        driver.quit.assert_called_once_with()

    def test_selenium_manager_defaults_are_bounded_and_do_not_replace_user_values(self):
        original_timeout = os.environ.get("SE_TIMEOUT")
        original_avoid_download = os.environ.get("SE_AVOID_BROWSER_DOWNLOAD")
        os.environ.pop("SE_TIMEOUT", None)
        os.environ["SE_AVOID_BROWSER_DOWNLOAD"] = "false"
        observed = {}
        webdriver = Mock()
        webdriver.ChromeOptions.return_value = Mock()
        webdriver.Chrome.side_effect = lambda **_kwargs: observed.update(
            timeout=os.environ.get("SE_TIMEOUT"),
            avoid_download=os.environ.get("SE_AVOID_BROWSER_DOWNLOAD"),
        ) or Mock()
        try:
            with patch.object(
                selenium_browser,
                "_selenium",
                return_value=(webdriver, Exception, Exception, Mock),
            ):
                selenium_browser._start_driver("chrome", None, True)
            self.assertEqual(
                observed,
                {
                    "timeout": str(selenium_browser.SELENIUM_MANAGER_TIMEOUT),
                    "avoid_download": "false",
                },
            )
            self.assertNotIn("SE_TIMEOUT", os.environ)
        finally:
            if original_timeout is None:
                os.environ.pop("SE_TIMEOUT", None)
            else:
                os.environ["SE_TIMEOUT"] = original_timeout
            if original_avoid_download is None:
                os.environ.pop("SE_AVOID_BROWSER_DOWNLOAD", None)
            else:
                os.environ["SE_AVOID_BROWSER_DOWNLOAD"] = original_avoid_download

    def test_safari_headless_is_rejected_before_driver_start(self):
        with self.assertRaisesRegex(RuntimeError, "does not support headless"):
            selenium_browser._options("safari", None, True)


if __name__ == "__main__":
    unittest.main()
