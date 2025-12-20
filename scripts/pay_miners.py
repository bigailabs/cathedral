#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "bittensor>=10.0.0",
#   "bittensor-wallet>=4.0.0",
#   "click>=8.1.0",
#   "grpcio>=1.60.0",
#   "grpcio-tools>=1.60.0",
#   "requests>=2.31.0",
#   "rich>=13.0.0",
# ]
# [tool.uv]
# prerelease = "allow"
# ///

"""
Basilica Miner Payment Script

Pay miners based on miner_revenue_summary data. Automatically detects the next
unpaid period from the database to ensure payments align exactly with
pre-computed period boundaries.

Usage:
    ./pay_miners.py --token-type tao
    ./pay_miners.py --token-type alpha
    ./pay_miners.py --dry-run --token-type tao
    ./pay_miners.py --auto-approve --token-type tao
    ./pay_miners.py --token-type tao --force  # When multiple unpaid periods exist

Environment Variables:
    BILLING_GRPC_ENDPOINT: gRPC endpoint for billing service (default: localhost:50051)
    BITTENSOR_NETWORK: Bittensor network (default: finney)
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

import click
import requests
from async_substrate_interface.async_substrate import ResultHandler
from rich.console import Console
from rich.table import Table

# Constants
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"
MARKUP_FACTOR = Decimal("1.10")  # 10% markup to remove (base_price = total_revenue / 1.10)
DEFAULT_NETUID = 39

# Derive proto path from script location
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_PROTO_PATH = REPO_ROOT / "crates" / "basilica-protocol" / "proto"


@dataclass
class MinerRevenueSummary:
    """Represents a miner revenue summary record from the billing service."""

    id: str
    node_id: str
    validator_id: str
    miner_uid: int
    miner_hotkey: str
    period_start: str
    period_end: str
    total_rentals: int
    completed_rentals: int
    failed_rentals: int
    total_revenue: Decimal
    total_hours: Decimal
    avg_hourly_rate: Optional[Decimal]
    avg_rental_duration_hours: Optional[Decimal]
    computed_at: str
    computation_version: int
    created_at: str
    paid: bool
    tx_hash: str


@dataclass
class PaymentInfo:
    """Payment information for a single miner revenue summary."""

    summary: MinerRevenueSummary
    base_price_usd: Decimal  # After removing markup
    tao_amount: Decimal  # Original TAO amount (used for staking)
    amount_tokens: Decimal  # Alpha amount (for display) or TAO if token_type=tao
    tx_hash: Optional[str] = None


@dataclass
class PaymentSummary:
    """Summary of all payments to be made."""

    payments: list[PaymentInfo] = field(default_factory=list)
    total_usd: Decimal = Decimal("0")
    total_tao: Decimal = Decimal("0")  # Total TAO to be staked
    total_tokens: Decimal = Decimal("0")  # Total tokens (alpha or TAO depending on token_type)
    tao_price_usd: Decimal = Decimal("0")
    alpha_price_usd: Optional[Decimal] = None  # Alpha/USD price (derived from TAO/USD and Alpha/TAO)
    token_type: str = "tao"
    netuid: int = DEFAULT_NETUID


class BillingClient:
    """Client for interacting with the billing gRPC service via grpcurl."""

    def __init__(self, endpoint: str, proto_path: Path):
        self.endpoint = endpoint
        self.proto_path = proto_path

    def get_all_unpaid_summaries(self) -> list[MinerRevenueSummary]:
        """Fetch ALL unpaid miner revenue summaries (no date filter) using pagination."""
        all_summaries: list[MinerRevenueSummary] = []
        offset = 0
        limit = 1000  # Max allowed by the service

        while True:
            request = {"limit": limit, "offset": offset}

            result = subprocess.run(
                [
                    "grpcurl",
                    "-import-path",
                    str(self.proto_path),
                    "-proto",
                    "billing.proto",
                    "-d",
                    json.dumps(request),
                    self.endpoint,
                    "basilica.billing.v1.BillingService/GetUnpaidMinerRevenueSummary",
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise RuntimeError(f"gRPC call failed: {result.stderr}")

            if not result.stdout.strip():
                break

            data = json.loads(result.stdout)
            batch = data.get("summaries", [])

            if not batch:
                break

            for s in batch:
                all_summaries.append(
                    MinerRevenueSummary(
                        id=s.get("id", ""),
                        node_id=s.get("nodeId", ""),
                        validator_id=s.get("validatorId", ""),
                        miner_uid=int(s.get("minerUid", 0)),
                        miner_hotkey=s.get("minerHotkey", ""),
                        period_start=s.get("periodStart", ""),
                        period_end=s.get("periodEnd", ""),
                        total_rentals=int(s.get("totalRentals", 0)),
                        completed_rentals=int(s.get("completedRentals", 0)),
                        failed_rentals=int(s.get("failedRentals", 0)),
                        total_revenue=Decimal(s.get("totalRevenue", "0")),
                        total_hours=Decimal(s.get("totalHours", "0")),
                        avg_hourly_rate=(
                            Decimal(s["avgHourlyRate"]) if s.get("avgHourlyRate") else None
                        ),
                        avg_rental_duration_hours=(
                            Decimal(s["avgRentalDurationHours"])
                            if s.get("avgRentalDurationHours")
                            else None
                        ),
                        computed_at=s.get("computedAt", ""),
                        computation_version=int(s.get("computationVersion", 0)),
                        created_at=s.get("createdAt", ""),
                        paid=s.get("paid", False),
                        tx_hash=s.get("txHash", ""),
                    )
                )

            # If we got fewer than limit, we've reached the end
            if len(batch) < limit:
                break

            offset += limit

        return all_summaries

    def mark_as_paid(self, summary_id: str, tx_hash: str) -> bool:
        """Mark a miner revenue summary as paid."""
        request = {
            "id": summary_id,
            "tx_hash": tx_hash,
        }

        result = subprocess.run(
            [
                "grpcurl",
                "-import-path",
                str(self.proto_path),
                "-proto",
                "billing.proto",
                "-d",
                json.dumps(request),
                self.endpoint,
                "basilica.billing.v1.BillingService/MarkMinerRevenuePaid",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(f"gRPC call failed: {result.stderr}")

        data = json.loads(result.stdout)
        return data.get("success", False)


class MinerPaymentProcessor:
    """Process miner payments from revenue summaries."""

    def __init__(
        self,
        billing_endpoint: str,
        proto_path: Path,
        network: str,
        wallet_name: str,
        wallet_path: str,
        token_type: str,
        netuid: int,
        dry_run: bool,
    ):
        self.billing_client = BillingClient(billing_endpoint, proto_path)
        self.network = network
        self.wallet_name = wallet_name
        self.wallet_path = wallet_path
        self.token_type = token_type
        self.netuid = netuid
        self.dry_run = dry_run
        self.console = Console()

        # Will be initialized when needed
        self.wallet = None
        self.subtensor = None

    def fetch_tao_price(self) -> Decimal:
        """Fetch current TAO/USD price from CoinGecko."""
        try:
            response = requests.get(COINGECKO_API_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            return Decimal(str(data["bittensor"]["usd"]))
        except Exception as e:
            raise RuntimeError(f"Failed to fetch TAO price: {e}")

    def calculate_payments(
        self, summaries: list[MinerRevenueSummary], tao_price: Decimal
    ) -> PaymentSummary:
        """Calculate payment amounts for each summary."""
        payment_summary = PaymentSummary(
            tao_price_usd=tao_price,
            token_type=self.token_type,
            netuid=self.netuid,
        )

        alpha_per_tao: Optional[Decimal] = None

        for summary in summaries:
            # Calculate base price by removing 10% markup
            # total_revenue = base + 10% markup = base * 1.10
            # base = total_revenue / 1.10
            base_price_usd = summary.total_revenue / MARKUP_FACTOR

            # Convert USD to TAO
            tao_amount = base_price_usd / tao_price

            # If alpha, convert TAO to alpha using subnet info
            if self.token_type == "alpha":
                amount_tokens, rate = self._tao_to_alpha(tao_amount)
                if rate is not None and alpha_per_tao is None:
                    alpha_per_tao = rate
            else:
                amount_tokens = tao_amount

            payment_info = PaymentInfo(
                summary=summary,
                base_price_usd=base_price_usd,
                tao_amount=tao_amount,
                amount_tokens=amount_tokens,
            )

            payment_summary.payments.append(payment_info)
            payment_summary.total_usd += base_price_usd
            payment_summary.total_tao += tao_amount
            payment_summary.total_tokens += amount_tokens

        # Calculate Alpha/USD price if we have the Alpha/TAO rate
        # Alpha/USD = TAO/USD / Alpha/TAO (since more alpha per TAO means alpha is cheaper)
        if alpha_per_tao is not None and alpha_per_tao > 0:
            payment_summary.alpha_price_usd = tao_price / alpha_per_tao

        return payment_summary

    def _tao_to_alpha(self, tao_amount: Decimal) -> tuple[Decimal, Optional[Decimal]]:
        """Convert TAO to Alpha tokens using subnet dynamic info.

        Returns:
            Tuple of (alpha_amount, alpha_per_tao_rate). Rate is None if conversion failed.
        """
        try:
            import bittensor as bt

            if self.subtensor is None:
                self.subtensor = bt.Subtensor(network=self.network)

            # Get subnet info for alpha conversion rate
            subnet = self.subtensor.subnet(netuid=self.netuid)
            if subnet is None:
                self.console.print(
                    f"[yellow]Warning: Could not get subnet {self.netuid} info, using TAO amount[/yellow]"
                )
                return tao_amount, None

            # Convert TAO to Alpha using subnet's exchange rate
            tao_balance = bt.Balance.from_tao(float(tao_amount))
            alpha_amount = subnet.tao_to_alpha(tao_balance)
            alpha_decimal = Decimal(str(alpha_amount.tao))

            # Calculate Alpha/TAO rate
            alpha_per_tao = alpha_decimal / tao_amount if tao_amount > 0 else None

            return alpha_decimal, alpha_per_tao
        except Exception as e:
            self.console.print(
                f"[yellow]Warning: Failed to convert TAO to Alpha: {e}[/yellow]"
            )
            return tao_amount, None

    def display_summary(self, summary: PaymentSummary) -> None:
        """Display payment summary for user confirmation."""
        table = Table(title="Miner Payment Summary")
        table.add_column("ID", style="dim")
        table.add_column("Miner Hotkey", style="cyan")
        table.add_column("Revenue (USD)", justify="right")
        table.add_column("Base Price (USD)", justify="right")
        table.add_column(f"Amount ({summary.token_type.upper()})", justify="right")
        table.add_column("Token Value (USD)", justify="right", style="green")

        for payment in sorted(
            summary.payments, key=lambda p: p.base_price_usd, reverse=True
        ):
            # Calculate USD value based on token type
            if summary.token_type == "alpha" and summary.alpha_price_usd is not None:
                usd_value = payment.amount_tokens * summary.alpha_price_usd
            else:
                # For TAO, use the TAO price
                usd_value = payment.amount_tokens * summary.tao_price_usd

            row = [
                payment.summary.id[:8] + "...",
                payment.summary.miner_hotkey[:16] + "...",
                f"${payment.summary.total_revenue:.4f}",
                f"${payment.base_price_usd:.4f}",
                f"{payment.amount_tokens:.6f}",
                f"${usd_value:.4f}",
            ]

            table.add_row(*row)

        self.console.print(table)
        self.console.print(f"\n[bold]Total Payments:[/bold]")
        self.console.print(f"  Records: {len(summary.payments)}")
        self.console.print(f"  Total USD (base): ${summary.total_usd:.4f}")
        self.console.print(
            f"  Total {summary.token_type.upper()}: {summary.total_tokens:.6f}"
        )

        # Calculate total token value in USD
        if summary.token_type == "alpha" and summary.alpha_price_usd is not None:
            total_token_value = summary.total_tokens * summary.alpha_price_usd
        else:
            total_token_value = summary.total_tokens * summary.tao_price_usd
        self.console.print(f"  Total Token Value (USD): ${total_token_value:.4f}")

        # Show pricing information
        self.console.print(f"\n[bold]Pricing:[/bold]")
        self.console.print(f"  TAO/USD: ${summary.tao_price_usd:.2f}")

        if summary.token_type == "alpha":
            if summary.alpha_price_usd is not None:
                self.console.print(f"  Alpha/USD: ${summary.alpha_price_usd:.6f}")
            else:
                self.console.print("  Alpha/USD: [yellow]Not available (dry run)[/yellow]")

        self.console.print(f"  Subnet: {summary.netuid}")

    def execute_payments(self, summary: PaymentSummary) -> int:
        """Execute blockchain transfers for each payment."""
        if self.dry_run:
            self.console.print(
                "[yellow]DRY RUN - No actual transfers will be made[/yellow]"
            )
            return 0

        import bittensor as bt
        from bittensor_wallet import Wallet

        # Initialize wallet
        self.console.print(f"Loading wallet '{self.wallet_name}' from {self.wallet_path}...")
        self.wallet = Wallet(name=self.wallet_name, path=self.wallet_path)
        self.wallet.unlock_coldkey()

        # Connect to subtensor
        self.console.print(f"Connecting to {self.network}...")
        self.subtensor = bt.Subtensor(network=self.network)

        successful_payments = 0

        for payment in summary.payments:
            try:
                self.console.print(
                    f"Transferring {payment.amount_tokens:.6f} {summary.token_type.upper()} "
                    f"to {payment.summary.miner_hotkey[:16]}..."
                )

                if summary.token_type == "tao":
                    # TAO transfer
                    response = self.subtensor.transfer(
                        wallet=self.wallet,
                        destination_ss58=payment.summary.miner_hotkey,
                        amount=bt.Balance.from_tao(float(payment.tao_amount)),
                        wait_for_inclusion=True,
                        wait_for_finalization=True,
                    )
                else:
                    # Alpha stake - stake TAO to the miner's hotkey on the subnet
                    # add_stake takes TAO amount and converts to alpha internally
                    response = self.subtensor.add_stake(
                        wallet=self.wallet,
                        hotkey_ss58=payment.summary.miner_hotkey,
                        netuid=summary.netuid,
                        amount=bt.Balance.from_tao(float(payment.tao_amount)),
                        wait_for_inclusion=True,
                        wait_for_finalization=True,
                    )

                if response.success:
                    if response.extrinsic_receipt is None:
                        raise RuntimeError("Success but no extrinsic_receipt")
                    tx_hash = response.extrinsic_receipt.extrinsic_hash
                    payment.tx_hash = tx_hash
                    self.console.print(f"  [green]Success: {tx_hash}[/green]")

                    # Mark as paid in billing service
                    try:
                        self.billing_client.mark_as_paid(payment.summary.id, tx_hash)
                        self.console.print(f"  [green]Marked as paid in billing[/green]")
                        successful_payments += 1
                    except Exception as e:
                        self.console.print(
                            f"  [yellow]Warning: Failed to mark as paid: {e}[/yellow]"
                        )
                else:
                    self.console.print(f"  [red]Transfer failed: {response.message}[/red]")

            except Exception as e:
                self.console.print(f"  [red]Error: {e}[/red]")

        return successful_payments


@click.command()
@click.option(
    "--token-type",
    type=click.Choice(["tao", "alpha"]),
    required=True,
    help="Token type for payment",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force payment of earliest period when multiple unpaid periods exist",
)
@click.option(
    "--netuid",
    type=int,
    default=DEFAULT_NETUID,
    help=f"Subnet UID (default: {DEFAULT_NETUID})",
)
@click.option(
    "--wallet-name",
    default="default",
    help="Wallet name",
)
@click.option(
    "--wallet-path",
    default="~/.bittensor/wallets",
    help="Wallet path",
)
@click.option(
    "--network",
    default="finney",
    help="Bittensor network (finney, test, local)",
)
@click.option(
    "--billing-endpoint",
    default="localhost:50051",
    envvar="BILLING_GRPC_ENDPOINT",
    help="Billing gRPC endpoint",
)
@click.option(
    "--proto-path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=DEFAULT_PROTO_PATH,
    help=f"Path to proto files (default: {DEFAULT_PROTO_PATH})",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show summary without making payments",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    help="Skip confirmation prompt (for cron jobs)",
)
def main(
    token_type: str,
    force: bool,
    netuid: int,
    wallet_name: str,
    wallet_path: str,
    network: str,
    billing_endpoint: str,
    proto_path: Path,
    dry_run: bool,
    auto_approve: bool,
):
    """Pay miners based on miner_revenue_summary data."""
    console = Console()

    console.print("[bold]Basilica Miner Payment Script[/bold]")
    console.print(f"Token: {token_type.upper()}")
    console.print(f"Subnet: {netuid}")
    console.print(f"Network: {network}")
    console.print(f"Endpoint: {billing_endpoint}")
    console.print("")

    # Create billing client for period detection
    billing_client = BillingClient(billing_endpoint, proto_path)

    # Fetch all unpaid summaries to detect periods
    console.print("Fetching unpaid revenue summaries...")
    try:
        all_summaries = billing_client.get_all_unpaid_summaries()
    except Exception as e:
        console.print(f"[red]Error fetching summaries: {e}[/red]")
        sys.exit(1)

    if not all_summaries:
        console.print("[yellow]No unpaid revenue summaries found.[/yellow]")
        sys.exit(0)

    # Extract distinct periods and group summaries
    periods: dict[tuple[str, str], list[MinerRevenueSummary]] = {}
    for s in all_summaries:
        key = (s.period_start, s.period_end)
        if key not in periods:
            periods[key] = []
        periods[key].append(s)

    sorted_periods = sorted(periods.keys(), key=lambda p: p[0])  # Sort by start date

    # Display found periods
    console.print(f"\n[bold]Found {len(sorted_periods)} unpaid period(s):[/bold]")
    for start, end in sorted_periods:
        period_summaries = periods[(start, end)]
        revenue = sum(s.total_revenue for s in period_summaries)
        console.print(f"  {start} to {end}: {len(period_summaries)} miners, ${revenue:.2f} revenue")

    # Fail if multiple periods and not forced
    if len(sorted_periods) > 1 and not force:
        console.print("\n[red]Error: Multiple unpaid periods found.[/red]")
        console.print("Use --force to pay the earliest period.")
        sys.exit(1)

    # Select earliest period
    period_start, period_end = sorted_periods[0]
    summaries = periods[(period_start, period_end)]
    console.print(f"\n[green]Selected period: {period_start} to {period_end}[/green]")
    console.print(f"Found {len(summaries)} unpaid records")

    # Create processor for payments
    processor = MinerPaymentProcessor(
        billing_endpoint=billing_endpoint,
        proto_path=proto_path,
        network=network,
        wallet_name=wallet_name,
        wallet_path=wallet_path,
        token_type=token_type,
        netuid=netuid,
        dry_run=dry_run,
    )

    # Fetch TAO price
    console.print("\nFetching TAO/USD price...")
    try:
        tao_price = processor.fetch_tao_price()
        console.print(f"TAO Price: ${tao_price:.2f}")
    except Exception as e:
        console.print(f"[red]Error fetching TAO price: {e}[/red]")
        sys.exit(1)

    # Calculate payments
    payment_summary = processor.calculate_payments(summaries, tao_price)

    # Display summary
    console.print("")
    processor.display_summary(payment_summary)

    # Confirm
    if not auto_approve and not dry_run:
        console.print("")
        if not click.confirm("Proceed with payments?"):
            console.print("[yellow]Cancelled.[/yellow]")
            sys.exit(0)

    # Execute payments
    successful = processor.execute_payments(payment_summary)

    if not dry_run:
        console.print(
            f"\n[green]Successfully paid {successful} of {len(payment_summary.payments)} records![/green]"
        )


if __name__ == "__main__":
    main()
