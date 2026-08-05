"""Run a one-shot scan and save usable free proxies to a file.

Candidates are fetched from ProxyScrape, checked and geolocated concurrently,
filtered by latency, and exported as text, JSON Lines, or CSV.  ``--check-url``
adds an HTTP preflight followed by optional Chrome/Selenium validation.

Examples::

    ./proxytools scan --workers 50 --max-latency 500 --output working.txt
    ./proxytools scan --samples 3 --format json --output proxies.jsonl
    ./proxytools scan --check-url https://example.com --headless
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
from urllib.parse import urlparse

from proxytools.checking import check_proxy
from proxytools.checking.browser import MIN_PAGE_LOAD_TIMEOUT, browser_check
from proxytools.config import positive_float, sample_count, worker_count
from proxytools.models import ProxyResult
from proxytools.output.serializers import filter_and_sort, write_results
from proxytools.sources.proxyscrape import fetch_all_proxies


def web_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("must be an absolute http:// or https:// URL")
    return value


def print_progress(done, total, working, valid, verified=None, bar_width=40):
    fraction = done / total if total else 1
    filled = int(bar_width * fraction)
    counts = f"{working} working, {valid} valid"
    if verified is not None:
        counts += f", {verified} browser verified"
    sys.stdout.write(
        f"\r[{'#' * filled}{'-' * (bar_width - filled)}] "
        f"{int(fraction * 100):3d}% ({done}/{total}, {counts})"
    )
    sys.stdout.flush()


def build_parser(prog="proxytools scan"):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=positive_float, default=5, help="Seconds per proxy check")
    parser.add_argument("--workers", type=worker_count, default=50, help="Network workers (1-100)")
    parser.add_argument("--output", default="working_proxies.txt", help="Output file")
    parser.add_argument("--format", choices=("text", "json", "csv"), default="text", help="Output format")
    parser.add_argument("--max-latency", type=positive_float, default=500, help="Maximum duration in ms")
    parser.add_argument("--samples", type=sample_count, default=1, help="Checks per proxy; median duration (1-5)")
    parser.add_argument("--check-url", type=web_url, help="URL to validate via requests and Selenium")
    parser.add_argument("--browser-workers", type=worker_count, default=1, help="Concurrent Chrome instances (1-100)")
    parser.add_argument("--headless", action="store_true", help="Run browser checks headlessly")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    page_timeout = max((args.max_latency * 2) / 1000.0, MIN_PAGE_LOAD_TIMEOUT)
    print("Fetching proxy lists from ProxyScrape (http, socks4, socks5)...")
    entries = fetch_all_proxies(verbose=True)
    if not entries:
        print("No proxies found.")
        return 0

    print(f"Fetched {len(entries)} protocol/address pairs. Checking with {args.workers} workers...")
    working: list[ProxyResult] = []
    valid: list[ProxyResult] = []
    verified: list[ProxyResult] = []
    browser_futures = set()
    interrupted = False
    network_pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    browser_pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.browser_workers) if args.check_url else None
    try:
        futures = [network_pool.submit(check_proxy, protocol, proxy, args.timeout, args.samples) for protocol, proxy in entries]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            if result.ok:
                working.append(result)
                if result.latency_ms is not None and result.latency_ms < args.max_latency:
                    valid.append(result)
                    if browser_pool:
                        browser_futures.add(
                            browser_pool.submit(browser_check, result, args.check_url, page_timeout, args.headless)
                        )
            completed = {item for item in browser_futures if item.done()}
            for item in completed:
                browser_futures.remove(item)
                checked = item.result()
                if checked:
                    verified.append(checked)
            print_progress(done, len(entries), len(working), len(valid), len(verified) if args.check_url else None)
        if browser_futures:
            print(f"\nNetwork scan complete; waiting for {len(browser_futures)} browser checks...")
            verified.extend(filter(None, (item.result() for item in concurrent.futures.as_completed(browser_futures))))
        print(f"\n{len(working)}/{len(entries)} proxies are working.")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\nInterrupted — saving completed results ({len(working)} working).")
    except RuntimeError as exc:
        interrupted = True
        print(f"\nError: {exc}", file=sys.stderr)
    finally:
        network_pool.shutdown(wait=not interrupted, cancel_futures=True)
        if browser_pool:
            browser_pool.shutdown(wait=not interrupted, cancel_futures=True)

    selected = filter_and_sort(verified if args.check_url else valid, args.max_latency)
    write_results(selected, args.output, args.format)
    qualifier = "browser-verified" if args.check_url else f"faster than {args.max_latency:g}ms"
    print(f"Saved {len(selected)} {qualifier} proxies to {args.output} ({args.format}).")
    if interrupted:
        os._exit(0)
    return 0
