"""Text, JSON Lines, and CSV proxy-result serialization."""

from __future__ import annotations

import csv
import json
from pathlib import Path

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


def write_results(results: list[ProxyResult], output_path: str, output_format: str) -> None:
    path = Path(output_path)
    records = [result_record(result) for result in results]
    with path.open("w", encoding="utf-8", newline="") as output:
        if output_format == "text":
            output.write("\n".join(format_result(result) for result in results))
            if results:
                output.write("\n")
        elif output_format == "json":
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            fieldnames = list(result_record(ProxyResult("", "", True, 0)).keys())
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)
