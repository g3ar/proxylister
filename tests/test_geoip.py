import gzip
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from proxylister import geoip


class FakeDownload:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


class GeoIPTests(unittest.TestCase):
    def test_current_database_is_downloaded_and_versioned_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "proxylister-geoip.mmdb"
            version = Path(directory) / "proxylister-geoip.version"
            response = FakeDownload(gzip.compress(b"valid-mmdb"))
            with patch.object(geoip, "geoip_database_path", return_value=database), patch.object(
                geoip, "geoip_version_path", return_value=version
            ), patch.object(geoip.requests, "get", return_value=response) as download, patch.object(
                geoip.maxminddb, "open_database", return_value=Mock()
            ):
                status = geoip.ensure_geoip_database(now=datetime(2026, 8, 6, tzinfo=UTC))

            self.assertTrue(status.updated)
            self.assertEqual(database.read_bytes(), b"valid-mmdb")
            self.assertEqual(version.read_text().strip(), "2026-08")
            self.assertIn("2026-08", download.call_args.args[0])

    def test_failed_update_keeps_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "proxylister-geoip.mmdb"
            version = Path(directory) / "proxylister-geoip.version"
            database.write_bytes(b"old-mmdb")
            version.write_text("2026-07\n")
            with patch.object(geoip, "geoip_database_path", return_value=database), patch.object(
                geoip, "geoip_version_path", return_value=version
            ), patch.object(geoip.requests, "get", side_effect=requests.ConnectionError("offline")):
                status = geoip.ensure_geoip_database(now=datetime(2026, 8, 6, tzinfo=UTC))

            self.assertEqual(status.path, database)
            self.assertIn("using existing database", status.warning)
            self.assertEqual(database.read_bytes(), b"old-mmdb")

    def test_lookup_normalizes_dbip_record(self):
        reader = Mock()
        reader.get.return_value = {
            "country": {"names": {"en": "Germany"}},
            "city": {"names": {"en": "Nuremberg"}},
            "location": {"latitude": 49.45, "longitude": 11.08},
        }
        with patch.object(geoip, "_reader", reader):
            location = geoip.locate("203.0.113.20")
        self.assertEqual(location["country"], "Germany")
        self.assertEqual(location["city"], "Nuremberg")


if __name__ == "__main__":
    unittest.main()
