"""
Shared fetching/checking logic for proxylister.py and proxymonitor.py.
Not meant to be run directly; see README.md for usage of the two scripts.
"""

import re
import sys

import requests

API_URL = "https://api.proxyscrape.com/v2/"
PROXY_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")
PROTOCOLS = ("http", "socks4", "socks5")

# A single request through the proxy both confirms it's alive and reveals its
# exit IP's geolocation, so no separate liveness check is needed.
GEO_URL = "http://ip-api.com/json/?fields=status,country,lat,lon,query"


def fetch_proxy_list(protocol, timeout_ms=10000, country="all"):
    """Fetch raw proxy list text from the ProxyScrape API and extract ip:port pairs."""
    params = {
        "request": "getproxies",
        "protocol": protocol,
        "timeout": timeout_ms,
        "country": country,
        "ssl": "all",
        "anonymity": "all",
    }
    resp = requests.get(API_URL, params=params, timeout=15)
    resp.raise_for_status()
    return sorted(set(PROXY_RE.findall(resp.text)))


def fetch_all_proxies(verbose=False):
    """
    Fetch and dedupe proxies across all protocols. Returns a list of
    (protocol, ip:port). verbose=False by default so this is safe to call
    repeatedly from proxymonitor.py's curses loop.
    """
    entries = []
    for protocol in PROTOCOLS:
        try:
            proxies = fetch_proxy_list(protocol)
        except requests.RequestException as e:
            print(f"Failed to fetch {protocol} proxy list: {e}", file=sys.stderr)
            continue
        if verbose:
            print(f"  {protocol}: {len(proxies)} proxies")
        entries.extend((protocol, p) for p in proxies)

    # The same ip:port can appear under more than one protocol's list.
    deduped = []
    seen = set()
    for protocol, proxy in entries:
        if proxy in seen:
            continue
        seen.add(proxy)
        deduped.append((protocol, proxy))

    if verbose and len(entries) != len(deduped):
        print(f"  Filtered {len(entries) - len(deduped)} duplicate ip:port entries")

    return deduped


def connection_string(protocol, proxy):
    """Build the scheme://ip:port string used in browser/OS proxy settings."""
    return f"{protocol}://{proxy}"


def check_proxy(protocol, proxy, timeout=5):
    """
    Check whether a proxy is alive and geolocate its exit IP. Returns a dict
    with protocol, proxy, ok, country, lat, lon, latency_ms on success, or
    ok=False on failure. latency_ms is response.elapsed (request-sent to
    headers-received), i.e. pure network time with no local overhead mixed in.
    """
    conn = connection_string(protocol, proxy)
    proxies = {"http": conn, "https": conn}
    try:
        r = requests.get(GEO_URL, proxies=proxies, timeout=timeout)
        data = r.json()
        if r.status_code == 200 and data.get("status") == "success":
            return {
                "protocol": protocol,
                "proxy": proxy,
                "ok": True,
                "country": data.get("country", "Unknown"),
                "lat": data.get("lat"),
                "lon": data.get("lon"),
                "latency_ms": round(r.elapsed.total_seconds() * 1000),
            }
    except (requests.RequestException, ValueError):
        pass
    return {"protocol": protocol, "proxy": proxy, "ok": False}


def summarize_by_country(results):
    """
    Group proxy results by country. Returns a list of
    {"country", "count", "fastest_ms"} dicts, sorted ascending by
    fastest_ms (each country's single fastest proxy).
    """
    by_country = {}
    for r in results:
        entry = by_country.setdefault(r["country"], {"country": r["country"], "count": 0, "fastest_ms": None})
        entry["count"] += 1
        if entry["fastest_ms"] is None or r["latency_ms"] < entry["fastest_ms"]:
            entry["fastest_ms"] = r["latency_ms"]
    return sorted(by_country.values(), key=lambda e: e["fastest_ms"])
