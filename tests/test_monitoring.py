import threading
import unittest

from proxytools.models import ProxyResult
from proxytools.monitoring import MonitorEngine
from proxytools.stability import StabilityConfig, StabilityPolicy


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


if __name__ == "__main__":
    unittest.main()
