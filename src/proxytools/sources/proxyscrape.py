"""ProxyScrape API client and proxy-list parsing."""

from __future__ import annotations

import concurrent.futures
import re
import sys

import requests

from proxytools.http import session

API_URL = "https://api.proxyscrape.com/v2/"
PROXY_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")
PROTOCOLS = ("http", "socks4", "socks5")


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
    response.raise_for_status()
    return sorted(set(PROXY_RE.findall(response.text)))


def fetch_all_proxies(verbose: bool = False) -> list[tuple[str, str]]:
    by_protocol: dict[str, list[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROTOCOLS)) as executor:
        futures = {executor.submit(fetch_proxy_list, protocol): protocol for protocol in PROTOCOLS}
        for future in concurrent.futures.as_completed(futures):
            protocol = futures[future]
            try:
                by_protocol[protocol] = future.result()
            except requests.RequestException as exc:
                print(f"Failed to fetch {protocol} proxy list: {exc}", file=sys.stderr)
                by_protocol[protocol] = []

    entries = [(protocol, proxy) for protocol in PROTOCOLS for proxy in by_protocol[protocol]]
    deduped = list(dict.fromkeys(entries))
    if verbose:
        for protocol in PROTOCOLS:
            print(f"  {protocol}: {len(by_protocol[protocol])} proxies")
        if len(entries) != len(deduped):
            print(f"  Filtered {len(entries) - len(deduped)} duplicate protocol/address entries")
    return deduped


def fetch_available_countries(timeout: float = 5) -> list[str]:
    try:
        response = session().get(
            f"{API_URL}getcountries",
            params={"ssl": "all", "anonymity": "all"},
            timeout=timeout,
        )
        response.raise_for_status()
        return sorted({country.strip() for country in response.text.splitlines() if country.strip()})
    except requests.RequestException as exc:
        print(f"Failed to fetch available countries: {exc}", file=sys.stderr)
        return []
