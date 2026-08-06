import unittest

from proxytools.config import web_url
from proxytools.models import ProxyResult
from proxytools.output.dashboard_widgets import format_duration
from proxytools.output.results import filter_and_sort
from proxytools.stability import ProxyHistory, StabilityConfig, StabilityPolicy
from proxytools.stability.history import expire_histories, update_advertised


class CliHelperTests(unittest.TestCase):
    def setUp(self):
        self.fast = ProxyResult("http", "1.2.3.4:80", True, 50, "United States", 1.0, 2.0)
        self.slow = ProxyResult("socks5", "5.6.7.8:1080", True, 600, "Germany", 3.0, 4.0)

    def test_filter_and_sort(self):
        self.assertEqual(filter_and_sort([self.slow, self.fast], 500), [self.fast])

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

    def test_stable_failure_gets_grace_then_degrades_and_recovers(self):
        config = StabilityConfig(
            min_checks=1,
            min_success_streak=1,
            min_alive_time=0,
            failure_tolerance=0,
        )
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        history.record(self.fast, 100, policy)
        self.assertEqual(history.state, "STABLE")
        history.record(ProxyResult("http", self.fast.proxy, False), 110, policy)
        self.assertEqual(history.state, "PROBATION")
        self.assertIn("failed", policy.blockers(history, 110))
        self.assertIsNone(history.alive_since)
        self.assertIsNone(history.stable_since)
        self.assertEqual(history.failure_since, 110)
        history.record(ProxyResult("http", self.fast.proxy, False), 169, policy)
        self.assertEqual(history.state, "PROBATION")
        history.record(ProxyResult("http", self.fast.proxy, False), 170, policy)
        self.assertEqual(history.state, "DEGRADED")
        history.record(self.fast, 180, policy)
        self.assertEqual(history.state, "STABLE")
        self.assertEqual(history.consecutive_successes, 1)
        self.assertIsNone(history.failure_since)

    def test_default_failure_tolerance_resets_alive_on_third_failure(self):
        config = StabilityConfig(
            min_checks=1,
            min_success_streak=1,
            min_alive_time=0,
        )
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        history.record(self.fast, 100, policy)
        history.record(ProxyResult("http", self.fast.proxy, False), 110, policy)
        self.assertEqual(history.alive_since, 100)
        self.assertEqual(history.state, "STABLE")
        history.record(ProxyResult("http", self.fast.proxy, False), 120, policy)
        self.assertEqual(history.alive_since, 100)
        self.assertEqual(history.state, "STABLE")
        history.record(ProxyResult("http", self.fast.proxy, False), 130, policy)
        self.assertIsNone(history.alive_since)
        self.assertEqual(history.state, "PROBATION")
        self.assertEqual(history.failure_since, 130)
        history.record(ProxyResult("http", self.fast.proxy, False), 189, policy)
        self.assertEqual(history.state, "PROBATION")
        history.record(ProxyResult("http", self.fast.proxy, False), 190, policy)
        self.assertEqual(history.state, "DEGRADED")
        history.record(self.fast, 200, policy)
        self.assertEqual(history.state, "STABLE")

    def test_one_slow_response_does_not_demote_stable_proxy(self):
        config = StabilityConfig(
            history_size=5, min_checks=1, min_success_streak=1, min_alive_time=0
        )
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        for now in range(100, 105):
            history.record(self.fast, now, policy)

        history.record(ProxyResult("http", self.fast.proxy, True, 800), 110, policy)

        self.assertEqual(history.state, "STABLE")
        self.assertEqual(history.consecutive_failures, 0)
        self.assertIsNone(history.failure_since)

    def test_sustained_slow_median_moves_stable_to_probation_until_recovery(self):
        config = StabilityConfig(
            history_size=5, min_checks=1, min_success_streak=1, min_alive_time=0
        )
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        for now in range(100, 105):
            history.record(self.fast, now, policy)

        for now in range(110, 113):
            history.record(ProxyResult("http", self.fast.proxy, True, 800), now, policy)

        self.assertEqual(history.median_latency, 800)
        self.assertEqual(history.state, "PROBATION")
        self.assertIsNone(history.failure_since)

        for now in range(120, 123):
            history.record(self.fast, now, policy)

        self.assertEqual(history.median_latency, 50)
        self.assertEqual(history.state, "STABLE")

    def test_url_failure_is_reachable_but_not_accepted(self):
        config = StabilityConfig(min_checks=1, min_success_streak=1, min_alive_time=0)
        policy = StabilityPolicy(config)
        history = ProxyHistory("http", self.fast.proxy, config.history_size)
        result = ProxyResult("http", self.fast.proxy, True, 125, failure_reason="url")

        history.record(result, 100, policy)

        sample = history.samples[-1]
        self.assertTrue(sample.reachable)
        self.assertFalse(sample.accepted)
        self.assertEqual(history.median_latency, 125)
        self.assertEqual(history.success_rate, 0)
        self.assertEqual(policy.blockers(history, 100)[0], "url")

    def test_duration_format(self):
        self.assertEqual(format_duration(65), "01:05")
        self.assertEqual(format_duration(299.9), "04:59")


if __name__ == "__main__":
    unittest.main()
