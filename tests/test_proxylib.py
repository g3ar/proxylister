import argparse
import unittest
from unittest.mock import patch

import requests

from proxytools import config
from proxytools.analytics import summarize_by_country
from proxytools.checking import proxy as checker
from proxytools.models import ProxyResult
from proxytools.sources import proxyscrape


class FakeResponse:
    def __init__(self, text="", status_code=200, payload=None):
        self.text = text
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError()

    def json(self):
        return self._payload


class ProxyLibraryTests(unittest.TestCase):
    def test_fetch_proxy_list_extracts_and_dedupes(self):
        response = FakeResponse("1.2.3.4:80\ninvalid\n1.2.3.4:80\n5.6.7.8:1080")
        with patch.object(proxyscrape, "session") as session:
            session.return_value.get.return_value = response
            self.assertEqual(proxyscrape.fetch_proxy_list("http"), ["1.2.3.4:80", "5.6.7.8:1080"])

    def test_all_protocols_for_same_address_are_preserved(self):
        lists = {"http": ["1.2.3.4:80"], "socks4": ["1.2.3.4:80"], "socks5": []}
        with patch.object(proxyscrape, "fetch_proxy_list", side_effect=lambda protocol: lists[protocol]):
            self.assertEqual(
                proxyscrape.fetch_all_proxies(),
                [("http", "1.2.3.4:80"), ("socks4", "1.2.3.4:80")],
            )

    def test_country_summary_is_sorted_by_fastest(self):
        results = [
            ProxyResult("http", "a:1", True, 80, "B"),
            ProxyResult("http", "b:2", True, 40, "A"),
            ProxyResult("http", "c:3", True, 60, "A"),
        ]
        summary = summarize_by_country(results)
        self.assertEqual([(item.country, item.count, item.fastest_ms) for item in summary], [("A", 2, 40), ("B", 1, 80)])

    def test_check_proxy_uses_median_complete_duration(self):
        response = FakeResponse(payload={"status": "success", "country": "A", "lat": 1, "lon": 2})
        with patch.object(checker, "session") as session, patch.object(
            checker.time, "perf_counter", side_effect=[0.0, 0.3, 1.0, 1.1, 2.0, 2.2]
        ):
            session.return_value.get.return_value = response
            result = checker.check_proxy("http", "1.2.3.4:80", samples=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.latency_ms, 200)

    def test_argument_validators_reject_bad_values(self):
        for value in ("0", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                config.positive_float(value)
        for value in ("0", "101"):
            with self.assertRaises(argparse.ArgumentTypeError):
                config.worker_count(value)
        for value in ("0", "6"):
            with self.assertRaises(argparse.ArgumentTypeError):
                config.sample_count(value)


if __name__ == "__main__":
    unittest.main()
