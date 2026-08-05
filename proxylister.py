#!/usr/bin/env python3
"""Fetch, validate, geolocate, and optionally browser-check free proxies."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

from proxylib import (
    ProxyResult,
    check_proxy,
    check_url_via_requests,
    connection_string,
    fetch_all_proxies,
    positive_float,
    sample_count,
    worker_count,
)

CHECK_URL_HOLD_SECONDS = 10
MIN_PAGE_LOAD_TIMEOUT = 10


def web_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("must be an absolute http:// or https:// URL")
    return value


def print_progress_bar(done, total, working_count, valid_count=None, verified_count=None, bar_width=40):
    fraction = done / total if total else 1
    filled = int(bar_width * fraction)
    counts = f"{working_count} working"
    if valid_count is not None:
        counts += f", {valid_count} valid"
    if verified_count is not None:
        counts += f", {verified_count} browser verified"
    sys.stdout.write(
        f"\r[{'#' * filled}{'-' * (bar_width - filled)}] "
        f"{int(fraction * 100):3d}% ({done}/{total}, {counts})"
    )
    sys.stdout.flush()


def result_record(result: ProxyResult) -> dict:
    coords = f"{result.lat},{result.lon}"
    return {
        "latency_ms": result.latency_ms,
        "protocol": result.protocol,
        "proxy": result.proxy,
        "connection": connection_string(result.protocol, result.proxy),
        "country": result.country,
        "latitude": result.lat,
        "longitude": result.lon,
        "maps_url": f"https://www.google.com/maps?q={coords}",
    }


def format_result(result: ProxyResult) -> str:
    record = result_record(result)
    return (
        f"{record['latency_ms']}ms {record['protocol']} {record['proxy']} "
        f"{record['connection']} {record['country']} "
        f"{record['latitude']},{record['longitude']} {record['maps_url']}"
    )


def filter_and_sort(working: list[ProxyResult], max_latency_ms: float) -> list[ProxyResult]:
    return sorted(
        (result for result in working if result.latency_ms is not None and result.latency_ms < max_latency_ms),
        key=lambda result: result.latency_ms,
    )


def write_results(results: list[ProxyResult], output_path: str, output_format: str) -> None:
    path = Path(output_path)
    records = [result_record(result) for result in results]
    with path.open("w", encoding="utf-8", newline="") as output:
        if output_format == "text":
            output.write("\n".join(format_result(result) for result in results))
            if results:
                output.write("\n")
        elif output_format == "json":
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            fieldnames = list(result_record(ProxyResult("", "", True, 0)).keys())
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)


def _final_document_status(driver):
    try:
        statuses = []
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"]).get("message", {})
            params = message.get("params", {})
            if message.get("method") == "Network.responseReceived" and params.get("type") == "Document":
                statuses.append(params.get("response", {}).get("status"))
        return statuses[-1] if statuses else None
    except Exception:
        return None


def verify_proxy_via_selenium(result, check_url, page_load_timeout, headless):
    """Import Selenium only when browser validation was explicitly requested."""
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
    except ImportError as exc:
        raise RuntimeError("Selenium is required for --check-url; install selenium>=4.10") from exc

    options = webdriver.ChromeOptions()
    options.add_argument(f"--proxy-server={connection_string(result.protocol, result.proxy)}")
    if headless:
        options.add_argument("--headless=new")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(page_load_timeout)
        driver.get(check_url)
        document_uri = driver.execute_script("return document.documentURI")
        final_status = _final_document_status(driver)
        if document_uri.startswith("chrome-error://") or (final_status is not None and final_status >= 400):
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


def browser_check(result, args, page_load_timeout):
    if not check_url_via_requests(result, args.check_url, page_load_timeout):
        return None
    return result if verify_proxy_via_selenium(
        result, args.check_url, page_load_timeout, args.headless
    ) else None


def build_parser():
    parser = argparse.ArgumentParser(description="Fetch, validate, and geolocate free proxies.")
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


def main():
    args = build_parser().parse_args()
    page_load_timeout = max((args.max_latency * 2) / 1000.0, MIN_PAGE_LOAD_TIMEOUT)
    print("Fetching proxy lists from ProxyScrape (http, socks4, socks5)...")
    entries = fetch_all_proxies(verbose=True)
    if not entries:
        print("No proxies found.")
        return

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
                        browser_futures.add(browser_pool.submit(browser_check, result, args, page_load_timeout))
            completed_browser = {item for item in browser_futures if item.done()}
            for item in completed_browser:
                browser_futures.remove(item)
                checked = item.result()
                if checked:
                    verified.append(checked)
            print_progress_bar(done, len(entries), len(working), len(valid), len(verified) if args.check_url else None)
        if browser_futures:
            print(f"\nNetwork scan complete; waiting for {len(browser_futures)} browser checks...")
            for item in concurrent.futures.as_completed(browser_futures):
                checked = item.result()
                if checked:
                    verified.append(checked)
        print(f"\n{len(working)}/{len(entries)} proxies are working.")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\nInterrupted — saving completed results ({len(working)} working).")
    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        interrupted = True
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


if __name__ == "__main__":
    main()
