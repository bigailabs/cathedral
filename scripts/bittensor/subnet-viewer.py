#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "bittensor>=9.0.0",
#   "textual>=1.0.0",
#   "click>=8.1.0",
# ]
# [tool.uv]
# prerelease = "allow"
# ///

"""
BitTensor Subnet Viewer TUI

Interactive terminal UI for inspecting BitTensor subnet configuration,
hyperparameters, neuron metagraph, and emission data.

Usage:
    ./subnet-viewer.py                              # List all subnets (finney)
    ./subnet-viewer.py --netuid 1                   # View subnet 1 details
    ./subnet-viewer.py --network test --netuid 1    # Testnet subnet 1
    ./subnet-viewer.py --network ws://custom:9944   # Custom node
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

import click

# Parse CLI args with click BEFORE importing bittensor, which hijacks argparse.
# We stash parsed values and proceed after.
_cli_network: str = "finney"
_cli_netuid: int | None = None


@click.command()
@click.option("--network", default="finney", help="Network: finney/test/local/custom URL")
@click.option("--netuid", default=None, type=int, help="Subnet UID (omit for list view)")
def _parse_cli(network: str, netuid: int | None) -> None:
    global _cli_network, _cli_netuid
    _cli_network = network
    _cli_netuid = netuid


_result = _parse_cli(standalone_mode=False)
if isinstance(_result, int):
    # --help was invoked (click returns 0), exit cleanly
    sys.exit(_result)

# Now safe to import bittensor (it will see an empty sys.argv)
_orig_argv = sys.argv
sys.argv = sys.argv[:1]

import bittensor  # noqa: E402

sys.argv = _orig_argv

logging.getLogger("bittensor").setLevel(logging.WARNING)

from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.screen import Screen  # noqa: E402
from textual.widgets import (  # noqa: E402
    Collapsible,
    DataTable,
    Footer,
    Header,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)


def truncate_key(key: str) -> str:
    """Truncate SS58 key for display."""
    if not key or len(key) < 20:
        return key or "N/A"
    return f"{key[:8]}...{key[-8:]}"


def fmt_val(value, fallback: str = "N/A") -> str:
    """Format a value for display, handling None and Balance types."""
    if value is None:
        return fallback
    try:
        return str(value)
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Subnet List Screen
# ---------------------------------------------------------------------------


class SubnetListScreen(Screen):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "select_subnet", "View Subnet"),
    ]

    CSS = """
    #loading { height: auto; }
    .hidden { display: none; }
    #subnet-table { height: 1fr; }
    """

    def __init__(self, network: str) -> None:
        super().__init__()
        self.network = network

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="loading")
        yield DataTable(id="subnet-table", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#subnet-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "NetUID", "Name", "Owner", "Tempo", "Neurons", "Max UIDs", "Emission"
        )
        self.load_subnets()

    @work(thread=True)
    def load_subnets(self) -> None:
        try:
            sub = bittensor.Subtensor(network=self.network)
            subnets = sub.get_all_subnets_info()
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))
            return

        self.app.call_from_thread(self._populate_table, subnets)

    def _show_error(self, msg: str) -> None:
        self.query_one("#loading").add_class("hidden")
        table = self.query_one("#subnet-table", DataTable)
        table.remove_class("hidden")
        self.notify(f"Error: {msg}", severity="error", timeout=10)

    def _populate_table(self, subnets) -> None:
        self.query_one("#loading").add_class("hidden")
        table = self.query_one("#subnet-table", DataTable)
        table.remove_class("hidden")
        table.clear()

        for sn in subnets:
            netuid = getattr(sn, "netuid", "?")
            name = getattr(sn, "subnet_name", "") or getattr(sn, "name", "") or ""
            owner = truncate_key(getattr(sn, "owner_ss58", "") or "")
            tempo = fmt_val(getattr(sn, "tempo", None))
            neurons = fmt_val(getattr(sn, "subnetwork_n", None))
            max_uids = fmt_val(getattr(sn, "max_n", None))
            emission = fmt_val(getattr(sn, "emission_value", None))
            table.add_row(str(netuid), name, owner, tempo, neurons, max_uids, emission, key=str(netuid))

    def action_refresh(self) -> None:
        self.query_one("#loading").remove_class("hidden")
        self.query_one("#subnet-table", DataTable).add_class("hidden")
        self.load_subnets()

    def action_select_subnet(self) -> None:
        table = self.query_one("#subnet-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        netuid = int(row_key.value)
        self.app.push_screen(SubnetDetailScreen(self.network, netuid))

    def action_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# Subnet Detail Screen
# ---------------------------------------------------------------------------


@dataclass
class SubnetData:
    """Container for fetched subnet data."""
    metagraph: object
    hyperparams: object


class SubnetDetailScreen(Screen):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("b", "go_back", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    CSS = """
    #loading { height: auto; }
    .hidden { display: none; }
    #detail-content { height: 1fr; }
    .section-label { color: $text-muted; margin: 1 0 0 0; }
    .field-row { margin: 0 0 0 2; }
    .field-name { color: $accent; min-width: 30; }
    .field-value { color: $text; }
    #metagraph-table { height: 1fr; }
    """

    def __init__(self, network: str, netuid: int) -> None:
        super().__init__()
        self.network = network
        self.netuid = netuid
        self.subnet_data: SubnetData | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="loading")
        with TabbedContent(id="detail-content", classes="hidden"):
            with TabPane("Identity", id="tab-identity"):
                yield VerticalScroll(id="identity-content")
            with TabPane("Hyperparameters", id="tab-hparams"):
                yield VerticalScroll(id="hparams-content")
            with TabPane("Metagraph", id="tab-metagraph"):
                yield DataTable(id="metagraph-table")
            with TabPane("Emissions", id="tab-emissions"):
                yield VerticalScroll(id="emissions-content")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"Subnet {self.netuid}"
        table = self.query_one("#metagraph-table", DataTable)
        table.cursor_type = "row"
        self.load_data()

    @work(thread=True)
    def load_data(self) -> None:
        try:
            sub = bittensor.Subtensor(network=self.network)
            metagraph = sub.metagraph(netuid=self.netuid, lite=True)
            hyperparams = sub.get_subnet_hyperparameters(netuid=self.netuid)
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))
            return

        data = SubnetData(metagraph=metagraph, hyperparams=hyperparams)
        self.app.call_from_thread(self._render_all, data)

    def _show_error(self, msg: str) -> None:
        self.query_one("#loading").add_class("hidden")
        self.query_one("#detail-content").remove_class("hidden")
        self.notify(f"Error: {msg}", severity="error", timeout=10)

    def _render_all(self, data: SubnetData) -> None:
        self.subnet_data = data
        self.query_one("#loading").add_class("hidden")
        self.query_one("#detail-content").remove_class("hidden")

        mg = data.metagraph
        name = getattr(mg, "name", None) or getattr(mg, "subnet_name", None) or f"Subnet {self.netuid}"
        self.sub_title = f"{name} (netuid {self.netuid})"

        self._render_identity(mg)
        self._render_hyperparams(mg, data.hyperparams)
        self._render_metagraph(mg)
        self._render_emissions(mg)

    # --- Identity Tab ---

    def _render_identity(self, mg) -> None:
        container = self.query_one("#identity-content")
        container.remove_children()

        identity = getattr(mg, "identity", None)  # dict or None

        fields = [
            ("Subnet Name", getattr(mg, "name", None)),
            ("Symbol", getattr(mg, "symbol", None)),
            ("Owner Coldkey", getattr(mg, "owner_coldkey", None)),
            ("Owner Hotkey", getattr(mg, "owner_hotkey", None)),
            ("Network Registered At", getattr(mg, "network_registered_at", None)),
        ]

        if identity and isinstance(identity, dict):
            fields.extend([
                ("Description", identity.get("description")),
                ("GitHub Repo", identity.get("github_repo")),
                ("Subnet URL", identity.get("subnet_url")),
                ("Discord", identity.get("discord")),
                ("Logo URL", identity.get("logo_url")),
                ("Contact", identity.get("subnet_contact")),
                ("Additional", identity.get("additional")),
            ])
        else:
            fields.append(("Identity", "Not set on-chain"))

        for label, value in fields:
            display_val = fmt_val(value, "Not set")
            container.mount(Static(f"  [bold]{label}:[/bold]  {display_val}"))

    # --- Hyperparameters Tab ---

    def _render_hyperparams(self, mg, hp) -> None:
        container = self.query_one("#hparams-content")
        container.remove_children()

        hparams = getattr(mg, "hparams", None)

        def _get(obj, attr, fallback="N/A"):
            if obj is None:
                return fallback
            val = getattr(obj, attr, None)
            if val is None:
                return fallback
            return str(val)

        # hparams = mg.hparams (human-readable values)
        # hp = SubnetHyperparameters (supplemental fields marked with *)
        sections = {
            "Consensus / Weights": [
                ("min_allowed_weights", hparams),
                ("max_weights_limit", hparams),
                ("weights_version", hparams),
                ("weights_rate_limit", hparams),
                ("alpha_high", hparams),
                ("alpha_low", hparams),
                ("alpha_sigmoid_steepness", hp),
                ("liquid_alpha_enabled", hparams),
                ("commit_reveal_weights_enabled", hparams),
                ("commit_reveal_period", hparams),
                ("rho", hparams),
                ("kappa", hparams),
            ],
            "Registration": [
                ("registration_allowed", hparams),
                ("pow_registration_allowed", hparams),
                ("immunity_period", hparams),
                ("burn", hparams),
                ("difficulty", hparams),
                ("min_difficulty", hparams),
                ("max_difficulty", hparams),
                ("min_burn", hparams),
                ("max_burn", hparams),
                ("adjustment_alpha", hparams),
                ("adjustment_interval", hparams),
                ("target_regs_per_interval", hparams),
                ("max_regs_per_block", hparams),
                ("serving_rate_limit", hparams),
            ],
            "Neuron / Validator": [
                ("num_uids", mg),
                ("max_uids", mg),
                ("max_validators", hparams),
                ("activity_cutoff", hparams),
            ],
            "Bonds": [
                ("bonds_moving_avg", hparams),
                ("bonds_reset_enabled", hp),
                ("liquid_alpha_enabled", hparams),
            ],
            "State": [
                ("subnet_is_active", hp),
                ("transfers_enabled", hp),
                ("user_liquidity_enabled", hp),
                ("yuma_version", hp),
            ],
        }

        for section_name, params in sections.items():
            lines = []
            for attr, source in params:
                val = _get(source, attr)
                lines.append(f"  {attr}: {val}")

            content = "\n".join(lines)
            container.mount(
                Collapsible(
                    Static(content),
                    title=section_name,
                    collapsed=False,
                )
            )

    # --- Metagraph Tab ---

    def _render_metagraph(self, mg) -> None:
        table = self.query_one("#metagraph-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "UID", "Hotkey", "Coldkey", "Total Stake", "Alpha Stake",
            "TAO Stake", "Trust", "Consensus", "Incentive", "Dividends",
            "Emission", "Active", "Val.Permit", "Axon",
        )

        n = getattr(mg, "n", 0) or 0
        if n == 0:
            n = len(getattr(mg, "uids", []))

        for i in range(n):
            uid = self._neuron_val(mg, "uids", i, str(i))
            hotkey = truncate_key(self._neuron_val(mg, "hotkeys", i, ""))
            coldkey = truncate_key(self._neuron_val(mg, "coldkeys", i, ""))
            total_stake = self._neuron_num(mg, "total_stake", i)
            alpha_stake = self._neuron_num(mg, "alpha_stake", i)
            tao_stake = self._neuron_num(mg, "tao_stake", i)
            trust = self._neuron_num(mg, "trust", i)
            consensus = self._neuron_num(mg, "consensus", i)
            incentive = self._neuron_num(mg, "incentive", i)
            dividends = self._neuron_num(mg, "dividends", i)
            emission = self._neuron_num(mg, "emission", i)
            active = self._neuron_val(mg, "active", i, "?")
            val_permit = self._neuron_val(mg, "validator_permit", i, "?")

            axon_ip = ""
            axons = getattr(mg, "axons", None)
            if axons is not None and i < len(axons):
                axon = axons[i]
                ip = getattr(axon, "ip", "") or ""
                port = getattr(axon, "port", "") or ""
                if ip and ip != "0.0.0.0":
                    axon_ip = f"{ip}:{port}"

            table.add_row(
                str(uid), hotkey, coldkey, total_stake, alpha_stake,
                tao_stake, trust, consensus, incentive, dividends,
                emission, str(active), str(val_permit), axon_ip,
            )

    def _neuron_val(self, mg, attr: str, idx: int, fallback: str = "") -> str:
        arr = getattr(mg, attr, None)
        if arr is None:
            return fallback
        try:
            val = arr[idx]
            if hasattr(val, "item"):
                return str(val.item())
            return str(val)
        except (IndexError, TypeError):
            return fallback

    def _neuron_num(self, mg, attr: str, idx: int) -> str:
        arr = getattr(mg, attr, None)
        if arr is None:
            return "N/A"
        try:
            val = arr[idx]
            if hasattr(val, "item"):
                val = val.item()
            fval = float(val)
            if fval == int(fval) and abs(fval) < 1e12:
                return str(int(fval))
            return f"{fval:.6f}"
        except (IndexError, TypeError, ValueError):
            return "N/A"

    # --- Emissions Tab ---

    def _render_emissions(self, mg) -> None:
        container = self.query_one("#emissions-content")
        container.remove_children()

        emissions = getattr(mg, "emissions", None)
        pool = getattr(mg, "pool", None)

        emission_fields = [
            ("subnet_emission", emissions),
            ("alpha_in_emission", emissions),
            ("alpha_out_emission", emissions),
            ("tao_in_emission", emissions),
            ("pending_alpha_emission", emissions),
            ("pending_root_emission", emissions),
        ]

        pool_fields = [
            ("alpha_out", pool),
            ("alpha_in", pool),
            ("tao_in", pool),
            ("subnet_volume", pool),
            ("moving_price", pool),
        ]

        container.mount(Static("  [bold underline]Emissions[/bold underline]"))
        for field, source in emission_fields:
            val = getattr(source, field, None) if source else None
            container.mount(Static(f"    [bold]{field}:[/bold]  {fmt_val(val)}"))

        container.mount(Static(""))
        container.mount(Static("  [bold underline]Pool / Reserve[/bold underline]"))
        for field, source in pool_fields:
            val = getattr(source, field, None) if source else None
            container.mount(Static(f"    [bold]{field}:[/bold]  {fmt_val(val)}"))

    # --- Actions ---

    def action_refresh(self) -> None:
        self.query_one("#loading").remove_class("hidden")
        self.query_one("#detail-content").add_class("hidden")
        self.load_data()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class SubnetViewerApp(App):
    TITLE = "BitTensor Subnet Viewer"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, network: str = "finney", initial_netuid: int | None = None) -> None:
        super().__init__()
        self.network = network
        self.initial_netuid = initial_netuid

    def on_mount(self) -> None:
        if self.initial_netuid is not None:
            self.push_screen(SubnetDetailScreen(self.network, self.initial_netuid))
        else:
            self.push_screen(SubnetListScreen(self.network))


def main() -> None:
    app = SubnetViewerApp(network=_cli_network, initial_netuid=_cli_netuid)
    app.run()


if __name__ == "__main__":
    main()
