"""Shared implementation used by every command-line tool in this project.

This module talks to the ProxyScrape API, downloads the currently advertised
HTTP, SOCKS4, and SOCKS5 endpoints, and keeps each protocol/address pair as a
separate candidate.  It also checks candidates through ``ip-api.com``: one
request confirms that the proxy works, measures its complete request duration,
and returns the exit country's name and coordinates.

The public helpers provide:

* concurrent proxy-list retrieval and deterministic deduplication;
* thread-local ``requests.Session`` reuse for worker threads;
* proxy and target-URL checks;
* typed ``ProxyResult`` and ``CountrySummary`` records;
* country aggregation and reusable argparse validators.

This is a library module, not a standalone program.  Keep it beside
``proxylister.py``, ``proxymonitor.py``, and ``proxycountry.py`` and run one of
those scripts instead.  Network access and ``requests[socks]`` are required.
See README.md for installation and end-user examples.
"""

from __future__ import annotations

import concurrent.futures
import argparse
from dataclasses import asdict, dataclass
import re
import statistics
import sys
import threading
import time
from typing import Iterable

import requests

API_URL = "https://api.proxyscrape.com/v2/"
GEO_URL = "http://ip-api.com/json/?fields=status,country,lat,lon,query"
PROXY_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")
PROTOCOLS = ("http", "socks4", "socks5")
MAX_WORKERS = 100
_thread_local = threading.local()


@dataclass(slots=True)
class ProxyResult:
    protocol: str
    proxy: str
    ok: bool
    latency_ms: int | None = None
    country: str = "Unknown"
    lat: float | None = None
    lon: float | None = None
    checked_at: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.protocol, self.proxy

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class CountrySummary:
    country: str
    count: int
    fastest_ms: int


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def worker_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_WORKERS}")
    return parsed


def sample_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= 5:
        raise argparse.ArgumentTypeError("must be between 1 and 5")
    return parsed


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def fetch_proxy_list(protocol: str, timeout_ms: int = 10000, country: str = "all") -> list[str]:
    """Fetch one protocol list and extract unique ip:port pairs."""
    params = {
        "request": "getproxies",
        "protocol": protocol,
        "timeout": timeout_ms,
        "country": country,
        "ssl": "all",
        "anonymity": "all",
    }
    response = _session().get(API_URL, params=params, timeout=15)
    response.raise_for_status()
    return sorted(set(PROXY_RE.findall(response.text)))


def fetch_all_proxies(verbose: bool = False) -> list[tuple[str, str]]:
    """Fetch all protocols concurrently and dedupe exact protocol/address pairs."""
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

    entries = [
        (protocol, proxy)
        for protocol in PROTOCOLS
        for proxy in by_protocol[protocol]
    ]
    deduped = list(dict.fromkeys(entries))
    if verbose:
        for protocol in PROTOCOLS:
            print(f"  {protocol}: {len(by_protocol[protocol])} proxies")
        if len(entries) != len(deduped):
            print(f"  Filtered {len(entries) - len(deduped)} duplicate protocol/address entries")
    return deduped


def connection_string(protocol: str, proxy: str) -> str:
    return f"{protocol}://{proxy}"


def check_proxy(protocol: str, proxy: str, timeout: float = 5, samples: int = 1) -> ProxyResult:
    """Check/geolocate a proxy and use the median complete request duration."""
    conn = connection_string(protocol, proxy)
    durations = []
    data = None
    for _ in range(samples):
        started = time.perf_counter()
        try:
            response = _session().get(
                GEO_URL,
                proxies={"http": conn, "https": conn},
                timeout=timeout,
            )
            candidate = response.json()
            if response.status_code != 200 or candidate.get("status") != "success":
                return ProxyResult(protocol=protocol, proxy=proxy, ok=False)
            durations.append(round((time.perf_counter() - started) * 1000))
            data = candidate
        except (requests.RequestException, ValueError):
            return ProxyResult(protocol=protocol, proxy=proxy, ok=False)
    if data is not None:
        return ProxyResult(
            protocol=protocol,
            proxy=proxy,
            ok=True,
            country=data.get("country", "Unknown"),
            lat=data.get("lat"),
            lon=data.get("lon"),
            latency_ms=round(statistics.median(durations)),
        )
    return ProxyResult(protocol=protocol, proxy=proxy, ok=False)


def check_url_via_requests(result: ProxyResult, url: str, timeout: float) -> bool:
    """Cheap preflight check of a target URL through a proxy before Chrome starts."""
    conn = connection_string(result.protocol, result.proxy)
    try:
        response = _session().get(
            url,
            proxies={"http": conn, "https": conn},
            timeout=timeout,
            stream=True,
        )
        try:
            return response.status_code < 400
        finally:
            response.close()
    except requests.RequestException:
        return False


def summarize_by_country(results: Iterable[ProxyResult]) -> list[CountrySummary]:
    by_country: dict[str, CountrySummary] = {}
    for result in results:
        if result.latency_ms is None:
            continue
        entry = by_country.get(result.country)
        if entry is None:
            by_country[result.country] = CountrySummary(result.country, 1, result.latency_ms)
        else:
            entry.count += 1
            entry.fastest_ms = min(entry.fastest_ms, result.latency_ms)
    return sorted(by_country.values(), key=lambda entry: entry.fastest_ms)


def fetch_available_countries(timeout: float = 5) -> list[str]:
    """Fetch ProxyScrape's country list without downloading proxy lists."""
    try:
        response = _session().get(
            f"{API_URL}getcountries",
            params={"ssl": "all", "anonymity": "all"},
            timeout=timeout,
        )
        response.raise_for_status()
        return sorted({country.strip() for country in response.text.splitlines() if country.strip()})
    except requests.RequestException as exc:
        print(f"Failed to fetch available countries: {exc}", file=sys.stderr)
        return []
