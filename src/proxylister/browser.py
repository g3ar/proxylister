"""Launch disposable interactive browser sessions through a selected proxy.

The monitor starts this module's helper in a detached Python process. The
helper creates a temporary browser profile, launches Chrome/Chromium in
incognito mode or Firefox in private mode, waits for it to close, and removes
the profile. No Selenium driver or user's normal browser profile is involved.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


BROWSER_EXECUTABLES = {
    "chrome": ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
    "firefox": ("firefox", "firefox-esr"),
}


class BrowserUnavailable(RuntimeError):
    """Raised when the requested browser is not installed or not in PATH."""


def find_browser(preference: str) -> tuple[str, str]:
    """Return ``(browser_family, executable_path)`` for a CLI preference."""
    families = ("chrome", "firefox") if preference == "auto" else (preference,)
    for family in families:
        for executable in BROWSER_EXECUTABLES[family]:
            path = shutil.which(executable)
            if path:
                return family, path
    names = ", ".join(name for family in families for name in BROWSER_EXECUTABLES[family])
    raise BrowserUnavailable(f"browser not found in PATH (looked for: {names})")


def launch_browser_session(preference: str, protocol: str, address: str, url: str):
    """Start the detached disposable-session helper and return its process."""
    family, executable = find_browser(preference)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "proxylister.browser_session",
            "--family", family,
            "--executable", executable,
            "--protocol", protocol,
            "--address", address,
            "--url", url,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return family, process
