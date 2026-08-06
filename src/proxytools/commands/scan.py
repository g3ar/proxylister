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

from proxytools.checking import check_proxy
from proxytools.checking.browser import MIN_PAGE_LOAD_TIMEOUT, browser_check
from proxytools.config import positive_float, sample_count, web_url, worker_count
from proxytools.models import ProxyResult
from proxytools.output.console import console, progress_display
from proxytools.output.serializers import filter_and_sort, write_results
from proxytools.sources.proxyscrape import fetch_all_proxies


def build_parser(prog="proxytools scan"):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    network = parser.add_argument_group("network")
    network.add_argument("-t", "--timeout", type=positive_float, default=5, help="Seconds per proxy check")
    network.add_argument("-w", "--workers", type=worker_count, default=50, help="Network workers (1-100)")
    network.add_argument("-l", "--max-latency", type=positive_float, default=500, help="Maximum duration in ms")
    network.add_argument("--samples", type=sample_count, default=1, help="Checks per proxy; median duration (1-5)")
    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output", default="working_proxies.txt", help="Output file")
    output.add_argument("-f", "--format", choices=("text", "json", "csv"), default="text", help="Output format")
    browser = parser.add_argument_group("browser validation")
    browser.add_argument("--check-url", type=web_url, help="URL to validate via requests and Selenium")
    browser.add_argument("--browser-workers", type=worker_count, default=1, help="Concurrent Chrome instances (1-100)")
    browser.add_argument("--headless", action="store_true", help="Run browser checks headlessly")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    page_timeout = max((args.max_latency * 2) / 1000.0, MIN_PAGE_LOAD_TIMEOUT)
    console.print("[bold]Fetching proxy lists from ProxyScrape[/bold] (http, socks4, socks5)…")
    entries = fetch_all_proxies(verbose=True)
    if not entries:
        console.print("[yellow]No proxies found.[/yellow]")
        return 0

    console.print(f"Fetched [bold]{len(entries)}[/bold] protocol/address pairs; using {args.workers} workers.")
    working: list[ProxyResult] = []
    valid: list[ProxyResult] = []
    verified: list[ProxyResult] = []
    browser_futures = set()
    interrupted = False
    network_pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    browser_pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.browser_workers) if args.check_url else None
    try:
        futures = [network_pool.submit(check_proxy, protocol, proxy, args.timeout, args.samples) for protocol, proxy in entries]
        with progress_display() as progress:
            task = progress.add_task("Checking proxies", total=len(entries), status="starting")
            for future in concurrent.futures.as_completed(futures):
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
                status = f"{len(working)} working, {len(valid)} valid"
                if args.check_url:
                    status += f", {len(verified)} browser verified"
                progress.update(task, advance=1, status=status)
        if browser_futures:
            console.print(f"Waiting for [bold]{len(browser_futures)}[/bold] browser checks…")
            verified.extend(filter(None, (item.result() for item in concurrent.futures.as_completed(browser_futures))))
        console.print(f"[bold green]{len(working)}[/bold green]/{len(entries)} proxies are working.")
    except KeyboardInterrupt:
        interrupted = True
        console.print(f"[yellow]Interrupted — saving {len(working)} completed results.[/yellow]")
    except RuntimeError as exc:
        interrupted = True
        console.print(f"[bold red]Error:[/bold red] {exc}", stderr=True)
    finally:
        network_pool.shutdown(wait=not interrupted, cancel_futures=True)
        if browser_pool:
            browser_pool.shutdown(wait=not interrupted, cancel_futures=True)

    selected = filter_and_sort(verified if args.check_url else valid, args.max_latency)
    write_results(selected, args.output, args.format)
    qualifier = "browser-verified" if args.check_url else f"faster than {args.max_latency:g}ms"
    console.print(f"Saved [bold]{len(selected)}[/bold] {qualifier} proxies to [cyan]{args.output}[/cyan] ({args.format}).")
    if interrupted:
        os._exit(0)
    return 0
