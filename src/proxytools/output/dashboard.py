"""Interactive Textual dashboard for proxy stability monitoring."""

from __future__ import annotations

import threading
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from proxytools.monitoring import MonitorEngine, MonitorRow, MonitorSnapshot
from proxytools.browser import BrowserUnavailable, launch_browser_session


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class MonitorDataTable(DataTable):
    """Data table that forwards monitor hotkeys instead of type-searching."""

    BINDINGS = [*DataTable.BINDINGS] + [
        Binding("q", "monitor_quit", "Quit"),
        Binding("s", "monitor_states", "States"),
        Binding("p", "monitor_protocols", "Protocols"),
        Binding("c", "monitor_country_filter", "Country"),
        Binding("r", "monitor_refresh", "Refresh"),
        Binding("b", "monitor_browser", "Browser"),
    ]

    def action_monitor_quit(self):
        self.app.action_quit()

    def action_monitor_states(self):
        self.app.action_states()

    def action_monitor_protocols(self):
        self.app.action_protocols()

    def action_monitor_country_filter(self):
        self.app.action_country_filter()

    def action_monitor_refresh(self):
        self.app.action_refresh()

    def action_monitor_browser(self):
        self.app.action_browser()


class StateSelectionList(SelectionList):
    """Selection list whose Enter key applies the enclosing modal."""

    BINDINGS = [
        *SelectionList.BINDINGS,
        Binding("enter", "apply_states", show=False, priority=True),
    ]

    def action_apply_states(self):
        self.screen.action_apply()


class StateFilterScreen(ModalScreen[set[str]]):
    """Keyboard-first multi-select modal for the finite proxy states."""

    DEFAULT_CSS = """
    StateFilterScreen { align: center middle; background: $background 60%; }
    StateFilterScreen > Vertical {
        width: 44; height: auto; max-height: 18;
        padding: 1 2; border: round $primary; background: $surface;
    }
    StateFilterScreen SelectionList { height: 7; margin-top: 1; }
    StateFilterScreen .hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        Binding("enter", "apply", "Apply"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, selected):
        super().__init__()
        self.initial = set(selected)

    def compose(self):
        with Vertical():
            yield Label("Proxy states")
            yield StateSelectionList(
                *( (state, state, state in self.initial) for state in ("STABLE", "PROBATION", "DEGRADED") ),
                id="state-options",
            )
            yield Label("Space toggle  •  Enter apply  •  Esc cancel", classes="hint")

    def action_apply(self):
        self.dismiss(set(self.query_one(StateSelectionList).selected))

    def action_cancel(self):
        self.dismiss(self.initial)


class ProtocolSelectionList(SelectionList):
    """Selection list whose Enter key applies the protocol filter."""

    BINDINGS = [
        *SelectionList.BINDINGS,
        Binding("enter", "apply_protocols", show=False, priority=True),
    ]

    def action_apply_protocols(self):
        self.screen.action_apply()


class ProtocolFilterScreen(ModalScreen[set[str]]):
    """Multi-select modal containing protocols present in the monitor."""

    DEFAULT_CSS = """
    ProtocolFilterScreen { align: center middle; background: $background 60%; }
    ProtocolFilterScreen > Vertical {
        width: 44; height: auto; max-height: 18;
        padding: 1 2; border: round $primary; background: $surface;
    }
    ProtocolFilterScreen SelectionList { height: 7; margin-top: 1; }
    ProtocolFilterScreen .hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        Binding("enter", "apply", "Apply"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, protocols, selected):
        super().__init__()
        self.protocols = tuple(protocols)
        self.initial = set(selected)

    def compose(self):
        with Vertical():
            yield Label("Proxy protocols")
            yield ProtocolSelectionList(
                *((protocol.upper(), protocol, protocol in self.initial) for protocol in self.protocols),
                id="protocol-options",
            )
            yield Label("Space toggle  •  Enter apply  •  Esc cancel", classes="hint")

    def action_apply(self):
        self.dismiss(set(self.query_one(ProtocolSelectionList).selected))

    def action_cancel(self):
        self.dismiss(self.initial)


