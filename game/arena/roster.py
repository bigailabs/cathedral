"""The miner-agent roster for an arena round. Each archetype is built to trip a
specific gate so the anti-cheat feed is fully exercised.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentSpec:
    agent_id: str
    hotkey: str
    coldkey: str
    archetype: str          # honest | copier | fake_attest | wrong_owner | spam | bad_encoder
    environment: str        # local-untrusted | stitch-runner | polaris-tee-cpu | polaris-tee-gpu | mocked-tee
    provisioned: bool       # holds an attestable compute slot
    cheat: str = "none"     # none | copy_witness | wrong_owner | fake_attest | spam | bad_encode
    claimed_env: str = ""   # the compute profile the agent ADVERTISES (default = environment)


def default_roster() -> list[AgentSpec]:
    """3 honest agents + the 6 required cheat archetypes."""
    return [
        AgentSpec("agent-aurora", "hk_aurora", "ck_aurora", "honest", "polaris-tee-cpu", True),
        AgentSpec("agent-borealis", "hk_borealis", "ck_borealis", "honest", "stitch-runner", True),
        AgentSpec("agent-cygnus", "hk_cygnus", "ck_cygnus", "honest", "polaris-tee-gpu", True),
        AgentSpec("agent-magpie", "hk_magpie", "ck_magpie", "copier", "local-untrusted", True,
                  cheat="copy_witness"),
        AgentSpec("agent-cuckoo", "hk_cuckoo", "ck_cuckoo", "fake_attest", "mocked-tee", False,
                  cheat="fake_attest"),
        AgentSpec("agent-jackdaw", "hk_jackdaw", "ck_jackdaw", "wrong_owner", "local-untrusted", True,
                  cheat="wrong_owner"),
        AgentSpec("agent-locust", "hk_locust", "ck_locust", "spam", "local-untrusted", True,
                  cheat="spam"),
        AgentSpec("agent-weevil", "hk_weevil", "ck_weevil", "bad_encoder", "local-untrusted", True,
                  cheat="bad_encode"),
        AgentSpec("agent-mantis", "hk_mantis", "ck_mantis", "forge_trace", "polaris-tee-cpu", True,
                  cheat="forge_trace"),
        AgentSpec("agent-hornet", "hk_hornet", "ck_hornet", "bad_replay", "stitch-runner", True,
                  cheat="bad_replay"),
        AgentSpec("agent-cricket", "hk_cricket", "ck_cricket", "stale_nonce", "stitch-runner", True,
                  cheat="stale_nonce"),
        AgentSpec("agent-termite", "hk_termite", "ck_termite", "no_decode_map", "local-untrusted", True,
                  cheat="no_decode_map"),
        AgentSpec("agent-wasp", "hk_wasp", "ck_wasp", "fake_compute_profile", "local-untrusted", True,
                  cheat="fake_compute_profile", claimed_env="polaris-tee-gpu"),
        # misclassify: solves correctly + attests, but COMMITS the wrong invariant
        # family for its proof (a mislabeled finding) -> fails the alignment gate.
        AgentSpec("agent-moth", "hk_moth", "ck_moth", "misclassify", "polaris-tee-cpu", True,
                  cheat="misclassify"),
        # hotkey stacking: ONE operator, two hotkeys under ONE coldkey -> collapsed
        AgentSpec("agent-swarm-a", "hk_swarm_a", "ck_swarm", "hotkey_stacking", "stitch-runner", True),
        AgentSpec("agent-swarm-b", "hk_swarm_b", "ck_swarm", "hotkey_stacking", "stitch-runner", True),
    ]
