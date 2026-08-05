"""Interactive Textual dashboard for proxy stability monitoring."""

from __future__ import annotations

import threading
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Input, Static

from proxytools.monitoring import MonitorEngine, MonitorRow, MonitorSnapshot
from proxytools.browser import BrowserUnavailable, launch_browser_session


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class ProxyMonitorApp(App):
    """Full-screen, keyboard-driven proxy monitor."""

    TITLE = "Proxy Tools"
    SUB_TITLE = "Stable proxy monitor"
    CSS = """
    Screen {
        layout: vertical;
    }
    #status {
        height: 3;
        padding: 0 1;
        content-align: left middle;
        background: $panel;
    }
    #table {
        height: 1fr;
    }
    #country-filter {
        display: none;
        dock: top;
        margin: 0 1;
    }
    #details {
        height: 8;
        padding: 1 2;
        border-top: solid $primary;
        background: $surface;
    }
    .paused {
        background: $warning-darken-2;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "pause", "Pause"),
        Binding("s", "stable_only", "Stable only"),
        Binding("c", "country_filter", "Country"),
        Binding("r", "refresh", "Refresh"),
        Binding("b", "browser", "Browser"),
        Binding("escape", "cancel_filter", "Cancel filter", show=False),
    ]
    COLUMNS = (
        ("State", "state"),
        ("Alive", "alive"),
        ("Checks", "checks"),
        ("Streak", "streak"),
        ("OK", "success"),
        ("Median", "median"),
        ("P95", "p95"),
        ("Jitter", "jitter"),
        ("Country", "country"),
        ("Blocked by", "blocked"),
        ("Connection", "connection"),
    )

    def __init__(
        self, engine: MonitorEngine, *, stable_only=False, autostart=True,
        browser="auto", browser_url="about:blank",
    ):
        super().__init__()
        self.engine = engine
        self.stable_only = stable_only
        self.autostart = autostart
        self.paused = False
        self.country_filter = ""
        self.stop_event = threading.Event()
        self.latest_snapshot: MonitorSnapshot | None = None
        self.pending_snapshot: MonitorSnapshot | None = None
        self.rows_by_key: dict[str, MonitorRow] = {}
        self.cells_by_key: dict[str, tuple] = {}
        self.completed_cycle = 0
        self.browser = browser
        self.browser_url = browser_url
        self.browser_process = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Starting monitor…", id="status")
        yield Input(placeholder="Country filter (empty = all countries)", id="country-filter")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("Select a proxy to see stability details.", id="details")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#table", DataTable)
        table.add_columns(*self.COLUMNS)
        if self.autostart:
            self.monitor_worker()

    def on_unmount(self):
        self.stop_event.set()
        self.engine.request_refresh()

    @work(thread=True, exclusive=True, group="monitor")
    def monitor_worker(self):
        self.engine.run(
            self.stop_event,
            lambda snapshot: self.call_from_thread(self.receive_snapshot, snapshot),
        )

    def receive_snapshot(self, snapshot: MonitorSnapshot):
        self.latest_snapshot = snapshot
        if self.paused:
            self.pending_snapshot = snapshot
            return
        # Waiting snapshots only change the countdown and elapsed wall clock.
        # Rewriting thousands of table cells once a second starves keyboard
        # events, so keep the table frozen until a proxy is checked again.
        if snapshot.phase == "waiting" and self.completed_cycle == snapshot.cycle:
            self._render_status(snapshot, len(self.rows_by_key))
            return
        self.render_snapshot(snapshot)

    def render_snapshot(self, snapshot: MonitorSnapshot):
        table = self.query_one("#table", DataTable)
        selected_key = None
        if table.row_count:
            selected_key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        rows = self.filtered_rows(snapshot)
        next_rows = {f"{row.key[0]}|{row.key[1]}": row for row in rows}
        for removed_key in self.rows_by_key.keys() - next_rows.keys():
            table.remove_row(removed_key)
            self.cells_by_key.pop(removed_key, None)
        for row in rows:
            key = f"{row.key[0]}|{row.key[1]}"
            cells = self._row_cells(row)
            if key in self.rows_by_key:
                # A new completed check is the authoritative signal that this
                # row changed. Avoid scanning/updating every cell merely because
                # its computed Alive duration advanced between snapshots.
                old_row = self.rows_by_key[key]
                if row.last_checked_at != old_row.last_checked_at or (
                    row.last_checked_at is None and row != old_row
                ):
                    previous_cells = self.cells_by_key.get(key, ())
                    for index, ((_, column_key), value) in enumerate(zip(self.COLUMNS, cells)):
                        if index >= len(previous_cells) or value != previous_cells[index]:
                            table.update_cell(key, column_key, value, update_width=False)
                    self.cells_by_key[key] = cells
            else:
                table.add_row(*cells, key=key)
                self.cells_by_key[key] = cells
        self.rows_by_key = next_rows
        cycle_complete = snapshot.checked == snapshot.total and snapshot.phase in {"checking", "waiting"}
        if cycle_complete:
            table.sort("median", key=self._latency_sort_key)
            self.completed_cycle = snapshot.cycle
            if selected_key in self.rows_by_key:
                table.move_cursor(row=table.get_row_index(selected_key), animate=False)
        self._render_status(snapshot, len(rows))
        if rows:
            current_key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            self._render_details(self.rows_by_key.get(current_key, rows[0]))
        else:
            self.query_one("#details", Static).update("No proxies match the current filter.")

    def _render_status(self, snapshot: MonitorSnapshot, visible: int):
        phase = {
            "fetching": "Fetching ProxyScrape lists…",
            "checking": f"Checking {snapshot.checked}/{snapshot.total}",
            "waiting": f"Next cycle in {snapshot.next_cycle_in}s",
        }.get(snapshot.phase, snapshot.phase.title())
        filters = ["stable only" if self.stable_only else "all states"]
        if self.country_filter:
            filters.append(f"country contains '{self.country_filter}'")
        text = (
            f"Cycle {snapshot.cycle} │ {phase} │ {snapshot.stable_count} stable │ "
            f"{snapshot.tracked_count} tracked │ {visible} visible │ {', '.join(filters)}"
        )
        if self.paused:
            text += " │ PAUSED (checks continue)"
        status = self.query_one("#status", Static)
        status.update(text)
        status.set_class(self.paused, "paused")

    def _render_details(self, row: MonitorRow):
        blocked = ", ".join(row.blockers) or "none"
        first_seen = self._timestamp(row.first_seen_at)
        last_failure = self._timestamp(row.last_failure_at)
        detail = Text.from_markup(
            f"[bold]{row.connection}[/bold]  [{self._state_color(row.state)}]{row.state}[/]\n"
            f"Country: {row.country}    Alive: {format_duration(row.alive_seconds)}    "
            f"Checks: {row.checks}/{row.required_checks}    Streak: {row.streak}    Success: {row.success_rate:.1%}\n"
            f"Median: {self._milliseconds(row.median_latency)}    P95: {self._milliseconds(row.p95_latency)}    "
            f"Jitter: {row.jitter}ms    Blocked by: {blocked}\n"
            f"First seen: {first_seen}    Observed uptime: {format_duration(row.total_observed_uptime)}    "
            f"Last failure: {last_failure}"
        )
        self.query_one("#details", Static).update(detail)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        row = self.rows_by_key.get(str(event.row_key.value))
        if row is not None:
            self._render_details(row)

    def action_pause(self):
        self.paused = not self.paused
        if not self.paused and self.pending_snapshot is not None:
            snapshot, self.pending_snapshot = self.pending_snapshot, None
            self.render_snapshot(snapshot)
        elif self.latest_snapshot is not None:
            self._render_status(self.latest_snapshot, len(self.rows_by_key))

    def action_stable_only(self):
        self.stable_only = not self.stable_only
        if self.latest_snapshot is not None:
            self.render_snapshot(self.latest_snapshot)

    def action_country_filter(self):
        country_input = self.query_one("#country-filter", Input)
        country_input.value = self.country_filter
        country_input.display = True
        country_input.focus()

    def action_cancel_filter(self):
        country_input = self.query_one("#country-filter", Input)
        if country_input.display:
            country_input.display = False
            self.query_one("#table", DataTable).focus()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id != "country-filter":
            return
        self.country_filter = event.value.strip()
        event.input.display = False
        self.query_one("#table", DataTable).focus()
        if self.latest_snapshot is not None:
            self.render_snapshot(self.latest_snapshot)

    def action_refresh(self):
        self.engine.request_refresh()
        self.notify("Refresh requested")

    def action_browser(self):
        if self.browser_process is not None and self.browser_process.poll() is None:
            self.notify("A browser session is already running", severity="warning")
            return
        table = self.query_one("#table", DataTable)
        if not table.row_count:
            self.notify("No proxy selected", severity="warning")
            return
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        row = self.rows_by_key.get(key)
        if row is None:
            self.notify("No proxy selected", severity="warning")
            return
        try:
            family, self.browser_process = launch_browser_session(
                self.browser, row.key[0], row.key[1], self.browser_url
            )
        except BrowserUnavailable as error:
            self.notify(str(error), severity="error", timeout=8)
            return
        self.notify(f"Opening {family} through {row.connection}")
        threading.Thread(
            target=self.browser_watcher,
            args=(self.browser_process,),
            name="proxytools-browser-watcher",
            daemon=True,
        ).start()

    def browser_watcher(self, process):
        return_code = process.wait()
        try:
            self.call_from_thread(self.browser_finished, process, return_code)
        except RuntimeError:
            # The detached helper owns cleanup if the monitor has already quit.
            pass

    def browser_finished(self, process, return_code):
        if self.browser_process is process:
            self.browser_process = None
        if return_code:
            self.notify("Browser session ended with an error", severity="error")
        else:
            self.notify("Browser session closed")

    def action_quit(self):
        self.stop_event.set()
        self.engine.request_refresh()
        self.exit()

    @staticmethod
    def _milliseconds(value):
        return f"{value}ms" if value is not None else "-"

    @staticmethod
    def _timestamp(value):
        return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S") if value else "-"

    @staticmethod
    def _latency_sort_key(value):
        if value == "-":
            return float("inf")
        return int(str(value).removesuffix("ms"))

    @staticmethod
    def _state_color(state):
        return {"STABLE": "green", "PROBATION": "yellow", "DEGRADED": "red"}[state]

    @classmethod
    def _state_text(cls, state):
        return Text(state, style=cls._state_color(state))

    def _row_cells(self, row: MonitorRow):
        return (
            self._state_text(row.state),
            format_duration(row.alive_seconds),
            f"{row.checks}/{row.required_checks}",
            str(row.streak),
            f"{row.success_rate:.0%}",
            self._milliseconds(row.median_latency),
            self._milliseconds(row.p95_latency),
            f"{row.jitter}ms",
            row.country,
            ",".join(row.blockers) or "-",
            row.connection,
        )

    def filtered_rows(self, snapshot: MonitorSnapshot):
        country_filter = self.country_filter.casefold()
        return [
            row
            for row in snapshot.rows
            if (not self.stable_only or row.state == "STABLE")
            and (not country_filter or country_filter in row.country.casefold())
        ]
