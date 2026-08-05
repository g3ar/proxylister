"""Aggregations over checked proxy results."""

from collections.abc import Iterable

from proxytools.models import CountrySummary, ProxyResult


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
