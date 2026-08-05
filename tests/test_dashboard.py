import unittest

from textual.widgets import DataTable, Static

from proxytools.monitoring import MonitorEngine, MonitorRow, MonitorSnapshot
from proxytools.output.dashboard import ProxyMonitorApp
from proxytools.stability import StabilityConfig, StabilityPolicy


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_populates_table_and_details(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()),
            workers=1,
            timeout=1,
            samples=1,
            refresh_interval=1,
            retention_time=60,
        )
        app = ProxyMonitorApp(engine, autostart=False)
        row = MonitorRow(
            key=("http", "1.2.3.4:80"),
            state="PROBATION",
            alive_seconds=59.9,
            checks=4,
            required_checks=5,
            streak=4,
            success_rate=1,
            median_latency=100,
            p95_latency=120,
            jitter=10,
            country="France",
            blockers=("alive", "checks"),
            connection="http://1.2.3.4:80",
        )
        snapshot = MonitorSnapshot(1, 4, 10, 0, 1, "checking", None, (row,))

        async with app.run_test() as pilot:
            app.receive_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 1)
            self.assertIn("Checking 4/10", str(app.query_one("#status", Static).content))
            self.assertIn("Blocked by: alive, checks", str(app.query_one("#details", Static).content))


if __name__ == "__main__":
    unittest.main()
