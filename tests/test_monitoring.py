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
            if snapshot.phase == "running" and snapshot.stable_count == 1:
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

    def test_active_lane_rechecks_saved_proxy_while_discovery_is_still_running(self):
        stop = threading.Event()
        checked_keys = []
        snapshots = []
        saved_checks = 0
        policy = StabilityPolicy(
            StabilityConfig(min_checks=1, min_success_streak=1, min_alive_time=0)
        )

        def checker(protocol, proxy, *_args):
            nonlocal saved_checks
            checked_keys.append((protocol, proxy))
            if proxy == "1.2.3.4:80":
                saved_checks += 1
                if saved_checks >= 2:
                    stop.set()
            else:
                stop.wait(0.5)
            return ProxyResult(protocol, proxy, True, 50, "France")

        engine = MonitorEngine(
            policy=policy,
            workers=1,
            timeout=1,
            samples=1,
            refresh_interval=0.05,
            retention_time=60,
            fetcher=lambda: [("http", "1.2.3.4:80"), ("http", "5.6.7.8:80")],
            checker=checker,
        )
        saved = ProxyHistory("http", "1.2.3.4:80", 10)
        saved.latest = ProxyResult("http", saved.proxy, True, 40, "France")
        saved.restored = True
        engine.histories[saved.key] = saved
        engine.run(stop, snapshots.append)

        self.assertGreaterEqual(checked_keys.count(("http", "1.2.3.4:80")), 2)
        self.assertIn(("http", "5.6.7.8:80"), checked_keys)
        resort_snapshots = [snapshot for snapshot in snapshots if snapshot.resort]
        self.assertTrue(resort_snapshots)
        self.assertTrue(all(
            snapshot.active_total > 0
            and snapshot.active_checked == snapshot.active_total
            for snapshot in resort_snapshots
        ))

    def test_browser_url_is_checked_with_requests_after_proxy_succeeds(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=1, timeout=3, samples=2,
            refresh_interval=1, retention_time=60, target_url="https://example.com",
            checker=lambda *args: ProxyResult("http", "1.2.3.4:80", True, 50, "France"),
        )
        with patch("proxytools.monitoring.check_url", return_value=True) as target_check:
            result = engine._check_candidate("http", "1.2.3.4:80")
        self.assertTrue(result.ok)
        target_check.assert_called_once_with(
            result, "https://example.com", 3, accept_forbidden=True
        )

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
        self.assertEqual(history.state, "PROBATION")
        self.assertEqual(history.failure_since, 100)
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

    def test_failed_https_identity_check_stays_failed_without_browser_url(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=1, timeout=3, samples=1,
            refresh_interval=1, retention_time=60,
            checker=lambda *args: ProxyResult("http", "1.2.3.4:80", False),
        )

        result = engine._check_candidate("http", "1.2.3.4:80")

        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
