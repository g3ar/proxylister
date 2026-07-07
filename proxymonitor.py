#!/usr/bin/env python3
"""
Live curses dashboard of currently-working free proxies from ProxyScrape.
See README.md for usage. Requires proxylib.py in the same directory.
"""

import argparse
import concurrent.futures
import curses
import os
import time

from proxylib import check_proxy, connection_string, fetch_all_proxies

# Lines render() prints before the first data row: status, controls, blank,
# header, separator.
FIXED_HEADER_LINES = 5

COLOR_FAST = 1    # green:  latency < 50% of --max-latency
COLOR_MEDIUM = 2  # yellow: latency < 80% of --max-latency
COLOR_SLOW = 3    # red:    latency >= 80% (but still under --max-latency)


def latency_color(latency_ms, max_latency):
    if latency_ms < max_latency * 0.5:
        return curses.color_pair(COLOR_FAST)
    if latency_ms < max_latency * 0.8:
        return curses.color_pair(COLOR_MEDIUM)
    return curses.color_pair(COLOR_SLOW)


def max_visible_rows(stdscr):
    """Row budget fixed to the terminal size at startup; resizing later has no effect."""
    height, _ = stdscr.getmaxyx()
    return max(height - FIXED_HEADER_LINES - 1, 1)


def enforce_capacity(tracked, limit):
    """Once over limit, permanently drop the highest-latency proxies (not just hide them)."""
    if len(tracked) <= limit:
        return False
    keep_keys = {
        r["proxy"] for r in sorted(tracked.values(), key=lambda r: r["latency_ms"])[:limit]
    }
    for key in list(tracked.keys()):
        if key not in keep_keys:
            del tracked[key]
    return True


def safe_addnstr(stdscr, y, x, text, width, attr=0):
    """addnstr, but swallow curses.error from writing to the terminal's last cell."""
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def render(stdscr, tracked, cycle, checked_this_cycle, total_this_cycle, max_rows, max_latency, paused):
    enforce_capacity(tracked, max_rows)
    rows = sorted(tracked.values(), key=lambda r: r["latency_ms"])

    height, width = stdscr.getmaxyx()
    stdscr.erase()

    status = (
        f"proxymonitor — cycle {cycle}, checked {checked_this_cycle}/{total_this_cycle} "
        f"this cycle, {len(rows)} currently valid"
    )
    if paused:
        status += "  [PAUSED]"
        safe_addnstr(stdscr, 0, 0, status, width - 1, curses.A_BOLD | curses.A_REVERSE)
    else:
        safe_addnstr(stdscr, 0, 0, status, width - 1, curses.A_BOLD)

    safe_addnstr(stdscr, 1, 0, "q: quit   p: pause/resume", width - 1, curses.A_DIM)

    header = f"{'LATENCY':>8}  {'PROTOCOL':<8}  {'COUNTRY':<20}  {'CHECKED':<8}  CONNECTION"
    safe_addnstr(stdscr, 3, 0, header, width - 1, curses.A_UNDERLINE)

    for i, r in enumerate(rows):
        y = FIXED_HEADER_LINES + i
        if y >= height:
            break
        conn = connection_string(r["protocol"], r["proxy"])
        line = (
            f"{r['latency_ms']:>6}ms  {r['protocol']:<8}  {r['country'][:20]:<20}  "
            f"{r.get('checked_at', ''):<8}  {conn}"
        )
        safe_addnstr(stdscr, y, 0, line, width - 1, latency_color(r["latency_ms"], max_latency))

    stdscr.refresh()


def poll_keys(stdscr):
    """Drain pending keypresses (non-blocking). Returns (quit, toggle_pause)."""
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


def run(stdscr, args):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_FAST, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_MEDIUM, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_SLOW, curses.COLOR_RED, -1)

    tracked = {}  # ip:port -> latest result dict, for every currently-valid proxy
    cycle = 0
    paused = False
    max_rows = max_visible_rows(stdscr)

    try:
        while True:
            quit_requested, toggled = poll_keys(stdscr)
            if toggled:
                paused = not paused
                render(stdscr, tracked, cycle, 0, 0, max_rows, args.max_latency, paused)
            if quit_requested:
                break

            cycle += 1
            entries = fetch_all_proxies()  # silent — no console prints during curses mode
            total_this_cycle = len(entries)
            checked_this_cycle = 0

            if not entries:
                continue

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
            pending = {
                executor.submit(check_proxy, protocol, proxy, args.timeout)
                for protocol, proxy in entries
            }
            quit_requested = False
            try:
                while pending:
                    # timeout=0.1 keeps keypresses responsive even during a
                    # slow tail end of a cycle — as_completed() alone would
                    # only poll once a future happens to finish.
                    done, pending = concurrent.futures.wait(
                        pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
                    )

                    key_quit, key_toggled = poll_keys(stdscr)
                    if key_toggled:
                        paused = not paused
                    if key_quit:
                        quit_requested = True
                        break

                    changed = False
                    for future in done:
                        result = future.result()
                        checked_this_cycle += 1
                        result["checked_at"] = time.strftime("%H:%M:%S")

                        key = result["proxy"]
                        is_valid = result["ok"] and result["latency_ms"] < args.max_latency

                        if is_valid:
                            if key not in tracked or tracked[key]["latency_ms"] != result["latency_ms"]:
                                changed = True
                            tracked[key] = result
                        elif key in tracked:
                            del tracked[key]
                            changed = True

                    if key_toggled or (changed and not paused):
                        render(
                            stdscr, tracked, cycle, checked_this_cycle, total_this_cycle,
                            max_rows, args.max_latency, paused,
                        )
            finally:
                # wait=False: on quit, don't block on threads still stuck in
                # a socket call — main() exits the process outright instead.
                executor.shutdown(wait=False, cancel_futures=True)

            if quit_requested:
                break

            if not paused:
                render(
                    stdscr, tracked, cycle, checked_this_cycle, total_this_cycle,
                    max_rows, args.max_latency, paused,
                )
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Live-monitor free proxies (http, socks4, socks5) from ProxyScrape."
    )
    parser.add_argument("--timeout", type=float, default=5, help="Seconds to wait per proxy check")
    parser.add_argument("--workers", type=int, default=50, help="Number of concurrent workers")
    parser.add_argument("--max-latency", type=float, default=500, help="Only track proxies faster than this (ms)")
    args = parser.parse_args()

    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass

    print("Stopped.")
    # Some worker threads may still be blocked in a socket call; those are
    # non-daemon and would hang normal interpreter exit. Exit immediately
    # instead — there's nothing to flush or save in this mode.
    os._exit(0)


if __name__ == "__main__":
    main()
