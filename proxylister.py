#!/usr/bin/env python3
"""
Fetch free proxies from ProxyScrape (all protocols), check which are alive,
and geolocate each working proxy's exit IP.

Usage:
    python proxylister.py [--timeout 5] [--workers 50] [--output working.txt] [--max-latency 500]
"""

import argparse
import os
import re
import sys
import concurrent.futures
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
    """Fetch proxies for every supported protocol. Returns a list of (protocol, ip:port)."""
    entries = []
    for protocol in PROTOCOLS:
        try:
            proxies = fetch_proxy_list(protocol)
        except requests.RequestException as e:
            print(f"Failed to fetch {protocol} proxy list: {e}", file=sys.stderr)
            continue
        print(f"  {protocol}: {len(proxies)} proxies")
        entries.extend((protocol, p) for p in proxies)
    return entries


def connection_string(protocol, proxy):
    """Build the scheme://ip:port string used in browser/OS proxy settings."""
    return f"{protocol}://{proxy}"


def check_proxy(protocol, proxy, timeout=5):
    """
    Check whether a proxy is alive and geolocate its exit IP.

    Returns a dict with protocol, proxy, ok, country, lat, lon, latency_ms
    on success, or ok=False on failure. latency_ms is response.elapsed —
    the time from request-sent to response-headers-received — which reflects
    pure network round trip (proxy connect + handshake + forward + reply)
    with no local JSON-parsing or scheduling overhead mixed in.
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
                "lat": data.get("lat"),
                "lon": data.get("lon"),
                "latency_ms": round(r.elapsed.total_seconds() * 1000),
            }
    except (requests.RequestException, ValueError):
        pass
    return {"protocol": protocol, "proxy": proxy, "ok": False}


def print_progress_bar(done, total, working_count, valid_count=None, bar_width=40):
    """Render an in-place CLI progress bar: [####----] 42/100 (7 working, 3 valid)"""
    fraction = done / total if total else 1
    filled = int(bar_width * fraction)
    bar = "#" * filled + "-" * (bar_width - filled)
    pct = int(fraction * 100)
    counts = f"{working_count} working"
    if valid_count is not None:
        counts += f", {valid_count} valid"
    sys.stdout.write(f"\r[{bar}] {pct:3d}% ({done}/{total}, {counts})")
    sys.stdout.flush()


def format_result(result):
    """Build the "<latency> protocol server:port <conn> <country> <lat,lon> <maps link>" output line."""
    conn = connection_string(result["protocol"], result["proxy"])
    coords = f"{result['lat']},{result['lon']}"
    maps_link = f"https://www.google.com/maps?q={coords}"
    return (
        f"{result['latency_ms']}ms {result['protocol']} {result['proxy']} "
        f"{conn} {result['country']} {coords} {maps_link}"
    )


def save_working(working, output_path, max_latency_ms=None):
    """
    Sort confirmed-working results by latency (low to high) and write to a
    single output file. If max_latency_ms is given, only proxies with lower
    latency than that threshold are kept; otherwise all working proxies are
    written.
    """
    working.sort(key=lambda result: result["latency_ms"])
    if max_latency_ms is not None:
        working = [r for r in working if r["latency_ms"] < max_latency_ms]

    lines = [format_result(result) for result in working]
    with open(output_path, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    if max_latency_ms is not None:
        print(f"Saved {len(lines)} proxies faster than {max_latency_ms}ms to {output_path}")
    else:
        print(f"Saved {len(lines)} working proxies to {output_path}, sorted by latency (low to high)")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch, validate, and geolocate free proxies (http, socks4, socks5) from ProxyScrape."
    )
    parser.add_argument("--timeout", type=float, default=5, help="Seconds to wait per proxy check")
    parser.add_argument("--workers", type=int, default=50, help="Number of concurrent workers")
    parser.add_argument("--output", default="working_proxies.txt", help="File to save working proxies to")
    parser.add_argument(
        "--max-latency",
        type=float,
        default=None,
        help="Only keep proxies with latency lower than this (ms). If omitted, all working proxies are saved.",
    )
    args = parser.parse_args()

    print("Fetching proxy lists from ProxyScrape (http, socks4, socks5)...")
    entries = fetch_all_proxies()

    if not entries:
        print("No proxies found.")
        sys.exit(0)

    print(f"Fetched {len(entries)} proxies total. Checking availability with {args.workers} workers...")
    print("(Press Ctrl+C at any time to stop early — proxies confirmed working so far will still be saved.)")

    working = []
    total = len(entries)
    working_count = 0
    valid_count = 0
    interrupted = False
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = [
            executor.submit(check_proxy, protocol, proxy, args.timeout)
            for protocol, proxy in entries
        ]
        done = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            done += 1
            if result["ok"]:
                working.append(result)
                working_count += 1
                if args.max_latency is not None and result["latency_ms"] < args.max_latency:
                    valid_count += 1
            print_progress_bar(done, total, working_count, valid_count if args.max_latency is not None else None)
        print(f"\n\n{working_count}/{total} proxies are working.")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\nInterrupted — stopping. {working_count} proxies confirmed working so far.")
    finally:
        if interrupted:
            # Don't block here: any still-running checks are stuck in a
            # blocking socket call (connect/read timeout) that won't notice
            # cancellation, and they're non-daemon threads — waiting on them,
            # or even letting the interpreter's normal atexit cleanup wait on
            # them, can hang or print noisy tracebacks on a further Ctrl+C.
            # Save what we have and exit immediately, skipping that wait.
            executor.shutdown(wait=False, cancel_futures=True)
            save_working(working, args.output, args.max_latency)
            os._exit(0)
        else:
            executor.shutdown(wait=True, cancel_futures=True)
            save_working(working, args.output, args.max_latency)


if __name__ == "__main__":
    main()