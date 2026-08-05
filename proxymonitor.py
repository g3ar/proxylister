#!/usr/bin/env python3
"""Continuously identify stable free proxies in a live terminal dashboard.

The monitor discovers HTTP, SOCKS4, and SOCKS5 candidates through ProxyScrape
and keeps a rolling history for every protocol/address pair.  A single good
check is not enough: a proxy becomes ``STABLE`` only after it has remained
continuously alive for ``--min-alive-time`` seconds and also satisfies the
configured check-count, success-rate, success-streak, median-latency, and
jitter thresholds.  New candidates are ``PROBATION``; a stable proxy that
fails a check becomes ``DEGRADED`` until it proves itself again.

Candidates that temporarily disappear from ProxyScrape remain under direct
observation for ``--retention-time`` seconds.  The table reports state, current
alive time, success ratio, median and p95 latency, jitter, country, and the
connection string.  Use ``--stable-only`` to hide probation and degraded rows.
Nothing is written to disk.

Typical usage::

    python proxymonitor.py --min-alive-time 60 --refresh-interval 10
    python proxymonitor.py --min-success-rate 0.9 --max-jitter 100 --stable-only

Press ``p`` to pause/resume display updates and ``q`` to quit.  Checks continue
while the display is paused.  Run ``python proxymonitor.py --help`` or see
README.md for the complete option reference.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
from collections import deque
from dataclasses import dataclass, field
import math
import os
import statistics
import time

from proxylib import (
    ProxyResult,
    check_proxy,
    connection_string,
    fetch_all_proxies,
    positive_float,
    sample_count,
    worker_count,
)

FIXED_HEADER_LINES = 5
COLOR_STABLE = 1
COLOR_PROBATION = 2
COLOR_DEGRADED = 3


def nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def probability(value: str) -> float:
    parsed = nonnegative_float(value)
    if parsed > 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


@dataclass(slots=True)
class CheckSample:
    checked_at: float
    ok: bool
    latency_ms: int | None


@dataclass(slots=True)
class StabilityConfig:
    history_size: int = 10
    min_checks: int = 5
    min_success_rate: float = 0.8
    min_success_streak: int = 3
    min_alive_time: float = 60
    max_latency: float = 500
    max_jitter: float = 150
    failure_tolerance: int = 0


@dataclass(slots=True)
class ProxyHistory:
    protocol: str
    proxy: str
    history_size: int
    samples: deque[CheckSample] = field(init=False)
    latest: ProxyResult | None = None
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    alive_since: float | None = None
    stable_since: float | None = None
    last_advertised_at: float = 0
    state: str = "PROBATION"

    def __post_init__(self):
        self.samples = deque(maxlen=self.history_size)

    @property
    def key(self) -> tuple[str, str]:
        return self.protocol, self.proxy

    @property
    def successful_latencies(self) -> list[int]:
        return [sample.latency_ms for sample in self.samples if sample.ok and sample.latency_ms is not None]

    @property
    def success_rate(self) -> float:
        if not self.samples:
            return 0
        return sum(sample.ok for sample in self.samples) / len(self.samples)

    @property
    def median_latency(self) -> int | None:
        values = self.successful_latencies
        return round(statistics.median(values)) if values else None

    @property
    def p95_latency(self) -> int | None:
        values = sorted(self.successful_latencies)
        if not values:
            return None
        return values[max(0, math.ceil(len(values) * 0.95) - 1)]

    @property
    def jitter(self) -> int:
        values = self.successful_latencies
        return round(statistics.pstdev(values)) if len(values) > 1 else 0

    def alive_for(self, now: float) -> float:
        return max(0, now - self.alive_since) if self.alive_since is not None else 0

    def blockers(self, now: float, config: StabilityConfig) -> list[str]:
        """Return the unmet stability criteria for dashboard diagnostics."""
        reasons = []
        if not self.samples or not self.samples[-1].ok:
            reasons.append("failed")
        if self.alive_for(now) < config.min_alive_time:
            reasons.append("alive")
        if len(self.samples) < config.min_checks:
            reasons.append("checks")
        if self.success_rate < config.min_success_rate:
            reasons.append("rate")
        if self.consecutive_successes < config.min_success_streak:
            reasons.append("streak")
        if self.median_latency is None or self.median_latency >= config.max_latency:
            reasons.append("latency")
        if self.jitter > config.max_jitter:
            reasons.append("jitter")
        return reasons

    def record(self, result: ProxyResult, now: float, config: StabilityConfig) -> None:
        succeeded = result.ok and result.latency_ms is not None and result.latency_ms < config.max_latency
        self.samples.append(CheckSample(now, succeeded, result.latency_ms if succeeded else None))

        if succeeded:
            self.latest = result
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            if self.alive_since is None:
                self.alive_since = now
        else:
            self.consecutive_successes = 0
            self.consecutive_failures += 1
            self.stable_since = None
            if self.consecutive_failures > config.failure_tolerance:
                self.alive_since = None

        qualifies = not self.blockers(now, config)
        if qualifies:
            if self.state != "STABLE":
                self.stable_since = now
            self.state = "STABLE"
        elif self.state in {"STABLE", "DEGRADED"} or (not succeeded and self.latest is not None):
            self.state = "DEGRADED"
        else:
            self.state = "PROBATION"


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def update_advertised(histories, entries, now, history_size):
    """Add newly advertised candidates and update discovery timestamps."""
    for protocol, proxy in entries:
        key = (protocol, proxy)
        if key not in histories:
            histories[key] = ProxyHistory(protocol, proxy, history_size)
        histories[key].last_advertised_at = now


def expire_histories(histories, now, retention_time):
    """Forget candidates absent from the source for longer than the TTL."""
    expired = [key for key, item in histories.items() if now - item.last_advertised_at > retention_time]
    for key in expired:
        del histories[key]
    return bool(expired)


def display_rows(histories, stable_only=False):
    rows = [item for item in histories.values() if item.latest is not None]
    if stable_only:
        rows = [item for item in rows if item.state == "STABLE"]
    state_order = {"STABLE": 0, "PROBATION": 1, "DEGRADED": 2}
    return sorted(
        rows,
        key=lambda item: (
            state_order[item.state],
            -item.success_rate,
            item.p95_latency if item.p95_latency is not None else math.inf,
            item.jitter,
        ),
    )


def safe_addnstr(stdscr, y, x, text, width, attr=0):
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def state_color(state):
    return curses.color_pair({"STABLE": COLOR_STABLE, "PROBATION": COLOR_PROBATION, "DEGRADED": COLOR_DEGRADED}[state])


def max_visible_rows(stdscr):
    height, _ = stdscr.getmaxyx()
    return max(height - FIXED_HEADER_LINES - 1, 1)


def render(stdscr, histories, cycle, checked, total, max_rows, paused, stable_only, config):
    now = time.monotonic()
    all_rows = display_rows(histories, stable_only)
    rows = all_rows[:max_rows]
    stable_count = sum(item.state == "STABLE" for item in histories.values())
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    status = f"proxymonitor — cycle {cycle}, checked {checked}/{total}, {stable_count} stable, {len(histories)} tracked"
    if paused:
        status += "  [PAUSED]"
    safe_addnstr(stdscr, 0, 0, status, width - 1, curses.A_BOLD | (curses.A_REVERSE if paused else 0))
    safe_addnstr(stdscr, 1, 0, "q: quit   p: pause/resume", width - 1, curses.A_DIM)
    header = (
        f"{'STATE':<9} {'ALIVE':>8} {'CHK':>5} {'STR':>3} {'OK':>4} "
        f"{'MED':>6} {'P95':>6} {'JIT':>5} {'COUNTRY':<12} {'BLOCKED BY':<18} CONNECTION"
    )
    safe_addnstr(stdscr, 3, 0, header, width - 1, curses.A_UNDERLINE)
    for index, item in enumerate(rows):
        y = FIXED_HEADER_LINES + index
        if y >= height:
            break
        latest = item.latest
        ratio = f"{round(item.success_rate * 100):>3}%"
        median = f"{item.median_latency}ms" if item.median_latency is not None else "-"
        p95 = f"{item.p95_latency}ms" if item.p95_latency is not None else "-"
        checks = f"{len(item.samples)}/{config.min_checks}"
        blocked_by = ",".join(item.blockers(now, config)) or "-"
        line = (
            f"{item.state:<9} {format_duration(item.alive_for(now)):>8} {checks:>5} "
            f"{item.consecutive_successes:>3} {ratio:>4} {median:>6} {p95:>6} "
            f"{item.jitter:>3}ms {latest.country[:12]:<12} {blocked_by:<18} "
            f"{connection_string(item.protocol, item.proxy)}"
        )
        safe_addnstr(stdscr, y, 0, line, width - 1, state_color(item.state))
    stdscr.refresh()


def poll_keys(stdscr):
    quit_requested = False
    toggle_pause = False
    while True:
        ch = stdscr.getch()
        if ch == -1:
            break
        if ch in (ord("q"), ord("Q")):
            quit_requested = True
        elif ch in (ord("p"), ord("P")):
            toggle_pause = not toggle_pause
    return quit_requested, toggle_pause


def wait_for_next_cycle(stdscr, seconds):
    deadline = time.monotonic() + seconds
    toggle_pause = False
    while time.monotonic() < deadline:
        quit_requested, toggled = poll_keys(stdscr)
        toggle_pause ^= toggled
        if quit_requested:
            return True, toggle_pause
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return False, toggle_pause


def stability_config(args):
    return StabilityConfig(
        history_size=args.history_size,
        min_checks=args.min_checks,
        min_success_rate=args.min_success_rate,
        min_success_streak=args.min_success_streak,
        min_alive_time=args.min_alive_time,
        max_latency=args.max_latency,
        max_jitter=args.max_jitter,
        failure_tolerance=args.alive_failure_tolerance,
    )


def run(stdscr, args):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_STABLE, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_PROBATION, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_DEGRADED, curses.COLOR_RED, -1)

    histories = {}
    config = stability_config(args)
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
                    render(stdscr, histories, cycle, 0, 0, max_rows, paused, args.stable_only, config)
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
                                history.record(result, time.monotonic(), config)
                        if (done or key_toggled) and not paused:
                            render(
                                stdscr, histories, cycle, checked, len(candidates),
                                max_rows, paused, args.stable_only, config,
                            )
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
                if quit_requested:
                    break

            if not paused:
                render(
                    stdscr, histories, cycle, checked, len(candidates),
                    max_rows, paused, args.stable_only, config,
                )
            should_quit, toggled = wait_for_next_cycle(stdscr, args.refresh_interval)
            paused ^= toggled
            if should_quit:
                break
    except KeyboardInterrupt:
        pass


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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


def positive_int(value: str) -> int:
    parsed = nonnegative_int(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def validate_args(parser, args):
    if args.min_checks > args.history_size:
        parser.error("--min-checks cannot exceed --history-size")
    if args.min_success_streak > args.history_size:
        parser.error("--min-success-streak cannot exceed --history-size")


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass
    print("Stopped.")
    os._exit(0)


if __name__ == "__main__":
    main()
