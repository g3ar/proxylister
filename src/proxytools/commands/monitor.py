"""Continuously identify stable proxies in a live terminal dashboard.

The monitor retains rolling measurements and classifies candidates as
``PROBATION``, ``STABLE``, or ``DEGRADED``.  Stability requires continuous
uptime plus configurable count, rate, streak, latency, and jitter thresholds.

Examples::

    ./proxytools monitor --min-alive-time 60 --refresh-interval 10
    ./proxytools monitor --min-success-rate 0.9 --max-jitter 100 --stable-only
"""

import argparse
import concurrent.futures
import curses
import os
import time

from proxytools.checking import check_proxy
from proxytools.config import (
    nonnegative_float,
    nonnegative_int,
    positive_float,
    positive_int,
    probability,
    sample_count,
    worker_count,
)
from proxytools.output.dashboard import (
    configure_screen,
    max_visible_rows,
    poll_keys,
    render,
    wait_for_next_cycle,
)
from proxytools.sources.proxyscrape import fetch_all_proxies
from proxytools.stability import StabilityConfig, StabilityPolicy
from proxytools.stability.history import expire_histories, update_advertised


def build_parser(prog="proxytools monitor"):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=positive_float, default=5, help="Seconds per proxy check")
    parser.add_argument("--workers", type=worker_count, default=50, help="Number of workers (1-100)")
    parser.add_argument("--max-latency", type=positive_float, default=500, help="Maximum median latency in ms")
    parser.add_argument("--samples", type=sample_count, default=1, help="Requests per check; median duration (1-5)")
    parser.add_argument("--refresh-interval", type=positive_float, default=10, help="Seconds between scan cycles")
    parser.add_argument("--history-size", type=positive_int, default=10, help="Recent checks retained per proxy")
    parser.add_argument("--min-checks", type=positive_int, default=5, help="Checks required before stable")
    parser.add_argument("--min-success-rate", type=probability, default=0.8, help="Required success ratio (0-1)")
    parser.add_argument("--min-success-streak", type=positive_int, default=3, help="Consecutive successes required")
    parser.add_argument("--min-alive-time", type=nonnegative_float, default=60, help="Continuous live seconds required")
    parser.add_argument("--max-jitter", type=nonnegative_float, default=150, help="Maximum latency deviation in ms")
    parser.add_argument("--alive-failure-tolerance", type=nonnegative_int, default=0, help="Failures allowed before alive time resets")
    parser.add_argument("--retention-time", type=positive_float, default=1800, help="Seconds to retain unadvertised proxies")
    parser.add_argument("--stable-only", action="store_true", help="Show only stable proxies")
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


def run(stdscr, args):
    configure_screen(stdscr)
    histories = {}
    policy = policy_from_args(args)
    cycle = 0
    paused = False
    max_rows = max_visible_rows(stdscr)
    try:
        while True:
            quit_requested, toggled = poll_keys(stdscr)
            paused ^= toggled
            if quit_requested:
                break

            cycle += 1
            now = time.monotonic()
            entries = fetch_all_proxies()
            update_advertised(histories, entries, now, args.history_size)
            expire_histories(histories, now, args.retention_time)
            candidates = list(histories)
            checked = 0
            if not candidates:
                if not paused:
                    render(stdscr, histories, cycle, 0, 0, max_rows, paused, args.stable_only, policy)
            else:
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
                pending = {
                    executor.submit(check_proxy, protocol, proxy, args.timeout, args.samples)
                    for protocol, proxy in candidates
                }
                quit_requested = False
                try:
                    while pending:
                        done, pending = concurrent.futures.wait(
                            pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        key_quit, key_toggled = poll_keys(stdscr)
                        paused ^= key_toggled
                        if key_quit:
                            quit_requested = True
                            break
                        for future in done:
                            result = future.result()
                            checked += 1
                            history = histories.get(result.key)
                            if history is not None:
                                result.checked_at = time.strftime("%H:%M:%S")
                                history.record(result, time.monotonic(), policy)
                        if (done or key_toggled) and not paused:
                            render(
                                stdscr, histories, cycle, checked, len(candidates),
                                max_rows, paused, args.stable_only, policy,
                            )
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
                if quit_requested:
                    break

            if not paused:
                render(
                    stdscr, histories, cycle, checked, len(candidates),
                    max_rows, paused, args.stable_only, policy,
                )
            should_quit, toggled = wait_for_next_cycle(stdscr, args.refresh_interval)
            paused ^= toggled
            if should_quit:
                break
    except KeyboardInterrupt:
        pass


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass
    print("Stopped.")
    os._exit(0)
