"""Find usable free proxies once and print them to standard output.

Candidates are fetched from ProxyScrape, checked and geolocated concurrently,
filtered by latency, and printed fastest-first. ``--url`` adds
the same lightweight target check used by monitor; ``--browser-check`` adds an
explicit Chrome/Selenium validation after that check.

Examples::

    ./proxytools list --max-latency 500
    ./proxytools list --url https://example.com
    ./proxytools list --url https://example.com --browser-check --headless
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys

from proxytools.checking import check_proxy, check_url, connection_string
from proxytools.checking.browser import MIN_PAGE_LOAD_TIMEOUT, browser_check
from proxytools.config import load_config, positive_float, web_url
from proxytools.models import ProxyResult
from proxytools.output.console import console, progress_display
from proxytools.output.results import filter_and_sort, format_result
from proxytools.sources.proxyscrape import ProxySourceUnavailable, fetch_all_proxies


def build_parser(prog="proxytools list", settings=None):
    settings = settings or load_config()
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debug", action="store_true", help="Print detailed proxy metadata")
    parser.add_argument("--url", type=web_url, default=settings.url, help="URL every proxy must reach")
    parser.add_argument(
        "--max-latency", type=positive_float, default=settings.max_latency,
        help=f"Maximum median latency in ms (config: {settings.max_latency:g})",
    )
    target = parser.add_argument_group("target validation")
    target.add_argument(
        "--browser-check", action="store_true",
        help="Additionally validate --url in one Selenium browser",
    )
    target.add_argument("--headless", action="store_true", help="Run the Selenium browser headlessly")
    return parser


def validate_args(parser, args):
    if args.browser_check and not args.url:
        parser.error("--browser-check requires --url")
    if args.headless and not args.browser_check:
        parser.error("--headless requires --browser-check")


def check_candidate(protocol, proxy, timeout, samples, url):
    """Run HTTPS identity and optional target checks without Selenium."""
    result = check_proxy(protocol, proxy, timeout, samples)
    if result.reachable and url and not check_url(
        result, url, timeout, accept_forbidden=True
    ):
        result.failure_reason = "url"
    return result


def main(argv=None):
    settings = load_config()
    parser = build_parser(settings=settings)
    args = parser.parse_args(argv)
    validate_args(parser, args)
    page_timeout = max((args.max_latency * 2) / 1000.0, MIN_PAGE_LOAD_TIMEOUT)
    console.print("[bold]Fetching proxy lists from ProxyScrape[/bold] (http, socks4, socks5)…")
    try:
        entries = fetch_all_proxies(verbose=True)
    except ProxySourceUnavailable as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return 1
    if not entries:
        console.print("[yellow]No proxies found.[/yellow]")
        return 0

    console.print(f"Fetched [bold]{len(entries)}[/bold] protocol/address pairs; using {settings.workers} workers.")
    working: list[ProxyResult] = []
    valid: list[ProxyResult] = []
    verified: list[ProxyResult] = []
    browser_future = None
    browser_submitted = 0
    interrupted = False
    operational_error = False
    network_pool = concurrent.futures.ThreadPoolExecutor(max_workers=settings.workers)
    browser_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1) if args.browser_check else None
    try:
        futures = [
            network_pool.submit(
                check_candidate, protocol, proxy, settings.timeout, settings.samples, args.url
            )
            for protocol, proxy in entries
        ]
        with progress_display() as progress:
            task = progress.add_task("Checking proxies", total=len(entries), status="starting")
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result.reachable:
                    working.append(result)
                    if (
                        not result.failure_reason
                        and result.latency_ms is not None
                        and result.latency_ms < args.max_latency
                    ):
                        valid.append(result)
                if browser_future is not None and browser_future.done():
                    checked = browser_future.result()
                    if checked:
                        verified.append(checked)
                    browser_future = None
                if browser_pool and browser_future is None and browser_submitted < len(valid):
                    result_to_check = valid[browser_submitted]
                    browser_submitted += 1
                    browser_future = browser_pool.submit(
                        browser_check, result_to_check, args.url, page_timeout, args.headless
                    )
                status = f"{len(working)} working, {len(valid)} valid"
                if args.browser_check:
                    status += f", {len(verified)} browser verified"
                progress.update(task, advance=1, status=status)
        remaining_browser_checks = len(valid) - browser_submitted + bool(browser_future)
        if remaining_browser_checks:
            console.print(
                f"Waiting for [bold]{remaining_browser_checks}[/bold] browser checks…"
            )
        while browser_pool and (browser_future is not None or browser_submitted < len(valid)):
            if browser_future is None:
                result_to_check = valid[browser_submitted]
                browser_submitted += 1
                browser_future = browser_pool.submit(
                    browser_check, result_to_check, args.url, page_timeout, args.headless
                )
            checked = browser_future.result()
            if checked:
                verified.append(checked)
            browser_future = None
        console.print(f"[bold green]{len(working)}[/bold green]/{len(entries)} proxies are working.")
    except KeyboardInterrupt:
        interrupted = True
        console.print(
            f"[yellow]Interrupted — cancelling queued checks and waiting for active requests; "
            f"{len(working)} results collected.[/yellow]"
        )
    except Exception as exc:
        interrupted = True
        operational_error = True
        console.print(f"[bold red]Error:[/bold red] {exc}")
    finally:
        network_pool.shutdown(wait=True, cancel_futures=interrupted)
        if browser_pool:
            browser_pool.shutdown(wait=True, cancel_futures=interrupted)

    if operational_error:
        return 1

    selected = filter_and_sort(verified if args.browser_check else valid, args.max_latency)
    qualifier = "browser-verified" if args.browser_check else f"faster than {args.max_latency:g}ms"
    console.print(f"Found [bold]{len(selected)}[/bold] {qualifier} proxies.")
    for result in selected:
        print(
            format_result(result) if args.debug else connection_string(result.protocol, result.proxy),
            file=sys.stdout,
        )
    return 0
