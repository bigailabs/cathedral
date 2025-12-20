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
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

import click
import requests
from rich.console import Console
from rich.table import Table


# =============================================================================
# Exception Hierarchy for Payment Errors
# =============================================================================


@dataclass
class PaymentContext:
    """Rich context for payment errors to enable debugging and reconciliation."""

    summary_id: Optional[str] = None
    miner_hotkey: Optional[str] = None
    miner_uid: Optional[int] = None
    tao_amount: Optional[Decimal] = None
    usd_amount: Optional[Decimal] = None
    tx_hash: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    token_type: Optional[str] = None
    netuid: Optional[int] = None
    payments_completed: int = 0
    payments_total: int = 0


class PaymentError(Exception):
    """Base exception for all payment script errors."""

    def __init__(
        self,
        message: str,
        operation: str,
        context: Optional[PaymentContext] = None,
        original_error: Optional[Exception] = None,
        exit_code: int = 1,
    ):
        self.message = message
        self.operation = operation
        self.context = context or PaymentContext()
        self.original_error = original_error
        self.exit_code = exit_code
        super().__init__(message)

    def format_for_console(self, console: Console) -> None:
        """Print rich formatted error to console."""
        console.print(f"\n[bold red]{'=' * 60}[/bold red]")
        console.print(f"[bold red]PAYMENT ERROR: {self.operation.upper()}[/bold red]")
        console.print(f"[bold red]{'=' * 60}[/bold red]")
        console.print(f"\n[red]Error:[/red] {self.message}")

        if self.original_error:
            console.print(
                f"[red]Cause:[/red] {type(self.original_error).__name__}: {self.original_error}"
            )

        ctx = self.context
        if ctx.summary_id:
            console.print(f"\n[bold]Payment Context:[/bold]")
            console.print(f"  Summary ID: {ctx.summary_id}")
        if ctx.miner_hotkey:
            console.print(f"  Miner Hotkey: {ctx.miner_hotkey}")
        if ctx.miner_uid is not None:
            console.print(f"  Miner UID: {ctx.miner_uid}")
        if ctx.tao_amount:
            console.print(f"  TAO Amount: {ctx.tao_amount}")
        if ctx.usd_amount:
            console.print(f"  USD Amount: ${ctx.usd_amount}")
        if ctx.tx_hash:
            console.print(f"  [yellow]TX Hash: {ctx.tx_hash}[/yellow]")
        if ctx.period_start and ctx.period_end:
            console.print(f"  Period: {ctx.period_start} to {ctx.period_end}")
        if ctx.payments_total > 0:
            console.print(
                f"\n[bold]Progress:[/bold] {ctx.payments_completed}/{ctx.payments_total} payments completed before failure"
            )

        console.print(f"[bold red]{'=' * 60}[/bold red]\n")


class WalletError(PaymentError):
    """Raised when wallet operations fail (unlock, initialization)."""

    def __init__(
        self,
        message: str,
        wallet_name: str,
        wallet_path: str,
        original_error: Optional[Exception] = None,
    ):
        super().__init__(
            message=message,
            operation="wallet_unlock",
            original_error=original_error,
            exit_code=2,
        )
        self.wallet_name = wallet_name
        self.wallet_path = wallet_path

    def format_for_console(self, console: Console) -> None:
        super().format_for_console(console)
        console.print("[bold]Recovery:[/bold]")
        console.print(f"  1. Check wallet exists: {self.wallet_path}/{self.wallet_name}")
        console.print("  2. Verify wallet password is correct")
        console.print("  3. Ensure wallet is not corrupted")
        console.print("  4. Script can be safely rerun after fixing wallet issues")


class SubtensorConnectionError(PaymentError):
    """Raised when connection to Bittensor network fails."""

    def __init__(
        self,
        message: str,
        network: str,
        original_error: Optional[Exception] = None,
    ):
        super().__init__(
            message=message,
            operation="subtensor_connect",
            original_error=original_error,
            exit_code=3,
        )
        self.network = network

    def format_for_console(self, console: Console) -> None:
        super().format_for_console(console)
        console.print("[bold]Recovery:[/bold]")
        console.print(f"  1. Check network connectivity to {self.network}")
        console.print("  2. Verify the Bittensor network is operational")
        console.print("  3. Try again with --network flag if using wrong network")
        console.print("  4. Script can be safely rerun after network issues resolve")


class TransferError(PaymentError):
    """Raised when a blockchain transfer fails."""

    def __init__(
        self,
        message: str,
        context: PaymentContext,
        original_error: Optional[Exception] = None,
    ):
        super().__init__(
            message=message,
            operation="blockchain_transfer",
            context=context,
            original_error=original_error,
            exit_code=4,
        )

    def format_for_console(self, console: Console) -> None:
        super().format_for_console(console)
        console.print("[bold]Recovery:[/bold]")
        console.print("  1. Check wallet balance is sufficient")
        console.print("  2. Verify miner hotkey is valid and registered")
        console.print("  3. Script can be safely rerun - this payment was NOT made")
        console.print(
            f"  4. {self.context.payments_completed} prior payment(s) in this run are complete"
        )


