#!/usr/bin/env python3
"""
Live-monitor free proxies from ProxyScrape (all protocols): repeatedly
fetches a fresh proxy list, checks which are alive and under a latency
threshold, and prints an accumulating live table (latency, protocol,
country, connection string) that updates as proxies are found or drop out.

Standalone from proxylister.py — CLI-only, no file output. Runs forever
until stopped with Ctrl+C.

Usage:
    python proxy_monitor.py [--timeout 5] [--workers 50] [--max-latency 500]
"""

import argparse
import concurrent.futures
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


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def render_table(tracked, cycle, checked_this_cycle, total_this_cycle):
    """Clear the screen and reprint the current accumulated table, sorted by latency."""
    clear_screen()
    rows = sorted(tracked.values(), key=lambda r: r["latency_ms"])

    print(
        f"proxy_monitor — cycle {cycle}, checked {checked_this_cycle}/{total_this_cycle} "
        f"this cycle, {len(rows)} currently valid  (Ctrl+C to stop)\n"
    )

    header = f"{'LATENCY':>8}  {'PROTOCOL':<8}  {'COUNTRY':<20}  CONNECTION"
    print(header)
    print("-" * len(header))
    for r in rows:
        conn = connection_string(r["protocol"], r["proxy"])
        print(f"{r['latency_ms']:>6}ms  {r['protocol']:<8}  {r['country'][:20]:<20}  {conn}")
    sys.stdout.flush()


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

    tracked = {}  # ip:port -> latest result dict, for every currently-valid proxy
    cycle = 0

    print("Starting proxy monitor — press Ctrl+C to stop.\n")

    try:
        while True:
            cycle += 1
            entries = fetch_all_proxies()
            total_this_cycle = len(entries)
            checked_this_cycle = 0

            if not entries:
                continue

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
            futures = [
                executor.submit(check_proxy, protocol, proxy, args.timeout)
                for protocol, proxy in entries
            ]
            try:
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    checked_this_cycle += 1

                    key = result["proxy"]
                    is_valid = result["ok"] and result["latency_ms"] < args.max_latency
                    changed = False

                    if is_valid:
                        if key not in tracked or tracked[key]["latency_ms"] != result["latency_ms"]:
                            changed = True
                        tracked[key] = result
                    elif key in tracked:
                        del tracked[key]
                        changed = True

                    if changed:
                        render_table(tracked, cycle, checked_this_cycle, total_this_cycle)
            finally:
                # Don't block waiting on any still-running (stuck-in-socket)
                # threads — if we're unwinding here it's because Ctrl+C was
                # pressed, and the outer handler exits the process outright
                # rather than waiting on them.
                executor.shutdown(wait=False, cancel_futures=True)

            # Redraw once more at the end of the cycle so the header's
            # "checked X/Y this cycle" reflects the fully completed cycle,
            # even if the last few checks didn't change the tracked set.
            render_table(tracked, cycle, checked_this_cycle, total_this_cycle)
    except KeyboardInterrupt:
        print("\n\nStopped.")
        # Some worker threads may still be blocked in a socket call; those
        # are non-daemon and would otherwise hang normal interpreter exit
        # waiting for them. Exit immediately instead — there's nothing to
        # flush or save in this mode.
        os._exit(0)


if __name__ == "__main__":
    main()