"""Continuously identify stable proxies in an interactive Textual dashboard.

The monitor retains rolling measurements and classifies candidates as
``PROBATION``, ``STABLE``, or ``DEGRADED``. Stability requires continuous
uptime plus configurable count, rate, streak, latency, and jitter thresholds.
Browser-facing country and city data come from the monthly local DB-IP Lite
database (IP Geolocation by DB-IP, https://db-ip.com), using the HTTPS exit IP
observed through each proxy.

Keyboard controls::

    q / Ctrl+C  quit        s  choose visible states
    p  choose protocols     c  choose a country
    b  open selected proxy  y  copy connection string
    Enter  show selected proxy analytics

Examples::

    ./proxylister monitor
    ./proxylister monitor --url https://example.com --max-latency 350
"""

import argparse

from proxylister.config import (
    load_config,
    positive_float,
    web_url,
)
from proxylister.monitoring import MonitorEngine
from proxylister.output.dashboard import ProxyMonitorApp
from proxylister.paths import database_path
from proxylister.stability import StabilityConfig, StabilityPolicy
from proxylister.storage import StateRepository


def build_parser(prog="proxylister monitor", settings=None):
    settings = settings or load_config()
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--url", type=web_url, default=settings.url,
        help="URL that every proxy must reach; also opened by 'b'",
    )
    parser.add_argument(
        "--max-latency", type=positive_float, default=settings.max_latency,
        help=f"Maximum median latency in ms (config: {settings.max_latency:g})",
    )
    return parser


def policy_from_args(args, settings=None):
    settings = settings or load_config()
    return StabilityPolicy(
        StabilityConfig(
            history_size=settings.history_size,
            min_checks=settings.min_checks,
            min_success_rate=settings.min_success_rate,
            min_success_streak=settings.min_success_streak,
            min_alive_time=settings.min_alive_time,
            max_latency=args.max_latency,
            max_jitter=settings.max_jitter,
            failure_tolerance=settings.alive_failure_tolerance,
            degraded_after=settings.degraded_after,
        )
    )


def engine_from_args(args, repository=None, settings=None):
    settings = settings or load_config()
    return MonitorEngine(
        policy=policy_from_args(args, settings),
        workers=settings.workers,
        timeout=settings.timeout,
        samples=settings.samples,
        refresh_interval=settings.refresh_interval,
        retention_time=settings.retention_time,
        repository=repository,
        target_url=args.url,
    )


def main(argv=None):
    settings = load_config()
    parser = build_parser(settings=settings)
    args = parser.parse_args(argv)
    repository = StateRepository(database_path())
    try:
        ProxyMonitorApp(
            engine_from_args(args, repository, settings),
            browser=settings.browser,
            browser_url=args.url or "about:blank",
        ).run()
    finally:
        repository.close()
    return 0
