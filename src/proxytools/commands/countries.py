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

from proxytools.checking import check_proxy
from proxytools.analytics import summarize_by_country
from proxytools.config import positive_float, sample_count, worker_count
from proxytools.output.console import console, progress_display
from proxytools.sources.proxyscrape import fetch_all_proxies, fetch_available_countries


def print_summary(results):
    header = f"{'COUNTRY':<25}  {'COUNT':>5}  FASTEST"
    console.print(f"\n[bold]{header}[/bold]\n{'-' * len(header)}")
    for entry in summarize_by_country(results):
        console.print(f"{entry.country[:25]:<25}  {entry.count:>5}  {entry.fastest_ms}ms")


def build_parser(prog="proxytools countries"):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    network = parser.add_argument_group("network")
    network.add_argument("-t", "--timeout", type=positive_float, default=5, help="Seconds per proxy check")
    network.add_argument("-w", "--workers", type=worker_count, default=50, help="Number of workers (1-100)")
    network.add_argument("-l", "--max-latency", type=positive_float, default=500, help="Maximum duration in ms")
    network.add_argument("--samples", type=sample_count, default=1, help="Checks per proxy; median duration (1-5)")
    mode = parser.add_argument_group("mode")
    mode.add_argument("--list-countries", action="store_true", help="List advertised countries and exit")
    mode.add_argument("--verbose", action="store_true", help="Print per-protocol source counts")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list_countries:
        for country in fetch_available_countries(args.timeout):
            console.print(country)
        return 0

    console.print("[bold]Fetching proxy lists from ProxyScrape[/bold] (http, socks4, socks5)…")
    entries = fetch_all_proxies(verbose=args.verbose)
    if not entries:
        console.print("[yellow]No proxies found.[/yellow]")
        return 0

    console.print(f"Fetched [bold]{len(entries)}[/bold] protocol/address pairs; using {args.workers} workers.")
    valid = []
    interrupted = False
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = [executor.submit(check_proxy, protocol, proxy, args.timeout, args.samples) for protocol, proxy in entries]
        with progress_display() as progress:
            task = progress.add_task("Checking proxies", total=len(entries), status="starting")
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result.ok and result.latency_ms is not None and result.latency_ms < args.max_latency:
                    valid.append(result)
                progress.update(task, advance=1, status=f"{len(valid)} valid")
        console.print(f"[bold green]{len(valid)}[/bold green]/{len(entries)} proxies are valid.")
    except KeyboardInterrupt:
        interrupted = True
        console.print(f"[yellow]Interrupted — summarizing {len(valid)} results.[/yellow]")
    finally:
        executor.shutdown(wait=not interrupted, cancel_futures=True)
    print_summary(valid) if valid else console.print("[yellow]No valid proxies found.[/yellow]")
    if interrupted:
        os._exit(0)
    return 0
