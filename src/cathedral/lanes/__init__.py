"""Cathedral task-family lanes.

Each subpackage implements one TaskFamily (see ``contract.py``). Lanes are
pure-Python, deterministic-from-seed, zero-I/O. The publisher imports them
in-process; nothing in this package may call the network, read the clock,
or use unseeded randomness.

See ``src/cathedral/lanes/contract.py`` for the protocol and rules.
See ``src/cathedral/lanes/synthetic_maxsat_v1/README.md`` for an example
lane brief.
"""
