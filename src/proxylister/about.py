"""Shared project identity displayed by the CLI and Textual monitor."""

from pathlib import Path
import sys

from proxylister import __version__


NAME = "ProxyLister"
DESCRIPTION = (
    "Discovers, validates, and continuously monitors public HTTP, SOCKS4, "
    "and SOCKS5 proxies."
)
AUTHORS = ("gear", "aider", "ChatGPT 5.6 Sol")
BUILD_DATE = "2026"


def _frozen_build_metadata() -> dict[str, str]:
    """Read metadata generated and embedded by the standalone build."""
    if not getattr(sys, "frozen", False):
        return {}
    metadata_file = Path(getattr(sys, "_MEIPASS")) / "proxylister-build.txt"
    try:
        lines = metadata_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    metadata = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key and value:
            metadata[key] = value
    return metadata


def format_about() -> str:
    """Return the authoritative human-readable project identity."""
    metadata = _frozen_build_metadata()
    build_date = metadata.get("build_utc", BUILD_DATE)
    result = (
        f"{NAME} {__version__}\n\n"
        f"{DESCRIPTION}\n\n"
        f"Authors: {', '.join(AUTHORS)}\n"
        f"Build date: {build_date}"
    )
    source_commit = metadata.get("source_commit")
    if source_commit:
        result += f"\nSource commit: {source_commit}"
    return result
