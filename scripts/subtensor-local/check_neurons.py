#!/usr/bin/env python3
"""Check number of neurons registered on subnet 2."""
import bittensor as bt
import os
import sys

os.environ['SSL_CERT_FILE'] = os.path.join(os.path.dirname(__file__), 'tls/ca.crt')

try:
    subtensor = bt.Subtensor(network='wss://localhost:9944')
    metagraph = subtensor.metagraph(netuid=2)
    print(len(metagraph.neurons))
    sys.exit(0)
except Exception:
    print(0)
    sys.exit(0)
