"""Fast HTTP-based proxy validation and geolocation."""

import statistics
import time

import requests

from proxytools.http import session
from proxytools.models import ProxyResult

GEO_URL = "http://ip-api.com/json/?fields=status,country,lat,lon,query"


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
    )


def check_url(result: ProxyResult, url: str, timeout: float) -> bool:
    conn = connection_string(result.protocol, result.proxy)
    try:
        response = session().get(
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
