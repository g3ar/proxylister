import csv
import json
from pathlib import Path
import tempfile
import unittest

from proxytools.commands.scan import web_url
from proxytools.models import ProxyResult
from proxytools.output.dashboard import format_duration
from proxytools.output.serializers import filter_and_sort, write_results
from proxytools.stability import ProxyHistory, StabilityConfig, StabilityPolicy
from proxytools.stability.history import expire_histories, update_advertised


class CliHelperTests(unittest.TestCase):
    def setUp(self):
        self.fast = ProxyResult("http", "1.2.3.4:80", True, 50, "United States", 1.0, 2.0)
        self.slow = ProxyResult("socks5", "5.6.7.8:1080", True, 600, "Germany", 3.0, 4.0)

    def test_filter_and_sort(self):
        self.assertEqual(filter_and_sort([self.slow, self.fast], 500), [self.fast])

    def test_output_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            for output_format in ("text", "json", "csv"):
                path = Path(directory) / output_format
                write_results([self.fast], str(path), output_format)
                content = path.read_text(encoding="utf-8")
                self.assertIn("United States", content)
                if output_format == "json":
                    self.assertEqual(json.loads(content)["protocol"], "http")
                if output_format == "csv":
                    self.assertEqual(next(csv.DictReader(content.splitlines()))["proxy"], "1.2.3.4:80")

    def test_url_validation(self):
        self.assertEqual(web_url("https://example.com/a"), "https://example.com/a")
        with self.assertRaises(Exception):
            web_url("example.com")

    def test_monitor_keeps_protocols_and_expires_after_retention(self):
        histories = {}
        entries = [("http", self.fast.proxy), ("socks4", self.fast.proxy)]
        update_advertised(histories, entries, now=100, history_size=10)
        self.assertEqual(set(histories), set(entries))
        self.assertFalse(expire_histories(histories, now=159, retention_time=60))
        self.assertTrue(expire_histories(histories, now=161, retention_time=60))
        self.assertFalse(histories)

    def test_proxy_becomes_stable_only_after_minimum_alive_time(self):
        config = StabilityConfig(
            history_size=10,
            min_checks=3,
            min_success_rate=0.8,
            min_success_streak=3,
            min_alive_time=60,
            max_latency=500,
            max_jitter=150,
        )
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        for now, latency in ((100, 100), (130, 110), (159, 90)):
            history.record(ProxyResult("http", self.fast.proxy, True, latency), now, policy)
        self.assertEqual(history.state, "PROBATION")
        self.assertEqual(policy.blockers(history, 159), ["alive"])
        history.record(ProxyResult("http", self.fast.proxy, True, 105), 160, policy)
        self.assertEqual(history.state, "STABLE")
        self.assertEqual(policy.blockers(history, 160), [])
        self.assertEqual(history.alive_for(160), 60)
        self.assertEqual(history.stable_since, 160)
        self.assertEqual(history.success_rate, 1)
        self.assertEqual(history.median_latency, 102)
        self.assertEqual(history.p95_latency, 110)
        self.assertEqual(history.jitter, 7)

    def test_failure_degrades_and_resets_continuous_alive_time(self):
        config = StabilityConfig(min_checks=1, min_success_streak=1, min_alive_time=0)
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        history.record(self.fast, 100, policy)
        self.assertEqual(history.state, "STABLE")
        history.record(ProxyResult("http", self.fast.proxy, False), 110, policy)
        self.assertEqual(history.state, "DEGRADED")
        self.assertIn("failed", policy.blockers(history, 110))
        self.assertIsNone(history.alive_since)
        self.assertIsNone(history.stable_since)
        history.record(self.fast, 120, policy)
        self.assertEqual(history.state, "DEGRADED")

    def test_failure_tolerance_preserves_alive_origin(self):
        config = StabilityConfig(
            min_checks=1,
            min_success_streak=1,
            min_alive_time=0,
            failure_tolerance=1,
        )
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        history.record(self.fast, 100, policy)
        history.record(ProxyResult("http", self.fast.proxy, False), 110, policy)
        self.assertEqual(history.alive_since, 100)
        history.record(ProxyResult("http", self.fast.proxy, False), 120, policy)
        self.assertIsNone(history.alive_since)

    def test_duration_format(self):
        self.assertEqual(format_duration(65), "01:05")
        self.assertEqual(format_duration(299.9), "04:59")


if __name__ == "__main__":
    unittest.main()
