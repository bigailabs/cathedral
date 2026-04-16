#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "bittensor>=9.0.0",
#   "substrate-interface>=1.7.0",
#   "click>=8.1.0",
# ]
# [tool.uv]
# prerelease = "allow"
# ///

"""
Cathedral Bittensor Network Monitor

Monitor extrinsics and transactions on a Bittensor substrate blockchain,
specifically focusing on miner and validator operations, with comprehensive
weights and incentives analysis.

Usage:
    ./monitoring.py                              # Monitor local network
    ./monitoring.py --network test               # Monitor testnet
    ./monitoring.py --network finney --netuid 1  # Monitor mainnet subnet 1
    ./monitoring.py --network ws://custom:9944   # Custom node URL
    ./monitoring.py --show-incentives            # Show weights and incentives every 50 blocks
    ./monitoring.py --weights-only               # Show weights and incentives once and exit
    ./monitoring.py --coldkey 5GrwvaEF5ZtKh...   # Filter incentives by coldkey

The tool monitors and displays:
- Miner registrations (serve_axon)
- Validator weight updates (set_weights)
- Other SubtensorModule transactions from known miners/validators
- Weights and incentives distribution with detailed analysis
- Weight consensus across validators
- Epoch timing and subnet information

Weights and Incentives Features:
- Top neurons by incentive with stake information
- Individual validator weight distributions
- Weight consensus analysis (which neurons receive most total weight)
- Epoch timing and blocks until next epoch
- Subnet tempo and validator information
- Coldkey filtering for specific neuron tracking

Example transaction output:
    2025-07-02 14:32:01 Block 12345 Extrinsic 2
      Type: miner
      Call: SubtensorModule.serve_axon
      Signer: 5GrwvaEF5ZtKh...
      Axon: 192.168.1.100:8091
      Netuid: 1

Example weights output:
    Top validators by stake and their weight distributions:
    1. Validator UID 204 (Stake: 249530.06)
       Sets weights on 25 neurons:
         UID  15: 0.0456
         UID  23: 0.0398

    Weight Consensus (neurons receiving most total weight):
     1. UID  15: 0.2345 total weight from 8 validators
     2. UID  23: 0.1987 total weight from 6 validators

Press Ctrl+C to stop monitoring.
"""

import sys
import time
from datetime import datetime
from typing import Set

import bittensor
import click
from substrateinterface import SubstrateInterface


