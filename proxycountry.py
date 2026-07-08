#!/usr/bin/env python3
"""
Scan free proxies from ProxyScrape and print a country breakdown of the
valid ones (count + fastest latency per country), sorted fastest first.
Also supports listing all available countries quickly via --list-countries.
See README.md for usage. Requires proxylib.py in the same directory.
"""

import argparse
import concurrent.futures
import os
import sys

from proxylib import check_proxy, fetch_all_proxies, summarize_by_country


def print_progress_bar(done, total, valid_count, bar_width=40):
    fraction = done / total if total else 1
    filled = int(bar_width * fraction)
    bar = "#" * filled + "-" * (bar_width - filled)
    pct = int(fraction * 100)
    sys.stdout.write(f"\r[{bar}] {pct:3d}% ({done}/{total}, {valid_count} valid)")
    sys.stdout.flush()


def print_summary(summary):
    header = f"{'COUNTRY':<25}  {'COUNT':>5}  FASTEST"
    print(f"\n{header}")
    print("-" * len(header))
    for entry in summary:
        print(f"{entry['country'][:25]:<25}  {entry['count']:>5}  {entry['fastest_ms']}ms")


def get_countries(entries, timeout=5):
    """
    Run a quick check on all proxies and return the set of countries
    reported by successful checks.
    """
    countries = set()
    total = len(entries)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(50, total)) as executor:
        futures = [
            executor.submit(check_proxy, protocol, proxy, timeout)
            for protocol, proxy in entries
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            done += 1
            if result["ok"]:
                countries.add(result.get("country", "Unknown"))
            print_progress_bar(done, total, len(countries))
    return sorted(countries)


def main():
    parser = argparse.ArgumentParser(
        description="Scan free proxies (http, socks4, socks5) and print a country breakdown of the valid ones."
    )
    parser.add_argument("--timeout", type=float, default=5, help="Seconds to wait per proxy check")
    parser.add_argument("--workers", type=int, default=50, help="Number of concurrent workers")
    parser.add_argument("--max-latency", type=float, default=500, help="Only count proxies faster than this (ms)")
    parser.add_argument(
        "--list-countries",
        action="store_true",
        help="List all countries reported by the proxy list and exit",
    )
    args = parser.parse_args()

    print("Fetching proxy lists from ProxyScrape (http, socks4, socks5)...")
    entries = fetch_all_proxies(verbose=True)

    if not entries:
        print("No proxies found.")
        sys.exit(0)

    if args.list_countries:
        # Quick country list mode
        countries = get_countries(entries, timeout=args.timeout)
        for c in countries:
            print(c)
        return

    print(f"Fetched {len(entries)} proxies total. Checking availability with {args.workers} workers...")
    print("(Press Ctrl+C at any time to stop early — the breakdown will use whatever was found so far.)")

    valid = []
    total = len(entries)
    done = 0
    interrupted = False
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = [
            executor.submit(check_proxy, protocol, proxy, args.timeout)
            for protocol, proxy in entries
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            done += 1
            if result["ok"] and result["latency_ms"] < args.max_latency:
                valid.append(result)
            print_progress_bar(done, total, len(valid))
        print(f"\n\n{len(valid)}/{total} proxies are valid.")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\nInterrupted — summarizing {len(valid)} valid proxies found so far.")
    finally:
        # wait=False on interrupt: any still-running checks are blocked in
        # a socket call and won't notice cancellation; os._exit() below
        # ends the process instead of hanging on those non-daemon threads.
        executor.shutdown(wait=not interrupted, cancel_futures=True)

    if valid:
        print_summary(summarize_by_country(valid))
    else:
        print("No valid proxies found.")

    if interrupted:
        os._exit(0)


if __name__ == "__main__":
    main()
