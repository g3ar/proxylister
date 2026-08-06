import threading
import unittest
from unittest.mock import patch

from proxytools.models import ProxyResult
from proxytools.monitoring import MonitorEngine
from proxytools.stability import StabilityConfig, StabilityPolicy
from proxytools.stability.history import ProxyHistory


class MonitorEngineTests(unittest.TestCase):
    def test_source_failure_is_reported_and_retried(self):
        stop = threading.Event()
        snapshots = []
        fetches = 0

        def fetcher():
            nonlocal fetches
            fetches += 1
            if fetches == 1:
                raise RuntimeError("temporary outage")
            return [("http", "1.2.3.4:80")]

        def checker(protocol, proxy, *_args):
            stop.set()
            return ProxyResult(protocol, proxy, True, 50, "France")

        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=2, timeout=1,
            samples=1, refresh_interval=0.01, retention_time=60,
            fetcher=fetcher, checker=checker,
        )
        engine.run(stop, snapshots.append)

        error = next(snapshot for snapshot in snapshots if snapshot.phase == "source_error")
        self.assertEqual(error.message, "temporary outage")
        self.assertGreaterEqual(fetches, 2)

    def test_checker_exception_records_failure_without_stopping_engine(self):
        stop = threading.Event()
        attempts = 0

        def checker(protocol, proxy, *_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("bad GeoIP record")
            stop.set()
            return ProxyResult(protocol, proxy, True, 50, "France")

        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=2, timeout=1,
            samples=1, refresh_interval=0.01, retention_time=60,
            fetcher=lambda: [("http", "1.2.3.4:80")], checker=checker,
        )
        engine.run(stop, lambda _snapshot: None)

        history = engine.histories[("http", "1.2.3.4:80")]
        self.assertGreaterEqual(attempts, 2)
        self.assertEqual(history.samples[0].failure_reason, "check error")
    def test_snapshot_hides_proxy_without_measured_latency(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=1, timeout=1,
            samples=1, refresh_interval=1, retention_time=60,
        )
        history = ProxyHistory("http", "1.2.3.4:80", 10)
        history.latest = ProxyResult("http", history.proxy, False)
        engine.histories[history.key] = history

        snapshot = engine.snapshot()

        self.assertEqual(snapshot.rows, ())
        self.assertEqual(snapshot.tracked_count, 1)

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

    def test_shutdown_progress_counts_only_running_work(self):
        stop = threading.Event()
        check_started = threading.Event()
        release_check = threading.Event()
        progress_started = threading.Event()
        progress = []

        def checker(protocol, proxy, *_args):
            check_started.set()
            release_check.wait(2)
            return ProxyResult(protocol, proxy, True, 50, "France")

        def shutdown_progress(completed, total):
            progress.append((completed, total))
            progress_started.set()

        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()),
            workers=2,
            timeout=1,
            samples=1,
            refresh_interval=1,
            retention_time=60,
            fetcher=lambda: [
                ("http", "1.2.3.4:80"),
                ("http", "2.3.4.5:80"),
                ("http", "3.4.5.6:80"),
            ],
            checker=checker,
        )
        runner = threading.Thread(
            target=engine.run,
            args=(stop, lambda _snapshot: None, shutdown_progress),
        )
        runner.start()
        self.assertTrue(check_started.wait(1))
        stop.set()
        self.assertTrue(progress_started.wait(1))
        release_check.set()
        runner.join(2)

        self.assertFalse(runner.is_alive())
        total = progress[0][1]
        self.assertEqual(total, 1)
        self.assertEqual(progress[0], (0, total))
        self.assertEqual(progress[-1], (total, total))
        self.assertEqual({reported_total for _, reported_total in progress}, {total})
        self.assertEqual(
            [completed for completed, _ in progress],
            sorted(completed for completed, _ in progress),
        )

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
        self.assertTrue(result.reachable)
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
        self.assertTrue(result.reachable)
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
        self.assertFalse(result.reachable)
        target_check.assert_not_called()

    def test_failed_https_identity_check_stays_failed_without_browser_url(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()), workers=1, timeout=3, samples=1,
            refresh_interval=1, retention_time=60,
            checker=lambda *args: ProxyResult("http", "1.2.3.4:80", False),
        )

        result = engine._check_candidate("http", "1.2.3.4:80")

        self.assertFalse(result.reachable)


if __name__ == "__main__":
    unittest.main()
