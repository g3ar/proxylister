"""Validate a proxy through one HTTPS identity service and local GeoIP."""

import statistics
import time

import requests

from proxytools.http import session
from proxytools.geoip import locate
from proxytools.models import ProxyResult

IDENTITY_URL = "https://api.ipify.org?format=json"


def connection_string(protocol: str, proxy: str) -> str:
    return f"{protocol}://{proxy}"


def check_proxy(protocol: str, proxy: str, timeout: float = 5, samples: int = 1) -> ProxyResult:
    conn = connection_string(protocol, proxy)
    durations = []
    data = None
    for _ in range(samples):
        started = time.perf_counter()
        try:
            response = session().get(
                IDENTITY_URL,
                proxies={"http": conn, "https": conn},
                timeout=timeout,
            )
            try:
                candidate = response.json()
                if response.status_code != 200 or not candidate.get("ip"):
                    return ProxyResult(protocol, proxy, reachable=False)
                durations.append(round((time.perf_counter() - started) * 1000))
                data = candidate
            finally:
                response.close()
        except (requests.RequestException, ValueError):
            return ProxyResult(protocol, proxy, reachable=False)
    if data is None:
        return ProxyResult(protocol, proxy, reachable=False)
    exit_ip = data["ip"]
    location = locate(exit_ip)
    return ProxyResult(
        protocol=protocol,
        proxy=proxy,
        reachable=True,
        latency_ms=round(statistics.median(durations)),
        country=location["country"],
        lat=location["lat"],
        lon=location["lon"],
        city=location["city"],
        exit_ip=exit_ip,
    )


def check_url(
    result: ProxyResult,
    url: str,
    timeout: float,
    *,
    accept_forbidden: bool = False,
) -> bool:
    """Check a target through a proxy without launching a browser.

    Interactive-browser monitoring may accept HTTP 403 because anti-bot sites
    commonly reject ``requests`` while allowing Chrome to complete a challenge.
    Callers doing strict content validation retain the default behavior.
    """
    conn = connection_string(result.protocol, result.proxy)
    try:
        response = session().get(
            url,
            proxies={"http": conn, "https": conn},
            timeout=timeout,
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            return response.status_code < 400 or (
                accept_forbidden and response.status_code == 403
            )
        finally:
            response.close()
    except requests.RequestException:
        return False
