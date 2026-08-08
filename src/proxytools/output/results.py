"""Prepare and persist successful proxy checks from the ``list`` command.

Normal CLI output remains suitable for shell pipelines. The same selected
connection strings are also atomically saved beside the launcher or frozen
executable, so an interrupted interactive scan retains useful completed work.
"""

from __future__ import annotations

from pathlib import Path

from proxytools.checking import connection_string
from proxytools.models import ProxyResult
from proxytools.paths import working_proxies_path


def result_record(result: ProxyResult) -> dict:
    coords = f"{result.lat},{result.lon}"
    return {
        "latency_ms": result.latency_ms,
        "protocol": result.protocol,
        "proxy": result.proxy,
        "connection": connection_string(result.protocol, result.proxy),
        "country": result.country,
        "latitude": result.lat,
        "longitude": result.lon,
        "maps_url": f"https://www.google.com/maps?q={coords}",
    }


def format_result(result: ProxyResult) -> str:
    record = result_record(result)
    return (
        f"{record['latency_ms']}ms {record['protocol']} {record['proxy']} "
        f"{record['connection']} {record['country']} "
        f"{record['latitude']},{record['longitude']} {record['maps_url']}"
    )


def filter_and_sort(results: list[ProxyResult], max_latency_ms: float) -> list[ProxyResult]:
    return sorted(
        (result for result in results if result.latency_ms is not None and result.latency_ms < max_latency_ms),
        key=lambda result: result.latency_ms,
    )


def write_proxy_file(results: list[ProxyResult]) -> tuple[Path, int]:
    """Atomically save plain connection strings and return the path and count."""
    path = working_proxies_path()
    temporary = path.with_name(f".{path.name}.tmp")
    content = "".join(
        f"{connection_string(result.protocol, result.proxy)}\n" for result in results
    )
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path, len(results)
