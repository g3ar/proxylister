#!/usr/bin/env python3
"""
Live-monitor free proxies from ProxyScrape (all protocols): repeatedly
fetches a fresh proxy list, checks which are alive and under a latency
threshold, and shows an accumulating live table (latency, protocol,
country, connection string) in a curses interface, color-coded by latency.

Standalone from proxylister.py — CLI-only, no file output. Runs forever
until stopped with 'q' or Ctrl+C. Press 'p' to pause/resume the display.

Usage:
    python proxy_monitor.py [--timeout 5] [--workers 50] [--max-latency 500]
"""

import argparse
import concurrent.futures
import curses
import os
import re
import sys

import requests

# ProxyScrape's public API backs the free-proxy-list page and returns a plain
# text list of "ip:port" entries — much easier to parse than the JS-rendered
# HTML page itself.
API_URL = "https://api.proxyscrape.com/v2/"
PROXY_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")
PROTOCOLS = ("http", "socks4", "socks5")

# A single request through the proxy both confirms it's alive and reveals the
# geolocation of its exit IP, so we don't need a separate liveness check.
GEO_URL = "http://ip-api.com/json/?fields=status,country,lat,lon,query"


def fetch_proxy_list(protocol, timeout_ms=10000, country="all"):
    """Fetch raw proxy list text from the ProxyScrape API and extract ip:port pairs."""
    params = {
        "request": "getproxies",
        "protocol": protocol,
        "timeout": timeout_ms,
        "country": country,
        "ssl": "all",
        "anonymity": "all",
    }
    resp = requests.get(API_URL, params=params, timeout=15)
    resp.raise_for_status()
    return sorted(set(PROXY_RE.findall(resp.text)))


def fetch_all_proxies():
    """Fetch proxies for every supported protocol. Returns a deduplicated list of (protocol, ip:port)."""
    entries = []
    for protocol in PROTOCOLS:
        try:
            proxies = fetch_proxy_list(protocol)
        except requests.RequestException as e:
            print(f"Failed to fetch {protocol} proxy list: {e}", file=sys.stderr)
            continue
        entries.extend((protocol, p) for p in proxies)

    # The same ip:port can show up under more than one protocol's list —
    # dedupe by ip:port alone, keeping whichever protocol it was first seen
    # under, so the same physical proxy isn't checked more than once.
    deduped = []
    seen = set()
    for protocol, proxy in entries:
        if proxy in seen:
            continue
        seen.add(proxy)
        deduped.append((protocol, proxy))
    return deduped


def connection_string(protocol, proxy):
    """Build the scheme://ip:port string used in browser/OS proxy settings."""
    return f"{protocol}://{proxy}"


def check_proxy(protocol, proxy, timeout=5):
    """
    Check whether a proxy is alive and geolocate its exit IP.

    Returns a dict with protocol, proxy, ok, country, latency_ms on success,
    or ok=False on failure. latency_ms is response.elapsed — the time from
    request-sent to response-headers-received — which reflects pure network
    round trip with no local JSON-parsing or scheduling overhead mixed in.
    """
    conn = connection_string(protocol, proxy)
    proxies = {"http": conn, "https": conn}
    try:
        r = requests.get(GEO_URL, proxies=proxies, timeout=timeout)
        data = r.json()
        if r.status_code == 200 and data.get("status") == "success":
            return {
                "protocol": protocol,
                "proxy": proxy,
                "ok": True,
                "country": data.get("country", "Unknown"),
                "latency_ms": round(r.elapsed.total_seconds() * 1000),
            }
    except (requests.RequestException, ValueError):
        pass
    return {"protocol": protocol, "proxy": proxy, "ok": False}


# Lines render() prints before the first data row: the status line, the
# controls line, a blank line, the column header, and the separator rule.
FIXED_HEADER_LINES = 5

# curses color pair numbers, initialized in run().
COLOR_FAST = 1    # green:  latency < 50% of --max-latency
COLOR_MEDIUM = 2  # yellow: latency < 80% of --max-latency
COLOR_SLOW = 3    # red:    latency >= 80% of --max-latency (but still under it)


