"""Curses rendering and keyboard handling for the stability monitor."""

from __future__ import annotations

import curses
import math
import time

from proxytools.checking import connection_string
from proxytools.stability import StabilityPolicy

FIXED_HEADER_LINES = 5
COLOR_STABLE = 1
COLOR_PROBATION = 2
COLOR_DEGRADED = 3


def configure_screen(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_STABLE, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_PROBATION, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_DEGRADED, curses.COLOR_RED, -1)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


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


def max_visible_rows(stdscr):
    height, _ = stdscr.getmaxyx()
    return max(height - FIXED_HEADER_LINES - 1, 1)


def state_color(state):
    colors = {"STABLE": COLOR_STABLE, "PROBATION": COLOR_PROBATION, "DEGRADED": COLOR_DEGRADED}
    return curses.color_pair(colors[state])


def render(stdscr, histories, cycle, checked, total, max_rows, paused, stable_only, policy: StabilityPolicy):
    now = time.monotonic()
    rows = display_rows(histories, stable_only)[:max_rows]
    stable_count = sum(item.state == "STABLE" for item in histories.values())
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    status = f"proxytools monitor — cycle {cycle}, checked {checked}/{total}, {stable_count} stable, {len(histories)} tracked"
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
        checks = f"{len(item.samples)}/{policy.config.min_checks}"
        blocked_by = ",".join(policy.blockers(item, now)) or "-"
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
