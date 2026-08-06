"""Fast HTTP-based proxy validation and geolocation."""

import statistics
import time

import requests

from proxytools.http import session
from proxytools.models import ProxyResult

GEO_URL = "http://ip-api.com/json/?fields=status,country,city,lat,lon,query"
HTTPS_GEO_URL = "https://ipwho.is/"


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
                GEO_URL,
                proxies={"http": conn, "https": conn},
                timeout=timeout,
            )
            candidate = response.json()
            if response.status_code != 200 or candidate.get("status") != "success":
                return ProxyResult(protocol, proxy, False)
            durations.append(round((time.perf_counter() - started) * 1000))
            data = candidate
        except (requests.RequestException, ValueError):
            return ProxyResult(protocol, proxy, False)
    if data is None:
        return ProxyResult(protocol, proxy, False)
    return ProxyResult(
        protocol=protocol,
        proxy=proxy,
        ok=True,
        latency_ms=round(statistics.median(durations)),
        country=data.get("country", "Unknown"),
        lat=data.get("lat"),
        lon=data.get("lon"),
        city=data.get("city", "Unknown"),
        exit_ip=data.get("query", ""),
        http_exit_ip=data.get("query", ""),
    )


def probe_https_route(result: ProxyResult, timeout: float) -> bool:
    """Attach the HTTPS exit IP and its GeoIP data using one proxy request.

    Failure is diagnostic-only: the configured browser URL remains the source
    of truth for target availability, so an outage of this metadata provider
    must not mark an otherwise working proxy dead.
    """
    conn = connection_string(result.protocol, result.proxy)
    try:
        response = session().get(
            HTTPS_GEO_URL,
            proxies={"http": conn, "https": conn},
            timeout=timeout,
        )
        try:
            data = response.json()
            if response.status_code != 200 or data.get("success") is False or not data.get("ip"):
                return False
            result.exit_ip = data["ip"]
            result.country = data.get("country", result.country)
            result.city = data.get("city", result.city) or "Unknown"
            result.lat = data.get("latitude", result.lat)
            result.lon = data.get("longitude", result.lon)
            return True
        finally:
            response.close()
    except (requests.RequestException, ValueError):
        return False


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
