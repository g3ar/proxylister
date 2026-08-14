"""ProxyScrape API client and proxy-list parsing."""

from __future__ import annotations

import concurrent.futures
import re
import sys

import requests

from proxylister.http import session

API_URL = "https://api.proxyscrape.com/v2/"
PROXY_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")
PROTOCOLS = ("http", "socks4", "socks5")


class ProxySourceUnavailable(RuntimeError):
    """Raised when every ProxyScrape protocol request fails."""


def fetch_proxy_list(protocol: str, timeout_ms: int = 10000, country: str = "all") -> list[str]:
    params = {
        "request": "getproxies",
        "protocol": protocol,
        "timeout": timeout_ms,
        "country": country,
        "ssl": "all",
        "anonymity": "all",
    }
    response = session().get(API_URL, params=params, timeout=15)
    try:
        response.raise_for_status()
        return sorted(set(PROXY_RE.findall(response.text)))
    finally:
        response.close()


def fetch_all_proxies(verbose: bool = False) -> list[tuple[str, str]]:
    by_protocol: dict[str, list[str]] = {}
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROTOCOLS)) as executor:
        futures = {executor.submit(fetch_proxy_list, protocol): protocol for protocol in PROTOCOLS}
        for future in concurrent.futures.as_completed(futures):
            protocol = futures[future]
            try:
                by_protocol[protocol] = future.result()
            except requests.RequestException as exc:
                print(f"Failed to fetch {protocol} proxy list: {exc}", file=sys.stderr)
                by_protocol[protocol] = []
                failures += 1

    if failures == len(PROTOCOLS):
        raise ProxySourceUnavailable("all ProxyScrape requests failed")

    entries = [(protocol, proxy) for protocol in PROTOCOLS for proxy in by_protocol[protocol]]
    deduped = list(dict.fromkeys(entries))
    if verbose:
        for protocol in PROTOCOLS:
            print(f"  {protocol}: {len(by_protocol[protocol])} proxies", file=sys.stderr)
        if len(entries) != len(deduped):
            print(
                f"  Filtered {len(entries) - len(deduped)} duplicate protocol/address entries",
                file=sys.stderr,
            )
    return deduped
