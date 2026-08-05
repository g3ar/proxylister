import unittest
from dataclasses import replace

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
            second_row = replace(
                row,
                key=("http", "5.6.7.8:80"),
                country="Germany",
                connection="http://5.6.7.8:80",
            )
            two_rows = replace(snapshot, rows=(row, second_row), tracked_count=2)
            app.receive_snapshot(two_rows)
            table = app.query_one(DataTable)
            table.move_cursor(row=1, animate=False)
            app.receive_snapshot(
                replace(two_rows, rows=(replace(row, alive_seconds=60.5), replace(second_row, alive_seconds=61)))
            )
            self.assertEqual(table.cursor_row, 1)
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.assertEqual(selected, "http|5.6.7.8:80")
            sorted_snapshot = replace(
                two_rows,
                checked=10,
                total=10,
                phase="waiting",
                rows=(replace(row, median_latency=200), replace(second_row, median_latency=50)),
            )
            app.receive_snapshot(sorted_snapshot)
            self.assertEqual(table.get_row_at(0)[5], "50ms")
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.assertEqual(selected, "http|5.6.7.8:80")
            await pilot.press("c")
            await pilot.press(*"spain", "enter")
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 0)
            await pilot.press("c")
            country_input = app.query_one("#country-filter")
            country_input.value = "fran"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 1)


if __name__ == "__main__":
    unittest.main()
