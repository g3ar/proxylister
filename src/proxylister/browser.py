"""Launch disposable interactive browser sessions through a selected proxy.

The monitor starts this module's helper in a detached Python process. The
helper creates a temporary browser profile, launches Chromium, Firefox, or
Edge in private mode, waits for it to close, and removes the profile. No
Selenium driver or user's normal browser profile is involved.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from proxylister.browser_capabilities import browser_candidates


BROWSER_EXECUTABLES = {
    "chrome": (
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
        "chrome", "chrome.exe",
    ),
    "firefox": ("firefox", "firefox-esr", "firefox.exe"),
    "edge": ("microsoft-edge", "microsoft-edge-stable", "msedge", "msedge.exe"),
}

WINDOWS_RELATIVE_PATHS = {
    "chrome": (
        "Google/Chrome/Application/chrome.exe",
        "Chromium/Application/chrome.exe",
    ),
    "firefox": ("Mozilla Firefox/firefox.exe",),
    "edge": ("Microsoft/Edge/Application/msedge.exe",),
}


class BrowserUnavailable(RuntimeError):
    """Raised when no verified interactive browser can be launched."""


def _windows_install_paths(family: str):
    roots = []
    for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        value = os.environ.get(name)
        if value and value not in roots:
            roots.append(value)
    for root in roots:
        for relative in WINDOWS_RELATIVE_PATHS.get(family, ()):
            yield Path(root) / Path(relative)


def _registry_install_paths(family: str):
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    executable_names = {
        "chrome": ("chrome.exe",),
        "firefox": ("firefox.exe",),
        "edge": ("msedge.exe",),
    }
    views = (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY)
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for executable in executable_names.get(family, ()):
            for view in views:
                key_name = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
                try:
                    with winreg.OpenKey(
                        root, key_name, 0, winreg.KEY_READ | view
                    ) as key:
                        value, _kind = winreg.QueryValueEx(key, None)
                except OSError:
                    continue
                if value:
                    yield Path(value)


def find_browser(
    preference: str, available: tuple[str, ...] = ("chrome", "firefox", "edge")
) -> tuple[str, str]:
    """Return an installed interactive browser permitted by the verified cache."""
    families = browser_candidates(preference, available)
    for family in families:
        for executable in BROWSER_EXECUTABLES[family]:
            path = shutil.which(executable)
            if path:
                return family, path
        if sys.platform == "win32":
            for path in (*_registry_install_paths(family), *_windows_install_paths(family)):
                if path.is_file():
                    return family, os.fspath(path)
    requested = ", ".join(families) or preference
    raise BrowserUnavailable(
        f"no verified interactive browser is available ({requested}); "
        "run ./proxylister detect_browsers"
    )


def probe_interactive_browser(family: str) -> bool:
    """Confirm that a supported native private-session executable is present."""
    if family == "safari":
        # Safari cannot satisfy the monitor's per-session proxy/profile
        # isolation contract through a native command-line launch.
        return False
    try:
        find_browser(family, (family,))
    except BrowserUnavailable:
        return False
    return True


def launch_browser_session(
    preference: str,
    available: tuple[str, ...],
    protocol: str,
    address: str,
    url: str,
):
    """Start the detached disposable-session helper and return its process."""
    family, executable = find_browser(preference, available)
    helper = (
        [sys.executable, "_browser_session"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "proxylister.browser_session"]
    )
    process = subprocess.Popen(
        [
            *helper,
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
