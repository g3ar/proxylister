#!/usr/bin/env python3
"""
Fetch, check, and geolocate free proxies from ProxyScrape; optionally
verify each fast one against a real URL with Selenium. See README.md for
usage. Requires proxylib.py in the same directory.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException

from proxylib import check_proxy, connection_string, fetch_all_proxies

CHECK_URL_HOLD_SECONDS = 10
MIN_PAGE_LOAD_TIMEOUT = 10  # floor for the derived Selenium page-load timeout, see main()


def print_progress_bar(done, total, working_count, valid_count=None, selenium_verified_count=None, bar_width=40):
    fraction = done / total if total else 1
    filled = int(bar_width * fraction)
    bar = "#" * filled + "-" * (bar_width - filled)
    pct = int(fraction * 100)
    counts = f"{working_count} working"
    if valid_count is not None:
        counts += f", {valid_count} valid"
    if selenium_verified_count is not None:
        counts += f", {selenium_verified_count} selenium verified"
    sys.stdout.write(f"\r[{bar}] {pct:3d}% ({done}/{total}, {counts})")
    sys.stdout.flush()


def format_result(result):
    conn = connection_string(result["protocol"], result["proxy"])
    coords = f"{result['lat']},{result['lon']}"
    maps_link = f"https://www.google.com/maps?q={coords}"
    return (
        f"{result['latency_ms']}ms {result['protocol']} {result['proxy']} "
        f"{conn} {result['country']} {coords} {maps_link}"
    )


def filter_and_sort(working, max_latency_ms):
    results = sorted(working, key=lambda result: result["latency_ms"])
    return [r for r in results if r["latency_ms"] < max_latency_ms]


def write_results(results, output_path, max_latency_ms, url_checked=False):
    lines = [format_result(result) for result in results]
    with open(output_path, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    if url_checked:
        print(f"Saved {len(lines)} proxies verified against the check URL to {output_path}")
    else:
        print(f"Saved {len(lines)} proxies faster than {max_latency_ms}ms to {output_path}")


def _final_document_status(driver):
    """
    HTTP status of the final main-document response, read from Chrome's
    performance log (requires goog:loggingPrefs -> {"performance": "ALL"}).
    None if it can't be determined. On a redirect, several "Document"
    entries appear (one per hop); the last one is what's actually rendered.
    """
    try:
        statuses = []
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"]).get("message", {})
            if message.get("method") != "Network.responseReceived":
                continue
            params = message.get("params", {})
            if params.get("type") == "Document":
                statuses.append(params.get("response", {}).get("status"))
        return statuses[-1] if statuses else None
    except Exception:
        return None


def verify_proxy_via_selenium(result, check_url, page_load_timeout, headless):
    """
    Load check_url through this proxy in Chrome. True on success (window
    stays open CHECK_URL_HOLD_SECONDS unless headless); False on any
    failure, closing the browser immediately with no wait.
    """
    conn = connection_string(result["protocol"], result["proxy"])

    options = webdriver.ChromeOptions()
    options.add_argument(f"--proxy-server={conn}")
    if headless:
        options.add_argument("--headless=new")
    # Needed to read the real HTTP status below — a dead proxy can return
    # its own valid-looking error page (502/403/etc.) with no exception.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(page_load_timeout)
        driver.get(check_url)

        # Chrome shows its own internal error page for network failures
        # (connection refused, tunnel failure, DNS, etc.) without raising —
        # this URI is how to detect that "successful" load was actually empty.
        document_uri = driver.execute_script("return document.documentURI")
        final_status = _final_document_status(driver)

        page_error = None
        if document_uri.startswith("chrome-error://"):
            page_error = f"page failed to load ({document_uri})"
        elif final_status is not None and final_status >= 400:
            page_error = f"page failed to load (HTTP {final_status})"

        if page_error:
            driver.quit()
            driver = None
            return False

        if not headless:
            time.sleep(CHECK_URL_HOLD_SECONDS)
        return True
    except (TimeoutException, WebDriverException):
        return False
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(
        description="Fetch, validate, and geolocate free proxies (http, socks4, socks5) from ProxyScrape."
    )
    parser.add_argument("--timeout", type=float, default=5, help="Seconds to wait per proxy check")
    parser.add_argument("--workers", type=int, default=50, help="Number of concurrent workers")
    parser.add_argument("--output", default="working_proxies.txt", help="File to save working proxies to")
    parser.add_argument("--max-latency", type=float, default=500, help="Only keep proxies faster than this (ms)")
    parser.add_argument("--check-url", default=None, help="URL to validate each fast proxy against via Selenium")
    parser.add_argument("--headless", action="store_true", help="Run --check-url checks without a visible window")
    args = parser.parse_args()

    # --max-latency measures a tiny JSON API call, not a full browser page
    # load, so 2x alone can be too short (even for ChromeDriver's own
    # timeout accounting to work) — floor it at MIN_PAGE_LOAD_TIMEOUT.
    page_load_timeout = max((args.max_latency * 2) / 1000.0, MIN_PAGE_LOAD_TIMEOUT)

    print("Fetching proxy lists from ProxyScrape (http, socks4, socks5)...")
    entries = fetch_all_proxies(verbose=True)

    if not entries:
        print("No proxies found.")
        sys.exit(0)

    print(f"Fetched {len(entries)} proxies total. Checking availability with {args.workers} workers...")
    print("(Press Ctrl+C at any time to stop early — proxies confirmed working so far will still be saved.)")

    working = []
    verified = []
    total = len(entries)
    working_count = 0
    valid_count = 0
    selenium_verified_count = 0
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
                if result["latency_ms"] < args.max_latency:
                    valid_count += 1
                    # Checked inline, right as the proxy is found valid —
                    # not batched into a separate pass afterward.
                    if args.check_url and verify_proxy_via_selenium(
                        result, args.check_url, page_load_timeout, args.headless
                    ):
                        verified.append(result)
                        selenium_verified_count += 1
            print_progress_bar(
                done, total, working_count, valid_count,
                selenium_verified_count if args.check_url else None,
            )
        print(f"\n\n{working_count}/{total} proxies are working.")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\nInterrupted — stopping. {working_count} proxies confirmed working so far.")
    finally:
        # wait=False on interrupt: any still-running checks are blocked in
        # a socket call and won't notice cancellation; os._exit() below
        # ends the process instead of hanging on those non-daemon threads.
        executor.shutdown(wait=not interrupted, cancel_futures=True)

    valid_results = filter_and_sort(working, args.max_latency)
    write_results(valid_results, args.output, args.max_latency)

    if args.check_url:
        verified_sorted = filter_and_sort(verified, args.max_latency)
        write_results(verified_sorted, args.output, args.max_latency, url_checked=True)

    if interrupted:
        os._exit(0)


if __name__ == "__main__":
    main()
