"""Download DB-IP Lite safely and resolve exit IPs from its local MMDB file.

The database is runtime state, not source code: it lives under ``geodb/``
beside the root launcher, is refreshed once per published calendar month, and
is replaced atomically only after the compressed download can be opened as a
valid MaxMind-format database. Lookups are local and safe to call concurrently
from monitor worker threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import os
from pathlib import Path
import shutil
import tempfile
import threading

import maxminddb
import requests

from proxytools.paths import geoip_database_path, geoip_version_path

DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{version}.mmdb.gz"
ATTRIBUTION = "IP Geolocation by DB-IP (https://db-ip.com)"
_reader = None
_reader_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class GeoIPStatus:
    path: Path | None
    updated: bool = False
    warning: str = ""


def _current_version(now=None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("%Y-%m")


def ensure_geoip_database(*, timeout=60, now=None) -> GeoIPStatus:
    """Ensure this clone has the current monthly DB-IP City Lite database."""
    database = geoip_database_path()
    version_file = geoip_version_path()
    version = _current_version(now)
    installed_version = ""
    try:
        installed_version = version_file.read_text(encoding="ascii").strip()
    except OSError:
        pass
    if database.is_file() and installed_version == version:
        return GeoIPStatus(database)

    compressed = None
    unpacked = None
    try:
        response = requests.get(DBIP_URL.format(version=version), stream=True, timeout=timeout)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(
            dir=database.parent, prefix=".proxytools-geoip-", delete=False
        ) as archive:
            compressed = Path(archive.name)
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    archive.write(chunk)
        response.close()
        with tempfile.NamedTemporaryFile(
            dir=database.parent, prefix=".proxytools-geoip-", delete=False
        ) as target:
            unpacked = Path(target.name)
            with gzip.open(compressed, "rb") as source:
                shutil.copyfileobj(source, target)
        probe = maxminddb.open_database(unpacked)
        probe.close()
        os.replace(unpacked, database)
        unpacked = None
        version_file.write_text(version + "\n", encoding="ascii")
        return GeoIPStatus(database, updated=True)
    except (OSError, ValueError, requests.RequestException, maxminddb.InvalidDatabaseError) as error:
        if database.is_file():
            return GeoIPStatus(database, warning=f"GeoIP update failed; using existing database: {error}")
        return GeoIPStatus(None, warning=f"GeoIP database unavailable; locations will be Unknown: {error}")
    finally:
        for temporary in (compressed, unpacked):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def configure_geoip(path: Path | None) -> None:
    """Open one shared reader, or disable local geolocation when path is absent."""
    global _reader
    with _reader_lock:
        if _reader is not None:
            _reader.close()
        _reader = maxminddb.open_database(path) if path is not None else None


def locate(ip: str) -> dict:
    """Return normalized country/city/coordinates for an IP address."""
    reader = _reader
    if reader is None or not ip:
        return {"country": "Unknown", "city": "Unknown", "lat": None, "lon": None}
    try:
        record = reader.get(ip) or {}
    except (ValueError, maxminddb.InvalidDatabaseError):
        record = {}
    country = record.get("country", {}).get("names", {}).get("en", "Unknown")
    city = record.get("city", {}).get("names", {}).get("en", "Unknown")
    location = record.get("location", {})
    return {
        "country": country,
        "city": city,
        "lat": location.get("latitude"),
        "lon": location.get("longitude"),
    }
