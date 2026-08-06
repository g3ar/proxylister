"""Reusable keyboard widgets for the Textual monitor dashboard.

The main dashboard owns monitoring state and rendering, while this module owns
the finite state/protocol pickers, searchable country picker, hotkey-forwarding
table, and small duration formatter. Keeping these interaction-only components
here makes UI behavior independently testable and keeps ``dashboard.py``
focused on translating monitor snapshots into visible rows.
"""

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option


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
        Binding("b", "monitor_browser", "Browser"),
        Binding("y", "monitor_copy", "Copy"),
        Binding("enter", "monitor_details", "Details", priority=True),
    ]

    def action_monitor_quit(self):
        self.app.action_quit()

    def action_monitor_states(self):
        self.app.action_states()

    def action_monitor_protocols(self):
        self.app.action_protocols()

    def action_monitor_country_filter(self):
        self.app.action_country_filter()

    def action_monitor_browser(self):
        self.app.action_browser()

    def action_monitor_copy(self):
        self.app.action_copy_connection()

    def action_monitor_details(self):
        self.app.action_details()


class ProxyDetailsScreen(ModalScreen[None]):
    """Full analytics for the proxy selected in the compact table."""

    DEFAULT_CSS = """
    ProxyDetailsScreen { align: center middle; background: $background 60%; }
    ProxyDetailsScreen > Vertical {
        width: 72; height: auto; max-height: 90%;
        padding: 1 2; border: round $primary; background: $surface;
    }
    ProxyDetailsScreen .hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        Binding("enter", "close", "Close"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, details):
        super().__init__()
        self.details = details

    def compose(self):
        with Vertical():
            yield Static(self.details, id="proxy-details")
            yield Label("Enter/Esc close", classes="hint")

    def action_close(self):
        self.dismiss(None)


class _ApplyingSelectionList(SelectionList):
    """Selection list whose Enter key applies the enclosing modal."""

    BINDINGS = [
        *SelectionList.BINDINGS,
        Binding("enter", "apply_filter", show=False, priority=True),
    ]

    def action_apply_filter(self):
        self.screen.action_apply()


class StateSelectionList(_ApplyingSelectionList):
    """State-selection widget kept public for UI tests and extensions."""


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
    BINDINGS = [Binding("enter", "apply", "Apply"), Binding("escape", "cancel", "Cancel")]

    def __init__(self, selected):
        super().__init__()
        self.initial = set(selected)

    def compose(self):
        with Vertical():
            yield Label("Proxy states")
            yield StateSelectionList(
                *((state, state, state in self.initial) for state in ("STABLE", "PROBATION", "DEGRADED")),
                id="state-options",
            )
            yield Label("Space toggle  •  Enter apply  •  Esc cancel", classes="hint")

    def action_apply(self):
        self.dismiss(set(self.query_one(StateSelectionList).selected))

    def action_cancel(self):
        self.dismiss(self.initial)


class ProtocolSelectionList(_ApplyingSelectionList):
    """Protocol-selection widget kept public for UI tests and extensions."""


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
    BINDINGS = [Binding("enter", "apply", "Apply"), Binding("escape", "cancel", "Cancel")]

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