class MarkAsPaidError(PaymentError):
    """
    CRITICAL: Raised when mark_as_paid fails AFTER successful blockchain transfer.

    This is the most dangerous error state - money has been transferred but
    the database record was not updated. Rerunning the script will cause
    double-payment.
    """

    def __init__(
        self,
        message: str,
        context: PaymentContext,
        original_error: Optional[Exception] = None,
    ):
        super().__init__(
            message=message,
            operation="mark_as_paid",
            context=context,
            original_error=original_error,
            exit_code=5,
        )

    def format_for_console(self, console: Console) -> None:
        ctx = self.context
        console.print(f"\n[bold red on white]{'!' * 60}[/bold red on white]")
        console.print(
            "[bold red on white]  CRITICAL: BLOCKCHAIN PAYMENT SUCCEEDED BUT DATABASE UPDATE FAILED  [/bold red on white]"
        )
        console.print(f"[bold red on white]{'!' * 60}[/bold red on white]")

        console.print(f"\n[red]Error:[/red] {self.message}")
        if self.original_error:
            console.print(
                f"[red]Cause:[/red] {type(self.original_error).__name__}: {self.original_error}"
            )

        console.print(f"\n[bold yellow]PAYMENT WAS SUCCESSFUL:[/bold yellow]")
        console.print(f"  TX Hash: [green]{ctx.tx_hash}[/green]")
        console.print(f"  Miner Hotkey: {ctx.miner_hotkey}")
        console.print(f"  Amount: {ctx.tao_amount} TAO (${ctx.usd_amount} USD)")

        console.print(f"\n[bold]DATABASE RECORD NOT UPDATED:[/bold]")
        console.print(f"  Summary ID: {ctx.summary_id}")

        console.print(
            f"\n[bold red]DO NOT RERUN THIS SCRIPT WITHOUT MANUAL INTERVENTION![/bold red]"
        )
        console.print("[bold]Required Manual Steps:[/bold]")
        console.print("  1. Verify the transaction on-chain using the TX hash above")
        console.print("  2. Manually mark this record as paid in the database:")
        console.print(
            f'     grpcurl -d \'{{"id": "{ctx.summary_id}", "tx_hash": "{ctx.tx_hash}"}}\' \\'
        )
        console.print(
            "       localhost:50051 basilica.billing.v1.BillingService/MarkMinerRevenuePaid"
        )
        console.print("  3. Only then rerun the script for remaining payments")

        console.print(
            f"\n[bold]Progress:[/bold] {ctx.payments_completed}/{ctx.payments_total} payments completed before failure"
        )
        console.print(f"[bold red]{'!' * 60}[/bold red]\n")


class PriceError(PaymentError):
    """Raised when TAO price fetch fails."""

    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(
            message=message,
            operation="fetch_tao_price",
            original_error=original_error,
            exit_code=6,
        )

    def format_for_console(self, console: Console) -> None:
        super().format_for_console(console)
        console.print("[bold]Recovery:[/bold]")
        console.print("  1. Check internet connectivity")
        console.print("  2. Verify CoinGecko API is accessible")
        console.print("  3. Script can be safely rerun - no payments were made")


