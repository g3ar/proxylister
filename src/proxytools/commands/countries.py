"""Summarize currently working proxies by exit country.

Normal mode checks candidates and prints country counts plus fastest latency.
``--list-countries`` uses ProxyScrape's fast country endpoint without checking
individual proxies.

Examples::

    ./proxytools countries --workers 50 --max-latency 500
    ./proxytools countries --list-countries
"""

import argparse
import concurrent.futures
import os
import sys

from proxytools.checking import check_proxy
from proxytools.analytics import summarize_by_country
from proxytools.config import positive_float, sample_count, worker_count
from proxytools.sources.proxyscrape import fetch_all_proxies, fetch_available_countries


def print_progress(done, total, valid, bar_width=40):
    fraction = done / total if total else 1
    filled = int(bar_width * fraction)
    sys.stdout.write(f"\r[{'#' * filled}{'-' * (bar_width - filled)}] {int(fraction * 100):3d}% ({done}/{total}, {valid} valid)")
    sys.stdout.flush()


def print_summary(results):
    header = f"{'COUNTRY':<25}  {'COUNT':>5}  FASTEST"
    print(f"\n{header}\n{'-' * len(header)}")
    for entry in summarize_by_country(results):
        print(f"{entry.country[:25]:<25}  {entry.count:>5}  {entry.fastest_ms}ms")


def build_parser(prog="proxytools countries"):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=positive_float, default=5, help="Seconds per proxy check")
    parser.add_argument("--workers", type=worker_count, default=50, help="Number of workers (1-100)")
    parser.add_argument("--max-latency", type=positive_float, default=500, help="Maximum duration in ms")
    parser.add_argument("--samples", type=sample_count, default=1, help="Checks per proxy; median duration (1-5)")
    parser.add_argument("--list-countries", action="store_true", help="List advertised countries and exit")
    parser.add_argument("--verbose", action="store_true", help="Print per-protocol source counts")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list_countries:
        for country in fetch_available_countries(args.timeout):
            print(country)
        return 0

    print("Fetching proxy lists from ProxyScrape (http, socks4, socks5)...")
    entries = fetch_all_proxies(verbose=args.verbose)
    if not entries:
        print("No proxies found.")
        return 0

    print(f"Fetched {len(entries)} protocol/address pairs. Checking with {args.workers} workers...")
    valid = []
    interrupted = False
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = [executor.submit(check_proxy, protocol, proxy, args.timeout, args.samples) for protocol, proxy in entries]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            if result.ok and result.latency_ms is not None and result.latency_ms < args.max_latency:
                valid.append(result)
            print_progress(done, len(entries), len(valid))
        print(f"\n\n{len(valid)}/{len(entries)} proxies are valid.")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\nInterrupted — summarizing {len(valid)} valid proxies found so far.")
    finally:
        executor.shutdown(wait=not interrupted, cancel_futures=True)
    print_summary(valid) if valid else print("No valid proxies found.")
    if interrupted:
        os._exit(0)
    return 0
