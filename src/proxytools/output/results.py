"""Prepare successful proxy checks for ``list`` command output.

Normal CLI output is emitted directly as connection strings by the command.
This module contains the remaining presentation policy: latency filtering,
fastest-first ordering, and the verbose one-line representation selected by
``--debug``. It performs no file I/O, so shell redirection remains the single
simple way to save or pipe results.
"""

from __future__ import annotations

from proxytools.checking import connection_string
from proxytools.models import ProxyResult


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
