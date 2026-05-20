"""Cathedral subnet — validator and miner.

Verifies signed Polaris evidence about regulatory and legal intelligence
cards, scores them, and sets weights on the Bittensor chain.
"""

__version__ = "1.1.25"

# Encoded version stamped on every `set_weights` extrinsic so on-chain
# observers can distinguish Cathedral-binary weight-sets from generic
# bittensor-SDK ones. Format: MAJOR*1_000_000 + MINOR*1_000 + PATCH.
SPEC_VERSION = 1_001_000
