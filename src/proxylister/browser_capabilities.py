"""Detect and cache browser capabilities belonging to one runtime host.

User preference remains portable in ``proxylister.conf``. Successful browser
probes are machine facts, so they live in ignored runtime state and are
refreshed explicitly by ``detect_browsers`` after the automatic first check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Callable, TextIO

from proxylister.paths import browser_capabilities_path


SCHEMA_VERSION = 1
BROWSER_LABELS = {
    "chrome": "Chrome/Chromium",
    "firefox": "Firefox",
    "edge": "Edge",
    "safari": "Safari",
}


def platform_browsers(platform: str | None = None) -> tuple[str, ...]:
    """Return the deterministic browser-family order for one platform."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return ("safari", "chrome", "firefox", "edge")
    return ("chrome", "firefox", "edge")


@dataclass(frozen=True, slots=True)
class BrowserCapabilities:
    checked_at: str
    platform: str
    selenium: tuple[str, ...]
    headless: tuple[str, ...]
    interactive: tuple[str, ...]
    schema: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "checked_at": self.checked_at,
            "platform": self.platform,
            "selenium": list(self.selenium),
            "headless": list(self.headless),
            "interactive": list(self.interactive),
        }


def _normalized_list(value: object, candidates: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("browser capability must be a list of names")
    names = tuple(value)
    if len(set(names)) != len(names) or any(name not in candidates for name in names):
        raise ValueError("browser capability contains invalid names")
    return names


def load_browser_capabilities(path: Path | None = None) -> BrowserCapabilities | None:
    """Load a valid cache for this platform, or report that detection is due."""
    path = path or browser_capabilities_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        candidates = platform_browsers()
        if data.get("schema") != SCHEMA_VERSION or data.get("platform") != sys.platform:
            return None
        return BrowserCapabilities(
            checked_at=str(data["checked_at"]),
            platform=str(data["platform"]),
            selenium=_normalized_list(data.get("selenium"), candidates),
            headless=_normalized_list(data.get("headless"), candidates),
            interactive=_normalized_list(data.get("interactive"), candidates),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_browser_capabilities(
    capabilities: BrowserCapabilities, path: Path | None = None
) -> Path:
    """Atomically replace the host capability cache."""
    path = path or browser_capabilities_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(capabilities.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def detect_browser_capabilities(
    *,
    selenium_probe: Callable[[str, bool], bool] | None = None,
    interactive_probe: Callable[[str], bool] | None = None,
) -> tuple[BrowserCapabilities, dict[tuple[str, str], str]]:
    """Probe every relevant family and retain concise failure reasons."""
    if selenium_probe is None:
        from proxylister.checking.browser import probe_selenium_browser

        selenium_probe = probe_selenium_browser
    if interactive_probe is None:
        from proxylister.browser import probe_interactive_browser

        interactive_probe = probe_interactive_browser

    available = {"selenium": [], "headless": [], "interactive": []}
    failures = {}

    def supported(family: str, mode: str, probe) -> bool:
        try:
            return bool(probe())
        except Exception as error:  # One broken family must not stop the scan.
            message = " ".join(str(error).split()) or type(error).__name__
            failures[(family, mode)] = f"{type(error).__name__}: {message}"[:240]
            return False

    for family in platform_browsers():
        headed = supported(
            family, "selenium", lambda: selenium_probe(family, False)
        )
        headless = family != "safari" and supported(
            family, "headless", lambda: selenium_probe(family, True)
        )
        interactive = supported(
            family, "interactive", lambda: interactive_probe(family)
        )
        if headed or headless:
            available["selenium"].append(family)
        if headless:
            available["headless"].append(family)
        if interactive:
            available["interactive"].append(family)

    capabilities = BrowserCapabilities(
        checked_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        platform=sys.platform,
        selenium=tuple(available["selenium"]),
        headless=tuple(available["headless"]),
        interactive=tuple(available["interactive"]),
    )
    return capabilities, failures


def ensure_browser_capabilities(on_detect: Callable[[], None] | None = None):
    """Run automatic detection only when this host has no valid cache."""
    current = load_browser_capabilities()
    if current is not None:
        return current, False, {}
    if on_detect is not None:
        on_detect()
    capabilities, failures = detect_browser_capabilities()
    save_browser_capabilities(capabilities)
    return capabilities, True, failures


def browser_candidates(preference: str, available: tuple[str, ...]) -> tuple[str, ...]:
    """Apply one user preference to an already verified capability list."""
    if preference == "auto":
        return available
    requested = tuple(preference.split(","))
    return tuple(family for family in requested if family in available)


def print_detection_report(
    capabilities: BrowserCapabilities,
    failures: dict[tuple[str, str], str] | None = None,
    *,
    stream: TextIO,
    details: bool = False,
) -> None:
    """Print a compact capability report and actionable missing-feature warnings."""
    print("Browser capabilities:", file=stream)
    for family in platform_browsers(capabilities.platform):
        modes = [
            label
            for label, values in (
                ("selenium", capabilities.selenium),
                ("headless", capabilities.headless),
                ("interactive", capabilities.interactive),
            )
            if family in values
        ]
        print(
            f"  {BROWSER_LABELS[family]}: {', '.join(modes) if modes else 'unavailable'}",
            file=stream,
        )
    if not capabilities.selenium:
        print(
            "Warning: no Selenium browser was verified; browser validation is disabled.\n"
            "Install or configure a supported browser, then run: ./proxylister detect_browsers",
            file=stream,
        )
    if not capabilities.interactive:
        print(
            "Warning: no interactive browser was verified; monitor action 'b' is disabled.\n"
            "Install or configure a supported browser, then run: ./proxylister detect_browsers",
            file=stream,
        )
    if details and failures:
        print("Probe failures:", file=stream)
        for family in platform_browsers(capabilities.platform):
            for mode in ("selenium", "headless", "interactive"):
                if reason := failures.get((family, mode)):
                    print(f"  {BROWSER_LABELS[family]} {mode}: {reason}", file=stream)
