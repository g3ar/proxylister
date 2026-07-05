#!/usr/bin/env python3
"""
Fetch free proxies from ProxyScrape (all protocols), check which are alive,
geolocate each working proxy's exit IP, and optionally verify the fastest
ones by loading a real page through each with Selenium.

Usage:
    python proxylister.py [--timeout 5] [--workers 50] [--output working.txt]
                           [--max-latency 500]
                           [--check-url URL] [--headless]
"""

import argparse
import json
import os
import re
import sys
import time
import concurrent.futures
import requests

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException

# ProxyScrape's public API backs the free-proxy-list page and returns a plain
# text list of "ip:port" entries — much easier to parse than the JS-rendered
# HTML page itself.
API_URL = "https://api.proxyscrape.com/v2/"
PROXY_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")
PROTOCOLS = ("http", "socks4", "socks5")

# A single request through the proxy both confirms it's alive and reveals the
# geolocation of its exit IP, so we don't need a separate liveness check.
GEO_URL = "http://ip-api.com/json/?fields=status,country,lat,lon,query"

# How long a verified proxy's browser window stays open before moving on.
CHECK_URL_HOLD_SECONDS = 10


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


def filter_and_sort(working, max_latency_ms):
    """Sort confirmed-working results by latency (low to high), keeping only those under max_latency_ms."""
    results = sorted(working, key=lambda result: result["latency_ms"])
    return [r for r in results if r["latency_ms"] < max_latency_ms]


def write_results(results, output_path, max_latency_ms, url_checked=False):
    """Write the given (already filtered/sorted) results to the output file."""
    lines = [format_result(result) for result in results]
    with open(output_path, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    if url_checked:
        print(f"Saved {len(lines)} proxies verified against the check URL to {output_path}")
    else:
        print(f"Saved {len(lines)} proxies faster than {max_latency_ms}ms to {output_path}")


def _final_document_status(driver):
    """
    Return the HTTP status code of the final main-document response for the
    page just loaded, by reading back Chrome's performance log (requires
    goog:loggingPrefs -> {"performance": "ALL"} to be set on the driver).
    Returns None if it can't be determined (log entries are best-effort and
    can be dropped/reordered by Chrome).

    If the request redirected, several "Document"-type responses will
    appear (one per hop); the last one is the page actually rendered.
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
    Open check_url through this one proxy using Selenium/Chrome. On success,
    holds the browser open for CHECK_URL_HOLD_SECONDS before closing it and
    returns True. On any page error — a Selenium/timeout exception, Chrome's
    internal network-error page, or an HTTP error status on the main
    document — closes the browser immediately with no wait and returns
    False. A KeyboardInterrupt closes the browser (via finally) and then
    propagates to the caller.
    """
    conn = connection_string(result["protocol"], result["proxy"])
    print(f"\nchecking {check_url} via {conn}")

    options = webdriver.ChromeOptions()
    options.add_argument(f"--proxy-server={conn}")
    if headless:
        options.add_argument("--headless=new")
    # Needed to read back the main document's real HTTP status after
    # load — many broken/free proxies return their own valid-looking
    # error response (502/403/504/etc.) instead of forwarding to the
    # target, which Chrome renders as a normal page with no exception.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(page_load_timeout)
        driver.get(check_url)

        # Chrome renders its own internal error page for network-level
        # failures (connection refused, tunnel/proxy failure, DNS error,
        # etc.) without Selenium raising an exception — driver.get()
        # reports success either way. document.documentURI is
        # "chrome-error://chromewebdata/" specifically for those internal
        # error pages, so it's the reliable way to catch a bad proxy that
        # "loaded" nothing real.
        document_uri = driver.execute_script("return document.documentURI")

        # A proxy can also "succeed" at the browser level while the
        # response itself is an error — e.g. a dead/misconfigured proxy
        # returning its own 502/403/504 page instead of forwarding to
        # the target. That has no exception and no chrome-error:// URI,
        # so we check the real HTTP status of the main document response
        # directly.
        final_status = _final_document_status(driver)

        page_error = None
        if document_uri.startswith("chrome-error://"):
            page_error = f"page failed to load ({document_uri})"
        elif final_status is not None and final_status >= 400:
            page_error = f"page failed to load (HTTP {final_status})"

        if page_error:
            print(f"  {page_error} — closing and dropping this proxy.")
            driver.quit()
            driver = None
            return False

        print(f"  Loaded OK — holding browser open for {CHECK_URL_HOLD_SECONDS}s...")
        time.sleep(CHECK_URL_HOLD_SECONDS)
        return True
    except (TimeoutException, WebDriverException) as e:
        reason = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"  Page error ({reason}) — closing and dropping this proxy.")
        return False
    finally:
        # Safety net for any path above that didn't already close the
        # driver (exceptions, Ctrl+C) — a no-op if it's already quit.
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
    parser.add_argument(
        "--max-latency",
        type=float,
        default=500,
        help="Only keep proxies with latency lower than this (ms). (default: 500)",
    )
    parser.add_argument(
        "--check-url",
        default=None,
        help="URL to open via Selenium through each proxy as soon as it's found valid, as an extra "
        "validation layer. The Selenium page-load timeout is derived as 2x --max-latency.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run --check-url browser checks headlessly (default: visible window)",
    )
    args = parser.parse_args()

    # Selenium's page-load timeout (seconds) is derived from --max-latency
    # (ms): a proxy that's already fast enough to pass the latency filter
    # gets twice that long, in seconds-equivalent terms, to load a real page.
    page_load_timeout = (args.max_latency * 2) / 1000.0

    print("Fetching proxy lists from ProxyScrape (http, socks4, socks5)...")
    entries = fetch_all_proxies()

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
                    # Check via Selenium immediately, right as this proxy is
                    # found valid — not deferred to a later batch pass. A
                    # Ctrl+C here propagates naturally up to the except
                    # block below (the browser still gets closed first, via
                    # verify_proxy_via_selenium's own finally).
                    if args.check_url and verify_proxy_via_selenium(
                        result, args.check_url, page_load_timeout, args.headless
                    ):
                        verified.append(result)
            print_progress_bar(done, total, working_count, valid_count)
        print(f"\n\n{working_count}/{total} proxies are working.")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\nInterrupted — stopping. {working_count} proxies confirmed working so far.")
    finally:
        # Don't block here: any still-running checks are stuck in a blocking
        # socket call (connect/read timeout) that won't notice cancellation,
        # and they're non-daemon threads — waiting on them can hang or print
        # noisy tracebacks on a further Ctrl+C. On interrupt, skip the wait
        # entirely and rely on os._exit() below to end the process cleanly.
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