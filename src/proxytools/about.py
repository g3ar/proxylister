"""Shared project identity displayed by the CLI and Textual monitor."""

from proxytools import __version__


NAME = "Proxy Tools"
DESCRIPTION = (
    "Discovers, validates, and continuously monitors public HTTP, SOCKS4, "
    "and SOCKS5 proxies."
)
AUTHORS = ("gear", "aider", "ChatGPT 5.6 Sol")
BUILD_DATE = "2026"


def format_about() -> str:
    """Return the authoritative human-readable project identity."""
    return (
        f"{NAME} {__version__}\n\n"
        f"{DESCRIPTION}\n\n"
        f"Authors: {', '.join(AUTHORS)}\n"
        f"Build date: {BUILD_DATE}"
    )
