import threading
import unittest
from unittest.mock import patch

from proxytools.models import ProxyResult
from proxytools.monitoring import MonitorEngine
from proxytools.stability import StabilityConfig, StabilityPolicy
from proxytools.stability.history import ProxyHistory


class MonitorEngineTests(unittest.TestCase):
    def test_engine_emits_immutable_stable_snapshot(self):
        stop = threading.Event()
        snapshots = []

        def publish(snapshot):
            snapshots.append(snapshot)
            if snapshot.phase == "checking" and snapshot.checked == 1:
                stop.set()

        engine = MonitorEngine(
            policy=StabilityPolicy(
                StabilityConfig(min_checks=1, min_success_streak=1, min_alive_time=0)
            ),
            workers=1,
            timeout=1,
            samples=1,
            refresh_interval=1,
            retention_time=60,
            fetcher=lambda: [("http", "1.2.3.4:80")],
            checker=lambda *args: ProxyResult("http", "1.2.3.4:80", True, 50, "France"),
        )
        engine.run(stop, publish)

        final = snapshots[-1]
        self.assertEqual(final.stable_count, 1)
        self.assertEqual(final.rows[0].state, "STABLE")
        self.assertEqual(final.rows[0].country, "France")
        self.assertEqual(final.rows[0].blockers, ())

    def test_saved_proxies_are_checked_before_new_source_entries_without_duplicates(self):
        stop = threading.Event()
        checked_keys = []
        phases = []
        policy = StabilityPolicy(
            StabilityConfig(min_checks=1, min_success_streak=1, min_alive_time=0)
        )

        def checker(protocol, proxy, *_args):
            checked_keys.append((protocol, proxy))
            return ProxyResult(protocol, proxy, True, 50, "France")

        def publish(snapshot):
            phases.append(snapshot.phase)
            if snapshot.phase == "checking_new" and snapshot.checked == 1:
                stop.set()

        engine = MonitorEngine(
            policy=policy,
            workers=1,
            timeout=1,
            samples=1,
            refresh_interval=1,
            retention_time=60,
            fetcher=lambda: [("http", "1.2.3.4:80"), ("http", "5.6.7.8:80")],
            checker=checker,
        )
        saved = ProxyHistory("http", "1.2.3.4:80", 10)
        saved.latest = ProxyResult("http", saved.proxy, True, 40, "France")
        saved.restored = True
        engine.histories[saved.key] = saved
        engine.run(stop, publish)

        self.assertEqual(
            checked_keys,
            [("http", "1.2.3.4:80"), ("http", "5.6.7.8:80")],
        )
        self.assertLess(phases.index("restoring"), phases.index("fetching"))
        self.assertLess(phases.index("fetching"), phases.index("checking_new"))

    def test_browser_url_is_checked_with_requests_after_proxy_succeeds(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=1, timeout=3, samples=2,
            refresh_interval=1, retention_time=60, target_url="https://example.com",
            checker=lambda *args: ProxyResult("http", "1.2.3.4:80", True, 50, "France"),
        )
        with patch("proxytools.monitoring.check_url", return_value=True) as target_check:
            result = engine._check_candidate("http", "1.2.3.4:80")
        self.assertTrue(result.ok)
        target_check.assert_called_once_with(result, "https://example.com", 3)

    def test_failed_browser_url_marks_complete_check_as_url_failure(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=1, timeout=3, samples=1,
            refresh_interval=1, retention_time=60, target_url="https://example.com",
            checker=lambda *args: ProxyResult("http", "1.2.3.4:80", True, 50, "France"),
        )
        with patch("proxytools.monitoring.check_url", return_value=False):
            result = engine._check_candidate("http", "1.2.3.4:80")
        history = ProxyHistory("http", result.proxy, 10)
        history.record(result, 100, engine.policy)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "url")
        self.assertEqual(history.state, "DEGRADED")
        self.assertEqual(engine.policy.blockers(history, 100)[0], "url")

    def test_browser_url_is_skipped_when_base_proxy_check_fails(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=1, timeout=3, samples=1,
            refresh_interval=1, retention_time=60, target_url="https://example.com",
            checker=lambda *args: ProxyResult("http", "1.2.3.4:80", False),
        )
        with patch("proxytools.monitoring.check_url") as target_check:
            result = engine._check_candidate("http", "1.2.3.4:80")
        self.assertFalse(result.ok)
        target_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
