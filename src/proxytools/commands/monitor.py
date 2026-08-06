"""Continuously identify stable proxies in an interactive Textual dashboard.

The monitor retains rolling measurements and classifies candidates as
``PROBATION``, ``STABLE``, or ``DEGRADED``. Stability requires continuous
uptime plus configurable count, rate, streak, latency, and jitter thresholds.

Keyboard controls::

    q  quit                 p  pause display (checks continue)
    s  choose visible states
    c  choose a country
    r  force next cycle     b  open selected proxy in a private browser

Examples::

    ./proxytools monitor --min-alive-time 60 --refresh-interval 10
    ./proxytools monitor --min-success-rate 0.9 --max-jitter 100 --stable-only
"""

import argparse

from proxytools.config import (
    nonnegative_float,
    nonnegative_int,
    positive_float,
    positive_int,
    probability,
    sample_count,
    web_url,
    worker_count,
)
from proxytools.monitoring import MonitorEngine
from proxytools.output.dashboard import ProxyMonitorApp
from proxytools.paths import database_path
from proxytools.stability import StabilityConfig, StabilityPolicy
from proxytools.storage import StateRepository


def build_parser(prog="proxytools monitor"):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    network = parser.add_argument_group("network")
    network.add_argument("-t", "--timeout", type=positive_float, default=5, help="Seconds per proxy check")
    network.add_argument("-w", "--workers", type=worker_count, default=50, help="Number of workers (1-100)")
    network.add_argument("--samples", type=sample_count, default=1, help="Requests per check; median duration (1-5)")
    network.add_argument("--refresh-interval", type=positive_float, default=10, help="Seconds between scan cycles")

    stability = parser.add_argument_group("stability")
    stability.add_argument("-l", "--max-latency", type=positive_float, default=500, help="Maximum median latency in ms")
    stability.add_argument("--history-size", type=positive_int, default=10, help="Recent checks retained per proxy")
    stability.add_argument("--min-checks", type=positive_int, default=5, help="Checks required before stable")
    stability.add_argument("--min-success-rate", type=probability, default=0.8, help="Required success ratio (0-1)")
    stability.add_argument("--min-success-streak", type=positive_int, default=3, help="Consecutive successes required")
    stability.add_argument("--min-alive-time", type=nonnegative_float, default=60, help="Continuous live seconds required")
    stability.add_argument("--max-jitter", type=nonnegative_float, default=150, help="Maximum latency deviation in ms")
    stability.add_argument("--alive-failure-tolerance", type=nonnegative_int, default=0, help="Failures allowed before alive time resets")
    stability.add_argument("--retention-time", type=positive_float, default=1800, help="Seconds to retain unadvertised proxies")

    display = parser.add_argument_group("display")
    display.add_argument("--stable-only", action="store_true", help="Initially show only stable proxies")
    browser = parser.add_argument_group("browser")
    browser.add_argument(
        "--browser", choices=("auto", "chrome", "firefox"), default="auto",
        help="Browser used by the 'b' action (default: auto)",
    )
    browser.add_argument(
        "--browser-url", type=web_url,
        help="URL that every proxy must reach via requests; also opened by 'b'",
    )
    persistence = parser.add_argument_group("persistence")
    persistence.add_argument(
        "--reset-history", action="store_true",
        help="Delete this clone's saved proxy history before monitoring",
    )
    return parser


def validate_args(parser, args):
    if args.min_checks > args.history_size:
        parser.error("--min-checks cannot exceed --history-size")
    if args.min_success_streak > args.history_size:
        parser.error("--min-success-streak cannot exceed --history-size")


def policy_from_args(args):
    return StabilityPolicy(
        StabilityConfig(
            history_size=args.history_size,
            min_checks=args.min_checks,
            min_success_rate=args.min_success_rate,
            min_success_streak=args.min_success_streak,
            min_alive_time=args.min_alive_time,
            max_latency=args.max_latency,
            max_jitter=args.max_jitter,
            failure_tolerance=args.alive_failure_tolerance,
        )
    )


def engine_from_args(args, repository=None):
    return MonitorEngine(
        policy=policy_from_args(args),
        workers=args.workers,
        timeout=args.timeout,
        samples=args.samples,
        refresh_interval=args.refresh_interval,
        retention_time=args.retention_time,
        repository=repository,
        target_url=args.browser_url,
    )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    repository = StateRepository(database_path())
    try:
        if args.reset_history:
            repository.reset()
        ProxyMonitorApp(
            engine_from_args(args, repository),
            stable_only=args.stable_only,
            browser=args.browser,
            browser_url=args.browser_url or "about:blank",
        ).run()
    finally:
        repository.close()
    return 0
