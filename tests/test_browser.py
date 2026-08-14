import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proxylister.browser import BrowserUnavailable, find_browser
from proxylister.browser_session import chrome_command, firefox_command


class BrowserSessionTests(unittest.TestCase):
    def test_auto_prefers_chrome_and_explicit_firefox_is_respected(self):
        paths = {"google-chrome": "/bin/chrome", "firefox": "/bin/firefox"}
        with patch("proxylister.browser.shutil.which", side_effect=paths.get):
            self.assertEqual(find_browser("auto"), ("chrome", "/bin/chrome"))
            self.assertEqual(find_browser("firefox"), ("firefox", "/bin/firefox"))

    def test_missing_browser_has_useful_error(self):
        with patch("proxylister.browser.shutil.which", return_value=None):
            with self.assertRaises(BrowserUnavailable):
                find_browser("auto")

    def test_chrome_uses_incognito_temporary_profile_and_proxy(self):
        profile = Path("/tmp/profile")
        command = chrome_command("chrome", profile, "socks5", "1.2.3.4:1080", "about:blank")
        self.assertIn("--incognito", command)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--proxy-server=socks5://1.2.3.4:1080", command)

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


if __name__ == "__main__":
    unittest.main()
