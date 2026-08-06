import unittest
from dataclasses import replace
from unittest.mock import Mock

from textual.widgets import DataTable, SelectionList, Static

from proxytools.monitoring import MonitorEngine, MonitorRow, MonitorSnapshot
from proxytools.output.dashboard import ProxyMonitorApp
from proxytools.output.dashboard_widgets import ProtocolSelectionList
from proxytools.stability import StabilityConfig, StabilityPolicy


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def test_diagnostics_columns_require_debug_mode(self):
        normal = ProxyMonitorApp(None, autostart=False)
        debug = ProxyMonitorApp(None, autostart=False, debug=True)

        normal_labels = {label for label, _key in normal.columns}
        debug_labels = {label for label, _key in debug.columns}
        self.assertEqual(normal_labels, {"State", "Country", "Median", "Alive", "Connection"})
        diagnostics = {
            "Checks", "Streak", "OK", "P95", "Jitter",
            "City", "Exit IP", "Blocked by",
        }
        self.assertTrue(diagnostics.isdisjoint(normal_labels))
        self.assertTrue(diagnostics <= debug_labels)

    async def test_normal_status_hides_backend_lane_details(self):
        app = ProxyMonitorApp(Mock(), autostart=False)
        snapshot = MonitorSnapshot(
            3, 3, 10, 2, 8, "running", None, (),
            active_checked=1, active_total=4,
            discovery_checked=2, discovery_total=6,
        )

        async with app.run_test() as pilot:
            app.receive_snapshot(snapshot)
            await pilot.pause()
            status = str(app.query_one("#status", Static).content)
            self.assertIn("Checking 3/10", status)
            self.assertNotIn("Active", status)
            self.assertNotIn("Discovery", status)

    async def test_snapshot_populates_table_and_details(self):
        engine = MonitorEngine(
            policy=StabilityPolicy(StabilityConfig()),
            workers=1,
            timeout=1,
            samples=1,
            refresh_interval=1,
            retention_time=60,
        )
        app = ProxyMonitorApp(engine, autostart=False, debug=True)
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
            await pilot.press("y")
            self.assertEqual(app._clipboard, row.connection)
            second_row = replace(
                row,
                key=("socks5", "5.6.7.8:80"),
                country="Germany",
                connection="socks5://5.6.7.8:80",
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
            self.assertEqual(selected, "socks5|5.6.7.8:80")
            before_resort_row = table.cursor_row
            app.receive_snapshot(replace(two_rows, incremental=True, changed_rows=(), resort=True))
            self.assertEqual(table.cursor_row, before_resort_row)
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.assertEqual(selected, "socks5|5.6.7.8:80")
            sorted_snapshot = replace(
                two_rows,
                checked=10,
                total=10,
                phase="waiting",
                rows=(
                    replace(row, state="PROBATION", median_latency=200),
                    replace(second_row, state="DEGRADED", median_latency=50),
                ),
            )
            app.receive_snapshot(sorted_snapshot)
            self.assertEqual(table.row_count, 1)
            await pilot.press("s")
            state_options = app.screen.query_one(SelectionList)
            state_options.select("DEGRADED")
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertEqual(app.selected_states, {"STABLE", "PROBATION", "DEGRADED"})
            self.assertEqual(table.row_count, 2)
            self.assertEqual(table.get_row_at(0)[0].plain, "PROBATION")
            self.assertEqual(table.get_row_at(0)[5], "200ms")
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.assertEqual(selected, "http|1.2.3.4:80")
            await pilot.press("c")
            await pilot.press(*"fran", "enter")
            await pilot.pause(0.1)
            self.assertEqual(app.country_filter, "France")
            self.assertEqual(app.query_one(DataTable).row_count, 1)
            await pilot.press("p")
            protocol_options = app.screen.query_one(ProtocolSelectionList)
            protocol_options.deselect("socks5")
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertEqual(app.selected_protocols, {"http"})
            self.assertEqual(app.query_one(DataTable).row_count, 1)
            await pilot.press("s")
            state_options = app.screen.query_one(SelectionList)
            state_options.deselect("STABLE").deselect("PROBATION").select("DEGRADED")
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertEqual(app.query_one(DataTable).row_count, 0)
            await pilot.press("c", "enter")
            await pilot.pause(0.1)
            self.assertEqual(app.country_filter, "")
            self.assertEqual(app.query_one(DataTable).row_count, 0)
            await pilot.press("p")
            app.screen.query_one(ProtocolSelectionList).select("socks5")
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertEqual(app.selected_protocols, {"http", "socks5"})
            self.assertEqual(app.query_one(DataTable).row_count, 1)


if __name__ == "__main__":
    unittest.main()
