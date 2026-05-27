"""Cathedral subnet validator and miner.

Verifies Cathedral-signed SAT/task-family rows and sets weights on the
Bittensor chain.
"""

__version__ = "2.0.1"

# Encoded version stamped on every `set_weights` extrinsic so on-chain
# observers can distinguish Cathedral-binary weight-sets from generic
# bittensor-SDK ones. Format: MAJOR*1_000_000 + MINOR*1_000 + PATCH.
SPEC_VERSION = 2_000_001