class BillingServiceError(PaymentError):
    """Raised when billing service operations fail."""

    def __init__(
        self,
        message: str,
        operation: str,
        original_error: Optional[Exception] = None,
    ):
        super().__init__(
            message=message,
            operation=operation,
            original_error=original_error,
            exit_code=7,
        )

    def format_for_console(self, console: Console) -> None:
        super().format_for_console(console)
        console.print("[bold]Recovery:[/bold]")
        console.print("  1. Check billing service is running")
        console.print("  2. Verify gRPC endpoint is correct")
        console.print("  3. Script can be safely rerun")

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
                raise BillingServiceError(
                    message=f"gRPC call failed: {result.stderr}",
                    operation="get_unpaid_summaries",
                )

            if not result.stdout.strip():
                break

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                raise BillingServiceError(
                    message=f"Invalid JSON response from billing service: {result.stdout[:200]}...",
                    operation="get_unpaid_summaries",
                    original_error=e,
                )
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
            raise BillingServiceError(
                message=f"gRPC call failed: {result.stderr}",
                operation="mark_as_paid",
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise BillingServiceError(
                message=f"Invalid JSON response from mark_as_paid: {result.stdout[:200]}...",
                operation="mark_as_paid",
                original_error=e,
            )
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
        except requests.RequestException as e:
            raise PriceError(
                message="Failed to fetch TAO price from CoinGecko",
                original_error=e,
            )

        try:
            data = response.json()
            price = data["bittensor"]["usd"]
            return Decimal(str(price))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise PriceError(
                message=f"Invalid response from CoinGecko API: {response.text[:200]}...",
                original_error=e,
            )

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
        """Execute blockchain transfers for each payment.

        Raises:
            WalletError: If wallet cannot be unlocked
            SubtensorConnectionError: If connection to Bittensor fails
            TransferError: If a blockchain transfer fails
            MarkAsPaidError: CRITICAL - If database update fails after successful transfer
        """
        if self.dry_run:
            self.console.print(
                "[yellow]DRY RUN - No actual transfers will be made[/yellow]"
            )
            return 0

        import bittensor as bt
        from bittensor_wallet import Wallet

        # Initialize wallet with proper error handling
        try:
            self.console.print(
                f"Loading wallet '{self.wallet_name}' from {self.wallet_path}..."
            )
            self.wallet = Wallet(name=self.wallet_name, path=self.wallet_path)
            self.wallet.unlock_coldkey()
        except Exception as e:
            raise WalletError(
                message=f"Failed to unlock wallet '{self.wallet_name}'",
                wallet_name=self.wallet_name,
                wallet_path=self.wallet_path,
                original_error=e,
            )

        # Connect to subtensor with proper error handling
        try:
            self.console.print(f"Connecting to {self.network}...")
            self.subtensor = bt.Subtensor(network=self.network)
        except Exception as e:
            raise SubtensorConnectionError(
                message=f"Failed to connect to Bittensor network '{self.network}'",
                network=self.network,
                original_error=e,
            )

        successful_payments = 0

        for payment in summary.payments:
            # Build rich context for this payment (used in error reporting)
            context = PaymentContext(
                summary_id=payment.summary.id,
                miner_hotkey=payment.summary.miner_hotkey,
                miner_uid=payment.summary.miner_uid,
                tao_amount=payment.tao_amount,
                usd_amount=payment.base_price_usd,
                period_start=payment.summary.period_start,
                period_end=payment.summary.period_end,
                token_type=summary.token_type,
                netuid=summary.netuid,
                payments_completed=successful_payments,
                payments_total=len(summary.payments),
            )

            self.console.print(
                f"Transferring {payment.amount_tokens:.6f} {summary.token_type.upper()} "
                f"to {payment.summary.miner_hotkey[:16]}..."
            )

            # Execute blockchain transfer
            try:
                if summary.token_type == "tao":
                    response = self.subtensor.transfer(
                        wallet=self.wallet,
                        destination_ss58=payment.summary.miner_hotkey,
                        amount=bt.Balance.from_tao(float(payment.tao_amount)),
                        wait_for_inclusion=True,
                        wait_for_finalization=True,
                    )
                else:
                    response = self.subtensor.add_stake(
                        wallet=self.wallet,
                        hotkey_ss58=payment.summary.miner_hotkey,
                        netuid=summary.netuid,
                        amount=bt.Balance.from_tao(float(payment.tao_amount)),
                        wait_for_inclusion=True,
                        wait_for_finalization=True,
                    )
            except Exception as e:
                raise TransferError(
                    message=f"Blockchain transfer failed for miner {payment.summary.miner_hotkey[:16]}...",
                    context=context,
                    original_error=e,
                )

            if not response.success:
                raise TransferError(
                    message=f"Transfer rejected: {response.message}",
                    context=context,
                )

            # Extract transaction hash
            if response.extrinsic_receipt is None:
                raise TransferError(
                    message="Transfer succeeded but no extrinsic_receipt returned",
                    context=context,
                )

            tx_hash = response.extrinsic_receipt.extrinsic_hash
            payment.tx_hash = tx_hash
            context.tx_hash = tx_hash
            self.console.print(f"  [green]Success: {tx_hash}[/green]")

            # CRITICAL: Mark as paid - MUST NOT FAIL SILENTLY
            # If this fails, we have a double-payment risk on rerun
            try:
                mark_result = self.billing_client.mark_as_paid(
                    payment.summary.id, tx_hash
                )
                if not mark_result:
                    raise RuntimeError("mark_as_paid returned False - record not updated")
                self.console.print(f"  [green]Marked as paid in billing[/green]")
                successful_payments += 1
            except Exception as e:
                # CRITICAL ERROR: Payment was made but database not updated
                # Script MUST stop here to prevent double-payment on rerun
                raise MarkAsPaidError(
                    message="Failed to mark payment as paid in billing service",
                    context=context,
                    original_error=e,
                )

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

    try:
        _run_payment_flow(
            console=console,
            token_type=token_type,
            force=force,
            netuid=netuid,
            wallet_name=wallet_name,
            wallet_path=wallet_path,
            network=network,
            billing_endpoint=billing_endpoint,
            proto_path=proto_path,
            dry_run=dry_run,
            auto_approve=auto_approve,
        )
    except PaymentError as e:
        e.format_for_console(console)
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        console.print("\n[yellow]Payment interrupted by user.[/yellow]")
        console.print(
            "[yellow]Some payments may have been made. Check transaction logs before rerunning.[/yellow]"
        )
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[bold red]Unexpected error: {e}[/bold red]")
        console.print(f"[red]Type: {type(e).__name__}[/red]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        sys.exit(1)


def _run_payment_flow(
    console: Console,
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
) -> None:
    """Run the payment flow. Raises PaymentError subclasses on failure."""
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
    all_summaries = billing_client.get_all_unpaid_summaries()

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
        console.print(
            f"  {start} to {end}: {len(period_summaries)} miners, ${revenue:.2f} revenue"
        )

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
    tao_price = processor.fetch_tao_price()
    console.print(f"TAO Price: ${tao_price:.2f}")

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