def latency_color(latency_ms, max_latency):
    """Pick a color pair based on how close latency_ms is to the max_latency threshold."""
    if latency_ms < max_latency * 0.5:
        return curses.color_pair(COLOR_FAST)
    if latency_ms < max_latency * 0.8:
        return curses.color_pair(COLOR_MEDIUM)
    return curses.color_pair(COLOR_SLOW)


def max_visible_rows(stdscr):
    """
    How many proxy rows fit in the terminal without the table scrolling,
    based on the terminal size at the moment the script starts. Computed
    once — resizing the terminal afterward has no effect on this.
    """
    height, _ = stdscr.getmaxyx()
    # -1 as a small safety margin so the last line isn't flush against the
    # very bottom edge of the terminal.
    return max(height - FIXED_HEADER_LINES - 1, 1)


def enforce_capacity(tracked, limit):
    """
    If more proxies are tracked than fit in the terminal, permanently drop
    the highest-latency ones (not just hide them) so the table never grows
    past what the screen can show without scrolling. Mutates tracked
    in-place. Returns True if anything was dropped.
    """
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
    """Drop overflow rows to fit the terminal (fixed at startup), then redraw the whole screen."""
    enforce_capacity(tracked, max_rows)
    rows = sorted(tracked.values(), key=lambda r: r["latency_ms"])

    height, width = stdscr.getmaxyx()
    stdscr.erase()

    status = (
        f"proxy_monitor — cycle {cycle}, checked {checked_this_cycle}/{total_this_cycle} "
        f"this cycle, {len(rows)} currently valid"
    )
    if paused:
        status += "  [PAUSED]"
        safe_addnstr(stdscr, 0, 0, status, width - 1, curses.A_BOLD | curses.A_REVERSE)
    else:
        safe_addnstr(stdscr, 0, 0, status, width - 1, curses.A_BOLD)

    safe_addnstr(stdscr, 1, 0, "q: quit   p: pause/resume", width - 1, curses.A_DIM)

    header = f"{'LATENCY':>8}  {'PROTOCOL':<8}  {'COUNTRY':<20}  CONNECTION"
    safe_addnstr(stdscr, 3, 0, header, width - 1, curses.A_UNDERLINE)

    for i, r in enumerate(rows):
        y = FIXED_HEADER_LINES + i
        if y >= height:
            break
        conn = connection_string(r["protocol"], r["proxy"])
        line = f"{r['latency_ms']:>6}ms  {r['protocol']:<8}  {r['country'][:20]:<20}  {conn}"
        safe_addnstr(stdscr, y, 0, line, width - 1, latency_color(r["latency_ms"], max_latency))

    stdscr.refresh()


def poll_keys(stdscr):
    """
    Drain all pending keypresses (non-blocking). Returns (quit, toggle_pause)
    — quit is True if 'q' was pressed, toggle_pause is True if 'p' was
    pressed (possibly more than once; net effect is what matters to caller).
    """
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
    """Main curses loop: cycles of fetch -> concurrent check -> live table update, until quit."""
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
            entries = fetch_all_proxies()
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
                    # timeout=0.1 guarantees we check for a keypress at
                    # least every 100ms, regardless of how long individual
                    # proxy checks take — as_completed() alone would only
                    # poll keys once a future happens to finish, which could
                    # lag several seconds during a slow tail end of a cycle.
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
                # Don't block waiting on any still-running (stuck-in-socket)
                # threads — if we're unwinding here it's because the user
                # quit, and we exit the process outright rather than
                # waiting on them (see main()).
                executor.shutdown(wait=False, cancel_futures=True)

            if quit_requested:
                break

            if not paused:
                # Redraw once more at the end of the cycle so the header's
                # "checked X/Y this cycle" reflects the fully completed
                # cycle, even if the last few checks didn't change anything.
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
    parser.add_argument(
        "--max-latency",
        type=float,
        default=500,
        help="Only track proxies with latency lower than this (ms). (default: 500)",
    )
    args = parser.parse_args()

    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass

    # curses.wrapper has already restored the terminal by this point.
    print("Stopped.")
    # Some worker threads may still be blocked in a socket call; those are
    # non-daemon and would otherwise hang normal interpreter exit waiting
    # for them. Exit immediately instead — there's nothing to flush or save
    # in this mode.
    os._exit(0)


if __name__ == "__main__":
    main()