class Monitor:
    def __init__(
        self,
        network: str = "local",
        netuid: int = 1,
        show_failures: bool = True,
        coldkey: str | None = None,
        show_incentives: bool = False,
    ):
        self.network = network
        self.netuid = netuid
        self.show_failures = show_failures
        self.coldkey = coldkey
        self.show_incentives = show_incentives
        self.substrate = None
        self.subtensor = None
        self.known_miners: Set[str] = set()
        self.known_validators: Set[str] = set()
        self.block_count = 0
        self.extrinsic_count = 0
        self.failed_count = 0

    def connect(self):
        """Connect to the Bittensor network."""
        if self.network == "local":
            url = "ws://127.0.0.1:9944"
        elif self.network == "test":
            url = "wss://test.finney.opentensor.ai:443"
        elif self.network == "finney":
            url = "wss://entrypoint-finney.opentensor.ai:443"
        else:
            url = self.network

        print(f"Connecting to {url}...")

        try:
            self.substrate = SubstrateInterface(url=url)
            self.subtensor = bittensor.subtensor(network=self.network)
            print(f"Connected to {url}")
            self.load_metagraph()
        except Exception as e:
            print(f"Failed to connect: {e}")
            sys.exit(1)

    def load_metagraph(self):
        """Load metagraph to identify miners and validators."""
        try:
            metagraph = self.subtensor.metagraph(self.netuid)

            for neuron in metagraph.neurons:
                if neuron.hotkey:
                    if hasattr(neuron, "validator_permit") and neuron.validator_permit:
                        self.known_validators.add(neuron.hotkey)
                    else:
                        self.known_miners.add(neuron.hotkey)

            print(
                f"Loaded {len(self.known_miners)} miners, {len(self.known_validators)} validators"
            )
        except Exception as e:
            print(f"Warning: Failed to load metagraph: {e}")

    def display_weights_and_incentives(self):
        """Display current weights and incentives information."""
        if not self.show_incentives:
            return

        try:
            current_block = self.subtensor.get_current_block()

            # Get subnet info for epoch calculations
            subnet_info = self.subtensor.get_subnet_info(netuid=self.netuid)
            tempo = subnet_info.tempo if subnet_info else 360  # Default tempo

            # Calculate next epoch block
            blocks_since_last_epoch = current_block % tempo
            blocks_until_next_epoch = tempo - blocks_since_last_epoch
            next_epoch_block = current_block + blocks_until_next_epoch

            metagraph = self.subtensor.metagraph(netuid=self.netuid)

            # Try to sync the metagraph to get weight data
            try:
                metagraph.sync(subtensor=self.subtensor)
                print(f"Metagraph synced. Weight matrix shape: {metagraph.W.shape}")
                print(f"Weights attribute shape: {metagraph.weights.shape}")

                # Check if weights might be stored differently
                if hasattr(metagraph, "bonds"):
                    print(f"Bonds shape: {metagraph.bonds.shape}")

                # Check for any validators with validator_permit
                validator_permits = sum(
                    1
                    for n in metagraph.neurons
                    if hasattr(n, "validator_permit") and n.validator_permit
                )
                print(f"Validators with permits: {validator_permits}")

            except Exception as e:
                print(f"Warning: Failed to sync metagraph: {e}")

            neurons = metagraph.neurons

            print(f"\n{'=' * 60}")
            print(f"WEIGHTS & INCENTIVES - SUBNET {self.netuid}")
            print(f"{'=' * 60}")
            print(f"Current block: {current_block}")
            print(f"Next epoch starts at block: {next_epoch_block}")
            print(f"Blocks until next epoch: {blocks_until_next_epoch}")
            print(f"Tempo: {tempo}")
            print(f"Total neurons: {len(neurons)}")

            if self.coldkey:
                filtered_neurons = [n for n in neurons if n.coldkey == self.coldkey]
                if filtered_neurons:
                    print(f"\nColdkey: {self.coldkey}")
                    print("-" * 40)
                    total_incentives = 0.0
                    for neuron in filtered_neurons:
                        print(f"UID: {neuron.uid}\t-> Incentive: {neuron.incentive:.4f}")
                        total_incentives += neuron.incentive
                    print("-" * 40)
                    print(f"Total incentives: {total_incentives:.4f}")
                else:
                    print(f"\nNo neurons found for coldkey: {self.coldkey}")
            else:
                print("\nTop 10 neurons by incentive:")
                print("-" * 40)
                sorted_neurons = sorted(neurons, key=lambda n: n.incentive, reverse=True)
                for i, neuron in enumerate(sorted_neurons[:10]):
                    stake_value = (
                        float(neuron.stake) if hasattr(neuron.stake, "__float__") else neuron.stake
                    )
                    print(
                        f"{i + 1:2d}. UID: {neuron.uid:3d} | Incentive: {neuron.incentive:.4f} | Stake: {stake_value}"
                    )

            # Display weight distributions
            self.display_weight_distributions(metagraph)

            print(f"{'=' * 60}\n")

        except Exception as e:
            print(f"Error displaying weights and incentives: {e}")

    def display_weight_distributions(self, metagraph):
        """Display weight distributions from validators."""
        try:
            print("\nWEIGHT DISTRIBUTIONS:")
            print("-" * 60)

            validators = [
                n
                for n in metagraph.neurons
                if hasattr(n, "validator_permit") and n.validator_permit
            ]

            if not validators:
                print("No validators found in subnet")
                return

            # Sort validators by stake (descending)
            validators_by_stake = sorted(validators, key=lambda n: n.stake, reverse=True)

            print(f"Found {len(validators)} validators")
            print("Top validators by stake and their weight distributions:")
            print()

            for i, validator in enumerate(validators_by_stake[:5]):  # Show top 5 validators
                stake_value = (
                    float(validator.stake)
                    if hasattr(validator.stake, "__float__")
                    else validator.stake
                )
                print(f"{i + 1}. Validator UID {validator.uid} (Stake: {stake_value})")

                # Get weights set by this validator from the metagraph
                try:
                    # Access weights through the metagraph weights attribute
                    if hasattr(metagraph, "weights") and metagraph.weights.shape[0] > 0:
                        validator_weights = metagraph.weights[validator.uid]

                        # Create list of (uid, weight) pairs and sort by weight
                        weight_pairs = []
                        for uid, weight in enumerate(validator_weights):
                            if weight > 0:
                                weight_pairs.append((uid, float(weight)))

                        # Sort by weight (descending)
                        weight_pairs.sort(key=lambda x: x[1], reverse=True)

                        if weight_pairs:
                            print(f"   Sets weights on {len(weight_pairs)} neurons:")
                            for uid, weight in weight_pairs[:10]:  # Show top 10 weights
                                print(f"     UID {uid:3d}: {weight:.4f}")

                            if len(weight_pairs) > 10:
                                print(f"     ... and {len(weight_pairs) - 10} more")
                        else:
                            print("   No weights set")
                    else:
                        print("   No weight data available - weights matrix is empty")
                except Exception as e:
                    print(f"   Weight data not available: {e}")
                print()

            # Show weight consensus (which neurons receive the most total weight)
            print("Weight Consensus (neurons receiving most total weight):")
            print("-" * 50)

            # Calculate total weight received by each neuron
            total_weights_received = {}
            try:
                if hasattr(metagraph, "weights") and metagraph.weights.shape[0] > 0:
                    for validator in validators:
                        try:
                            validator_weights = metagraph.weights[validator.uid]
                            for uid, weight in enumerate(validator_weights):
                                if weight > 0:
                                    total_weights_received[uid] = total_weights_received.get(
                                        uid, 0
                                    ) + float(weight)
                        except Exception:
                            continue

                    # Sort by total weight received
                    sorted_by_total_weight = sorted(
                        total_weights_received.items(), key=lambda x: x[1], reverse=True
                    )

                    for i, (uid, total_weight) in enumerate(sorted_by_total_weight[:10]):
                        validator_count = sum(
                            1
                            for v in validators
                            if uid < len(metagraph.weights[v.uid])
                            and metagraph.weights[v.uid][uid] > 0
                        )
                        print(
                            f"{i + 1:2d}. UID {uid:3d}: {total_weight:.4f} total weight from {validator_count} validators"
                        )
                else:
                    print("No weight data available - weights matrix is empty")
                    print("This could mean:")
                    print("- Validators haven't set weights yet in this tempo period")
                    print("- The subnet is new or inactive")
            except Exception as e:
                print(f"Weight consensus calculation failed: {e}")

        except Exception as e:
            print(f"Error displaying weight distributions: {e}")

    def categorize_address(self, address: str) -> str:
        """Categorize an address as miner, validator, or unknown."""
        if address in self.known_validators:
            return "validator"
        elif address in self.known_miners:
            return "miner"
        else:
            return "unknown"

    def process_extrinsic(self, extrinsic_data, block_number: int, idx: int):
        """Process a single extrinsic."""
        try:
            # Extract the actual data
            extrinsic = extrinsic_data.value if hasattr(extrinsic_data, "value") else extrinsic_data

            # Skip unsigned extrinsics (inherents like timestamp)
            if not extrinsic.get("signature"):
                return

            # Extract call information
            call = extrinsic.get("call")
            if not call:
                return

            module = call.get("call_module", "unknown")
            function = call.get("call_function", "unknown")

            # Skip non-SubtensorModule calls unless they're system errors
            if module != "SubtensorModule" and module != "System":
                return

            # Get signer address
            signer = extrinsic.get("address", "unknown")
            if hasattr(signer, "value"):
                signer = signer.value

            category = self.categorize_address(str(signer))

            # Extract call arguments
            args_dict = {}
            if "call_args" in call:
                for arg in call["call_args"]:
                    args_dict[arg["name"]] = arg["value"]

            # Log the extrinsic
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n{timestamp} Block {block_number} Extrinsic {idx}")
            print(f"  Type: {category}")
            print(f"  Call: {module}.{function}")
            print(f"  Signer: {signer}")

            # Show raw parameters for all calls
            if args_dict:
                print("  Parameters:")
                for key, value in args_dict.items():
                    print(f"    {key}: {value}")

            # Log specific formatted details for important calls
            if function == "serve_axon" and args_dict:
                ip = args_dict.get("ip", "unknown")
                port = args_dict.get("port", "unknown")
                netuid = args_dict.get("netuid", "unknown")
                print(f"  Summary: Registering on subnet {netuid} at {ip}:{port}")

            elif function == "set_weights" and args_dict:
                dests = args_dict.get("dests", [])
                values = args_dict.get("values", [])
                version = args_dict.get("version_key", "unknown")
                print(f"  Summary: Setting {len(dests)} weights, version {version}")

                # Show all weights
                if dests and values:
                    print("  Weight details:")
                    weights = sorted(
                        zip(dests, values, strict=False), key=lambda x: x[1], reverse=True
                    )
                    for uid, weight in weights:
                        print(f"    UID {uid}: {weight}")

            self.extrinsic_count += 1

        except Exception as e:
            print(f"Error processing extrinsic: {e}")

    def process_events(self, block_hash, block_number: int):
        """Process events for a block to find transaction results."""
        if not self.show_failures:
            return

        try:
            # Get events for this block
            events = self.substrate.get_events(block_hash)

            if not events:
                return

            for event in events:
                event_data = event.value if hasattr(event, "value") else event

                # Check for ExtrinsicSuccess or ExtrinsicFailed events
                if event_data.get("module_id") == "System":
                    event_id = event_data.get("event_id")

                    if event_id == "ExtrinsicFailed":
                        self.failed_count += 1

                        # Extract failure information
                        attributes = event_data.get("attributes", {})
                        error_info = attributes.get("dispatch_error", {})
                        extrinsic_idx = attributes.get("extrinsic_index", "unknown")

                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{timestamp} Block {block_number} - FAILED TRANSACTION")
                        print(f"  Extrinsic Index: {extrinsic_idx}")

                        # Try to decode the error
                        if isinstance(error_info, dict):
                            if "Module" in error_info:
                                module_info = error_info["Module"]
                                if isinstance(module_info, dict):
                                    print(f"  Module: {module_info.get('index', 'unknown')}")
                                    print(f"  Error: {module_info.get('error', 'unknown')}")
                            elif "BadOrigin" in error_info:
                                print("  Error: BadOrigin - insufficient permissions")
                            elif "CannotLookup" in error_info:
                                print("  Error: CannotLookup - failed to lookup account")
                            elif "ConsumerRemaining" in error_info:
                                print("  Error: ConsumerRemaining - account has active consumers")
                            elif "NoProviders" in error_info:
                                print("  Error: NoProviders - account has no providers")
                            elif "TooManyConsumers" in error_info:
                                print("  Error: TooManyConsumers - too many consumers")
                            elif "Token" in error_info:
                                token_error = error_info["Token"]
                                print(f"  Error: Token error - {token_error}")
                            elif "Arithmetic" in error_info:
                                arith_error = error_info["Arithmetic"]
                                print(f"  Error: Arithmetic error - {arith_error}")
                            else:
                                print(f"  Error Info: {error_info}")
                        else:
                            print(f"  Raw Error: {error_info}")

        except Exception as e:
            # Don't interrupt monitoring for event processing errors
            if self.block_count < 5:  # Only show errors in first few blocks
                print(f"Debug: Event processing error: {e}")

    def monitor(self):
        """Monitor blocks for extrinsics and events."""
        print("Starting monitoring...")
        if self.show_failures:
            print("Monitoring both successful and failed transactions")
        else:
            print("Monitoring successful transactions only")
        if self.show_incentives:
            print("Weights and incentives will be displayed every 50 blocks")
            print("Displaying initial weights and incentives...")
            self.display_weights_and_incentives()
        print("Press Ctrl+C to stop\n")

        last_block_hash = None

        try:
            while True:
                try:
                    # Get latest block
                    block_hash = self.substrate.get_block_hash()

                    if block_hash != last_block_hash:
                        last_block_hash = block_hash
                        block = self.substrate.get_block(block_hash=block_hash)

                        if block:
                            block_number = block["header"]["number"]
                            self.block_count += 1

                            # Display weights and incentives every 50 blocks
                            if self.show_incentives and self.block_count % 50 == 0:
                                self.display_weights_and_incentives()

                            # Progress indicator every 10 blocks
                            if self.block_count % 10 == 0:
                                status = f"[Block {block_number}] Monitoring... ({self.extrinsic_count} extrinsics"
                                if self.show_failures:
                                    status += f", {self.failed_count} failed"
                                status += ")"
                                print(f"\r{status}", end="", flush=True)

                            # Process events first to catch failures
                            if self.show_failures:
                                self.process_events(block_hash, block_number)

                            # Process extrinsics
                            if "extrinsics" in block:
                                for idx, extrinsic in enumerate(block["extrinsics"]):
                                    self.process_extrinsic(extrinsic, block_number, idx)

                    time.sleep(0.5)

                except Exception as e:
                    print(f"\nError in monitoring loop: {e}")
                    time.sleep(5)

        except KeyboardInterrupt:
            print("\n\nMonitoring stopped.")
            print(f"Processed {self.block_count} blocks")
            print(f"Found {self.extrinsic_count} SubtensorModule extrinsics")
            if self.show_failures:
                print(f"Failed transactions detected: {self.failed_count}")
            if self.show_incentives:
                self.display_weights_and_incentives()