class CountrySearchInput(Input):
    """Search input that lets arrows navigate the sibling option list."""

    BINDINGS = [
        Binding("down", "country_down", show=False, priority=True),
        Binding("up", "country_up", show=False, priority=True),
    ]

    def action_country_down(self):
        self.screen.move_highlight(1)

    def action_country_up(self):
        self.screen.move_highlight(-1)


class CountryFilterScreen(ModalScreen[str]):
    """Searchable exact-match picker built from currently known countries."""

    DEFAULT_CSS = """
    CountryFilterScreen { align: center middle; background: $background 60%; }
    CountryFilterScreen > Vertical {
        width: 58; height: 70%; max-height: 28;
        padding: 1 2; border: round $primary; background: $surface;
    }
    CountryFilterScreen Input { margin: 1 0; }
    CountryFilterScreen OptionList { height: 1fr; }
    CountryFilterScreen .hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, countries, selected):
        super().__init__()
        self.countries = tuple(countries)
        self.selected_country = selected

    def compose(self):
        with Vertical():
            yield Label("Country")
            yield CountrySearchInput(placeholder="Type to filter countries…", id="country-search")
            yield OptionList(id="country-options")
            yield Label("Type search  •  ↑/↓ select  •  Enter apply  •  Esc cancel", classes="hint")

    def on_mount(self):
        self.update_options("")
        self.query_one(CountrySearchInput).focus()

    def on_input_changed(self, event: Input.Changed):
        self.update_options(event.value)

    def on_input_submitted(self, event: Input.Submitted):
        self.apply_highlighted()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss("" if event.option.id == "__all__" else str(event.option.id))

    def update_options(self, query):
        folded = query.strip().casefold()
        values = [country for country in self.countries if not folded or folded in country.casefold()]
        options = [] if folded else [Option("All countries", id="__all__")]
        options.extend(Option(country, id=country) for country in values)
        option_list = self.query_one(OptionList)
        option_list.clear_options().add_options(options)
        option_list.highlighted = 0 if options else None

    def move_highlight(self, delta):
        option_list = self.query_one(OptionList)
        count = option_list.option_count
        if count:
            option_list.highlighted = max(0, min(count - 1, (option_list.highlighted or 0) + delta))

    def apply_highlighted(self):
        option_list = self.query_one(OptionList)
        if option_list.highlighted is not None:
            option = option_list.get_option_at_index(option_list.highlighted)
            self.dismiss("" if option.id == "__all__" else str(option.id))

    def action_cancel(self):
        self.dismiss(self.selected_country)


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
    #details {
        height: 8;
        padding: 1 2;
        border-top: solid $primary;
        background: $surface;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "states", "States"),
        Binding("p", "protocols", "Protocols"),
        Binding("c", "country_filter", "Country"),
        Binding("r", "refresh", "Refresh"),
        Binding("b", "browser", "Browser"),
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
        ("City", "city"),
        ("Exit IP", "exit_ip"),
        ("Blocked by", "blocked"),
        ("Connection", "connection"),
    )

    def __init__(
        self, engine: MonitorEngine, *, stable_only=False, autostart=True,
        browser="auto", browser_url="about:blank",
    ):
        super().__init__()
        self.engine = engine
        self.selected_states = {"STABLE"} if stable_only else {"STABLE", "PROBATION"}
        self.selected_protocols = {"http", "socks4", "socks5"}
        self.autostart = autostart
        self.country_filter = ""
        self.stop_event = threading.Event()
        self.latest_snapshot: MonitorSnapshot | None = None
        self.rows_by_key: dict[str, MonitorRow] = {}
        self.all_rows_by_key: dict[str, MonitorRow] = {}
        self.cells_by_key: dict[str, tuple] = {}
        self.completed_cycle = 0
        self.browser = browser
        self.browser_url = browser_url
        self.browser_process = None
        self.stopping = False
        self.filter_rebuilding = False
        self.filter_generation = 0
        self.filter_pending_snapshot = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Starting monitor…", id="status")
        yield MonitorDataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("Select a proxy to see stability details.", id="details")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#table", DataTable)
        table.add_columns(*self.COLUMNS)
        table.focus()
        if self.autostart:
            self.monitor_worker()

    def on_unmount(self):
        self.stop_event.set()
        self.engine.request_refresh()

    @work(thread=True, exclusive=True, group="monitor")
    def monitor_worker(self):
        try:
            self.engine.run(
                self.stop_event,
                lambda snapshot: self.call_from_thread(self.receive_snapshot, snapshot),
            )
        finally:
            try:
                self.call_from_thread(self.monitor_stopped)
            except RuntimeError:
                pass

    def monitor_stopped(self):
        if self.stopping:
            self.exit()

    def receive_snapshot(self, snapshot: MonitorSnapshot):
        self.latest_snapshot = snapshot
        if snapshot.incremental:
            self.all_rows_by_key.update({
                f"{row.key[0]}|{row.key[1]}": row for row in snapshot.changed_rows
            })
        else:
            self.all_rows_by_key = {
                f"{row.key[0]}|{row.key[1]}": row for row in snapshot.rows
            }
        if self.filter_rebuilding:
            self.filter_pending_snapshot = snapshot
            return
        # Waiting snapshots only change the countdown and elapsed wall clock.
        # Rewriting thousands of table cells once a second starves keyboard
        # events, so keep the table frozen until a proxy is checked again.
        if snapshot.phase == "waiting" and self.completed_cycle == snapshot.cycle:
            self._render_status(snapshot, len(self.rows_by_key))
            return
        if (
            snapshot.incremental
            and (0 < snapshot.checked < snapshot.total or snapshot.phase == "running")
            and snapshot.phase in {"restoring", "checking_new", "checking", "running"}
        ):
            self.render_changed_rows(snapshot)
            return
        self.render_snapshot(snapshot)

    def render_changed_rows(self, snapshot: MonitorSnapshot):
        """Apply progress updates without walking every row in the table."""
        table = self.query_one("#table", DataTable)
        for row in snapshot.changed_rows:
            key = f"{row.key[0]}|{row.key[1]}"
            if not self._row_visible(row):
                if key in self.rows_by_key:
                    table.remove_row(key)
                    self.rows_by_key.pop(key, None)
                    self.cells_by_key.pop(key, None)
                continue
            cells = self._row_cells(row)
            if key not in self.rows_by_key:
                table.add_row(*cells, key=key)
            else:
                previous_cells = self.cells_by_key.get(key, ())
                for index, ((_, column_key), value) in enumerate(zip(self.COLUMNS, cells)):
                    if index >= len(previous_cells) or value != previous_cells[index]:
                        table.update_cell(key, column_key, value, update_width=False)
            self.rows_by_key[key] = row
            self.cells_by_key[key] = cells
        self._render_status(snapshot, len(self.rows_by_key))
        if snapshot.resort:
            table.sort("state", "median", key=self._monitor_sort_key)

        if table.row_count:
            selected = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            changed = self.rows_by_key.get(selected)
            if changed is not None and changed.key in {row.key for row in snapshot.changed_rows}:
                self._render_details(changed)

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
        cycle_complete = snapshot.checked == snapshot.total and snapshot.phase in {
            "restoring", "checking_new", "checking", "waiting"
        }
        if cycle_complete:
            table.sort("state", "median", key=self._monitor_sort_key)
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
            "running": (
                f"Active {snapshot.active_checked}/{snapshot.active_total} │ "
                f"Discovery {snapshot.discovery_checked}/{snapshot.discovery_total}"
            ),
            "restoring": f"Rechecking saved proxies {snapshot.checked}/{snapshot.total}",
            "fetching": "Fetching ProxyScrape lists…",
            "checking_new": f"Checking new proxies {snapshot.checked}/{snapshot.total}",
            "checking": f"Checking {snapshot.checked}/{snapshot.total}",
            "waiting": f"Next cycle in {snapshot.next_cycle_in}s",
        }.get(snapshot.phase, snapshot.phase.title())
        state_order = ("STABLE", "PROBATION", "DEGRADED")
        selected = [state.lower() for state in state_order if state in self.selected_states]
        filters = [f"states: {'+'.join(selected) or 'none'}"]
        filters.append(f"protocols: {'+'.join(sorted(self.selected_protocols)) or 'none'}")
        if self.country_filter:
            filters.append(f"country: {self.country_filter}")
        text = (
            f"Cycle {snapshot.cycle} │ {phase} │ {snapshot.stable_count} stable │ "
            f"{snapshot.tracked_count} tracked │ {visible} visible │ {', '.join(filters)}"
        )
        status = self.query_one("#status", Static)
        status.update(text)

    def _render_details(self, row: MonitorRow):
        blocked = ", ".join(row.blockers) or "none"
        first_seen = self._timestamp(row.first_seen_at)
        last_failure = self._timestamp(row.last_failure_at)
        restored = "  [yellow]Restored from database; awaiting verification[/]" if row.restored else ""
        detail = Text.from_markup(
            f"[bold]{row.connection}[/bold]  [{self._state_color(row.state)}]{row.state}{'*' if row.restored else ''}[/]{restored}\n"
            f"Country: {row.country}    City: {row.city}    Exit IP: {row.exit_ip or '-'}    "
            f"Alive: {format_duration(row.alive_seconds)}    "
            f"Checks: {row.checks}/{row.required_checks}    Streak: {row.streak}    Success: {row.success_rate:.1%}\n"
            f"Median: {self._milliseconds(row.median_latency)}    P95: {self._milliseconds(row.p95_latency)}    "
            f"Jitter: {row.jitter}ms    Blocked by: {blocked}\n"
            f"First seen: {first_seen}    Observed uptime: {format_duration(row.total_observed_uptime)}    "
            f"Last failure: {last_failure}\n"
            f"HTTP exit: {row.http_exit_ip or '-'}    HTTPS exit: {row.exit_ip or '-'}    "
            f"Route: {'split' if row.http_exit_ip and row.exit_ip and row.http_exit_ip != row.exit_ip else 'same/unknown'}"
        )
        self.query_one("#details", Static).update(detail)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        row = self.rows_by_key.get(str(event.row_key.value))
        if row is not None:
            self._render_details(row)

    def action_states(self):
        self.push_screen(StateFilterScreen(self.selected_states), self.apply_state_filter)

    def apply_state_filter(self, selected):
        self.selected_states = set(selected)
        if self.latest_snapshot is not None:
            self.schedule_filter_rebuild()

    def action_protocols(self):
        order = {"http": 0, "socks4": 1, "socks5": 2}
        protocols = sorted(
            {row.key[0] for row in self.all_rows_by_key.values()},
            key=lambda protocol: (order.get(protocol, 99), protocol),
        )
        self.push_screen(
            ProtocolFilterScreen(protocols, self.selected_protocols),
            self.apply_protocol_filter,
        )

    def apply_protocol_filter(self, selected):
        self.selected_protocols = set(selected)
        if self.latest_snapshot is not None:
            self.schedule_filter_rebuild()

    def action_country_filter(self):
        countries = sorted(
            {row.country for row in self.all_rows_by_key.values() if row.country != "Unknown"},
            key=str.casefold,
        )
        if any(row.country == "Unknown" for row in self.all_rows_by_key.values()):
            countries.append("Unknown")
        self.push_screen(
            CountryFilterScreen(countries, self.country_filter), self.apply_country_filter
        )

    def apply_country_filter(self, country):
        self.country_filter = country
        if self.latest_snapshot is not None:
            self.schedule_filter_rebuild()

    def schedule_filter_rebuild(self):
        """Rebuild a large filtered table in small event-loop-friendly chunks."""
        self.filter_generation += 1
        generation = self.filter_generation
        self.filter_rebuilding = True
        self.filter_pending_snapshot = None
        self.query_one("#status", Static).update("Applying table filter…")
        self.call_after_refresh(self._begin_filter_rebuild, generation)

    def _begin_filter_rebuild(self, generation):
        if generation != self.filter_generation or self.latest_snapshot is None:
            return
        table = self.query_one("#table", DataTable)
        selected_key = None
        if table.row_count:
            selected_key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        rows = [row for row in self.all_rows_by_key.values() if self._row_visible(row)]
        table.clear()
        self.rows_by_key = {}
        self.cells_by_key = {}
        self._add_filter_chunk(generation, rows, 0, selected_key)

    def _add_filter_chunk(self, generation, rows, offset, selected_key):
        if generation != self.filter_generation:
            return
        table = self.query_one("#table", DataTable)
        end = min(offset + 200, len(rows))
        for row in rows[offset:end]:
            key = f"{row.key[0]}|{row.key[1]}"
            cells = self._row_cells(row)
            table.add_row(*cells, key=key)
            self.rows_by_key[key] = row
            self.cells_by_key[key] = cells
        if end < len(rows):
            self.set_timer(0.01, lambda: self._add_filter_chunk(generation, rows, end, selected_key))
            return

        table.sort("state", "median", key=self._monitor_sort_key)
        if selected_key in self.rows_by_key:
            table.move_cursor(row=table.get_row_index(selected_key), animate=False)
        self.filter_rebuilding = False
        snapshot = self.filter_pending_snapshot or self.latest_snapshot
        self.filter_pending_snapshot = None
        self._render_status(snapshot, len(self.rows_by_key))
        if table.row_count:
            key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            self._render_details(self.rows_by_key[key])
        else:
            self.query_one("#details", Static).update("No proxies match the current filter.")

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
        if self.stopping:
            return
        self.stopping = True
        self.stop_event.set()
        self.engine.request_refresh()
        self.query_one("#status", Static).update(
            "Stopping… waiting for active network requests to finish or reach their timeout."
        )
        self.notify("Stopping monitor…", timeout=3)
        if not self.autostart:
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

    @classmethod
    def _monitor_sort_key(cls, values):
        state, latency = values
        state_name = state.plain if isinstance(state, Text) else str(state)
        state_name = state_name.removesuffix("*")
        state_order = {"STABLE": 0, "PROBATION": 1, "DEGRADED": 2}
        return state_order.get(state_name, 99), cls._latency_sort_key(latency)

    @staticmethod
    def _state_color(state):
        return {"STABLE": "green", "PROBATION": "yellow", "DEGRADED": "red"}[state]

    @classmethod
    def _state_text(cls, state):
        return Text(state, style=cls._state_color(state.removesuffix("*")))

    def _row_cells(self, row: MonitorRow):
        return (
            self._state_text(f"{row.state}{'*' if row.restored else ''}"),
            format_duration(row.alive_seconds),
            f"{row.checks}/{row.required_checks}",
            str(row.streak),
            f"{row.success_rate:.0%}",
            self._milliseconds(row.median_latency),
            self._milliseconds(row.p95_latency),
            f"{row.jitter}ms",
            row.country,
            row.city,
            row.exit_ip or "-",
            ",".join(row.blockers) or "-",
            row.connection,
        )

    def filtered_rows(self, snapshot: MonitorSnapshot):
        return [row for row in snapshot.rows if self._row_visible(row)]

    def _row_visible(self, row):
        country_filter = self.country_filter.casefold()
        return (
            row.state in self.selected_states
            and row.key[0] in self.selected_protocols
            and (not country_filter or country_filter == row.country.casefold())
        )
