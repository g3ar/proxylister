"""Detect browsers usable by ProxyLister and refresh the host capability cache."""

from __future__ import annotations

import argparse
import sys

from proxylister.browser_capabilities import (
    detect_browser_capabilities,
    print_detection_report,
    save_browser_capabilities,
)
from proxylister.config import load_config


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="proxylister detect_browsers", description=__doc__
    )
    parser.parse_args(argv)
    load_config()
    print(
        "Checking browser capabilities (Selenium Manager may download drivers)...",
        flush=True,
    )
    capabilities, failures = detect_browser_capabilities()
    path = save_browser_capabilities(capabilities)
    print_detection_report(
        capabilities, failures, stream=sys.stdout, details=True
    )
    print(f"Saved browser capabilities to {path}")
    return 0