@click.command()
@click.option(
    "--network", default="local", help="Network to connect to (local, test, finney, or custom URL)"
)
@click.option("--netuid", default=1, type=int, help="Network UID to monitor")
@click.option("--no-failures", is_flag=True, help="Don't show failed transactions")
@click.option("--coldkey", type=str, help="Coldkey to filter incentives display")
@click.option("--show-incentives", is_flag=True, help="Show weights and incentives every 50 blocks")
@click.option("--weights-only", is_flag=True, help="Show weights and incentives once and exit")
def main(
    network: str,
    netuid: int,
    no_failures: bool,
    coldkey: str | None,
    show_incentives: bool,
    weights_only: bool,
):
    """Monitor Bittensor blockchain for miner and validator extrinsics."""
    print("Cathedral Network Monitor")
    print(f"Network: {network}, Netuid: {netuid}")
    if coldkey:
        print(f"Coldkey filter: {coldkey}")
    if show_incentives:
        print("Incentives monitoring: enabled")
    print("-" * 50)

    monitor = Monitor(
        network=network,
        netuid=netuid,
        show_failures=not no_failures,
        coldkey=coldkey,
        show_incentives=show_incentives or weights_only,
    )
    monitor.connect()

    if weights_only:
        monitor.display_weights_and_incentives()
    else:
        monitor.monitor()


if __name__ == "__main__":
    main()
